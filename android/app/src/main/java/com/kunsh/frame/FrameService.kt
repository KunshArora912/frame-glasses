package com.kunsh.frame

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.Manifest
import android.content.pm.PackageManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Binder
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
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
        const val KEY_G2_URL = "g2_url"          // G2 laptop link (g2/phone_link.py)
        const val KEY_G2_ON = "g2_on"
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
    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private var wantConnected = false          // auto-reconnect while true
    private val RECONNECT_MS = 8000L
    var savedAddress: String?
        get() = prefs.getString(KEY_ADDR, null)
        set(v) { prefs.edit().putString(KEY_ADDR, v).apply() }
    var savedG2Url: String
        get() = prefs.getString(KEY_G2_URL, G2Link.DEFAULT_URL) ?: G2Link.DEFAULT_URL
        set(v) { prefs.edit().putString(KEY_G2_URL, v).apply() }
    private var g2Loc: G2Location? = null
    var savedRelayUrl: String
        get() = prefs.getString(KEY_RELAY, "ws://localhost:8765/phone") ?: ""
        set(v) { prefs.edit().putString(KEY_RELAY, v).apply() }

    override fun onCreate() {
        super.onCreate()
        ble = FrameBle(this, this)
        relay = Relay(this)
        G2Link.log = { addLog(it) }
        PhoneHub.init(this)
        PhoneHub.log = { msg -> main.post { addLog(msg) } }
        G2Link.onIntervalChanged = { if (g2Loc != null) startG2Location() }
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Frame link", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Keeps the Bluetooth link to the glasses alive"
            })
    }

    override fun onBind(intent: Intent?): IBinder = binder
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        g2Loc?.stop(); g2Loc = null           // the G2 socket itself may live on in the notification listener
        if (PhoneHub.wanted) PhoneHub.stop(forget = false)
        if (G2Link.log != null) G2Link.log = null
        G2Link.onIntervalChanged = null
        relay.unlink()
        ble.disconnect()
        super.onDestroy()
    }

    // ------------------------------------------------------------- actions
    fun connect(forceScan: Boolean = false) {
        if (G2Link.wanted) {                  // G2 mode: the laptop holds the glasses' link, the phone never connects
            addLog("G2 laptop link is on: Frame Bluetooth connect refused (Disconnect the G2 link first)")
            return
        }
        if (PhoneHub.wanted) {
            addLog("G2 phone hub is on: Frame Bluetooth connect refused (Disconnect the phone hub first)")
            return
        }
        wantConnected = true
        goForeground("Connecting to Frame")
        ble.connect(if (forceScan) null else savedAddress)
    }

    fun disconnect() {
        wantConnected = false
        main.removeCallbacks(reconnect)
        ble.disconnect()
        if (G2Link.wanted) goForeground("G2 laptop link") else if (PhoneHub.wanted) goForeground("G2 phone hub") else stopForeground(STOP_FOREGROUND_REMOVE)
    }

    // ------------------------------------------------------- G2 phone hub
    /** Laptop | Phone. Switching to Phone stops the laptop link; switching to Laptop stops the phone hub. */
    fun setHubMode(mode: String) {
        if (mode == PhoneHub.mode) return
        PhoneHub.mode = mode
        if (mode == "phone") { if (G2Link.wanted) stopG2() } else { if (PhoneHub.wanted) stopPhoneHub() }
        addLog("G2 hub mode: $mode")
    }

    /** This phone holds both G2 arms (no laptop). */
    fun startPhoneHub() {
        if (G2Link.wanted) stopG2()
        if (wantConnected || ble.connected) {
            addLog("phone hub: closing the Frame Bluetooth link")
            wantConnected = false
            main.removeCallbacks(reconnect); main.removeCallbacks(reconnectCheck)
            ble.disconnect()
        }
        PhoneHub.mode = "phone"
        PhoneHub.start()
        goForeground("G2 phone hub")
        addLog("G2 phone hub on")
    }

    fun stopPhoneHub() {
        PhoneHub.stop()
        if (!wantConnected && !ble.connected && !G2Link.wanted) stopForeground(STOP_FOREGROUND_REMOVE)
        addLog("G2 phone hub off")
    }

    /** The app was opened with the phone hub left on (e.g. after a reboot): connect again. */
    fun resumePhoneHubIfWanted() {
        if (PhoneHub.mode == "phone" && PhoneHub.resumeWanted && !PhoneHub.wanted) startPhoneHub()
    }

    // ------------------------------------------------------- G2 laptop link
    /** Forward phone notifications + GPS to g2/phone_link.py. Never touches Bluetooth. */
    fun startG2(url: String) {
        if (PhoneHub.wanted) stopPhoneHub()          // only one host may hold the glasses
        if (wantConnected || ble.connected) {
            addLog("G2 mode: closing the Frame Bluetooth link")
            wantConnected = false
            main.removeCallbacks(reconnect); main.removeCallbacks(reconnectCheck)
            ble.disconnect()
        }
        val u = url.trim().ifBlank { G2Link.DEFAULT_URL }
        savedG2Url = u
        prefs.edit().putBoolean(KEY_G2_ON, true).apply()
        G2Link.start(u)
        goForeground("G2 laptop link")
        startG2Location()
        if (!G2NotificationListener.hasAccess(this)) addLog("G2: notification access not granted; only GPS is forwarded")
        addLog("G2 laptop link on: $u")
    }

    /** The app was opened with the G2 link left on (e.g. after a reboot): bring back socket + GPS + foreground. */
    fun resumeG2IfWanted() {
        if (PhoneHub.mode != "phone" && prefs.getBoolean(KEY_G2_ON, false) && g2Loc == null) startG2(savedG2Url)
    }

    fun stopG2() {
        prefs.edit().putBoolean(KEY_G2_ON, false).apply()
        G2Link.stop()
        g2Loc?.stop(); g2Loc = null
        G2Link.setFixText("off")
        if (PhoneHub.wanted) goForeground("G2 phone hub")
        else if (!wantConnected && !ble.connected) stopForeground(STOP_FOREGROUND_REMOVE) else goForeground("Frame link")
        addLog("G2 laptop link off")
    }

    /** (Re)start GPS for the G2 link; called again after a permission grant or an interval change. */
    fun startG2Location() {
        if (!G2Link.wanted) return
        val loc = g2Loc ?: G2Location(this) { G2Link.sendLoc(it) }
        g2Loc = loc
        if (loc.hasPermission()) goForeground("G2 laptop link")   // adds the location type to the service
        val msg = loc.start(G2Link.locIntervalMs)
        addLog("G2: $msg")
        if (!msg.startsWith("GPS")) G2Link.setFixText(msg)
    }

    // The glasses sleep, reboot after a firmware update, or walk out of range;
    // keep trying the saved address until the user presses Disconnect.
    private val reconnect: Runnable = object : Runnable {
        override fun run() {
            if (wantConnected && !ble.connected) {
                addLog("retrying ...")
                ble.connect(savedAddress)
                main.postDelayed(reconnectCheck, 20_000)
            }
        }
    }
    private val reconnectCheck: Runnable = object : Runnable {
        override fun run() {
            if (wantConnected && !ble.connected) { ble.disconnect(); main.postDelayed(reconnect, RECONNECT_MS) }
        }
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
        if (connected) {
            main.removeCallbacks(reconnect); main.removeCallbacks(reconnectCheck)
            goForeground("Connected to ${name ?: "Frame"}")
        } else {
            textBuf.setLength(0)
            updateNotification(if (wantConnected) "Frame away; retrying" else "Frame not connected")
            if (wantConnected) { main.removeCallbacks(reconnect); main.postDelayed(reconnect, RECONNECT_MS) }
        }
        addLog(if (connected) "connected to ${name ?: "Frame"}" else if (wantConnected) "not connected; will retry" else "not connected")
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
        android.util.Log.i("FrameService", msg)
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

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

    private fun goForeground(text: String) {
        val n = notification(text)
        if (Build.VERSION.SDK_INT < 29) { startForeground(NOTIF_ID, n); return }
        var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
        if (G2Link.wanted && hasLocationPermission()) type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        // phone hub: the Even AI replacement listens on the phone mic while the screen is off
        if (PhoneHub.wanted && Build.VERSION.SDK_INT >= 30 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED)
            type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        try {
            startForeground(NOTIF_ID, n, type)
        } catch (e: Exception) {              // SecurityException / ForegroundServiceStartNotAllowedException
            addLog("foreground (type $type) refused: ${e.message}; retrying as connectedDevice only")
            try { startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE) }
            catch (e2: Exception) { addLog("foreground refused: ${e2.message}") }
        }
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTIF_ID, notification(text))
    }
}
