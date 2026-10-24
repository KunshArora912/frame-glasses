package com.kunsh.g2core

import com.kunsh.g2core.Pb.messageField
import com.kunsh.g2core.Pb.varintField

/**
 * On-glass menu / app launcher (SID 0x03), port of g2/native/menu.py. The push REPLACES the whole menu; the app's rule
 * is 5..10 items, no duplicates. Built-in ids LIVE; plugin items DECODED.
 */
object Menu {
    const val SID_MENU = 0x03
    const val CMD_APP_SEND_MENU_INFO = 0
    const val CMD_OS_RESPONSE_MENU_INFO = 1

    val BUILTIN = mapOf(1 to "Dashboard", 4 to "Notifications", 5 to "Translate", 6 to "Teleprompt", 7 to "Even AI", 8 to "Navigate",
        11 to "Conversate", 48 to "Terminal", 266 to "Silent mode")
    const val DASHBOARD = 1
    const val NOTIFICATIONS = 4
    const val TRANSLATE = 5
    const val TELEPROMPT = 6
    const val EVEN_AI = 7
    const val NAVIGATE = 8
    const val CONVERSATE = 11
    const val TERMINAL = 48
    const val SILENT_MODE = 266
    val DEFAULT_MENU = listOf(1, 4, 5, 6, 7)
    const val MIN_ITEMS = 5
    const val MAX_ITEMS = 10
    const val ITEM_BUILTIN = 0
    const val ITEM_PLUGIN = 1
    const val PLUGIN_ICON = 1
    const val NAME_MAX_CHARS = 31

    data class PluginItem(val name: String, val appId: Int, val icon: Int = PLUGIN_ICON)

    /** Control chars stripped, <=31 code points, 'custom' if empty. */
    fun pluginName(name: String?): String {
        val n = (name ?: "").filter { !(it.code in 0x00..0x1F || it.code == 0x7F) }.trim()
        return if (n.isNotEmpty()) Text.head(n, NAME_MAX_CHARS) else "custom"
    }

    fun builtinItem(appId: Int): ByteArray = varintField(1, ITEM_BUILTIN) + varintField(4, appId)

    fun pluginItem(item: PluginItem): ByteArray {
        val w = Writer().varint(1, ITEM_PLUGIN).varint(2, item.icon)
        var name = pluginName(item.name).toByteArray(Charsets.UTF_8)
        // Byte-for-byte the Python loop (menu.py plugin_item). NOTE: it drops the last byte and then trailing
        // continuation bytes, which can leave a dangling UTF-8 lead byte (e.g. 31 x "é" -> 15 x "é" + 0xC3).
        // Kept for parity with the vectors; names that fit in 31 bytes (ASCII up to 31 chars) are unaffected.
        while (name.size > NAME_MAX_CHARS) {
            var end = name.size - 1
            while (end > 0 && (name[end - 1].toInt() and 0xC0) == 0x80) end--
            name = name.copyOf(end)
        }
        if (name.isNotEmpty()) w.bytes(3, name)
        return w.varint(4, item.appId).toBytes()
    }

    /** The app-side rule; null when valid. */
    fun validate(builtinIds: List<Int>, plugins: List<PluginItem> = emptyList()): String? {
        val bad = builtinIds.filter { it !in BUILTIN }
        if (bad.isNotEmpty()) return "not a menu app: $bad"
        val all = builtinIds + plugins.map { it.appId }
        if (all.toSet().size != all.size) return "duplicate menu items"
        if (all.size !in MIN_ITEMS..MAX_ITEMS) return "the menu needs $MIN_ITEMS to $MAX_ITEMS items (has ${all.size})"
        return null
    }

    /** Whole-menu replacement `{1: 0, 2: m, 3: {1: count, 2: Item...}}` (LIVE for built-ins). Reply: cmd 1 echoing the magic. */
    fun menuPush(magic: Int, builtinIds: List<Int> = DEFAULT_MENU, plugins: List<PluginItem> = emptyList(), strict: Boolean = false): ByteArray {
        if (strict) validate(builtinIds, plugins)?.let { throw IllegalArgumentException(it) }
        val items = builtinIds.map { builtinItem(it) } + plugins.map { pluginItem(it) }
        val send = Writer().varint(1, items.size)
        items.forEach { send.message(2, it) }
        return varintField(1, CMD_APP_SEND_MENU_INFO) + varintField(2, magic) + messageField(3, send.toBytes())
    }

    fun parseMenuReply(payload: ByteArray): Pair<Int, Int> = Pb.readInt(payload, 1) to Pb.readInt(payload, 2)

    data class MenuEvent(val kind: String, val sid: Int, val appId: Int?, val detail: String = "") {
        val appName: String get() = BUILTIN[appId ?: -1] ?: String.format(java.util.Locale.ROOT, "0x%02x", appId ?: 0)
    }

    private val OPEN_CMDS = mapOf(0x08 to (1 to "MENU_STARTUP"), 0x06 to (162 to "FILE_LIST_REQUEST"), 0x05 to (164 to "LANG_SWITCH_REQ"),
        0x07 to (1 to "WAKE_UP"))

    /** Map what arrives after a menu choice onto a MenuEvent (0x0D foreground push, native app opening, 0xE0 cmd 17 plugin). */
    fun parseSelectionEvent(sid: Int, payload: ByteArray): MenuEvent? {
        val cmd = Pb.readInt(payload, 1)
        if (sid == 0x0D) {
            val st = Pb.readBytes(payload, 3) ?: return null
            return MenuEvent("foreground", sid, Pb.readInt(st, 2, 0), String.format(java.util.Locale.ROOT, "bg=0x%02x", Pb.readInt(st, 1, 0)))
        }
        if (sid == 0xE0 && cmd == 17) {
            val sub = Pb.readBytes(payload, 20)
            return MenuEvent("plugin", sid, sub?.let { Pb.readInt(it, 1, 0) }, "MENU_STARTUP")
        }
        val open = OPEN_CMDS[sid] ?: return null
        if (cmd == open.first) {
            if (sid == 0x07) {
                val ctrl = Pb.readBytes(payload, 3)
                if (ctrl == null || Pb.readInt(ctrl, 1, 0) != 1) return null
            }
            return MenuEvent("open", sid, sid, open.second)
        }
        return null
    }
}
