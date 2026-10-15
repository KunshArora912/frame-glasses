package com.kunsh.frame

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import java.util.ArrayDeque
import java.util.UUID

/**
 * The Bluetooth link to Frame: scan by service UUID, connect, MTU 247, notify
 * on RX, serialised writes to TX. Frame requires an encrypted link, so Android
 * shows its pairing dialog on the first connection; after that the bond is
 * silent. All callbacks to the listener arrive on the main thread.
 */
@SuppressLint("MissingPermission")
class FrameBle(private val ctx: Context, private val listener: Listener) {

    interface Listener {
        fun onBleState(connected: Boolean, name: String?, address: String?)
        fun onPacket(bytes: ByteArray)
        fun onLog(msg: String)
    }

    companion object {
        val SERVICE: UUID = UUID.fromString("7a230001-5475-a6a4-654c-8431f6ad49c4")
        val TX: UUID = UUID.fromString("7a230002-5475-a6a4-654c-8431f6ad49c4")
        val RX: UUID = UUID.fromString("7a230003-5475-a6a4-654c-8431f6ad49c4")
        val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
        const val MTU = 247
        const val SCAN_MS = 10_000L
    }

    private class Write(val data: ByteArray, val withResponse: Boolean, val done: (Throwable?) -> Unit)

    private val main = Handler(Looper.getMainLooper())
    private val adapter: BluetoothAdapter?
        get() = (ctx.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter

    private var gatt: BluetoothGatt? = null
    private var tx: BluetoothGattCharacteristic? = null
    private var scanCb: ScanCallback? = null
    private val queue = ArrayDeque<Write>()
    private var inFlight: Write? = null

    var connected = false; private set
    var mtu = 23; private set
    var name: String? = null; private set
    var address: String? = null; private set

    val maxPayload: Int get() = mtu - 3

    // ---------------------------------------------------------------- connect
    fun connect(savedAddress: String?) {
        val ad = adapter
        if (ad == null || !ad.isEnabled) { listener.onLog("Bluetooth is off"); return }
        if (gatt != null) { listener.onLog("already connecting/connected"); return }
        if (savedAddress != null) {
            listener.onLog("connecting to saved Frame $savedAddress")
            connectTo(ad.getRemoteDevice(savedAddress))
        } else {
            scanAndConnect(ad)
        }
    }

    private fun scanAndConnect(ad: BluetoothAdapter) {
        val scanner = ad.bluetoothLeScanner ?: run { listener.onLog("no BLE scanner"); return }
        listener.onLog("scanning for Frame ...")
        val cb = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                main.post {
                    if (scanCb !== this) return@post
                    stopScan()
                    listener.onLog("found ${result.device.name ?: result.device.address}")
                    connectTo(result.device)
                }
            }
            override fun onScanFailed(errorCode: Int) {
                main.post { stopScan(); listener.onLog("scan failed ($errorCode)") }
            }
        }
        scanCb = cb
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(SERVICE)).build()
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        scanner.startScan(listOf(filter), settings, cb)
        main.postDelayed({
            if (scanCb === cb) { stopScan(); listener.onLog("no Frame found: tap the glasses 3x and retry") }
        }, SCAN_MS)
    }

    private fun stopScan() {
        val cb = scanCb ?: return
        scanCb = null
        try { adapter?.bluetoothLeScanner?.stopScan(cb) } catch (_: Exception) {}
    }

    private fun connectTo(dev: BluetoothDevice) {
        name = dev.name ?: "Frame"
        address = dev.address
        gatt = dev.connectGatt(ctx, false, gattCb, BluetoothDevice.TRANSPORT_LE)
    }

    fun disconnect() {
        stopScan()
        val g = gatt
        gatt = null
        tx = null
        try { g?.disconnect() } catch (_: Exception) {}
        try { g?.close() } catch (_: Exception) {}
        setConnected(false)
    }

    private fun setConnected(v: Boolean) {
        if (connected == v) return
        connected = v
        if (!v) {
            val pending = ArrayList(queue); queue.clear()
            inFlight?.done(IllegalStateException("link dropped")); inFlight = null
            pending.forEach { it.done(IllegalStateException("link dropped")) }
        }
        listener.onBleState(v, name, address)
    }

    // ----------------------------------------------------------------- writes
    fun write(data: ByteArray, withResponse: Boolean = true, done: (Throwable?) -> Unit = {}) {
        main.post { queue.add(Write(data, withResponse, done)); pump() }
    }

    private fun pump() {
        if (inFlight != null) return
        val w = queue.poll() ?: return
        val g = gatt; val c = tx
        if (g == null || c == null || !connected) { w.done(IllegalStateException("not connected")); pump(); return }
        inFlight = w
        val type = if (w.withResponse) BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                   else BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        val ok = if (Build.VERSION.SDK_INT >= 33) {
            g.writeCharacteristic(c, w.data, type) == BluetoothStatusCodes.SUCCESS
        } else {
            @Suppress("DEPRECATION")
            run { c.writeType = type; c.value = w.data; g.writeCharacteristic(c) }
        }
        if (!ok) { inFlight = null; w.done(IllegalStateException("write rejected")); pump() }
    }

    // -------------------------------------------------------------- callbacks
    private val gattCb = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            main.post {
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    listener.onLog("link up, negotiating MTU")
                    if (!g.requestMtu(MTU)) g.discoverServices()
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                    listener.onLog(if (status == 0) "disconnected" else "disconnected (status $status)")
                    if (gatt === g) { gatt = null; tx = null }
                    try { g.close() } catch (_: Exception) {}
                    setConnected(false)
                }
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, m: Int, status: Int) {
            main.post { mtu = m; listener.onLog("MTU $m"); g.discoverServices() }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            main.post {
                val svc = g.getService(SERVICE)
                val rx = svc?.getCharacteristic(RX)
                tx = svc?.getCharacteristic(TX)
                if (svc == null || rx == null || tx == null) { listener.onLog("Frame service not found"); disconnect(); return@post }
                g.setCharacteristicNotification(rx, true)
                val d = rx.getDescriptor(CCCD)
                if (d == null) { setConnected(true); return@post }
                // this write is what makes Android pair on a fresh bond
                if (Build.VERSION.SDK_INT >= 33) {
                    g.writeDescriptor(d, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
                } else {
                    @Suppress("DEPRECATION")
                    run { d.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE; g.writeDescriptor(d) }
                }
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            main.post {
                if (status == BluetoothGatt.GATT_SUCCESS) setConnected(true)
                else { listener.onLog("notifications refused ($status): pair with Frame first"); disconnect() }
            }
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            main.post {
                val w = inFlight ?: return@post
                inFlight = null
                w.done(if (status == BluetoothGatt.GATT_SUCCESS) null else IllegalStateException("gatt write $status"))
                pump()
            }
        }

        // API 33+
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            val copy = value.copyOf()
            main.post { listener.onPacket(copy) }
        }

        // API < 33
        @Deprecated("Deprecated in Java")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            if (Build.VERSION.SDK_INT >= 33) return
            @Suppress("DEPRECATION")
            val copy = c.value?.copyOf() ?: return
            main.post { listener.onPacket(copy) }
        }
    }
}
