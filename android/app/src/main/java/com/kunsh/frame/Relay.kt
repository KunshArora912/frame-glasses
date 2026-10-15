package com.kunsh.frame

import android.os.Handler
import android.os.Looper
import android.util.Base64
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * WebSocket bridge to relay_server.py on the laptop (the /phone endpoint).
 * Same JSON protocol as phone/app.js. Reconnects every 5 s while wanted.
 */
class Relay(private val listener: Listener) {

    interface Listener {
        fun onRelayState(linked: Boolean)
        fun onRelayWrite(id: Long, data: ByteArray, withResponse: Boolean)
        fun onLog(msg: String)
    }

    private val main = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(8, TimeUnit.SECONDS)
        .build()

    private var ws: WebSocket? = null
    var linked = false; private set
    var wanted = false; private set
    var url: String = ""; private set

    fun link(url: String) {
        this.url = url
        wanted = true
        open()
    }

    fun unlink() {
        wanted = false
        ws?.close(1000, null)
        ws = null
        setLinked(false)
    }

    private fun open() {
        if (!wanted || ws != null) return
        listener.onLog("linking to $url")
        val req = Request.Builder().url(url).build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                main.post { setLinked(true); listener.onLog("laptop linked") }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val m = try { JSONObject(text) } catch (_: Exception) { return }
                if (m.optString("t") == "write") {
                    val id = m.optLong("id")
                    val data = try { Base64.decode(m.optString("d"), Base64.DEFAULT) } catch (_: Exception) { return }
                    val r = m.optBoolean("r", true)
                    main.post { listener.onRelayWrite(id, data, r) }
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

    private fun dropped(socket: WebSocket, why: String) {
        if (ws !== socket) return
        ws = null
        setLinked(false)
        if (wanted) {
            listener.onLog("laptop link dropped ($why); retrying in 5 s")
            main.postDelayed({ if (wanted && ws == null) open() }, 5000)
        }
    }

    private fun setLinked(v: Boolean) {
        if (linked == v) return
        linked = v
        listener.onRelayState(v)
    }

    // ------------------------------------------------------------- outgoing
    private fun send(m: JSONObject) {
        ws?.send(m.toString())
    }

    fun hello(name: String?, mtu: Int) =
        send(JSONObject().put("t", "hello").put("name", name ?: JSONObject.NULL).put("mtu", mtu))

    fun ble(connected: Boolean, name: String?, mtu: Int) =
        send(JSONObject().put("t", "ble").put("state", if (connected) "connected" else "disconnected")
            .put("name", name ?: JSONObject.NULL).put("mtu", mtu))

    fun notify(bytes: ByteArray) =
        send(JSONObject().put("t", "notify").put("d", Base64.encodeToString(bytes, Base64.NO_WRAP)))

    fun wrote(id: Long) = send(JSONObject().put("t", "wrote").put("id", id))

    fun error(id: Long, msg: String) = send(JSONObject().put("t", "error").put("id", id).put("msg", msg))
}
