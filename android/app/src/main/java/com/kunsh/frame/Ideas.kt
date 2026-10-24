package com.kunsh.frame

import java.io.File

/**
 * Ideas saved by voice ("save an idea: ..."), one JSON object per line in an app-private file
 * (`files/ideas.jsonl`). Never leaves the phone unless "Ideas on dashboard" is on, and then only to the glasses.
 */
class IdeaStore(private val file: File, private val clock: () -> Long = System::currentTimeMillis) {
    data class Idea(val id: Long, val timeMs: Long, val text: String)

    companion object {
        const val MAX_CHARS = 1000
    }

    @Synchronized
    fun add(text: String): Idea {
        val t = text.trim().replace(Regex("\\s+"), " ").take(MAX_CHARS)
        require(t.isNotEmpty()) { "empty idea" }
        val now = clock()
        val last = all().maxOfOrNull { it.id } ?: 0L
        val idea = Idea(maxOf(now, last + 1), now, t)
        file.parentFile?.mkdirs()
        file.appendText(Json.write(mapOf("id" to idea.id, "time" to idea.timeMs, "text" to idea.text)) + "\n", Charsets.UTF_8)
        return idea
    }

    /** Oldest first; corrupt lines are skipped. */
    @Synchronized
    fun all(): List<Idea> {
        if (!file.exists()) return emptyList()
        return file.readLines(Charsets.UTF_8).mapNotNull { line ->
            if (line.isBlank()) return@mapNotNull null
            try {
                val o = Json.obj(line)
                val text = o.str("text") ?: return@mapNotNull null
                Idea(o.num("id")?.toLong() ?: return@mapNotNull null, o.num("time")?.toLong() ?: 0L, text)
            } catch (_: Exception) { null }
        }
    }

    /** Newest first. */
    fun latest(n: Int): List<Idea> = all().asReversed().take(n)

    @Synchronized
    fun clear() { file.delete() }
}
