package com.kunsh.frame

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.ZoneId
import java.time.ZonedDateTime

/** Scripted HTTP: returns the queued responses in order and records every request. */
class FakeHttp(vararg responses: Pair<Int, String>) : HttpPost {
    data class Req(val url: String, val headers: Map<String, String>, val body: Map<String, Any?>)

    val queue = ArrayDeque(responses.toList())
    val requests = ArrayList<Req>()
    val gets = ArrayList<String>()
    var getHandler: (String) -> HttpPost.Result = { HttpPost.Result(404, "") }

    override suspend fun post(url: String, headers: Map<String, String>, body: String): HttpPost.Result {
        requests.add(Req(url, headers, Json.obj(body)))
        val (c, b) = queue.removeFirstOrNull() ?: error("no scripted response left")
        return HttpPost.Result(c, b)
    }

    override suspend fun get(url: String, headers: Map<String, String>): HttpPost.Result { gets.add(url); return getHandler(url) }
}

class FakeCalendar : CalendarStore {
    val added = ArrayList<CalendarStore.Event>()
    val existing = ArrayList<CalendarStore.Event>()
    override fun add(e: CalendarStore.Event): String { added.add(e); return "Personal" }
    override fun list(fromMs: Long, toMs: Long) = existing.filter { it.endMs > fromMs && it.startMs < toMs }.sortedBy { it.startMs }
}

class ClaudeAgentTest {
    private val zone = ZoneId.of("America/Los_Angeles")
    private val now = ZonedDateTime.of(2026, 10, 24, 9, 30, 0, 0, zone)

    private fun tmpIdeas() = IdeaStore(File(Files.createTempDirectory("ideas").toFile(), "ideas.jsonl"))

    private fun agent(http: FakeHttp, cal: CalendarStore = FakeCalendar(), maxRounds: Int = 4): ClaudeAgent {
        val tools = PhoneTools(cal, tmpIdeas(), http, zone = { zone }, now = { now.toInstant().toEpochMilli() })
        return ClaudeAgent(http, tools.tools(), now = { now }, maxToolRounds = maxRounds)
    }

    private fun toolUse(id: String, name: String, inputJson: String, thinking: Boolean = true) = 200 to """
        {"id":"msg_1","type":"message","role":"assistant","model":"claude-opus-5-5","stop_reason":"tool_use",
         "content":[${if (thinking) """{"type":"thinking","thinking":"","signature":"sig123"},""" else ""}
                    {"type":"text","text":"Adding it."},
                    {"type":"tool_use","id":"$id","name":"$name","input":$inputJson}]}
    """.trimIndent()

    private fun final(text: String, model: String = "claude-opus-5-5") = 200 to
        """{"id":"msg_2","type":"message","role":"assistant","model":"$model","stop_reason":"end_turn","content":[{"type":"text","text":${Json.write(text)}}]}"""

    @Test
    fun toolCallRunsCalendarAndReturnsFinalText() = runBlocking {
        val cal = FakeCalendar()
        val http = FakeHttp(
            toolUse("toolu_1", "add_calendar_event", """{"title":"Dentist","start":"2026-10-25T15:00:00-07:00","duration_minutes":30,"location":"Main St"}"""),
            final("Added Dentist tomorrow at 3 PM."),
        )
        val a = agent(http, cal).ask("sk-test-key-000000000000", "add dentist tomorrow at 3 for half an hour on Main St")

        assertEquals("Added Dentist tomorrow at 3 PM.", a.text)
        assertEquals(listOf("add_calendar_event"), a.toolCalls)
        assertEquals("claude-opus-5-5", a.model)
        assertNull(a.error)

        assertEquals(1, cal.added.size)
        val e = cal.added[0]
        assertEquals("Dentist", e.title)
        assertEquals("Main St", e.location)
        assertEquals(ZonedDateTime.of(2026, 10, 25, 15, 0, 0, 0, zone).toInstant().toEpochMilli(), e.startMs)
        assertEquals(30 * 60_000L, e.endMs - e.startMs)
        assertFalse(e.allDay)

        // request shape
        assertEquals(2, http.requests.size)
        val r1 = http.requests[0]
        assertEquals(ClaudeAgent.URL, r1.url)
        assertEquals("sk-test-key-000000000000", r1.headers["x-api-key"])
        assertEquals("2023-06-01", r1.headers["anthropic-version"])
        assertEquals("claude-opus-5-5", r1.body["model"])
        assertEquals(400L, r1.body["max_tokens"])
        assertTrue((r1.body["system"] as String).contains("60 words"))
        assertTrue((r1.body["system"] as String).contains("2026-10-24"))
        val toolNames = r1.body["tools"].jArr()!!.map { it.jObj()!!["name"] }
        assertEquals(listOf("add_calendar_event", "list_calendar_events", "save_idea", "get_stock_quote"), toolNames)

        // second request: assistant content echoed unchanged (thinking block included), then one user message of tool_results
        val msgs = http.requests[1].body["messages"].jArr()!!.map { it.jObj()!! }
        assertEquals(listOf("user", "assistant", "user"), msgs.map { it["role"] })
        val echoed = msgs[1]["content"].jArr()!!.map { it.jObj()!! }
        assertEquals(listOf("thinking", "text", "tool_use"), echoed.map { it["type"] })
        assertEquals("sig123", echoed[0]["signature"])
        val results = msgs[2]["content"].jArr()!!.map { it.jObj()!! }
        assertEquals(1, results.size)
        assertEquals("tool_result", results[0]["type"])
        assertEquals("toolu_1", results[0]["tool_use_id"])
        assertTrue((results[0]["content"] as String).contains("Dentist"))
        assertNull(results[0]["is_error"])
    }

    @Test
    fun toolFailureIsReportedAsErrorResult() = runBlocking {
        val http = FakeHttp(
            toolUse("toolu_9", "add_calendar_event", """{"title":"X","start":"not a time"}""", thinking = false),
            final("I couldn't read that time."),
        )
        val a = agent(http).ask("sk-test-key-000000000000", "add X")
        assertEquals("I couldn't read that time.", a.text)
        val res = http.requests[1].body["messages"].jArr()!!.last().jObj()!!["content"].jArr()!![0].jObj()!!
        assertEquals(true, res["is_error"])
    }

    @Test
    fun stopsAfterMaxToolRounds() = runBlocking {
        val loop = toolUse("t", "list_calendar_events", "{}", thinking = false)
        val http = FakeHttp(loop, loop, loop)
        val a = agent(http, maxRounds = 2).ask("sk-test-key-000000000000", "loop")
        assertEquals(3, http.requests.size)               // 2 tool rounds, then the third tool_use ends the loop
        assertEquals("Adding it.", a.text)
        assertEquals(2, a.toolCalls.size)
    }

    @Test
    fun fallsBackToSonnetWhenOpusOverloaded() = runBlocking {
        val http = FakeHttp(529 to """{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}""",
            final("Paris.", "claude-sonnet-5-5"))
        val a = agent(http).ask("sk-test-key-000000000000", "capital of France?")
        assertEquals("Paris.", a.text)
        assertEquals("claude-sonnet-5-5", http.requests[1].body["model"])
    }

    @Test
    fun dropsServerFallbackWhenRejected() = runBlocking {
        val http = FakeHttp(400 to """{"type":"error","error":{"type":"invalid_request_error","message":"fallbacks: unknown beta"}}""",
            final("Hi."))
        val ag = agent(http)
        val a = ag.ask("sk-test-key-000000000000", "hi")
        assertEquals("Hi.", a.text)
        assertEquals("default", http.requests[0].body["fallbacks"])
        assertNull(http.requests[1].body["fallbacks"])
        assertNull(http.requests[1].headers["anthropic-beta"])
        assertEquals("claude-opus-5-5", http.requests[1].body["model"])
        assertFalse(ag.serverFallback)
    }

    @Test
    fun badKeyIsNotRetriedOnAnotherModel() = runBlocking {
        val http = FakeHttp(401 to """{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}""")
        val a = agent(http).ask("sk-bad-key-0000000000000", "hi")
        assertEquals(1, http.requests.size)
        assertTrue(a.text.contains("key"))
    }

    @Test
    fun refusalGivesShortApology() = runBlocking {
        val http = FakeHttp(200 to """{"model":"claude-opus-5-5","stop_reason":"refusal","content":[]}""")
        val a = agent(http).ask("sk-test-key-000000000000", "x")
        assertEquals("refusal", a.error)
        assertTrue(a.text.startsWith("Sorry"))
    }

    @Test
    fun saveIdeaToolStoresIdea() = runBlocking {
        val ideas = tmpIdeas()
        val http = FakeHttp(toolUse("t1", "save_idea", """{"text":"Glasses-friendly recipe timer"}""", thinking = false), final("Saved."))
        var pushed: String? = null
        val tools = PhoneTools(FakeCalendar(), ideas, http, zone = { zone }, onIdea = { pushed = it.text; "shown on the dashboard" })
        val a = ClaudeAgent(http, tools.tools(), now = { now }).ask("sk-test-key-000000000000", "save an idea: recipe timer")
        assertEquals("Saved.", a.text)
        assertEquals(listOf("Glasses-friendly recipe timer"), ideas.all().map { it.text })
        assertEquals("Glasses-friendly recipe timer", pushed)
        val res = http.requests[1].body["messages"].jArr()!!.last().jObj()!!["content"].jArr()!![0].jObj()!!
        assertTrue((res["content"] as String).contains("shown on the dashboard"))
    }
}
