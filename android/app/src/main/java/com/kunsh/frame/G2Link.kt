package com.kunsh.frame

import android.location.Location
import android.os.Build
import android.os.Handler
import android.os.Looper
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * G2 laptop link: WebSocket to g2/phone_link.py on the laptop (default ws://localhost:8768/phone,
 * reached over USB with `adb reverse tcp:8768 tcp:8768`). The laptop holds the glasses' Bluetooth
 * link; this phone only forwards its notifications and GPS fixes. Nothing here touches Bluetooth.
 *
 * A process-wide singleton so the notification listener (bound by the system) and FrameService
 * share one socket. Reconnects with backoff (2 s .. 30 s) while wanted. All state changes happen on
 * the main thread.
 */
object G2Link {

    const val DEFAULT_URL = "ws://localhost:8768/phone"
    /** Never forwarded: this app and Even's own app. */
    val NEVER = setOf("com.kunsh.frame", "com.even.sg")
    private const val QUEUE_MAX = 20

    data class State(val linked: Boolean, val text: String)
    /** The glasses' display settings as the laptop last read them ("display" message). */
    data class Display(val height: Int, val distance: Int, val brightness: Int, val auto: Boolean,
                       val battery: Int, val charging: Boolean)

    private val main = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(5, TimeUnit.SECONDS)
        .build()
    private val clock = SimpleDateFormat("HH:mm:ss", Locale.US)

    private val _state = MutableLiveData(State(false, "off"))
    private val _lastNotif = MutableLiveData("none yet")
    private val _lastFix = MutableLiveData("none yet")
    val state: LiveData<State> get() = _state
    val lastNotif: LiveData<String> get() = _lastNotif
    val lastFix: LiveData<String> get() = _lastFix
    private val _display = MutableLiveData<Display?>(null)
    val display: LiveData<Display?> get() = _display

    @Volatile var wanted = false; private set
    @Volatile var linked = false; private set
    var url: String = DEFAULT_URL; private set
    /** From the laptop's config message: forward only these packages when non-empty. */
    @Volatile var allow: Set<String> = emptySet(); private set
    @Volatile var locIntervalMs: Long = 5000L; private set
    /** Called (main thread) when the laptop changes the location interval. */
    var onIntervalChanged: (() -> Unit)? = null
    var log: ((String) -> Unit)? = null

    private var ws: WebSocket? = null
    private var backoffMs = 2000L
    private val queue = ArrayDeque<String>()

    /** Main thread only (activity, service and listener callbacks all are). */
    fun start(url: String) {
        val u = url.ifBlank { DEFAULT_URL }
        if (wanted && u == this.url && ws != null) return      // already linked / connecting to this URL
        this.url = u
        wanted = true
        backoffMs = 2000L
        main.removeCallbacks(retry)
        ws?.cancel(); ws = null
        open()
    }

    fun stop() {
        wanted = false
        main.removeCallbacks(retry)
        ws?.close(1000, null)
        ws = null
        linked = false
        queue.clear()
        _display.value = null
        _state.value = State(false, "off")
    }

    private val retry = Runnable { if (wanted && ws == null) open() }

    private fun open() {
        if (!wanted || ws != null) return
        _state.value = State(false, "connecting to $url")
        val req = try { Request.Builder().url(url).build() } catch (e: IllegalArgumentException) {
            _state.value = State(false, "bad URL: $url"); wanted = false; return
        }
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                main.post { opened(webSocket) }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val m = try { JSONObject(text) } catch (_: Exception) { return }
                when (m.optString("t")) {
                    "config" -> main.post { applyConfig(m) }
                    "display" -> {
                        val d = Display(m.optInt("height"), m.optInt("distance"), m.optInt("brightness"),
                            m.optInt("auto") != 0, m.optInt("battery"), m.optBoolean("charging"))
                        main.post { _display.value = d }
                    }
                    "error" -> main.post { log?.invoke("laptop: ${m.optString("msg")}") }
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                main.post { dropped(webSocket, "closed") }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                main.post { dropped(webSocket, t.message ?: "failure") }
            }
        })
    }

    private fun opened(webSocket: WebSocket) {
        if (ws !== webSocket) return
        linked = true
        backoffMs = 2000L
        _state.value = State(true, "linked to laptop")
        log?.invoke("G2 laptop link up")
        webSocket.send(JSONObject().put("t", "hello").put("device", Build.MODEL ?: "android").toString())
        while (queue.isNotEmpty()) webSocket.send(queue.removeFirst())
    }

    private fun dropped(socket: WebSocket, why: String) {
        if (ws !== socket) return
        ws = null
        linked = false
        _display.value = null
        if (!wanted) { _state.value = State(false, "off"); return }
        val s = backoffMs / 1000
        _state.value = State(false, "laptop not reachable ($why); retry in ${s}s")
        main.postDelayed(retry, backoffMs)
        backoffMs = (backoffMs * 2).coerceAtMost(30_000L)
    }

    private fun applyConfig(m: JSONObject) {
        val a = m.optJSONArray("notif_allow")
        allow = if (a == null) emptySet() else (0 until a.length()).map { a.optString(it) }.filter { it.isNotEmpty() }.toSet()
        val sec = m.optDouble("loc_interval_s", Double.NaN)
        if (!sec.isNaN() && sec >= 1.0) {
            val ms = (sec * 1000).toLong()
            if (ms != locIntervalMs) { locIntervalMs = ms; onIntervalChanged?.invoke() }
        }
        log?.invoke("laptop config: ${if (allow.isEmpty()) "all apps" else "${allow.size} apps"}, GPS every ${locIntervalMs / 1000}s")
    }

    private fun send(text: String, queueIfOffline: Boolean): Boolean {
        val s = ws
        if (linked && s != null && s.send(text)) return true
        if (queueIfOffline) {
            queue.addLast(text)
            while (queue.size > QUEUE_MAX) queue.removeFirst()
        }
        return false
    }

    // ------------------------------------------------------------- outgoing
    fun sendNotif(pkg: String, appName: String, title: String, text: String, time: Long, key: String,
                  ongoing: Boolean, silent: Boolean) = main.post {
        if (!wanted || pkg in NEVER) return@post
        if (allow.isNotEmpty() && pkg !in allow) return@post
        val m = JSONObject().put("t", "notif").put("app", pkg).put("app_name", appName)
            .put("title", title).put("text", text).put("time", time).put("key", key)
            .put("ongoing", ongoing).put("silent", silent)
        val sent = send(m.toString(), true)
        _lastNotif.value = "$appName: ${title.ifBlank { text }.take(60)}  ${clock.format(Date())}" + if (sent) "" else " (queued)"
    }

    fun sendRemoved(key: String) = main.post {
        if (wanted) send(JSONObject().put("t", "notif_removed").put("key", key).toString(), false)
    }

    fun sendLoc(loc: Location) = main.post {
        if (!wanted) return@post
        val m = JSONObject().put("t", "loc").put("lat", loc.latitude).put("lon", loc.longitude).put("time", loc.time)
        if (loc.hasAccuracy()) m.put("acc", loc.accuracy.toDouble())
        if (loc.hasSpeed()) m.put("speed", loc.speed.toDouble())
        if (loc.hasBearing()) m.put("bearing", loc.bearing.toDouble())
        val sent = send(m.toString(), false)
        _lastFix.value = String.format(Locale.US, "%.5f, %.5f  ±%.0f m  %s  %s%s", loc.latitude, loc.longitude,
            if (loc.hasAccuracy()) loc.accuracy else 0f, loc.provider ?: "", clock.format(Date(loc.time)),
            if (sent) "" else " (not sent)")
    }

    /** Display setting change from the Display card; the laptop validates, coalesces and applies it. */
    fun sendSet(key: String, value: Int) = main.post {
        if (wanted) send(JSONObject().put("t", "set").put("key", key).put("value", value).toString(), false)
    }

    fun setFixText(s: String) = main.post { _lastFix.value = s }
}
