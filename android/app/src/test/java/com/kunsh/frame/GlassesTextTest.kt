package com.kunsh.frame

import com.kunsh.g2core.EvenAi
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class GlassesTextTest {
    @Test
    fun shortAnswerIsOneChunk() {
        val c = GlassesText.chunks("The answer is 4.")
        assertEquals(1, c.size)
        assertArrayEquals("The answer is 4.".toByteArray(), c[0])
    }

    @Test
    fun longAnswerSplitsAt512BytesWithoutBreakingCharacters() {
        val text = "Grüße aus München! ".repeat(60) + "日本語のテキスト。".repeat(40)
        val chunks = GlassesText.chunks(text)
        assertTrue(chunks.size > 1)
        chunks.forEach { assertTrue("chunk ${it.size} B", it.size in 1..512) }
        // every chunk is valid UTF-8 on its own and they join back to the original
        val joined = chunks.joinToString("") { String(it, Charsets.UTF_8) }
        assertEquals(text, joined)
        chunks.forEach { val s = String(it, Charsets.UTF_8); assertArrayEquals(it, s.toByteArray(Charsets.UTF_8)) }
    }

    @Test
    fun replyPayloadsCarryEndFlagOnlyOnLast() {
        val text = "x".repeat(1300)
        val chunks = GlassesText.chunks(text)
        assertEquals(3, chunks.size)
        val payloads = chunks.mapIndexed { i, c -> EvenAi.reply(100 + i, c, final = i == chunks.size - 1) }
        val end = byteArrayOf(0x30, 0x01)                      // field 6 varint 1
        fun hasEnd(p: ByteArray) = p.size >= 2 && p[p.size - 2] == end[0] && p[p.size - 1] == end[1]
        assertEquals(listOf(false, false, true), payloads.map { hasEnd(it) })
    }

    @Test
    fun readingTime() {
        assertEquals(8_000L, GlassesText.readingTimeMs(""))
        assertEquals(8_000L + 60L * 100, GlassesText.readingTimeMs("a".repeat(100)))
        assertEquals(90_000L, GlassesText.readingTimeMs("a".repeat(5000)))
    }

    @Test
    fun cleanStripsMarkdown() {
        assertEquals("Title\nOne\nTwo bold and code", GlassesText.clean("## Title\n- One\n* Two **bold** and `code`"))
        assertEquals("a b", GlassesText.clean("  a    b  "))
    }
}
