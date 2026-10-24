package com.kunsh.g2core

import com.kunsh.g2core.Pb.bytesField
import com.kunsh.g2core.Pb.encodeVarint
import com.kunsh.g2core.Pb.messageField
import com.kunsh.g2core.Pb.stringField
import com.kunsh.g2core.Pb.varintField
import java.util.Locale
import java.util.regex.Pattern

/**
 * Full-screen native apps: Teleprompter (SID 0x06), Translate (SID 0x05), Navigation (SID 0x08); port of
 * g2/native/apps.py. Teleprompter hub flow DECODED / community flow COMMUNITY; Translate DECODED; Navigation DECODED.
 */
object Apps {
    // ═══ Teleprompter (SID 0x06) ═══════════════════════════════════════════════════════
    const val SID_TELEPROMPTER = 0x06
    const val TP_CMD_CONTROL = 1
    const val TP_CMD_FILE_LIST = 2
    const val TP_CMD_PAGE_DATA = 3
    const val TP_CMD_STATUS_NOTIFY = 161
    const val TP_CMD_FILE_LIST_REQUEST = 162
    const val TP_CMD_FILE_SELECT = 163
    const val TP_CMD_PAGE_DATA_REQUEST = 164
    const val TP_CMD_PAGE_SCROLL_SYNC = 165
    const val TP_CMD_COMM_RESP = 166
    const val TP_CMD_PAGE_LOAD_DONE = 167
    const val TP_CMD_MODE_SWITCH_REQ = 168
    const val TP_CMD_MODE_SWITCH_RSP = 169
    const val TP_CMD_END_SESSION_REQUEST = 170
    const val TP_CMD_HEARTBEAT = 255
    val TP_FIELD = mapOf(1 to 3, 2 to 4, 3 to 5, 4 to 6, 161 to 7, 162 to 8, 163 to 9, 164 to 10, 165 to 11, 166 to 12, 255 to 13, 167 to 14,
        168 to 15, 169 to 16, 170 to 17)
    const val TP_CTRL_START = 1
    const val TP_CTRL_PAUSE = 2
    const val TP_CTRL_RESUME = 3
    const val TP_CTRL_CLOSE = 4
    const val TP_MODE_AI = 0
    const val TP_MODE_MANUAL = 1
    const val TP_MODE_AUTO = 2
    const val TP_DISPLAY_WIDTH = 567
    const val TP_DISPLAY_LINES = 9
    const val TP_LINES_PER_PAGE = 10
    const val TP_CHARS_PER_LINE = 28
    const val TP_PAGE_MAX_BYTES = 1024
    const val TP_FILENAME_MAX_CHARS = 30

    fun tpPackage(cmd: Int, magic: Int, inner: ByteArray): ByteArray =
        varintField(1, cmd) + varintField(2, magic) + messageField(TP_FIELD.getValue(cmd), inner)

    private fun nz(field: Int, value: Int): ByteArray = if (value != 0) varintField(field, value) else ByteArray(0)

    fun tpSettings(totalPages: Int, totalLines: Int, mode: Int = TP_MODE_MANUAL, startPage: Int = 0, startLine: Int = 0,
                   displayWidth: Int = TP_DISPLAY_WIDTH, scrollIntervalMs: Int = 0, countdownS: Int = 0, displayLines: Int = TP_DISPLAY_LINES): ByteArray =
        nz(1, mode) + nz(2, startPage) + nz(3, startLine) + nz(4, totalPages) + nz(5, totalLines) + nz(6, displayWidth) +
            nz(7, scrollIntervalMs) + nz(8, countdownS) + nz(9, displayLines)

    fun tpStart(magic: Int, totalPages: Int, totalLines: Int, mode: Int = TP_MODE_MANUAL, startPage: Int = 0, startLine: Int = 0,
                scrollIntervalMs: Int = 0, countdownS: Int = 0): ByteArray {
        val inner = varintField(1, TP_CTRL_START) + messageField(2, tpSettings(totalPages, totalLines, mode, startPage, startLine,
            scrollIntervalMs = scrollIntervalMs, countdownS = countdownS))
        return tpPackage(TP_CMD_CONTROL, magic, inner)
    }

    fun tpCtrl(magic: Int, ctrl: Int): ByteArray = tpPackage(TP_CMD_CONTROL, magic, varintField(1, ctrl))
    fun tpPause(magic: Int): ByteArray = tpCtrl(magic, TP_CTRL_PAUSE)
    fun tpResume(magic: Int): ByteArray = tpCtrl(magic, TP_CTRL_RESUME)
    fun tpClose(magic: Int): ByteArray = tpCtrl(magic, TP_CTRL_CLOSE)

    fun tpPageText(lines: List<String>): ByteArray = (lines.joinToString("\n") + " \n").toByteArray(Charsets.UTF_8)

    fun tpPage(magic: Int, pageId: Int, lines: List<String>): ByteArray {
        val text = tpPageText(lines)
        require(text.size <= TP_PAGE_MAX_BYTES) { "teleprompter page text is ${text.size} bytes; firmware limit is $TP_PAGE_MAX_BYTES" }
        return tpPackage(TP_CMD_PAGE_DATA, magic, nz(1, pageId) + nz(2, lines.size) + bytesField(3, text))
    }

    fun tpHeartbeat(magic: Int, pageId: Int = 0, lineId: Int = 0): ByteArray = tpPackage(TP_CMD_HEARTBEAT, magic, nz(1, pageId) + nz(2, lineId))
    fun tpScrollSync(magic: Int, pageId: Int, lineId: Int): ByteArray = tpPackage(TP_CMD_PAGE_SCROLL_SYNC, magic, nz(1, pageId) + nz(2, lineId))

    fun tpFileList(magic: Int, files: List<Pair<ByteArray, String>>, menuMode: Int? = null): ByteArray {
        val w = Writer()
        for ((fid, name) in files) w.message(1, bytesField(1, fid) + stringField(2, Text.head(name, TP_FILENAME_MAX_CHARS)))
        if (menuMode != null) w.message(2, nz(1, menuMode))
        return tpPackage(TP_CMD_FILE_LIST, magic, w.toBytes())
    }

    fun tpModeSwitchResponse(magic: Int, error: Int = 0): ByteArray = tpPackage(TP_CMD_MODE_SWITCH_RSP, magic, nz(1, error))

    /** str.expandtabs(tabsize) on code points. */
    internal fun expandTabs(s: String, tab: Int): String {
        val sb = StringBuilder()
        var col = 0
        var i = 0
        while (i < s.length) {
            val cp = s.codePointAt(i)
            i += Character.charCount(cp)
            when (cp) {
                '\t'.code -> { val n = tab - col % tab; repeat(n) { sb.append(' ') }; col += n }
                '\n'.code, '\r'.code -> { sb.appendCodePoint(cp); col = 0 }
                else -> { sb.appendCodePoint(cp); col++ }
            }
        }
        return sb.toString()
    }

    private const val WS = "\\t\\n\\x0b\\x0c\\r "
    private val WORDSEP: Pattern = Pattern.compile(
        "([$WS]+" +
            "|(?<=[\\w!\"'&.,?])-{2,}(?=\\w)" +
            "|[^$WS]+?(?:-(?:(?<=[^\\d\\W]{2}-)|(?<=[^\\d\\W]-[^\\d\\W]-))(?=[^\\d\\W]-?[^\\d\\W])" +
            "|(?=[$WS]|\\z)" +
            "|(?<=[\\w!\"'&.,?])(?=-{2,}\\w)))",
        Pattern.UNICODE_CHARACTER_CLASS,
    )

    private fun cpLen(s: String) = s.codePointCount(0, s.length)
    private fun isBlank(s: String) = s.trim().isEmpty()

    /** textwrap.wrap(text, width) with the default TextWrapper options (break_long_words, break_on_hyphens, drop_whitespace). */
    fun textwrapWrap(text0: String, width: Int): List<String> {
        var text = expandTabs(text0, 8)
        text = text.map { if (it == '\t' || it == '\n' || it == '\u000B' || it == '\u000C' || it == '\r') ' ' else it }.joinToString("")
        val chunks = ArrayList<String>()
        val m = WORDSEP.matcher(text)
        var last = 0
        while (m.find()) {
            if (m.end() == m.start()) continue
            if (m.start() > last) chunks.add(text.substring(last, m.start()))
            chunks.add(m.group(1))
            last = m.end()
        }
        if (last < text.length) chunks.add(text.substring(last))
        val rev = ArrayList(chunks.filter { it.isNotEmpty() }.reversed())
        val lines = ArrayList<String>()
        while (rev.isNotEmpty()) {
            val cur = ArrayList<String>()
            var curLen = 0
            if (isBlank(rev.last()) && lines.isNotEmpty()) rev.removeAt(rev.size - 1)
            while (rev.isNotEmpty()) {
                val l = cpLen(rev.last())
                if (curLen + l <= width) { cur.add(rev.removeAt(rev.size - 1)); curLen += l } else break
            }
            if (rev.isNotEmpty() && cpLen(rev.last()) > width) {
                val spaceLeft = if (width < 1) 1 else width - curLen
                val chunk = rev.last()
                var end = spaceLeft
                if (cpLen(chunk) > spaceLeft) {
                    val headStr = chunk.substring(0, chunk.offsetByCodePoints(0, spaceLeft))
                    val hyphen = headStr.lastIndexOf('-')
                    if (hyphen > 0 && chunk.substring(0, hyphen).any { it != '-' }) end = chunk.codePointCount(0, hyphen) + 1
                }
                val cut = chunk.offsetByCodePoints(0, minOf(end, cpLen(chunk)))
                cur.add(chunk.substring(0, cut))
                rev[rev.size - 1] = chunk.substring(cut)
                curLen = cur.sumOf { cpLen(it) }
            }
            if (cur.isNotEmpty() && isBlank(cur.last())) { curLen -= cpLen(cur.last()); cur.removeAt(cur.size - 1) }
            if (cur.isNotEmpty()) lines.add(cur.joinToString(""))
        }
        return lines
    }

    /** Word-wrap into lines of <= charsPerLine, pages of <= linesPerPage and <= 1022 B (the hub's textwrap layout). */
    fun tpPaginate(script: String, charsPerLine: Int = TP_CHARS_PER_LINE, linesPerPage: Int = TP_LINES_PER_PAGE): List<List<String>> {
        val lines = ArrayList<String>()
        for (p0 in script.replace("\r\n", "\n").replace("\r", "\n").split("\n")) {
            val para = expandTabs(p0, 4).trim()
            val w = textwrapWrap(para, charsPerLine)
            if (w.isEmpty()) lines.add("") else lines.addAll(w)
        }
        while (lines.isNotEmpty() && lines.last().isEmpty()) lines.removeAt(lines.size - 1)
        while (lines.isNotEmpty() && lines.first().isEmpty()) lines.removeAt(0)
        val budget = TP_PAGE_MAX_BYTES - 2
        val pages = ArrayList<List<String>>()
        var cur = ArrayList<String>()
        var used = 0
        for (line in lines) {
            val pieces = ArrayList<String>()
            var rest = line
            while (Text.utf8Len(rest) > budget) {
                val head = Text.utf8Fit(rest, budget).ifEmpty { String(Character.toChars(rest.codePointAt(0))) }
                pieces.add(head)
                rest = rest.substring(head.length)
            }
            pieces.add(rest)
            for (piece in pieces) {
                var need = Text.utf8Len(piece) + if (cur.isNotEmpty()) 1 else 0
                if (cur.isNotEmpty() && (cur.size >= linesPerPage || used + need > budget)) {
                    pages.add(cur)
                    cur = ArrayList(); used = 0; need = Text.utf8Len(piece)
                }
                cur.add(piece)
                used += need
            }
        }
        if (cur.isNotEmpty()) pages.add(cur)
        return pages
    }

    data class TeleprompterMessage(val cmd: Int, val magic: Int, var page: Int? = null, var line: Int? = null, var value: Int? = null,
                                   var error: Int? = null, var fileId: ByteArray? = null, var startPage: Int? = null, var endPage: Int? = null) {
        val isClose: Boolean get() = cmd == TP_CMD_END_SESSION_REQUEST || (cmd == TP_CMD_STATUS_NOTIFY && value == TP_CTRL_CLOSE)
    }

    fun tpParse(payload: ByteArray): TeleprompterMessage {
        val m = TeleprompterMessage(Pb.readInt(payload, 1), Pb.readInt(payload, 2))
        val fld = TP_FIELD[m.cmd] ?: return m
        val sub = Pb.readBytes(payload, fld) ?: return m
        fun v(n: Int) = Pb.readInt(sub, n, 0)
        when (m.cmd) {
            TP_CMD_PAGE_DATA_REQUEST -> m.page = v(1)
            TP_CMD_PAGE_SCROLL_SYNC -> { m.page = v(1); m.line = v(2) }
            TP_CMD_HEARTBEAT -> { m.page = v(3); m.line = v(4) }
            TP_CMD_PAGE_LOAD_DONE -> { m.startPage = v(1); m.endPage = v(2) }
            TP_CMD_STATUS_NOTIFY -> { m.value = v(1); m.error = v(2) }
            TP_CMD_CONTROL -> { m.value = v(1); m.error = v(3) }
            TP_CMD_FILE_SELECT -> m.fileId = Pb.readBytes(sub, 1) ?: ByteArray(0)
            TP_CMD_MODE_SWITCH_REQ -> m.value = v(1)
            TP_CMD_COMM_RESP, TP_CMD_MODE_SWITCH_RSP -> m.error = v(1)
        }
        return m
    }

    // ── community teleprompter flow (COMMUNITY) ────────────────────────────────────────

    val COMMUNITY_TP_DISPLAY_CONFIG: ByteArray = Pb.unhex(
        "08011213080210904E1D00E094442500000000280030001213" + "0803100D0F1D00408D442500000000280030001212080410" +
            "001D000088422500000000280030" + "001212080510001D00009242250000A242280030001212080610001D0000C6" + "42250000C442280030001800")

    fun communityPacket(seq: Int, sid: Int, flag: Int, payload: ByteArray): ByteArray =
        Framing.addCrc(byteArrayOf(0xAA.toByte(), 0x21, seq.toByte(), (payload.size + 2).toByte(), 0x01, 0x01, sid.toByte(), flag.toByte()) + payload)

    fun communityTpDisplayConfig(seq: Int, msgId: Int): ByteArray {
        val payload = byteArrayOf(0x08, 0x02, 0x10) + encodeVarint(msgId) + byteArrayOf(0x22, 0x6A) + COMMUNITY_TP_DISPLAY_CONFIG
        return communityPacket(seq, 0x0E, 0x20, payload)
    }

    fun communityTpInit(seq: Int, msgId: Int, totalLines: Int = 10, manualMode: Boolean = true): ByteArray {
        val mode = if (manualMode) 0x00 else 0x01
        val contentHeight = maxOf(1, (totalLines * 2665) / 140)
        val display = byteArrayOf(0x08, 0x01, 0x10, 0x00, 0x18, 0x00, 0x20, 0x8B.toByte(), 0x02) + byteArrayOf(0x28) + encodeVarint(contentHeight) +
            byteArrayOf(0x30, 0xE6.toByte(), 0x01) + byteArrayOf(0x38, 0x8E.toByte(), 0x0A) + byteArrayOf(0x40, 0x05, 0x48, mode.toByte())
        val settings = byteArrayOf(0x08, 0x01, 0x12, display.size.toByte()) + display
        val payload = byteArrayOf(0x08, 0x01, 0x10) + encodeVarint(msgId) + byteArrayOf(0x1A, settings.size.toByte()) + settings
        return communityPacket(seq, SID_TELEPROMPTER, 0x20, payload)
    }

    fun communityTpPage(seq: Int, msgId: Int, pageNum: Int, text: String): ByteArray {
        val tb = ("\n" + text).toByteArray(Charsets.UTF_8)
        val inner = byteArrayOf(0x08) + encodeVarint(pageNum) + byteArrayOf(0x10, 0x0A) + byteArrayOf(0x1A) + encodeVarint(tb.size) + tb
        val payload = byteArrayOf(0x08, 0x03, 0x10) + encodeVarint(msgId) + byteArrayOf(0x2A) + encodeVarint(inner.size) + inner
        return communityPacket(seq, SID_TELEPROMPTER, 0x20, payload)
    }

    fun communityTpMarker(seq: Int, msgId: Int): ByteArray {
        val payload = byteArrayOf(0x08, 0xFF.toByte(), 0x01, 0x10) + encodeVarint(msgId) + byteArrayOf(0x6A, 0x04, 0x08, 0x00, 0x10, 0x06)
        return communityPacket(seq, SID_TELEPROMPTER, 0x20, payload)
    }

    fun communityTpSync(seq: Int, msgId: Int): ByteArray {
        val payload = byteArrayOf(0x08, 0x0E, 0x10) + encodeVarint(msgId) + byteArrayOf(0x6A, 0x00)
        return communityPacket(seq, 0x80, 0x00, payload)
    }

    private fun pySplit(s: String): List<String> = s.split(Regex("\\s+")).filter { it.isNotEmpty() }

    fun communityTpFormat(text0: String, charsPerLine: Int = 25, linesPerPage: Int = 10, minPages: Int = 14): List<String> {
        val text = text0.replace("\\n", "\n")
        var wrapped = ArrayList<String>()
        for (line in text.split("\n")) {
            if (line.trim().isEmpty()) { wrapped.add(""); continue }
            var current = ""
            for (word in pySplit(line)) {
                if (cpLen(current) + cpLen(word) + 1 > charsPerLine) {
                    if (current.isNotEmpty()) wrapped.add(current.trim())
                    current = "$word "
                } else current += "$word "
            }
            if (current.trim().isNotEmpty()) wrapped.add(current.trim())
        }
        if (wrapped.isEmpty()) wrapped = arrayListOf(text)
        while (wrapped.size < linesPerPage) wrapped.add(" ")
        val pages = ArrayList<String>()
        var i = 0
        while (i < wrapped.size) {
            val pl = ArrayList(wrapped.subList(i, minOf(i + linesPerPage, wrapped.size)))
            while (pl.size < linesPerPage) pl.add(" ")
            pages.add(pl.joinToString("\n") + " \n")
            i += linesPerPage
        }
        while (pages.size < minPages) pages.add(List(linesPerPage) { " " }.joinToString("\n") + " \n")
        return pages
    }

    /** Display config, init, 10 pages, marker, 0x80 sync, remaining pages (0.1 s apart in their script). */
    fun communityTpFrames(text: String, seqStart: Int = 0x08, msgIdStart: Int = 100, manualMode: Boolean = true): List<ByteArray> {
        val pages = communityTpFormat(text)
        var seq = seqStart
        var mid = msgIdStart
        val out = ArrayList<ByteArray>()
        fun nxt(f: (Int, Int) -> ByteArray) { out.add(f(seq, mid)); seq = (seq + 1) and 0xFF; mid += 1 }
        nxt(::communityTpDisplayConfig)
        nxt { s, m -> communityTpInit(s, m, pages.size * 10, manualMode) }
        pages.take(10).forEachIndexed { i, p -> nxt { s, m -> communityTpPage(s, m, i, p) } }
        nxt(::communityTpMarker)
        nxt(::communityTpSync)
        pages.drop(10).forEachIndexed { i, p -> nxt { s, m -> communityTpPage(s, m, i + 10, p) } }
        return out
    }

    // ═══ Translate (SID 0x05) DECODED ══════════════════════════════════════════════════
    const val SID_TRANSLATE = 0x05
    const val TR_CMD_CTRL = 1
    const val TR_CMD_RESULT = 2
    const val TR_CMD_NOTIFY = 161
    const val TR_CMD_COMM_RESP = 162
    const val TR_CMD_MODE_SWITCH = 163
    const val TR_CMD_LANG_SWITCH_REQ = 164
    const val TR_CMD_LANG_SWITCH_RSP = 165
    const val TR_CMD_HEARTBEAT = 255
    val TR_FIELD = mapOf(1 to 3, 2 to 4, 161 to 5, 163 to 6, 162 to 7, 255 to 8, 164 to 9, 165 to 10)
    const val TR_OPEN = 1
    const val TR_CLOSE = 2
    const val TR_PAUSE = 3
    const val TR_RESUME = 4
    const val TR_TEXT_MAX_BYTES = 1024
    const val TR_KEY_MAX_BYTES = 8

    fun trPackage(cmd: Int, magic: Int, inner: ByteArray): ByteArray =
        varintField(1, cmd) + varintField(2, magic) + messageField(TR_FIELD.getValue(cmd), inner)

    fun trOpen(magic: Int, srcLang: String, dstLang: String, srcKey: String? = null, dstKey: String? = null,
               langList: List<Pair<String, String>> = emptyList()): ByteArray {
        val src = srcLang.trim().uppercase(Locale.ROOT)
        val dst = dstLang.trim().uppercase(Locale.ROOT)
        val w = Writer().varint(1, TR_OPEN).string(2, "$src>$dst")
        for ((key, text) in langList) w.message(5, stringField(1, Text.utf8Fit(key, TR_KEY_MAX_BYTES)) + stringField(2, text))
        w.string(6, Text.utf8Fit(if (!srcKey.isNullOrEmpty()) srcKey.trim().uppercase(Locale.ROOT) else src, TR_KEY_MAX_BYTES))
        w.string(7, Text.utf8Fit(if (!dstKey.isNullOrEmpty()) dstKey.trim().uppercase(Locale.ROOT) else dst, TR_KEY_MAX_BYTES))
        return trPackage(TR_CMD_CTRL, magic, w.toBytes())
    }

    fun trCtrl(magic: Int, tcmd: Int): ByteArray = trPackage(TR_CMD_CTRL, magic, varintField(1, tcmd))
    fun trClose(magic: Int): ByteArray = trCtrl(magic, TR_CLOSE)
    fun trPause(magic: Int): ByteArray = trCtrl(magic, TR_PAUSE)
    fun trResume(magic: Int): ByteArray = trCtrl(magic, TR_RESUME)

    fun trText(magic: Int, srcText: String, dstText: String, final: Boolean = true, speaker: ByteArray? = null): ByteArray {
        val dst = if (dstText == " ") dstText else dstText.trim()
        val w = Writer().string(1, Text.utf8Fit(srcText.trim(), TR_TEXT_MAX_BYTES)).string(2, Text.utf8Fit(dst, TR_TEXT_MAX_BYTES))
        if (final) w.varint(4, 1)
        if (speaker != null && speaker.isNotEmpty()) w.bytes(5, speaker)
        return trPackage(TR_CMD_RESULT, magic, w.toBytes())
    }

    fun trHeartbeat(magic: Int): ByteArray = trPackage(TR_CMD_HEARTBEAT, magic, ByteArray(0))
    fun trLangSwitchResponse(magic: Int, error: Int = 0): ByteArray =
        trPackage(TR_CMD_LANG_SWITCH_RSP, magic, if (error != 0) varintField(1, error) else ByteArray(0))

    data class TranslateMessage(val cmd: Int, val magic: Int, var value: Int? = null, var error: Int? = null, var langKey: String? = null) {
        val isClose: Boolean get() = cmd == TR_CMD_NOTIFY && value == TR_CLOSE
    }

    fun trParse(payload: ByteArray): TranslateMessage {
        val m = TranslateMessage(Pb.readInt(payload, 1), Pb.readInt(payload, 2))
        val fld = TR_FIELD[m.cmd] ?: return m
        val sub = Pb.readBytes(payload, fld) ?: return m
        when (m.cmd) {
            TR_CMD_NOTIFY -> { m.value = Pb.readInt(sub, 1, 0); m.error = Pb.readInt(sub, 2, 0) }
            TR_CMD_MODE_SWITCH -> m.value = Pb.readInt(sub, 1, 0)
            TR_CMD_LANG_SWITCH_REQ -> m.langKey = Pb.readString(sub, 1)
            TR_CMD_COMM_RESP, TR_CMD_LANG_SWITCH_RSP -> m.error = Pb.readInt(sub, 1, 0)
            TR_CMD_CTRL -> { m.value = Pb.readInt(sub, 1, 0); m.error = Pb.readInt(sub, 4, 0) }
        }
        return m
    }

    // ═══ Navigation (SID 0x08) DECODED ═════════════════════════════════════════════════
    const val SID_NAVIGATION = 0x08
    const val NAV_CMD_HEARTBEAT = 0
    const val NAV_CMD_MENU_STARTUP = 1
    const val NAV_CMD_LOCATION_LIST = 2
    const val NAV_CMD_LOCATION_NONE = 3
    const val NAV_CMD_LOCATION_SELECTED = 4
    const val NAV_CMD_START = 5
    const val NAV_CMD_ERROR_INFO = 6
    const val NAV_CMD_BASIC_INFO = 7
    const val NAV_CMD_RECALCULATING = 10
    const val NAV_CMD_ARRIVE = 11
    const val NAV_CMD_STOP = 12
    const val NAV_CMD_NOTIFY_EXIT = 13
    const val NAV_CMD_VIEW_CHANGED = 14
    const val NAV_CMD_COMPASS_CHANGED = 15
    const val NAV_CMD_SWITCH_TRANSPORT_REQ = 18
    const val NAV_CMD_SWITCH_TRANSPORT_RSP = 19
    const val NAV_CMD_END_SESSION_REQ = 20
    const val NAV_F_EXT = 3
    const val NAV_F_LIST = 4
    const val NAV_F_INFO = 5
    const val NAV_F_TRSP = 12
    private val NAV_SUB: Map<Int, Pair<Int, Int?>> = mapOf(2 to (4 to 4), 7 to (5 to 9), 4 to (8 to 2), 8 to (6 to 4), 9 to (7 to 7),
        14 to (9 to null), 15 to (10 to null), 18 to (11 to null), 19 to (12 to null))
    const val NAV_METHOD_WALKING = 1
    const val NAV_METHOD_CYCLING = 2
    const val NAV_TEXT_MAX_BYTES = 60
    const val NAV_MISSING = "--"
    const val NAV_MAX_LOCATIONS = 20
    const val NAV_ERR_FAIL = 7
    val NAV_DIRECTION: Map<String, Int> = listOf(
        "DEPART", "STRAIGHT", "TURN_RIGHT", "TURN_LEFT", "TURN_SLIGHT_RIGHT", "TURN_SLIGHT_LEFT", "TURN_SHARP_RIGHT", "TURN_SHARP_LEFT",
        "UTURN_CLOCKWISE", "UTURN_COUNTERCLOCKWISE", "ROUNDABOUT_SHARP_RIGHT_CW", "ROUNDABOUT_RIGHT_CCW", "ROUNDABOUT_RIGHT_CW",
        "ROUNDABOUT_SLIGHT_RIGHT_CCW", "ROUNDABOUT_SLIGHT_RIGHT_CW", "ROUNDABOUT_STRAIGHT_CCW", "ROUNDABOUT_STRAIGHT_CW",
        "ROUNDABOUT_SLIGHT_LEFT_CCW", "ROUNDABOUT_SLIGHT_LEFT_CW", "ROUNDABOUT_LEFT_CCW", "ROUNDABOUT_LEFT_CW", "ROUNDABOUT_SHARP_LEFT_CCW",
        "ROUNDABOUT_SHARP_LEFT_CW", "ROUNDABOUT_UTURN_CCW", "ROUNDABOUT_UTURN_CW", "ROUNDABOUT_CCW", "ROUNDABOUT_CW", "ROUNDABOUT_EXIT_CCW",
        "ROUNDABOUT_EXIT_CW", "MERGE_RIGHT", "MERGE_LEFT", "FORK_RIGHT", "FORK_LEFT", "MERGE_UNSPECIFIED", "ROUNDABOUT_SHARP_RIGHT_CCW",
    ).mapIndexed { i, s -> s to i + 1 }.toMap()
    private val NAV_ALIASES = mapOf("RIGHT" to "TURN_RIGHT", "LEFT" to "TURN_LEFT", "SLIGHT_RIGHT" to "TURN_SLIGHT_RIGHT",
        "SLIGHT_LEFT" to "TURN_SLIGHT_LEFT", "UTURN" to "UTURN_COUNTERCLOCKWISE")

    fun navPackage(cmd: Int, magic: Int, field: Int, inner: ByteArray): ByteArray =
        varintField(1, cmd) + varintField(2, magic) + messageField(field, inner)

    /** <=limit UTF-8 bytes without splitting a character; null/blank -> "--". */
    fun navText(text: String?, limit: Int = NAV_TEXT_MAX_BYTES): ByteArray =
        if (text == null || text.trim().isEmpty()) NAV_MISSING.toByteArray() else Text.utf8Fit(text, limit).toByteArray(Charsets.UTF_8)

    private fun navSimple(cmd: Int, magic: Int) = navPackage(cmd, magic, NAV_F_EXT, ByteArray(0))
    fun navStart(magic: Int): ByteArray = navSimple(NAV_CMD_START, magic)
    fun navHeartbeat(magic: Int): ByteArray = navSimple(NAV_CMD_HEARTBEAT, magic)
    fun navRecalculating(magic: Int): ByteArray = navSimple(NAV_CMD_RECALCULATING, magic)
    fun navArrive(magic: Int): ByteArray = navSimple(NAV_CMD_ARRIVE, magic)
    fun navStop(magic: Int): ByteArray = navSimple(NAV_CMD_STOP, magic)
    fun navLocationNone(magic: Int): ByteArray = navSimple(NAV_CMD_LOCATION_NONE, magic)

    fun navDirectionIndex(direction: String): Int {
        val key = direction.trim().uppercase(Locale.ROOT).replace(" ", "_").replace("-", "_")
        return NAV_DIRECTION[NAV_ALIASES[key] ?: key] ?: throw IllegalArgumentException("unknown direction $direction")
    }

    fun navBasicInfo(magic: Int, direction: Int, distance: String? = null, road: String? = null, remainingTime: String? = null,
                     remainingDistance: String? = null, eta: String? = null, speed: String? = null, method: Int = NAV_METHOD_WALKING,
                     counter: Int = 0): ByteArray {
        val e = if (eta != null && eta.trim().isNotEmpty() && !eta.startsWith("ETA")) "ETA: $eta" else eta
        val w = Writer().varint(1, direction)
        listOf(2 to distance, 3 to road, 4 to remainingTime, 5 to remainingDistance, 6 to e, 7 to speed).forEach { (f, t) -> w.bytes(f, navText(t)) }
        if (method != 0) w.varint(8, method)
        if (counter != 0) w.varint(10, counter)
        return navPackage(NAV_CMD_BASIC_INFO, magic, NAV_F_INFO, w.toBytes())
    }

    fun navBasicInfo(magic: Int, direction: String, distance: String? = null, road: String? = null, remainingTime: String? = null,
                     remainingDistance: String? = null, eta: String? = null, speed: String? = null, method: Int = NAV_METHOD_WALKING,
                     counter: Int = 0): ByteArray =
        navBasicInfo(magic, navDirectionIndex(direction), distance, road, remainingTime, remainingDistance, eta, speed, method, counter)

    fun navErrorInfo(magic: Int, message: String, index: Int = 0, error: Int = NAV_ERR_FAIL): ByteArray {
        val inner = varintField(1, error) + bytesField(2, navText(message)) + (if (index != 0) varintField(3, index) else ByteArray(0))
        return navPackage(NAV_CMD_ERROR_INFO, magic, NAV_F_EXT, inner)
    }

    fun navLocationList(magic: Int, names: List<String>, icons: List<Int> = emptyList()): ByteArray {
        require(names.size <= NAV_MAX_LOCATIONS && icons.size <= NAV_MAX_LOCATIONS) { "at most $NAV_MAX_LOCATIONS locations" }
        val w = Writer().varint(1, names.size)
        names.forEach { w.bytes(2, navText(it)) }
        if (icons.isNotEmpty()) w.bytes(3, icons.fold(ByteArray(0)) { a, i -> a + encodeVarint(i) })
        return navPackage(NAV_CMD_LOCATION_LIST, magic, NAV_F_LIST, w.toBytes())
    }

    fun navSwitchTransportResponse(magic: Int, status: Int = 0): ByteArray =
        navPackage(NAV_CMD_SWITCH_TRANSPORT_RSP, magic, NAV_F_TRSP, if (status != 0) varintField(1, status) else ByteArray(0))

    data class NavigationMessage(val cmd: Int, val magic: Int, var value: Int? = null, var location: String? = null, var method: Int? = null,
                                 var error: Int? = null) {
        val isExit: Boolean get() = cmd == NAV_CMD_NOTIFY_EXIT || cmd == NAV_CMD_END_SESSION_REQ
    }

    fun navParse(payload: ByteArray): NavigationMessage {
        val m = NavigationMessage(Pb.readInt(payload, 1, 0), Pb.readInt(payload, 2))
        val (fld, errFld) = NAV_SUB[m.cmd] ?: (NAV_F_EXT to 1)
        val sub = Pb.readBytes(payload, fld) ?: return m
        when (m.cmd) {
            NAV_CMD_LOCATION_SELECTED -> { m.location = Pb.readString(sub, 1); m.value = Pb.readInt(sub, 3, 0); m.method = Pb.readInt(sub, 4, 0) }
            NAV_CMD_VIEW_CHANGED, NAV_CMD_COMPASS_CHANGED -> m.value = Pb.readInt(sub, 1, 0)
            NAV_CMD_SWITCH_TRANSPORT_REQ -> m.method = Pb.readInt(sub, 1, 0)
        }
        if (errFld != null) m.error = Pb.readInt(sub, errFld, 0)
        return m
    }
}
