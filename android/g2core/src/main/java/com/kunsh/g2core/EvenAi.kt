package com.kunsh.g2core

import com.kunsh.g2core.Pb.messageField
import com.kunsh.g2core.Pb.varintField

/**
 * Even AI (SID 0x07) and the 0x6402 microphone packet layout (port of g2/native/even_ai.py).
 *
 * Session (LIVE in the hub): glasses CTRL WAKE_UP -> app ENTER -> HEARTBEAT every 2 s -> LC3 audio on 0x6402 ->
 * ASK -> ANALYSE -> REPLY chunks (<=512 B, 150 ms apart, 6:1 on the last) -> EXIT.
 * Community minimal path: ENTER -> 0.3 s -> ASK -> 1 s -> REPLY with explicit zero fields 1/2/3, no heartbeat.
 */
object EvenAi {
    const val SID_EVEN_AI = 0x07

    const val CMD_CTRL = 1
    const val CMD_VAD_INFO = 2
    const val CMD_ASK = 3
    const val CMD_ANALYSE = 4
    const val CMD_REPLY = 5
    const val CMD_SKILL = 6
    const val CMD_PROMPT = 7
    const val CMD_EVENT = 8
    const val CMD_HEARTBEAT = 9
    const val CMD_CONFIG = 10
    const val CMD_COMM_RSP = 0xA1
    val CMD_NAMES = mapOf(1 to "CTRL", 2 to "VAD_INFO", 3 to "ASK", 4 to "ANALYSE", 5 to "REPLY", 6 to "SKILL", 7 to "PROMPT", 8 to "EVENT",
        9 to "HEARTBEAT", 10 to "CONFIG", 0xA1 to "COMM_RSP")
    val FIELD_FOR_CMD = mapOf(1 to 3, 2 to 4, 3 to 5, 4 to 6, 5 to 7, 6 to 8, 7 to 9, 8 to 10, 9 to 11, 0xA1 to 12, 10 to 13)

    const val CTRL_WAKE_UP = 1
    const val CTRL_ENTER = 2
    const val CTRL_EXIT = 3
    const val EVENT_SCROLL = 1
    const val EVENT_STREAM_COMPLETE = 2

    const val REPLY_MAX_BYTES = 512
    const val REPLY_PACE_MS = 150L
    const val HEARTBEAT_INTERVAL_MS = 2000L
    const val DEFAULT_STREAM_SPEED = 80

    fun pkg(cmd: Int, magic: Int, inner: ByteArray): ByteArray =
        varintField(1, cmd) + varintField(2, magic) + messageField(FIELD_FOR_CMD.getValue(cmd), inner)

    fun ctrl(magic: Int, status: Int): ByteArray = pkg(CMD_CTRL, magic, varintField(1, status))

    /** CTRL ENTER `{1:1, 2:m, 3:{1: 2}}` (LIVE + COMMUNITY). */
    fun enter(magic: Int): ByteArray = ctrl(magic, CTRL_ENTER)

    /** CTRL EXIT `{3: {1: 3}}`; stop the heartbeat afterwards. (Python name: exit_) */
    fun exit(magic: Int): ByteArray = ctrl(magic, CTRL_EXIT)

    /** `{1: 9, 2: m, 11: {1: count}}` every 2 s while the session is open (LIVE). */
    fun heartbeat(magic: Int, count: Int): ByteArray = pkg(CMD_HEARTBEAT, magic, varintField(1, count))

    fun config(magic: Int, voiceSwitch: Int = 0, streamSpeed: Int = DEFAULT_STREAM_SPEED): ByteArray =
        pkg(CMD_CONFIG, magic, varintField(1, voiceSwitch) + varintField(2, streamSpeed))

    /** The exact CONFIG of the captured app connect sequence `{13: {1: 0, 2: 80, 4: 0}}`. */
    fun configCaptured(magic: Int): ByteArray = pkg(CMD_CONFIG, magic, varintField(1, 0) + varintField(2, 80) + varintField(4, 0))

    private fun textInfo(text: ByteArray, final: Boolean = false, community: Boolean = false): ByteArray {
        val pre = if (community) varintField(1, 0) + varintField(2, 0) + varintField(3, 0) else ByteArray(0)
        val inner = pre + messageField(4, text)
        return if (final) inner + varintField(6, 1) else inner
    }

    /** ASK `{1: 3, 2: m, 5: {4: bytes text}}` (LIVE); community adds explicit zero fields 1/2/3. */
    fun ask(magic: Int, text: String, community: Boolean = false): ByteArray =
        pkg(CMD_ASK, magic, textInfo(text.toByteArray(Charsets.UTF_8), community = community))

    /** ANALYSE `{1: 4, 2: m, 6: {}}` - the "thinking" screen (LIVE). */
    fun analyse(magic: Int): ByteArray = pkg(CMD_ANALYSE, magic, ByteArray(0))

    /** One REPLY chunk `{1: 5, 2: m, 7: {4: chunk[, 6: 1]}}`, chunk <= 512 UTF-8 bytes. */
    fun reply(magic: Int, chunk: ByteArray, final: Boolean, community: Boolean = false): ByteArray {
        require(chunk.size <= REPLY_MAX_BYTES) { "reply chunk is ${chunk.size} bytes; limit $REPLY_MAX_BYTES" }
        return pkg(CMD_REPLY, magic, textInfo(chunk, final = final && !community, community = community))
    }

    fun reply(magic: Int, chunk: String, final: Boolean, community: Boolean = false): ByteArray =
        reply(magic, chunk.toByteArray(Charsets.UTF_8), final, community)

    /** Split into <=limit-byte UTF-8 chunks without cutting a character; always >= 1 chunk. */
    fun replyChunks(text: String, limit: Int = REPLY_MAX_BYTES): List<ByteArray> {
        val lim = maxOf(1, minOf(limit, REPLY_MAX_BYTES))
        val chunks = ArrayList<ByteArray>()
        val cur = java.io.ByteArrayOutputStream()
        var i = 0
        while (i < text.length) {
            val cp = text.codePointAt(i)
            val b = String(Character.toChars(cp)).toByteArray(Charsets.UTF_8)
            i += Character.charCount(cp)
            if (cur.size() > 0 && cur.size() + b.size > lim) {
                chunks.add(cur.toByteArray())
                cur.reset()
            }
            cur.write(b)
        }
        chunks.add(cur.toByteArray())
        return chunks
    }

    /** All REPLY payloads for [text]; the last carries the end flag. */
    fun replySequence(magicFn: () -> Int, text: String): List<ByteArray> {
        val chunks = replyChunks(text)
        return chunks.mapIndexed { i, c -> reply(magicFn(), c, final = i == chunks.size - 1) }
    }

    fun vadInfo(magic: Int, status: Int): ByteArray = pkg(CMD_VAD_INFO, magic, varintField(1, status))
    fun prompt(magic: Int, code: Int): ByteArray = pkg(CMD_PROMPT, magic, varintField(1, code))
    fun event(magic: Int, eventId: Int): ByteArray = pkg(CMD_EVENT, magic, varintField(1, eventId))

    data class EvenAIMessage(val cmd: Int, val magic: Int, var value: Int? = null, var error: Int? = null, var text: ByteArray? = null) {
        val name: String get() = CMD_NAMES[cmd] ?: "cmd=$cmd"
        val isWakeUp: Boolean get() = cmd == CMD_CTRL && value == CTRL_WAKE_UP
        val isExit: Boolean get() = cmd == CMD_CTRL && value == CTRL_EXIT
        val isStreamComplete: Boolean get() = cmd == CMD_EVENT && value == EVENT_STREAM_COMPLETE
    }

    fun parse(payload: ByteArray): EvenAIMessage {
        val m = EvenAIMessage(Pb.readInt(payload, 1), Pb.readInt(payload, 2))
        val fld = FIELD_FOR_CMD[m.cmd]
        val sub = if (fld != null) Pb.readBytes(payload, fld) else null
        if (sub != null) {
            if (m.cmd == CMD_COMM_RSP) m.error = Pb.readInt(sub, 1, 0)
            else { m.value = Pb.readInt(sub, 1, 0); m.error = Pb.readInt(sub, 2, 0) }
            if (m.cmd == CMD_ASK || m.cmd == CMD_REPLY) m.text = Pb.readBytes(sub, 4)
        }
        return m
    }

    // ── microphone packets ─────────────────────────────────────────────────────────────

    const val MIC_SAMPLE_RATE = 16000
    const val MIC_FRAME_BYTES = 40
    const val MIC_FRAMES_PER_PACKET = 5
    const val MIC_AUDIO_BYTES = MIC_FRAME_BYTES * MIC_FRAMES_PER_PACKET
    const val MIC_PACKET_BYTES = MIC_AUDIO_BYTES + 5
    const val MIC_SAMPLES_PER_FRAME = 160

    /** Five 40-byte LC3 frames (10 ms, 16 kHz mono) plus the optional trailer. */
    class MicPacket(val frames: List<ByteArray>, val signalRatio: Int?, val direction: Int?, val counter: Int?) {
        val lc3: ByteArray get() = frames.fold(ByteArray(0)) { a, b -> a + b }
    }

    /** Raw notify on 0x6402: 0..199 five LC3 frames, 200..201 int16 LE ratio, 202..203 int16 LE direction, 204 counter. */
    fun parseMicPacket(data: ByteArray): MicPacket? {
        if (data.size < MIC_AUDIO_BYTES) return null
        val frames = (0 until MIC_FRAMES_PER_PACKET).map { data.copyOfRange(it * MIC_FRAME_BYTES, (it + 1) * MIC_FRAME_BYTES) }
        if (data.size < MIC_PACKET_BYTES) return MicPacket(frames, null, null, null)
        fun i16(o: Int): Int = ((data[o].toInt() and 0xFF) or (data[o + 1].toInt() shl 8)).toShort().toInt()
        return MicPacket(frames, i16(MIC_AUDIO_BYTES), i16(MIC_AUDIO_BYTES + 2), data[MIC_AUDIO_BYTES + 4].toInt() and 0xFF)
    }

    /** Mic enable without an Even AI session: SID 0xE0 cmd 15 `{18: {1: on}}` (LIVE). Send on SID 0xE0. */
    fun evenhubAudioControl(magic: Int, enable: Boolean): ByteArray =
        varintField(1, 15) + varintField(2, magic) + messageField(18, varintField(1, if (enable) 1 else 0))
}
