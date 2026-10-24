package com.kunsh.frame

import com.kunsh.g2core.EvenAi
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.selects.onTimeout
import kotlinx.coroutines.selects.select
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.cancellation.CancellationException

/**
 * The phone's replacement for Even AI (SID 0x07), port of g2/even_ai.py with the phone's microphone instead of the
 * glasses' LC3 stream:
 *
 *   glasses -> CTRL WAKE_UP           (Even AI picked from the glasses menu / the long-press gesture)
 *   phone   -> CTRL ENTER, HEARTBEAT every 2 s
 *   phone   :  SpeechRecognizer on the phone mic (<= [listenMs])
 *   phone   -> ASK {transcript}, 300 ms, ANALYSE ("thinking")
 *   phone   :  Claude (+ phone tools)
 *   phone   -> REPLY chunks <= 512 B, 150 ms apart, end flag on the last
 *   glasses -> EVENT SCROLL / STREAM_COMPLETE  restarts the reading time
 *   either  -> CTRL EXIT              (glasses: the user double-tapped; phone: reading time over)
 */
class EvenAiTurns(
    private val scope: CoroutineScope,
    private val send: suspend (ByteArray) -> Unit,
    private val magic: () -> Int,
    private val listen: suspend () -> PhoneSpeech.Heard,
    private val answer: suspend (String) -> String,
    private val log: (String) -> Unit = {},
    private val onPhase: (String) -> Unit = {},
    val listenMs: Long = 15_000,
    val thinkMs: Long = 50_000,
) {
    data class Turn(var question: String = "", var answer: String = "", var error: String? = null) {
        val exit = CompletableDeferred<Unit>()
        val events = Channel<Unit>(Channel.CONFLATED)
    }

    @Volatile var turn: Turn? = null
        private set
    @Volatile private var job: Job? = null
    @Volatile private var hb: Job? = null
    @Volatile var sessionOpen = false
        private set
    @Volatile var lastQuestion = ""; private set
    @Volatile var lastAnswer = ""; private set

    val running: Boolean get() = job?.isActive == true

    /** Feed every inbound SID 0x07 payload here. */
    fun onFrame(payload: ByteArray) {
        val m = try { EvenAi.parse(payload) } catch (e: Exception) { log("Even AI: undecodable frame"); return }
        if (m.cmd != EvenAi.CMD_HEARTBEAT) log("Even AI <- ${m.name}${m.value?.let { " $it" } ?: ""}")
        val t = turn
        when {
            m.isWakeUp -> {
                if (running || sessionOpen) log("Even AI: wake ignored, a turn is running")
                else job = scope.launch { runTurn() }
            }
            m.isExit -> {
                stopHeartbeat()
                sessionOpen = false
                if (t != null) { t.exit.complete(Unit); job?.cancel() }
                onPhase("idle (closed on the glasses)")
            }
            t != null && m.cmd == EvenAi.CMD_EVENT -> t.events.trySend(Unit)
            t != null && (m.cmd == EvenAi.CMD_PROMPT || (m.cmd == EvenAi.CMD_COMM_RSP && (m.error ?: 0) != 0)) ->
                t.error = t.error ?: "${m.name} ${m.value ?: m.error}"
        }
    }

    /** End an open turn from the phone (sends EXIT). */
    fun cancel() { job?.cancel() }

    private fun startHeartbeat() {
        stopHeartbeat()
        hb = scope.launch {
            var n = 0
            while (isActive) {
                delay(EvenAi.HEARTBEAT_INTERVAL_MS)
                try { send(EvenAi.heartbeat(magic(), ++n)) } catch (e: CancellationException) { throw e } catch (e: Exception) {
                    log("Even AI: heartbeat stopped: ${e.message}"); return@launch
                }
            }
        }
    }

    private fun stopHeartbeat() { hb?.cancel(); hb = null }

    private suspend fun runTurn() {
        val t = Turn()
        turn = t
        try {
            send(EvenAi.enter(magic()))
            sessionOpen = true
            startHeartbeat()
            onPhase("listening on the phone microphone")
            val heard = withTimeoutOrNull(listenMs) { listen() } ?: PhoneSpeech.Heard(null, "phone mic", "listening timed out")
            if (t.exit.isCompleted) return
            val q = heard.text?.trim().orEmpty()
            t.question = q
            lastQuestion = q
            if (q.isNotEmpty()) {
                log("Even AI heard (${heard.engine}): \"$q\"")
                send(EvenAi.ask(magic(), q))
                delay(300)
            } else {
                t.error = heard.error
                log("Even AI: no question (${heard.error})")
            }
            send(EvenAi.analyse(magic()))
            onPhase("thinking")
            val a = if (q.isEmpty()) {
                if (heard.error?.contains("permission") == true) "The phone app needs microphone permission." else "Sorry, I didn't catch that."
            } else {
                withTimeoutOrNull(thinkMs) { answer(q) } ?: "Sorry, that took too long."
            }
            val text = GlassesText.clean(a).ifEmpty { "(no answer)" }
            t.answer = text
            lastAnswer = text
            if (t.exit.isCompleted) return
            val chunks = GlassesText.chunks(text)
            chunks.forEachIndexed { i, c ->
                if (t.exit.isCompleted) return
                send(EvenAi.reply(magic(), c, final = i == chunks.size - 1))
                if (i < chunks.size - 1) delay(EvenAi.REPLY_PACE_MS)
            }
            onPhase("answered")
            linger(t, text)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            t.error = t.error ?: e.message
            log("Even AI turn failed: $e")
        } finally {
            withContext(NonCancellable) {
                stopHeartbeat()
                if (sessionOpen && !t.exit.isCompleted) {
                    try { send(EvenAi.exit(magic())) } catch (e: Exception) { log("Even AI: EXIT not sent: ${e.message}") }
                }
                sessionOpen = false
                turn = null
                onPhase("idle")
            }
        }
    }

    /** Keep the session open for the reading time; any EVENT (scroll / stream complete) restarts it; EXIT ends it. */
    @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
    private suspend fun linger(t: Turn, text: String) {
        val ms = GlassesText.readingTimeMs(text)
        while (!t.exit.isCompleted) {
            val again = select<Boolean> {
                t.events.onReceive { true }
                t.exit.onAwait { false }
                onTimeout(ms) { false }
            }
            if (!again) return
        }
    }
}
