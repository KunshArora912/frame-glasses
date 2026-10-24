package com.kunsh.frame

import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/** The calendar as the tools see it (CalendarContract on the phone, a fake in tests). */
interface CalendarStore {
    data class Event(val title: String, val startMs: Long, val endMs: Long, val allDay: Boolean = false, val location: String = "",
                     val calendar: String = "")

    /** Insert into the chosen writable calendar; returns that calendar's display name. */
    fun add(e: Event): String

    /** Event instances overlapping [fromMs, toMs), sorted by start. */
    fun list(fromMs: Long, toMs: Long): List<Event>
}

/**
 * The tools Claude can call while answering a question from the glasses. Everything runs on the phone:
 * calendar via [CalendarStore], ideas via [IdeaStore], stock quotes from Yahoo / Stooq (no key).
 */
class PhoneTools(
    private val calendar: CalendarStore?,
    private val ideas: IdeaStore,
    private val http: HttpPost,
    private val zone: () -> ZoneId = { ZoneId.systemDefault() },
    private val now: () -> Long = System::currentTimeMillis,
    /** Called after an idea is stored (the hub pushes the quick list when "Ideas on dashboard" is on). */
    private val onIdea: suspend (IdeaStore.Idea) -> String? = { null },
) {
    companion object {
        const val DEFAULT_MINUTES = 60L
        const val MAX_LIST = 15

        private fun obj(props: Map<String, Any?>, required: List<String>) =
            mapOf("type" to "object", "properties" to props, "required" to required)
        private fun s(desc: String) = mapOf("type" to "string", "description" to desc)

        /** ISO 8601 date-time (with or without offset; a space instead of T is accepted) or a date -> (instant ms, dateOnly). */
        fun parseTime(s: String, zone: ZoneId): Pair<Long, Boolean> {
            val t = s.trim().replace(' ', 'T')
            try { return OffsetDateTime.parse(t).toInstant().toEpochMilli() to false } catch (_: Exception) { }
            try { return ZonedDateTime.parse(t).toInstant().toEpochMilli() to false } catch (_: Exception) { }
            try { return LocalDateTime.parse(t).atZone(zone).toInstant().toEpochMilli() to false } catch (_: Exception) { }
            try { return LocalDate.parse(t).atStartOfDay(zone).toInstant().toEpochMilli() to true } catch (_: Exception) { }
            throw IllegalArgumentException("cannot read the time '$s'; use ISO 8601 like 2026-10-25T15:00")
        }

        fun fmt(ms: Long, zone: ZoneId, allDay: Boolean = false): String {
            val z = Instant.ofEpochMilli(ms).atZone(if (allDay) ZoneId.of("UTC") else zone)
            return z.format(DateTimeFormatter.ofPattern(if (allDay) "EEE MMM d" else "EEE MMM d HH:mm", Locale.US))
        }
    }

    fun tools(): List<AgentTool> = listOf(
        AgentTool(
            "add_calendar_event",
            "Add an event to the user's phone calendar. Give start as ISO 8601 local date-time (or a date for an all-day " +
                "event) and either end or duration_minutes (default 60).",
            obj(mapOf(
                "title" to s("Event title"),
                "start" to s("Start, ISO 8601, e.g. 2026-10-25T15:00:00-07:00"),
                "end" to s("End, ISO 8601 (optional)"),
                "duration_minutes" to mapOf("type" to "integer", "description" to "Length in minutes when no end is given"),
                "location" to s("Location (optional)"),
            ), listOf("title", "start")),
        ) { addEvent(it) },
        AgentTool(
            "list_calendar_events",
            "List the user's calendar events between start and end (ISO 8601). Defaults: from now until 24 hours later.",
            obj(mapOf("start" to s("Range start, ISO 8601 (optional)"), "end" to s("Range end, ISO 8601 (optional)")), emptyList()),
        ) { listEvents(it) },
        AgentTool(
            "save_idea",
            "Save an idea, note or thought the user wants to remember. Store the user's words, lightly cleaned up.",
            obj(mapOf("text" to s("The idea")), listOf("text")),
        ) { saveIdea(it) },
        AgentTool(
            "get_stock_quote",
            "Get the latest price for a stock ticker symbol (e.g. AAPL, MSFT). Prices may be delayed.",
            obj(mapOf("symbol" to s("Ticker symbol")), listOf("symbol")),
        ) { Stocks.fetch(http, (it["symbol"] as? String) ?: throw IllegalArgumentException("symbol missing")).describe() },
    )

    suspend fun addEvent(input: Map<String, Any?>): String {
        val cal = calendar ?: throw IllegalStateException("calendar permission not granted on the phone")
        val title = (input["title"] as? String)?.trim().orEmpty().ifEmpty { throw IllegalArgumentException("title missing") }
        val z = zone()
        val (start, dateOnly) = parseTime(input["start"] as? String ?: throw IllegalArgumentException("start missing"), z)
        val endStr = (input["end"] as? String)?.takeIf { it.isNotBlank() }
        val minutes = (input["duration_minutes"] as? Number)?.toLong()
        val e = if (dateOnly) {
            // CalendarContract all-day events are UTC midnight to UTC midnight
            val day = Instant.ofEpochMilli(start).atZone(z).toLocalDate()
            val startUtc = day.atStartOfDay(ZoneId.of("UTC")).toInstant().toEpochMilli()
            val days = endStr?.let { maxOf(1L, java.time.temporal.ChronoUnit.DAYS.between(day, LocalDate.parse(it.trim().take(10)))) } ?: 1L
            CalendarStore.Event(title, startUtc, startUtc + days * 86_400_000L, true, (input["location"] as? String).orEmpty())
        } else {
            val end = endStr?.let { parseTime(it, z).first } ?: (start + (minutes ?: DEFAULT_MINUTES) * 60_000L)
            require(end > start) { "end is before start" }
            CalendarStore.Event(title, start, end, false, (input["location"] as? String).orEmpty())
        }
        val name = cal.add(e)
        return "added \"${e.title}\" ${fmt(e.startMs, z, e.allDay)}" + (if (e.allDay) " (all day)" else "-" +
            Instant.ofEpochMilli(e.endMs).atZone(z).format(DateTimeFormatter.ofPattern("HH:mm", Locale.US))) +
            (if (e.location.isNotEmpty()) " at ${e.location}" else "") + " to calendar $name"
    }

    suspend fun listEvents(input: Map<String, Any?>): String {
        val cal = calendar ?: throw IllegalStateException("calendar permission not granted on the phone")
        val z = zone()
        val from = (input["start"] as? String)?.takeIf { it.isNotBlank() }?.let { parseTime(it, z).first } ?: now()
        val to = (input["end"] as? String)?.takeIf { it.isNotBlank() }?.let {
            val (ms, dateOnly) = parseTime(it, z); if (dateOnly) ms + 86_400_000L else ms
        } ?: (from + 86_400_000L)
        val events = cal.list(from, to)
        if (events.isEmpty()) return "no events between ${fmt(from, z)} and ${fmt(to, z)}"
        val lines = events.take(MAX_LIST).map { e ->
            (if (e.allDay) fmt(e.startMs, z, true) + " all day" else fmt(e.startMs, z) + "-" +
                Instant.ofEpochMilli(e.endMs).atZone(z).format(DateTimeFormatter.ofPattern("HH:mm", Locale.US))) +
                ": " + e.title + (if (e.location.isNotEmpty()) " @ " + e.location else "")
        }
        return lines.joinToString("\n") + if (events.size > MAX_LIST) "\n(+${events.size - MAX_LIST} more)" else ""
    }

    suspend fun saveIdea(input: Map<String, Any?>): String {
        val text = (input["text"] as? String)?.trim().orEmpty()
        require(text.isNotEmpty()) { "text missing" }
        val idea = ideas.add(text)
        val pushed = try { onIdea(idea) } catch (e: Exception) { "dashboard push failed: ${e.message}" }
        return "saved idea #${ideas.all().size}" + (pushed?.let { "; $it" } ?: "")
    }
}
