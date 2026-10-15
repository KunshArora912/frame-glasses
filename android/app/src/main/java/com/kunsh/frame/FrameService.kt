package com.kunsh.frame

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Binder
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Owns the Bluetooth link and the laptop relay so they outlive the screen.
 * Runs as a foreground service (connected-device type) while connected.
 */
class FrameService : Service(), FrameBle.Listener, Relay.Listener {

    inner class LocalBinder : Binder() { val service: FrameService get() = this@FrameService }

    data class Status(val pct: Int?, val charging: Boolean?, val fw: String?, val seenAt: Long)
    data class BleState(val connected: Boolean, val name: String?)

    companion object {
        const val CHANNEL = "frame"
        const val NOTIF_ID = 1
        const val PREFS = "frame"
        const val KEY_ADDR = "frame_addr"
        const val KEY_RELAY = "relay_url"
        const val LOG_MAX = 300
        val APPS = listOf("assistant", "shell", "todo", "navigate", "showcase")
    }

    lateinit var ble: FrameBle; private set
    lateinit var relay: Relay; private set

    private val binder = LocalBinder()
    private val _status = MutableLiveData<Status>()
    private val _bleState = MutableLiveData(BleState(false, null))
    private val _relayState = MutableLiveData(false)
    private val _log = MutableLiveData<List<String>>(emptyList())
    private val logLines = ArrayDeque<String>()
    private val textBuf = StringBuilder()
    private val clock = SimpleDateFormat("HH:mm:ss", Locale.US)

    val status: LiveData<Status> get() = _status
    val bleState: LiveData<BleState> get() = _bleState
    val relayState: LiveData<Boolean> get() = _relayState
    val log: LiveData<List<String>> get() = _log

    private val prefs by lazy { getSharedPreferences(PREFS, Context.MODE_PRIVATE) }
    var savedAddress: String?
        get() = prefs.getString(KEY_ADDR, null)
        set(v) { prefs.edit().putString(KEY_ADDR, v).apply() }
    var savedRelayUrl: String
        get() = prefs.getString(KEY_RELAY, "ws://localhost:8765/phone") ?: ""
        set(v) { prefs.edit().putString(KEY_RELAY, v).apply() }

    override fun onCreate() {
        super.onCreate()
        ble = FrameBle(this, this)
        relay = Relay(this)
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Frame link", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Keeps the Bluetooth link to the glasses alive"
            })
    }

    override fun onBind(intent: Intent?): IBinder = binder
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        relay.unlink()
        ble.disconnect()
        super.onDestroy()
    }

    // ------------------------------------------------------------- actions
    fun connect(forceScan: Boolean = false) {
        goForeground("Connecting to Frame")
        ble.connect(if (forceScan) null else savedAddress)
    }

    fun disconnect() {
        ble.disconnect()
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    fun forgetDevice() { savedAddress = null; addLog("forgot saved Frame; next Connect scans") }

    fun linkRelay(url: String) { savedRelayUrl = url; relay.link(url) }
    fun unlinkRelay() = relay.unlink()

    fun sendLua(s: String) {
        val bytes = s.toByteArray(Charsets.UTF_8)
        if (bytes.size > ble.maxPayload) { addLog("too long for one packet (${ble.maxPayload} max)"); return }
        addLog("> $s")
        ble.write(bytes) { e -> if (e != null) addLog("write failed: ${e.message}") }
    }

    fun sendBreak() { addLog("> break"); ble.write(byteArrayOf(0x03)) }
    fun sendReset() { addLog("> reset (wake screen)"); ble.write(byteArrayOf(0x04)) }

    /** Same request the glasses' menu makes; frame_host.py on the laptop acts on it. */
    fun askLaunch(id: String) {
        if (!relay.linked) { addLog("laptop not linked"); return }
        relay.notify("launch:$id\n".toByteArray())
        addLog("asked laptop for $id")
    }

    // ----------------------------------------------------- FrameBle.Listener
    override fun onBleState(connected: Boolean, name: String?, address: String?) {
        if (connected && address != null) savedAddress = address
        _bleState.value = BleState(connected, name)
        relay.ble(connected, name, ble.mtu)
        if (connected) goForeground("Connected to ${name ?: "Frame"}")
        else { textBuf.setLength(0); updateNotification("Frame not connected") }
        addLog(if (connected) "connected to ${name ?: "Frame"}" else "not connected")
    }

    override fun onPacket(bytes: ByteArray) {
        if (relay.linked) relay.notify(bytes)
        if (bytes.isNotEmpty() && bytes[0] == 0x01.toByte()) return   // data channel: laptop's business
        textBuf.append(String(bytes, Charsets.UTF_8))
        while (true) {
            val i = textBuf.indexOf("\n")
            if (i < 0) break
            handleLine(textBuf.substring(0, i).trim())
            textBuf.delete(0, i + 1)
        }
        if (textBuf.length > 400) { handleLine(textBuf.toString().trim()); textBuf.setLength(0) }
    }

    private fun handleLine(line: String) {
        if (line.isEmpty()) return
        when {
            line.startsWith("status:") -> {
                val p = line.split(":")
                val pct = p.getOrNull(1)?.toIntOrNull()
                val chg = when (p.getOrNull(2)) { "1" -> true; "0" -> false; else -> null }
                val fw = p.getOrNull(3)
                _status.value = Status(pct, chg, fw, System.currentTimeMillis())
                val state = when (chg) { true -> if ((pct ?: 0) >= 99) "charged" else "charging"; false -> "on battery"; null -> "" }
                updateNotification("Frame ${pct ?: "--"}% $state")
            }
            line.startsWith("launch:") -> addLog("glasses asked for ${line.substring(7)}" +
                    if (relay.linked) " -> laptop" else " (laptop not linked)")
            else -> addLog("< $line")
        }
    }

    // -------------------------------------------------------- Relay.Listener
    override fun onRelayState(linked: Boolean) {
        _relayState.value = linked
        if (linked) {
            relay.hello(ble.name, ble.mtu)
            if (ble.connected) relay.ble(true, ble.name, ble.mtu)
        }
    }

    override fun onRelayWrite(id: Long, data: ByteArray, withResponse: Boolean) {
        ble.write(data, withResponse) { e ->
            if (e == null) relay.wrote(id) else relay.error(id, e.message ?: "write failed")
        }
    }

    override fun onLog(msg: String) = addLog(msg)

    // ------------------------------------------------------------- plumbing
    private fun addLog(msg: String) {
        logLines.addLast("[${clock.format(Date())}] $msg")
        while (logLines.size > LOG_MAX) logLines.removeFirst()
        _log.value = logLines.toList()
    }

    private fun notification(text: String): Notification {
        val pi = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_fg)
            .setContentTitle("Frame")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(pi)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun goForeground(text: String) {
        val n = notification(text)
        if (Build.VERSION.SDK_INT >= 29) startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        else startForeground(NOTIF_ID, n)
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notification(text))
    }
}
