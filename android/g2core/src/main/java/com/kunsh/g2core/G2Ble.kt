package com.kunsh.g2core

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
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.Build
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import java.util.UUID

/**
 * BLE link to both arms of an Even Realities G2 over android.bluetooth (port of the transport half of
 * men-g2-ble-gateway `client.py` + `scan.py`). No app-specific code; the caller must hold BLUETOOTH_SCAN /
 * BLUETOOTH_CONNECT (API 31+) or BLUETOOTH + ACCESS_FINE_LOCATION (API 26-30) before calling [scanForPair] / [connect].
 *
 * - scan by advertised name prefix "Even G2_" (arms advertise `Even G2_<xx>_L_<serial>` / `_R_`); the serial is the
 *   first 14 ASCII bytes of the manufacturer data (fallback: the name after `_L_`/`_R_`); L and R are paired by serial.
 * - connect both arms with autoConnect=false, request MTU [Config.mtu] (whatever the arm grants is used), discover
 *   services, enable notify on 0x5402 (both), 0x6402 (left only) and 0x7402 (best effort).
 * - one serialized GATT queue per arm: writes are write-without-response, [Config.packetGapMs] apart (the gateway's
 *   ble_packet_gap_ms = 8).
 * - per-arm reconnect with exponential backoff after an unexpected disconnect; [state] and [readyEvents] report it
 *   (re-run [G2Session.reinitSide] when an arm becomes ready again).
 */
@SuppressLint("MissingPermission")
class G2Ble(
    context: Context,
    parentScope: CoroutineScope,
    val config: Config = Config(),
    private val log: (String) -> Unit = {},
) : G2Transport {

    data class Config(
        val namePrefix: String = "Even G2_",
        /** MTU to request; 247 gives full 232-byte frame chunks. A higher grant is used as is. */
        val mtu: Int = 247,
        val packetGapMs: Long = 8,
        val reconnectBaseMs: Long = 1000,
        val reconnectMaxMs: Long = 30_000,
        /** createBond() before connecting when the arm is not bonded (the Python gateway connects with pair=True). */
        val bondBeforeConnect: Boolean = false,
        val opTimeoutMs: Long = 5000,
        /** how long to wait for onCharacteristicWrite of a write-without-response before moving on */
        val writeAckTimeoutMs: Long = 500,
    )

    enum class ArmState { DISCONNECTED, CONNECTING, DISCOVERING, READY }

    data class Found(val device: BluetoothDevice, val name: String, val side: Side, val serial: String, val rssi: Int)
    data class G2Pair(val serial: String, val left: BluetoothDevice, val right: BluetoothDevice)

    companion object {
        const val UUID_BASE = "00002760-08c2-11e1-9073-0e8ac72e"
        val SERVICE: UUID = uuid("0000")
        val CHAR_WRITE: UUID = uuid("5401")
        val CHAR_NOTIFY: UUID = uuid("5402")
        val CHAR_MIC: UUID = uuid("6402")
        val CHAR_FILE_WRITE: UUID = uuid("7401")
        val CHAR_FILE_NOTIFY: UUID = uuid("7402")
        val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        fun uuid(suffix: String): UUID = UUID.fromString(UUID_BASE + suffix)

        /** "_L_" / "_R_" in the advertised name -> side. */
        fun sideFromName(name: String): Side? {
            val u = name.uppercase()
            return when { "_L_" in u -> Side.LEFT; "_R_" in u -> Side.RIGHT; else -> null }
        }

        /** First 14 bytes of the manufacturer data as ASCII, control chars removed (scan.py). */
        fun serialFromManufacturer(data: ByteArray?): String? {
            if (data == null || data.size < 14) return null
            val s = String(data, 0, 14, Charsets.ISO_8859_1).filter { it.code in 0x20..0x7E }
            return s.ifEmpty { null }
        }

        fun serialFromName(name: String): String? {
            val u = name.uppercase()
            val i = maxOf(u.indexOf("_L_"), u.indexOf("_R_"))
            return if (i >= 0 && i + 3 < name.length) name.substring(i + 3) else null
        }
    }

    private val appContext = context.applicationContext
    private val scope = CoroutineScope(parentScope.coroutineContext + SupervisorJob(parentScope.coroutineContext[Job]))
    private val adapter: BluetoothAdapter? = (appContext.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    private val _inbound = MutableSharedFlow<G2Inbound>(extraBufferCapacity = 2048, onBufferOverflow = BufferOverflow.DROP_OLDEST)
    override val inbound: SharedFlow<G2Inbound> = _inbound.asSharedFlow()

    private val _state = MutableStateFlow(mapOf(Side.LEFT to ArmState.DISCONNECTED, Side.RIGHT to ArmState.DISCONNECTED))
    val state: StateFlow<Map<Side, ArmState>> = _state.asStateFlow()

    private val _ready = MutableSharedFlow<Side>(extraBufferCapacity = 16)
    /** Emits an arm each time it (re)becomes READY. */
    val readyEvents: SharedFlow<Side> = _ready.asSharedFlow()

    private val arms = HashMap<Side, Arm>()

    val bothReady: Boolean get() = _state.value.values.all { it == ArmState.READY }

    override val maxWrite: Int
        get() {
            val m = synchronized(arms) { arms.values.filter { it.isReady }.map { it.mtu - 3 } }
            return m.minOrNull() ?: Framing.DEFAULT_MAX_WRITE
        }

    // ── scan ───────────────────────────────────────────────────────────────────────────

    /** Scan until both arms of one pair are seen (optionally only [serial]); null on timeout. */
    suspend fun scanForPair(timeoutMs: Long = 15_000, serial: String? = null): G2Pair? {
        val scanner = adapter?.bluetoothLeScanner ?: throw IllegalStateException("Bluetooth is off or unavailable")
        val seen = HashMap<String, MutableMap<Side, Found>>()
        val result = CompletableDeferred<G2Pair>()
        val cb = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, r: ScanResult) {
                val f = toFound(r) ?: return
                if (serial != null && !f.serial.contains(serial)) return
                val group = synchronized(seen) { seen.getOrPut(f.serial) { HashMap() }.also { it[f.side] = f }.toMap() }
                val l = group[Side.LEFT]
                val rr = group[Side.RIGHT]
                if (l != null && rr != null) result.complete(G2Pair(f.serial, l.device, rr.device))
            }

            override fun onBatchScanResults(results: MutableList<ScanResult>) { results.forEach { onScanResult(0, it) } }
            override fun onScanFailed(errorCode: Int) { result.completeExceptionally(IllegalStateException("BLE scan failed: $errorCode")) }
        }
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        scanner.startScan(null, settings, cb)
        try {
            return withTimeoutOrNull(timeoutMs) { result.await() }
        } finally {
            try { scanner.stopScan(cb) } catch (e: Exception) { log("stopScan: $e") }
        }
    }

    private fun toFound(r: ScanResult): Found? {
        val name = r.scanRecord?.deviceName ?: r.device.name ?: return null
        if (!name.startsWith(config.namePrefix, ignoreCase = true)) return null
        val side = sideFromName(name) ?: return null
        val md = r.scanRecord?.manufacturerSpecificData
        val mBytes = if (md != null && md.size() > 0) md.valueAt(0) else null
        val serial = serialFromManufacturer(mBytes) ?: serialFromName(name) ?: return null
        return Found(r.device, name, side, serial, r.rssi)
    }

    // ── connect ────────────────────────────────────────────────────────────────────────

    fun connect(pair: G2Pair) = connect(pair.left, pair.right)

    /** Connect by saved addresses (no scan). */
    fun connect(leftAddress: String, rightAddress: String) {
        val a = adapter ?: throw IllegalStateException("Bluetooth unavailable")
        connect(a.getRemoteDevice(leftAddress), a.getRemoteDevice(rightAddress))
    }

    fun connect(left: BluetoothDevice, right: BluetoothDevice) {
        synchronized(arms) {
            arms.values.forEach { it.close() }
            arms.clear()
            arms[Side.LEFT] = Arm(Side.LEFT, left)
            arms[Side.RIGHT] = Arm(Side.RIGHT, right)
        }
        // the gateway connects left, then right
        scope.launch {
            arms[Side.LEFT]?.start()
            delay(50)
            arms[Side.RIGHT]?.start()
        }
    }

    /** Suspend until both arms are READY (true) or [timeoutMs] passes (false). */
    suspend fun awaitReady(timeoutMs: Long = 30_000): Boolean =
        withTimeoutOrNull(timeoutMs) { _state.first { m -> m.values.all { it == ArmState.READY } }; true } ?: false

    /** Disconnect both arms for good (no reconnect). */
    fun disconnect() {
        synchronized(arms) { arms.values.forEach { it.close() }; arms.clear() }
        _state.value = mapOf(Side.LEFT to ArmState.DISCONNECTED, Side.RIGHT to ArmState.DISCONNECTED)
    }

    private fun setState(side: Side, s: ArmState) {
        _state.value = _state.value.toMutableMap().also { it[side] = s }
        if (s == ArmState.READY) _ready.tryEmit(side)
    }

    // ── writes ─────────────────────────────────────────────────────────────────────────

    override suspend fun write(side: Side, frame: ByteArray, char: G2Char) {
        require(char.writeSuffix != null) { "$char is notify-only" }
        if (side == Side.BOTH) {
            val l = arm(Side.LEFT)
            val r = arm(Side.RIGHT)
            coroutineScope { listOf(async { l.write(char, frame) }, async { r.write(char, frame) }).awaitAll() }
        } else {
            arm(side).write(char, frame)
        }
    }

    private fun arm(side: Side): Arm = synchronized(arms) { arms[side] } ?: throw IllegalStateException("$side arm not connected")

    // ── one arm ────────────────────────────────────────────────────────────────────────

    private class WriteReq(val char: G2Char, val data: ByteArray, val done: CompletableDeferred<Unit>)

    private inner class Arm(val side: Side, val device: BluetoothDevice) {
        @Volatile var gatt: BluetoothGatt? = null
        @Volatile var mtu = 23
        @Volatile var closed = false
        @Volatile var ready = false
        val isReady: Boolean get() = ready && !closed
        private var attempt = 0
        private val op = Mutex()
        @Volatile private var pending: CompletableDeferred<Int>? = null
        @Volatile private var pendingMtu: CompletableDeferred<Int>? = null
        private val queue = Channel<WriteReq>(Channel.UNLIMITED)
        private var setupJob: Job? = null
        private var reconnectJob: Job? = null
        private val worker: Job = scope.launch { for (req in queue) process(req) }
        private val chars = HashMap<UUID, BluetoothGattCharacteristic>()

        suspend fun start() {
            if (closed) return
            setState(side, ArmState.CONNECTING)
            if (config.bondBeforeConnect && device.bondState == BluetoothDevice.BOND_NONE) {
                log("$side: createBond")
                device.createBond()
                withTimeoutOrNull(30_000) { while (device.bondState != BluetoothDevice.BOND_BONDED) delay(250) }
            }
            if (closed) return
            log("$side: connectGatt")
            gatt = device.connectGatt(appContext, false, callback, BluetoothDevice.TRANSPORT_LE)
        }

        fun close() {
            closed = true
            ready = false
            reconnectJob?.cancel()
            setupJob?.cancel()
            worker.cancel()
            queue.close()
            while (true) {
                val r = queue.tryReceive().getOrNull() ?: break
                r.done.completeExceptionally(IllegalStateException("$side arm closed"))
            }
            pending?.cancel(); pendingMtu?.cancel()
            gatt?.let { g -> try { g.disconnect() } catch (e: Exception) { }; try { g.close() } catch (e: Exception) { } }
            gatt = null
        }

        suspend fun write(char: G2Char, data: ByteArray) {
            val req = WriteReq(char, data, CompletableDeferred())
            if (queue.trySend(req).isFailure) throw IllegalStateException("$side arm closed")
            req.done.await()
        }

        private suspend fun process(req: WriteReq) {
            try {
                val g = gatt
                if (!isReady || g == null) throw IllegalStateException("cannot send to disconnected G2 arm: $side")
                val uuid = if (req.char == G2Char.FILE) CHAR_FILE_WRITE else CHAR_WRITE
                val ch = chars[uuid] ?: throw IllegalStateException("$side: characteristic $uuid not found")
                op.withLock { writeNoResponse(g, ch, req.data) }
                req.done.complete(Unit)
                if (config.packetGapMs > 0) delay(config.packetGapMs)
            } catch (e: Throwable) {
                req.done.completeExceptionally(e)
            }
        }

        @Suppress("DEPRECATION")
        private suspend fun writeNoResponse(g: BluetoothGatt, ch: BluetoothGattCharacteristic, data: ByteArray) {
            val type = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
            repeat(100) {
                val d = CompletableDeferred<Int>()
                pending = d
                val started = if (Build.VERSION.SDK_INT >= 33) {
                    g.writeCharacteristic(ch, data, type) == BluetoothStatusCodes.SUCCESS
                } else {
                    ch.writeType = type
                    ch.value = data
                    g.writeCharacteristic(ch)
                }
                if (started) {
                    withTimeoutOrNull(config.writeAckTimeoutMs) { d.await() }
                    pending = null
                    return
                }
                pending = null
                delay(10) // stack busy (ERROR_GATT_WRITE_REQUEST_BUSY / false): retry
            }
            throw IllegalStateException("$side: write failed (stack busy)")
        }

        @Suppress("DEPRECATION")
        private suspend fun enableNotify(g: BluetoothGatt, uuid: UUID, required: Boolean): Boolean {
            val ch = chars[uuid]
            if (ch == null) {
                if (required) throw IllegalStateException("$side: characteristic $uuid not found")
                log("$side: $uuid not present"); return false
            }
            if (!g.setCharacteristicNotification(ch, true)) {
                if (required) throw IllegalStateException("$side: setCharacteristicNotification($uuid) failed")
                return false
            }
            val desc = ch.getDescriptor(CCCD) ?: run { if (required) throw IllegalStateException("$side: no CCCD on $uuid"); return false }
            val value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            val status = op.withLock {
                val d = CompletableDeferred<Int>()
                pending = d
                val started = if (Build.VERSION.SDK_INT >= 33) g.writeDescriptor(desc, value) == BluetoothStatusCodes.SUCCESS
                else { desc.value = value; g.writeDescriptor(desc) }
                val st = if (started) withTimeoutOrNull(config.opTimeoutMs) { d.await() } else null
                pending = null
                st
            }
            if (status != BluetoothGatt.GATT_SUCCESS) {
                if (required) throw IllegalStateException("$side: enabling notify on $uuid failed ($status)")
                log("$side: notify on $uuid unavailable ($status)"); return false
            }
            return true
        }

        private suspend fun setup(g: BluetoothGatt) {
            setState(side, ArmState.DISCOVERING)
            delay(100)
            val m = CompletableDeferred<Int>()
            pendingMtu = m
            if (g.requestMtu(config.mtu)) {
                withTimeoutOrNull(config.opTimeoutMs) { m.await() }?.let { mtu = it }
            }
            pendingMtu = null
            log("$side: MTU $mtu")
            val d = CompletableDeferred<Int>()
            pending = d
            if (!g.discoverServices()) throw IllegalStateException("$side: discoverServices refused")
            val st = withTimeoutOrNull(config.opTimeoutMs * 2) { d.await() }
            pending = null
            if (st != BluetoothGatt.GATT_SUCCESS) throw IllegalStateException("$side: service discovery failed ($st)")
            chars.clear()
            for (s in g.services) for (c in s.characteristics) chars[c.uuid] = c
            if (chars[CHAR_WRITE] == null) throw IllegalStateException("$side: 0x5401 missing")
            delay(50)
            enableNotify(g, CHAR_NOTIFY, required = true)
            if (side == Side.LEFT) { delay(50); enableNotify(g, CHAR_MIC, required = false) }
            delay(50)
            enableNotify(g, CHAR_FILE_NOTIFY, required = false)
            attempt = 0
            ready = true
            setState(side, ArmState.READY)
            log("$side: ready")
        }

        private fun scheduleReconnect(reason: String) {
            if (closed) return
            ready = false
            setState(side, ArmState.DISCONNECTED)
            val wait = minOf(config.reconnectMaxMs, config.reconnectBaseMs shl minOf(attempt, 5))
            attempt++
            log("$side: $reason; reconnect in $wait ms")
            reconnectJob?.cancel()
            reconnectJob = scope.launch { delay(wait); start() }
        }

        private fun emit(uuid: UUID, value: ByteArray) {
            val c = when (uuid) { CHAR_NOTIFY -> G2Char.COMMAND; CHAR_MIC -> G2Char.MIC; CHAR_FILE_NOTIFY -> G2Char.FILE; else -> return }
            _inbound.tryEmit(G2Inbound(side, c, value.copyOf()))
        }

        val callback = object : BluetoothGattCallback() {
            override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
                if (closed) { try { g.close() } catch (e: Exception) { }; return }
                if (newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS) {
                    gatt = g
                    setupJob?.cancel()
                    setupJob = scope.launch {
                        try { setup(g) } catch (e: Exception) {
                            log("$side: setup failed: ${e.message}")
                            try { g.disconnect(); g.close() } catch (x: Exception) { }
                            gatt = null
                            scheduleReconnect("setup failed")
                        }
                    }
                } else {
                    setupJob?.cancel()
                    pending?.complete(-1); pendingMtu?.complete(mtu)
                    try { g.close() } catch (e: Exception) { }
                    gatt = null
                    scheduleReconnect("disconnected (status $status)")
                }
            }

            override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
                pendingMtu?.complete(if (status == BluetoothGatt.GATT_SUCCESS) mtu else this@Arm.mtu)
            }

            override fun onServicesDiscovered(g: BluetoothGatt, status: Int) { pending?.complete(status) }
            override fun onDescriptorWrite(g: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) { pending?.complete(status) }
            override fun onCharacteristicWrite(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, status: Int) { pending?.complete(status) }

            @Deprecated("API < 33 path")
            @Suppress("DEPRECATION")
            override fun onCharacteristicChanged(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
                if (Build.VERSION.SDK_INT < 33) emit(characteristic.uuid, characteristic.value ?: return)
            }

            override fun onCharacteristicChanged(g: BluetoothGatt, characteristic: BluetoothGattCharacteristic, value: ByteArray) {
                if (Build.VERSION.SDK_INT >= 33) emit(characteristic.uuid, value)
            }
        }
    }
}
