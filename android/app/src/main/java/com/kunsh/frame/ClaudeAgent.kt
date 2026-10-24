package com.kunsh.frame

import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.concurrent.TimeUnit
import kotlin.coroutines.cancellation.CancellationException
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** One HTTP request; the agent and the stock fetcher only need this, so tests swap in a fake. */
interface HttpPost {
    class Result(val code: Int, val body: String)
    suspend fun post(url: String, headers: Map<String, String>, body: String): Result
    suspend fun get(url: String, headers: Map<String, String> = emptyMap()): Result = throw UnsupportedOperationException("GET")
}

/** OkHttp implementation; cancelling the coroutine cancels the call. */
class OkHttpPost(private val client: OkHttpClient = DEFAULT) : HttpPost {
    companion object {
        val DEFAULT: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(40, TimeUnit.SECONDS)
            .callTimeout(45, TimeUnit.SECONDS)
            .build()
    }

    override suspend fun post(url: String, headers: Map<String, String>, body: String): HttpPost.Result =
        exec(Request.Builder().url(url).post(body.toRequestBody("application/json".toMediaType())), headers)

    override suspend fun get(url: String, headers: Map<String, String>): HttpPost.Result = exec(Request.Builder().url(url).get(), headers)

    private suspend fun exec(rb: Request.Builder, headers: Map<String, String>): HttpPost.Result {
        headers.forEach { (k, v) -> rb.header(k, v) }
        val call = client.newCall(rb.build())
        return suspendCancellableCoroutine { cont ->
            cont.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) { if (cont.isActive) cont.resumeWithException(e) }
                override fun onResponse(call: Call, response: Response) {
                    val r = try { response.use { HttpPost.Result(it.code, it.body?.string().orEmpty()) } } catch (e: IOException) {
                        if (cont.isActive) cont.resumeWithException(e); return
                    }
                    if (cont.isActive) cont.resume(r)
                }
            })
        }
    }
}

/** A tool Claude may call; [run] returns the tool_result text (an exception becomes an is_error result). */
class AgentTool(val name: String, val description: String, val schema: Map<String, Any?>, val run: suspend (Map<String, Any?>) -> String)

/**
 * Claude Messages API with a manual tool-use loop (assistant tool_use -> run tools -> one user message with every
 * tool_result -> continue; at most [maxToolRounds] tool rounds). Answers are meant for the glasses: short plain text.
 * The key is passed per call and never logged. Models are tried in order ([models]: claude-opus-5-5, then
 * claude-sonnet-5-5 when the first fails for a reason other than a bad key or a bad request).
 */
class ClaudeAgent(
    private val http: HttpPost,
    private val tools: List<AgentTool>,
    private val log: (String) -> Unit = {},
    private val now: () -> ZonedDateTime = { ZonedDateTime.now() },
    val models: List<String> = listOf("claude-opus-5-5", "claude-sonnet-5-5"),
    val maxTokens: Int = 400,
    val maxToolRounds: Int = 4,
) {
    companion object {
        const val URL = "https://api.anthropic.com/v1/messages"
        const val VERSION = "2023-06-01"
        /** Server-side refusal fallback ("default" routing). Dropped automatically if the API rejects it. */
        const val FALLBACK_BETA = "server-side-fallback-2026-07-01"

        const val SYSTEM_PROMPT = "You answer questions asked through smart glasses. The answer is shown on a small " +
            "heads-up display, so reply in plain text only: no markdown, no lists, no headings, no emoji. Keep every " +
            "answer to 60 words or fewer; one or two short sentences is best. Use the tools when the user asks about " +
            "their calendar, wants an event added, wants to save an idea or note, or asks for a stock price. After " +
            "using a tool, confirm briefly what you did or found. If a tool fails, say so in a few words."
    }

    class ApiError(val code: Int, message: String) : Exception(message)

    data class Answer(val text: String, val model: String?, val toolCalls: List<String>, val error: String? = null)

    @Volatile var serverFallback = true
        private set

    fun systemPrompt(): String {
        val t = now()
        return SYSTEM_PROMPT + "\nCurrent local date and time: " +
            t.format(DateTimeFormatter.ofPattern("EEEE yyyy-MM-dd HH:mm", Locale.US)) +
            " (time zone " + t.zone.id + ", UTC offset " + t.offset.id + "). Use ISO 8601 times with this offset in tool inputs."
    }

    private fun toolDefs(): List<Map<String, Any?>> =
        tools.map { mapOf("name" to it.name, "description" to it.description, "input_schema" to it.schema) }

    private suspend fun call(key: String, model: String, messages: List<Map<String, Any?>>): Map<String, Any?> {
        while (true) {
            val body = LinkedHashMap<String, Any?>()
            body["model"] = model
            body["max_tokens"] = maxTokens
            body["system"] = systemPrompt()
            if (tools.isNotEmpty()) body["tools"] = toolDefs()
            body["messages"] = messages
            body["output_config"] = mapOf("effort" to "low")
            if (serverFallback) body["fallbacks"] = "default"
            val headers = linkedMapOf("x-api-key" to key, "anthropic-version" to VERSION, "content-type" to "application/json")
            if (serverFallback) headers["anthropic-beta"] = FALLBACK_BETA
            val r = http.post(URL, headers, Json.write(body))
            if (r.code == 200) return Json.obj(r.body)
            val msg = try { Json.obj(r.body)["error"].jObj()?.str("message") } catch (_: Exception) { null } ?: r.body.take(200)
            if (r.code == 400 && serverFallback && (msg.contains("fallback", true) || msg.contains("beta", true))) {
                log("Claude: server-side fallback not accepted ($msg); retrying without it")
                serverFallback = false
                continue
            }
            throw ApiError(r.code, "HTTP ${r.code}: $msg")
        }
    }

    private fun texts(content: List<Any?>): String =
        content.mapNotNull { it.jObj() }.filter { it["type"] == "text" }.mapNotNull { it.str("text") }.joinToString(" ").trim()

    /** Ask [question]; tries each model in turn. Never throws except for cancellation. */
    suspend fun ask(key: String, question: String): Answer {
        var lastErr: String? = null
        for (model in models) {
            try {
                return askWith(key, model, question)
            } catch (e: CancellationException) {
                throw e
            } catch (e: ApiError) {
                lastErr = e.message
                log("Claude $model failed: ${e.message}")
                if (e.code == 401 || e.code == 403) return Answer("The Claude API key was rejected. Check it in the phone app.", model, emptyList(), e.message)
                if (e.code == 400) break                      // the request itself is wrong; another model will not help
            } catch (e: Exception) {
                lastErr = "${e.javaClass.simpleName}: ${e.message}"
                log("Claude $model failed: $lastErr")
            }
        }
        return Answer("Sorry, Claude is not reachable right now.", null, emptyList(), lastErr)
    }

    private suspend fun askWith(key: String, model: String, question: String): Answer {
        val messages = ArrayList<Map<String, Any?>>()
        messages.add(mapOf("role" to "user", "content" to question))
        val calls = ArrayList<String>()
        var rounds = 0
        while (true) {
            val resp = call(key, model, messages)
            val content = resp["content"].jArr() ?: emptyList()
            val stop = resp.str("stop_reason")
            val served = resp.str("model") ?: model
            when (stop) {
                "tool_use" -> {
                    val uses = content.mapNotNull { it.jObj() }.filter { it["type"] == "tool_use" }
                    if (rounds >= maxToolRounds || uses.isEmpty()) {
                        val t = texts(content)
                        return Answer(t.ifEmpty { "Sorry, that needed too many steps." }, served, calls, "tool rounds exhausted")
                    }
                    rounds++
                    messages.add(mapOf("role" to "assistant", "content" to content))   // whole content (thinking blocks too), unchanged
                    val results = ArrayList<Map<String, Any?>>()
                    for (u in uses) {
                        val name = u.str("name") ?: ""
                        val id = u.str("id") ?: ""
                        val input = u["input"].jObj() ?: emptyMap()
                        calls.add(name)
                        val tool = tools.firstOrNull { it.name == name }
                        var err = false
                        val out = if (tool == null) { err = true; "unknown tool $name" } else try {
                            tool.run(input)
                        } catch (e: CancellationException) {
                            throw e
                        } catch (e: Exception) {
                            err = true
                            "failed: ${e.message ?: e.javaClass.simpleName}"
                        }
                        log("tool $name -> ${if (err) "error: " else ""}${out.take(120)}")
                        val r = linkedMapOf<String, Any?>("type" to "tool_result", "tool_use_id" to id, "content" to out)
                        if (err) r["is_error"] = true
                        results.add(r)
                    }
                    messages.add(mapOf("role" to "user", "content" to results))
                }
                "refusal" -> return Answer("Sorry, I can't help with that one.", served, calls, "refusal")
                else -> {
                    val t = texts(content)
                    if (t.isEmpty() && stop == "max_tokens") return Answer("Sorry, the answer was cut off.", served, calls, "max_tokens")
                    return Answer(t.ifEmpty { "(no answer)" }, served, calls)
                }
            }
        }
    }
}

/** Text shaping for the glasses' Even AI screen. */
object GlassesText {
    /** Drop markdown the model may still produce; collapse whitespace. */
    fun clean(s: String): String = s
        .replace(Regex("```[a-zA-Z]*"), "")
        .replace(Regex("(\\*\\*|__|`)"), "")
        .replace(Regex("(?m)^\\s{0,3}#{1,6}\\s+"), "")
        .replace(Regex("(?m)^\\s*[-*•]\\s+"), "")
        .replace(Regex("[ \\t]+"), " ")
        .replace(Regex("\\n{3,}"), "\n\n")
        .trim()

    /** REPLY chunks: <= 512 UTF-8 bytes each, never splitting a character (g2core EvenAi.replyChunks). */
    fun chunks(text: String): List<ByteArray> = com.kunsh.g2core.EvenAi.replyChunks(text)

    /** How long the answer stays before we EXIT: 8 s + 60 ms per character, at most 90 s (Even app readingTime). */
    fun readingTimeMs(text: String): Long = minOf(90_000L, 8_000L + 60L * text.length)
}
