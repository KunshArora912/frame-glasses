package com.kunsh.g2core

import com.kunsh.g2core.Pb.bytesField
import com.kunsh.g2core.Pb.messageField
import com.kunsh.g2core.Pb.varintField
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import java.util.TimeZone

/**
 * Phone-style notification cards: SID 0x04 control (LIVE) and the JSON card over the EvenFile service (0xC4 cmd /
 * 0xC5 data on characteristic 0x7401, replies on 0x7402 and 0x5402). Port of g2/native/notifications.py.
 *
 * Hub form (DECODED/UNTESTED): START -> per 4096-byte block a DATA prefix + 232-byte 0xC5 packets -> RESULT_CHECK, each
 * awaited. Community form (COMMUNITY): the same control frames, JSON in ONE 0xC5 frame (<=234 B), fire and forget.
 *
 * CRC: the EvenFile checksum is CRC-32 poly 0x1EDC6F41, MSB-first, init 0, no final XOR (check 0xC052A8C8). That is NOT
 * the standard CRC-32C (reflected, check 0xE3069283); both exist here, only [crc32Efs] goes on the wire.
 */
object Notifications {
    const val SID_NOTIFICATION = 0x04
    const val SID_FILE_CMD = 0xC4
    const val SID_FILE_DATA = 0xC5

    const val CMD_CTRL = 1
    const val CMD_WHITELIST_CTRL = 3
    const val F_CTRL = 3
    const val F_IOS = 4
    const val F_RESP = 5
    const val F_WHITELIST_CTRL = 6
    const val F_WHITELIST_CHK = 7

    const val CID_START = 0
    const val CID_DATA = 1
    const val CID_RESULT_CHECK = 2
    val RSP_NAMES = mapOf(0 to "SUCCESS", 2 to "DATA_CRC_ERR", 4 to "TIMEOUT", 5 to "NO_RESOURCES", 6 to "RESULT_CHECK_FAIL")
    val RSP_RETRY = setOf(2, 4, 5)
    const val FILE_TYPE_WHITELIST = 0
    const val FILE_TYPE_NOTIFICATION = 1
    val FILE_PATH: ByteArray = "user/notify_whitelist.json".toByteArray(Charsets.US_ASCII)
    const val BLOCK_SIZE = 4096
    const val PACKET_DATA = 232
    const val COMMUNITY_MAX_JSON = 234

    const val JSON_APP_ID_MAX = 63
    const val JSON_TITLE_MAX = 63
    const val JSON_SUBTITLE_MAX = 63
    const val JSON_MESSAGE_MAX = 511
    const val JSON_DISPLAY_NAME_MAX = 31
    const val APP_ID_MAX = 32
    const val DISPLAY_NAME_MAX = 32

    // ── checksums ──────────────────────────────────────────────────────────────────────

    private val EFS = IntArray(256) { i ->
        var c = i shl 24
        repeat(8) { c = if (c and 0x80000000.toInt() != 0) (c shl 1) xor 0x1EDC6F41 else c shl 1 }
        c
    }

    /** EvenFile CRC-32: poly 0x1EDC6F41, MSB-first, init 0, no XOR. crc32Efs("123456789") == 0xC052A8C8. */
    fun crc32Efs(data: ByteArray): Long {
        var crc = 0
        for (b in data) crc = (crc shl 8) xor EFS[((crc ushr 24) xor (b.toInt() and 0xFF)) and 0xFF]
        return crc.toLong() and 0xFFFFFFFFL
    }

    private val C = IntArray(256) { i ->
        var c = i
        repeat(8) { c = if (c and 1 != 0) (c ushr 1) xor 0x82F63B78.toInt() else c ushr 1 }
        c
    }

    /** Standard CRC-32C (reference/tests only). crc32c("123456789") == 0xE3069283. */
    fun crc32c(data: ByteArray): Long {
        var crc = -1
        for (b in data) crc = (crc ushr 8) xor C[(crc xor (b.toInt() and 0xFF)) and 0xFF]
        return (crc xor -1).toLong() and 0xFFFFFFFFL
    }

    // ── SID 0x04 control (LIVE) ────────────────────────────────────────────────────────

    /** `{1: 1, 2: m, 3: {1 enable, 2 autoDisplay, 3 dispTime[, 5 dnd]}}`. Send on every connect. */
    fun notificationCtrl(magic: Int, enable: Boolean = true, autoDisplay: Boolean = true, dispTime: Int = 5, dnd: Boolean = false): ByteArray {
        val ctrl = Writer().varint(1, if (enable) 1 else 0).varint(2, if (autoDisplay) 1 else 0).varint(3, dispTime)
        if (dnd) ctrl.varint(5, 1)
        return varintField(1, CMD_CTRL) + varintField(2, magic) + messageField(F_CTRL, ctrl.toBytes())
    }

    /** `{1: 3, 2: m, 6: {1: 1}}`: bypass the glasses-side app whitelist (LIVE). */
    fun whitelistDisable(magic: Int, disable: Boolean = true): ByteArray =
        varintField(1, CMD_WHITELIST_CTRL) + varintField(2, magic) + messageField(F_WHITELIST_CTRL, varintField(1, if (disable) 1 else 0))

    /** iOS app register (DECODED). */
    fun iosRegister(magic: Int, appId: String, displayName: String, cmd: Int = CMD_CTRL): ByteArray {
        val inner = bytesField(1, Text.utf8Fit(appId, APP_ID_MAX).toByteArray(Charsets.UTF_8)) +
            bytesField(2, Text.utf8Fit(displayName, DISPLAY_NAME_MAX).toByteArray(Charsets.UTF_8))
        return varintField(1, cmd) + varintField(2, magic) + messageField(F_IOS, inner)
    }

    data class NotificationMessage(
        val cmd: Int, val magic: Int, var enable: Int? = null, var autoDisplay: Int? = null, var dispTime: Int? = null, var dnd: Int? = null,
        var appId: String? = null, var displayName: String? = null, var respCmd: Int? = null, var whitelistDisable: Int? = null,
        var whitelistCrc: Long? = null, var checkStatus: Int? = null, var error: Int = 0,
    )

    fun parseNotification(payload: ByteArray): NotificationMessage {
        val m = NotificationMessage(Pb.readInt(payload, 1), Pb.readInt(payload, 2))
        Pb.readBytes(payload, F_CTRL)?.let {
            m.enable = Pb.readInt(it, 1, 0); m.autoDisplay = Pb.readInt(it, 2, 0); m.dispTime = Pb.readInt(it, 3, 0); m.dnd = Pb.readInt(it, 5, 0)
            m.error = Pb.readInt(it, 4, 0)
        }
        Pb.readBytes(payload, F_IOS)?.let {
            m.appId = Pb.readString(it, 1); m.displayName = Pb.readString(it, 2)
            m.error = Pb.readInt(it, 3, 0).takeIf { e -> e != 0 } ?: m.error
        }
        Pb.readBytes(payload, F_RESP)?.let {
            m.respCmd = Pb.readInt(it, 1, 0); m.error = Pb.readInt(it, 2, 0).takeIf { e -> e != 0 } ?: m.error
        }
        Pb.readBytes(payload, F_WHITELIST_CTRL)?.let {
            m.whitelistDisable = Pb.readInt(it, 1, 0); m.error = Pb.readInt(it, 2, 0).takeIf { e -> e != 0 } ?: m.error
        }
        Pb.readBytes(payload, F_WHITELIST_CHK)?.let {
            m.whitelistCrc = Pb.readVarint(it, 1, 0); m.checkStatus = Pb.readInt(it, 2, 0)
            m.error = Pb.readInt(it, 3, 0).takeIf { e -> e != 0 } ?: m.error
        }
        return m
    }

    // ── the JSON card ──────────────────────────────────────────────────────────────────

    /** Python json.dumps(s, ensure_ascii=False). */
    fun jsonStr(s: String): String {
        val sb = StringBuilder(s.length + 2).append('"')
        for (ch in s) {
            when (ch) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                '\b' -> sb.append("\\b")
                '\u000C' -> sb.append("\\f")
                else -> if (ch.code < 0x20) sb.append(String.format(java.util.Locale.ROOT, "\\u%04x", ch.code)) else sb.append(ch)
            }
        }
        return sb.append('"').toString()
    }

    private val DATE_FMT: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmss")

    /**
     * Compact UTF-8 JSON in the app's key order:
     * `{"android_notification":{"msg_id","action":0,"app_identifier","title","subtitle","message","time_s","date","display_name"}}`.
     * [now] is unix seconds (default: current time); [utcOffsetS] defaults to this device's zone at [now]. Same checksum =>
     * cached, not re-displayed: vary msgId/text.
     */
    fun notificationJson(
        title: String, message: String, subtitle: String = "", displayName: String = "Even Realities",
        appIdentifier: String = "com.even.sg", msgId: Long? = null, now: Long? = null, utcOffsetS: Int? = null,
    ): ByteArray {
        val t = now ?: (System.currentTimeMillis() / 1000)
        val ti = Text.utf8Fit(title, JSON_TITLE_MAX)
        val su = Text.utf8Fit(subtitle, JSON_SUBTITLE_MAX)
        val me = Text.utf8Fit(message, JSON_MESSAGE_MAX)
        val dn = Text.utf8Fit(displayName, JSON_DISPLAY_NAME_MAX)
        val ai = Text.utf8Fit(appIdentifier, JSON_APP_ID_MAX)
        val off = utcOffsetS ?: (TimeZone.getDefault().getOffset(t * 1000) / 1000)
        val date = DATE_FMT.format(Instant.ofEpochSecond(t + off).atOffset(ZoneOffset.UTC))
        val mid = msgId ?: (t and 0x7FFFFFFF)
        val body = "{\"android_notification\":{\"msg_id\":$mid,\"action\":0,\"app_identifier\":${jsonStr(ai)},\"title\":${jsonStr(ti)}," +
            "\"subtitle\":${jsonStr(su)},\"message\":${jsonStr(me)},\"time_s\":$t,\"date\":\"$date\",\"display_name\":${jsonStr(dn)}}}"
        return body.toByteArray(Charsets.UTF_8)
    }

    // ── EvenFile frames, hub form ──────────────────────────────────────────────────────

    private fun header(seq: Int, length: Int, total: Int, num: Int, sid: Int): ByteArray =
        byteArrayOf(0xAA.toByte(), Framing.DIR_TX.toByte(), seq.toByte(), length.toByte(), total.toByte(), num.toByte(), sid.toByte(), Framing.FLAG_PLAIN.toByte())

    fun efsSingle(seq: Int, sid: Int, payload: ByteArray): ByteArray {
        val body = payload + Framing.crc16Bytes(payload)
        return header(seq, body.size, 1, 1, sid) + body
    }

    /** 93 bytes: `00` + u32 LE fileType + u32 LE length + u32 LE crc32Efs + 80-byte zero-padded path. */
    fun efsStartHeader(data: ByteArray, fileType: Int = FILE_TYPE_NOTIFICATION, path: ByteArray = FILE_PATH): ByteArray {
        val p = ByteArray(80)
        path.copyInto(p, 0, 0, minOf(80, path.size))
        return byteArrayOf(CID_START.toByte()) + Pb.le32(fileType) + Pb.le32(data.size) + Pb.le32(crc32Efs(data).toInt()) + p
    }

    fun efsStart(seq: Int, data: ByteArray, fileType: Int = FILE_TYPE_NOTIFICATION): ByteArray = efsSingle(seq, SID_FILE_CMD, efsStartHeader(data, fileType))
    fun efsDataPrefix(seq: Int): ByteArray = efsSingle(seq, SID_FILE_CMD, byteArrayOf(CID_DATA.toByte()))
    fun efsResultCheck(seq: Int): ByteArray = efsSingle(seq, SID_FILE_CMD, byteArrayOf(CID_RESULT_CHECK.toByte()))

    /** The block as 0xC5 frames of 232 bytes; CRC16 of the whole block only on the last; extra CRC-only packet when len%232 is 0 or 231. */
    fun efsDataPackets(seq: Int, block: ByteArray): List<ByteArray> {
        val crc = Framing.crc16Bytes(block)
        val chunks = ArrayList<ByteArray>()
        var i = 0
        while (i < block.size) { chunks.add(block.copyOfRange(i, minOf(i + PACKET_DATA, block.size))); i += PACKET_DATA }
        if (chunks.isEmpty()) chunks.add(ByteArray(0))
        val r = block.size % PACKET_DATA
        if (block.isNotEmpty() && (r == 0 || r == PACKET_DATA - 1)) chunks.add(ByteArray(0))
        return chunks.mapIndexed { idx, chunk ->
            val n = idx + 1
            val body = if (n == chunks.size) chunk + crc else chunk
            header(seq, body.size, chunks.size, n, SID_FILE_DATA) + body
        }
    }

    data class EfsStep(val name: String, val frames: List<ByteArray>, val expectCid: Int, val offset: Int = 0)

    /** The hub transfer as awaitable steps: START -> DATA per block -> RESULT_CHECK. DECODED/UNTESTED. */
    fun hubNotificationSteps(seqFn: () -> Int, data: ByteArray, fileType: Int = FILE_TYPE_NOTIFICATION): List<EfsStep> {
        val steps = ArrayList<EfsStep>()
        steps.add(EfsStep("start", listOf(efsStart(seqFn(), data, fileType)), CID_START))
        var off = 0
        while (off < maxOf(data.size, 1)) {
            val block = data.copyOfRange(minOf(off, data.size), minOf(off + BLOCK_SIZE, data.size))
            val s = seqFn()
            steps.add(EfsStep("data", listOf(efsDataPrefix(s)) + efsDataPackets(s, block), CID_DATA, off))
            off += BLOCK_SIZE
        }
        steps.add(EfsStep("result_check", listOf(efsResultCheck(seqFn())), CID_RESULT_CHECK))
        return steps
    }

    /** Replies are raw `[cid, rsp]`; null when too short. */
    fun parseFileReply(payload: ByteArray): Pair<Int, Int>? =
        if (payload.size < 2) null else (payload[0].toInt() and 0xFF) to (payload[1].toInt() and 0xFF)

    // ── EvenFile frames, community form ────────────────────────────────────────────────

    fun communityPacket(seq: Int, sid: Int, payload: ByteArray, total: Int = 1, num: Int = 1): ByteArray =
        header(seq, payload.size + 2, total, num, sid) + payload + Framing.crc16Bytes(payload)

    fun communityFileCheck(data: ByteArray, seq: Int = 0x10): ByteArray {
        val crc = crc32Efs(data)
        val name = ByteArray(80)
        FILE_PATH.copyInto(name)
        val body = Pb.le32(0x100) + Pb.le32((data.size.toLong() * 256 and 0xFFFFFFFFL).toInt()) + Pb.le32(((crc shl 8) and 0xFFFFFFFFL).toInt()) +
            byteArrayOf(((crc shr 24) and 0xFF).toByte()) + name
        return communityPacket(seq, SID_FILE_CMD, body)
    }

    fun communityStart(seq: Int = 0x49): ByteArray = communityPacket(seq, SID_FILE_CMD, byteArrayOf(0x01))

    fun communityData(data: ByteArray, seq: Int = 0x49): List<ByteArray> {
        val chunks = ArrayList<ByteArray>()
        var i = 0
        while (i < data.size) { chunks.add(data.copyOfRange(i, minOf(i + COMMUNITY_MAX_JSON, data.size))); i += COMMUNITY_MAX_JSON }
        if (chunks.isEmpty()) chunks.add(ByteArray(0))
        return chunks.mapIndexed { idx, c -> communityPacket(seq, SID_FILE_DATA, c, chunks.size, idx + 1) }
    }

    fun communityEnd(seq: Int = 0xDA): ByteArray = communityPacket(seq, SID_FILE_CMD, byteArrayOf(0x02))

    /** FILE_CHECK, START, DATA..., END; [strict] rejects JSON over the 234-byte single-frame limit. */
    fun communityNotificationFrames(data: ByteArray, strict: Boolean = true): List<ByteArray> {
        require(!strict || data.size <= COMMUNITY_MAX_JSON) { "community form needs the JSON in one packet (<= $COMMUNITY_MAX_JSON B), got ${data.size}" }
        return listOf(communityFileCheck(data), communityStart()) + communityData(data) + listOf(communityEnd())
    }
}
