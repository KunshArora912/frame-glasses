package com.kunsh.frame

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.time.ZoneId
import java.time.ZonedDateTime

class IdeaStoreTest {
    private fun file() = File(Files.createTempDirectory("ideas").toFile(), "sub/ideas.jsonl")

    @Test
    fun addListLatestAndPersist() {
        val f = file()
        val s = IdeaStore(f, clock = { 1_000L })
        s.add("  first   idea ")
        s.add("second \"quoted\" idea\nwith newline")
        s.add("dritte Idee – ümlauts")
        val all = IdeaStore(f).all()                         // a fresh instance reads the same file
        assertEquals(listOf("first idea", "second \"quoted\" idea with newline", "dritte Idee – ümlauts"), all.map { it.text })
        assertEquals(3, all.map { it.id }.toSet().size)       // unique ids even with a frozen clock
        assertEquals(listOf("dritte Idee – ümlauts", "second \"quoted\" idea with newline"), s.latest(2).map { it.text })
        assertEquals(3, f.readLines().size)
    }

    @Test
    fun corruptLinesAreSkipped() {
        val f = file()
        val s = IdeaStore(f)
        s.add("good")
        f.appendText("{not json\n\n")
        s.add("also good")
        assertEquals(listOf("good", "also good"), s.all().map { it.text })
    }

    @Test
    fun emptyIdeaRejectedAndLongTrimmed() {
        val s = IdeaStore(file())
        try { s.add("   "); fail() } catch (_: IllegalArgumentException) { }
        val i = s.add("x".repeat(5000))
        assertEquals(IdeaStore.MAX_CHARS, i.text.length)
        s.clear()
        assertTrue(s.all().isEmpty())
    }

    @Test
    fun calendarToolsParseTimesAndList() {
        val z = ZoneId.of("Europe/Berlin")
        val (ms, dateOnly) = PhoneTools.parseTime("2026-10-25 15:00", z)
        assertEquals(ZonedDateTime.of(2026, 10, 25, 15, 0, 0, 0, z).toInstant().toEpochMilli(), ms)
        assertEquals(false, dateOnly)
        assertEquals(true, PhoneTools.parseTime("2026-10-25", z).second)
        assertEquals(ZonedDateTime.of(2026, 10, 25, 14, 0, 0, 0, ZoneId.of("UTC")).toInstant().toEpochMilli(),
            PhoneTools.parseTime("2026-10-25T14:00:00Z", z).first)
    }
}
