package com.kunsh.g2core

import com.kunsh.g2core.Pb.messageField
import com.kunsh.g2core.Pb.varintField

/**
 * Glasses settings: SID 0x09 G2SettingPackage, SID 0x20 module config, SID 0x80 GET_DEVICE_INFO (port of
 * g2/native/settings.py).
 *
 * Confidence: SET brightness/auto/silent/wear/head-up/units/gestures LIVE; display height/distance and dominant
 * hand DECODED (never sent by a hub); READ + readback LIVE; module timeout/language LIVE; device info/restart/
 * reset/unpair DECODED.
 */
object Settings {
    const val SID_SETTINGS = 0x09
    const val SID_MODULE = 0x20
    const val SID_DEVICE = 0x80

    const val CMD_SET = 1
    const val CMD_READ = 2
    const val CMD_DEVICE_EVENT = 3
    const val CMD_DEVICE_ACK = 4
    const val CMD_APP_ACK = 5
    val CMD_NAMES = mapOf(1 to "SET", 2 to "READ", 3 to "DEVICE_EVENT", 4 to "DEVICE_ACK", 5 to "APP_ACK")

    const val READ_BRIGHTNESS = 0
    const val READ_BASIC = 1

    const val F_BRIGHTNESS = 1
    const val F_HEIGHT = 2
    const val F_DISTANCE = 3
    const val F_HEAD_UP = 4
    const val F_WEAR = 5
    const val F_SILENT = 6
    const val F_APP_PAGE = 7
    const val F_ADVANCED = 8
    const val F_UNITS = 9
    const val F_GESTURES = 10
    const val F_HAND = 11
    const val F_CONTROL = 12

    const val BRIGHTNESS_MIN = 1
    const val BRIGHTNESS_MAX = 50
    const val HEAD_UP_ANGLE_MIN = 0
    const val HEAD_UP_ANGLE_MAX = 60
    const val DISPLAY_LEVEL_MAX = 20
    const val ENUM_MAX = 15

    /** (screen_on, operation_type, app_type) the official app sends on every connect. */
    val GESTURES_APP_DEFAULT: List<Triple<Int, Int, Int>> = listOf(Triple(0, 0, 0), Triple(0, 1, 0), Triple(0, 2, 0))

    const val MOD_CMD_LANGUAGE = 0
    const val MOD_CMD_READ_TIMEOUT = 1
    const val MOD_CMD_SET_TIMEOUT = 2
    const val TIMEOUT_NEVER = 65365
    val TIMEOUT_CHOICES = listOf(3, 5, 10, 15, 30, TIMEOUT_NEVER)
    val LANGUAGES = mapOf(
        0 to "English", 1 to "German", 2 to "French", 3 to "Spanish", 4 to "Italian", 5 to "Japanese",
        6 to "Chinese (Traditional)", 7 to "Chinese (Simplified)", 8 to "Korean",
    )

    const val INFO_ALL = 0
    val INFO_NAMES = mapOf(
        0 to "ALL_INFO", 1 to "DEVICE_BRIGHTNESS", 2 to "DEVICE_ANTI_SHAKE_ENABLE", 3 to "DEVICE_DISPLAY_MODE",
        4 to "DEVICE_WORK_MODE", 5 to "DEVICE_WAKEUP_ANGLE", 6 to "DEVICE_GLASSES_SN", 7 to "DEVICE_DEVICE_SN", 8 to "DEVICE_BLE_MAC",
    )
    const val DEV_CMD_UNPAIR = 9
    const val DEV_CMD_SET_INFO = 11
    const val DEV_CMD_GET_INFO = 12
    const val DEV_CMD_FACTORY_RESET = 13
    const val DEV_CMD_QUICK_RESTART = 15

    private fun intIn(v: Int, lo: Int, hi: Int, what: String): Int {
        require(v in lo..hi) { "$what must be an integer $lo..$hi, got $v" }
        return v
    }

    private fun flag(v: Boolean): Int = if (v) 1 else 0

    /** SET = `{1: 1, 2: magic, 3: {leaf: inner}}`. */
    fun buildSet(magic: Int, leaf: Int, inner: ByteArray): ByteArray =
        varintField(1, CMD_SET) + varintField(2, magic) + messageField(3, messageField(leaf, inner))

    /** READ `{1: 2, 2: m, 4: {1: type}}`; type 0 brightness, 1 basic (LIVE). */
    fun readSettings(magic: Int, settingType: Int = READ_BASIC): ByteArray {
        val t = intIn(settingType, 0, 1, "setting_type")
        return varintField(1, CMD_READ) + varintField(2, magic) + messageField(4, varintField(1, t))
    }

    /** Brightness level 1..50 (LIVE). */
    fun brightness(magic: Int, level: Int): ByteArray =
        buildSet(magic, F_BRIGHTNESS, varintField(2, intIn(level, BRIGHTNESS_MIN, BRIGHTNESS_MAX, "brightness level")))

    fun autoBrightness(magic: Int, on: Boolean): ByteArray = buildSet(magic, F_BRIGHTNESS, varintField(1, flag(on)))

    fun brightnessCalibration(magic: Int, left: Int, right: Int): ByteArray =
        buildSet(magic, F_BRIGHTNESS, varintField(3, intIn(left, 0, 255, "left")) + varintField(4, intIn(right, 0, 255, "right")))

    /** Display height 0..20 (DECODED, never sent by a hub). */
    fun displayHeight(magic: Int, level: Int): ByteArray =
        buildSet(magic, F_HEIGHT, varintField(1, intIn(level, 0, DISPLAY_LEVEL_MAX, "display height")))

    /** Display distance 0..20 (DECODED). */
    fun displayDistance(magic: Int, level: Int): ByteArray =
        buildSet(magic, F_DISTANCE, varintField(1, intIn(level, 0, DISPLAY_LEVEL_MAX, "display distance")))

    fun headUp(magic: Int, enabled: Boolean, angle: Int? = null): ByteArray {
        val w = Writer().varint(1, flag(enabled))
        if (angle != null) w.varint(2, intIn(angle, HEAD_UP_ANGLE_MIN, HEAD_UP_ANGLE_MAX, "head-up angle"))
        return buildSet(magic, F_HEAD_UP, w.toBytes())
    }

    fun wearDetection(magic: Int, on: Boolean): ByteArray = buildSet(magic, F_WEAR, varintField(1, flag(on)))
    fun silentMode(magic: Int, on: Boolean): ByteArray = buildSet(magic, F_SILENT, varintField(1, flag(on)))

    /** `{9: {1 unit_format, 2 distance, 3 time(0 24h 1 12h), 4 date, 5 temp(1 C 2 F)}}` each 0..15 (LIVE). */
    fun units(magic: Int, unitFormat: Int = 0, distanceUnit: Int = 0, timeFormat: Int = 1, dateFormat: Int = 0, temperatureUnit: Int = 1): ByteArray {
        val vals = listOf(unitFormat, distanceUnit, timeFormat, dateFormat, temperatureUnit)
        val names = listOf("unit_format", "distance_unit", "time_format", "date_format", "temperature_unit")
        val w = Writer()
        vals.forEachIndexed { i, v -> w.varint(i + 1, intIn(v, 0, ENUM_MAX, names[i])) }
        return buildSet(magic, F_UNITS, w.toBytes())
    }

    /** `{10: {1: {1 screen_on, 2 operation_type, 3 app_type}...}}` (LIVE). */
    fun gestures(magic: Int, entries: List<Triple<Int, Int, Int>> = GESTURES_APP_DEFAULT): ByteArray {
        require(entries.isNotEmpty()) { "entries must not be empty" }
        val w = Writer()
        for (e in entries) {
            w.message(1, varintField(1, intIn(e.first, 0, 1, "screen_on")) + varintField(2, intIn(e.second, 0, ENUM_MAX, "operation_type")) +
                varintField(3, intIn(e.third, 0, 1, "app_type")))
        }
        return buildSet(magic, F_GESTURES, w.toBytes())
    }

    fun dominantHand(magic: Int, hand: Int, ringMac: ByteArray? = null): ByteArray {
        val w = Writer().varint(1, intIn(hand, 0, 1, "hand"))
        if (ringMac != null) {
            require(ringMac.size == 6) { "ring_mac must be 6 bytes" }
            w.bytes(2, ringMac)
        }
        return buildSet(magic, F_HAND, w.toBytes())
    }

    fun appAck(magic: Int, packageId: Int = 0, flag: Int = 0): ByteArray =
        varintField(1, CMD_APP_ACK) + varintField(2, magic) + messageField(7, varintField(1, packageId) + varintField(2, flag))

    private fun module(cmd: Int, magic: Int, fld: Int, inner: ByteArray): ByteArray =
        varintField(1, cmd) + varintField(2, magic) + messageField(fld, inner)

    /** Screen-off time; 3/5/10/15/30 or null/0 = never (65365) (LIVE). */
    fun displayTimeout(magic: Int, seconds: Int?): ByteArray {
        val v = if (seconds == null || seconds == 0) TIMEOUT_NEVER else {
            require(seconds in TIMEOUT_CHOICES) { "display timeout must be one of $TIMEOUT_CHOICES or never, got $seconds" }
            seconds
        }
        return module(MOD_CMD_SET_TIMEOUT, magic, 4, varintField(1, v))
    }

    fun readDisplayTimeout(magic: Int): ByteArray = module(MOD_CMD_READ_TIMEOUT, magic, 4, ByteArray(0))

    fun language(magic: Int, index: Int): ByteArray = module(MOD_CMD_LANGUAGE, magic, 3, varintField(1, intIn(index, 0, 8, "language index")))

    fun language(magic: Int, name: String): ByteArray {
        val idx = LANGUAGES.entries.firstOrNull { it.value.equals(name, ignoreCase = true) }?.key
            ?: throw IllegalArgumentException("unknown language $name")
        return language(magic, idx)
    }

    private fun dev(cmd: Int, magic: Int, fld: Int, inner: ByteArray): ByteArray =
        varintField(1, cmd) + varintField(2, magic) + messageField(fld, inner)

    /** `{1: 12, 2: m, 11: {1: {1: item}}}` (DECODED). Read-only. */
    fun getDeviceInfo(magic: Int, item: Int = INFO_ALL): ByteArray {
        require(item in INFO_NAMES) { "item must be one of 0..8, got $item" }
        return dev(DEV_CMD_GET_INFO, magic, 11, messageField(1, varintField(1, item)))
    }

    /** DISRUPTIVE: reboots the glasses (DECODED). */
    fun quickRestart(magic: Int): ByteArray = dev(DEV_CMD_QUICK_RESTART, magic, 14, ByteArray(0))

    /** DESTRUCTIVE: wipes settings and bonds (DECODED). */
    fun factoryReset(magic: Int): ByteArray = dev(DEV_CMD_FACTORY_RESET, magic, 12, ByteArray(0))

    fun unpair(magic: Int): ByteArray = dev(DEV_CMD_UNPAIR, magic, 8, ByteArray(0))

    // ── parsing ────────────────────────────────────────────────────────────────────────

    /** Readback field 4 of a SID 0x09 reply/push. null = absent. */
    data class Snapshot(
        var settingType: Int? = null,
        var brightnessLevel: Int? = null,
        var displayHeight: Int? = null,
        var displayDistance: Int? = null,
        var leftVersion: String? = null,
        var rightVersion: String? = null,
        var headUp: Int? = null,
        var headUpAngle: Int? = null,
        var headUpCalibration: Int? = null,
        var wearDetection: Int? = null,
        var runningStatus: Int? = null,
        var battery: Int? = null,
        var charging: Int? = null,
        var silentMode: Int? = null,
        var leftCalibration: Int? = null,
        var rightCalibration: Int? = null,
        var headUpRecalibrationOk: Int? = null,
        var brightnessAuto: Int? = null,
        var unreadCount: Int? = null,
        val extra: MutableMap<Int, Any> = LinkedHashMap(),
    ) {
        /** A frame with field 4 but no setting_type is an unsolicited push. */
        val isPush: Boolean get() = settingType == null

        fun asMap(): Map<String, Any> {
            val m = LinkedHashMap<String, Any>()
            fun put(k: String, v: Any?) { if (v != null) m[k] = v }
            put("setting_type", settingType); put("brightness_level", brightnessLevel); put("display_height", displayHeight)
            put("display_distance", displayDistance); put("left_version", leftVersion); put("right_version", rightVersion)
            put("head_up", headUp); put("head_up_angle", headUpAngle); put("head_up_calibration", headUpCalibration)
            put("wear_detection", wearDetection); put("running_status", runningStatus); put("battery", battery)
            put("charging", charging); put("silent_mode", silentMode); put("left_calibration", leftCalibration)
            put("right_calibration", rightCalibration); put("head_up_recalibration_ok", headUpRecalibrationOk)
            put("brightness_auto", brightnessAuto); put("unread_count", unreadCount)
            extra.forEach { (k, v) -> m["field_$k"] = v }
            return m
        }
    }

    fun parseSnapshot(sub: ByteArray): Snapshot {
        val s = Snapshot()
        for (f in Pb.iterFields(sub)) {
            if (f.number !in 1..19) {
                s.extra[f.number] = if (f.wire == 0) f.value else f.asBytes
                continue
            }
            if (f.number == 5 || f.number == 6) {
                if (f.wire == Pb.WIRE_LEN) { if (f.number == 5) s.leftVersion = f.asStr else s.rightVersion = f.asStr }
                continue
            }
            if (f.wire != Pb.WIRE_VARINT) continue
            val v = f.value.toInt()
            when (f.number) {
                1 -> s.settingType = v; 2 -> s.brightnessLevel = v; 3 -> s.displayHeight = v; 4 -> s.displayDistance = v
                7 -> s.headUp = v; 8 -> s.headUpAngle = v; 9 -> s.headUpCalibration = v; 10 -> s.wearDetection = v
                11 -> s.runningStatus = v; 12 -> s.battery = v; 13 -> s.charging = v; 14 -> s.silentMode = v
                15 -> s.leftCalibration = v; 16 -> s.rightCalibration = v; 17 -> s.headUpRecalibrationOk = v
                18 -> s.brightnessAuto = v; 19 -> s.unreadCount = v
            }
        }
        return s
    }

    data class Ack(val packageId: Int, val flag: Int)

    data class SettingsMessage(
        val cmd: Int,
        val magic: Int,
        var snapshot: Snapshot? = null,
        /** field 3 (echo of a SET): leaf number -> raw leaf bytes. */
        var set: Map<Int, ByteArray>? = null,
        var event: Pair<Int, Int>? = null,
        var ack: Ack? = null,
    ) {
        val name: String get() = CMD_NAMES[cmd] ?: "cmd=$cmd"
        val ok: Boolean? get() = ack?.let { it.flag == 0 }
    }

    /** Decode any SID 0x09 message. */
    fun parse(payload: ByteArray): SettingsMessage {
        val m = SettingsMessage(Pb.readInt(payload, 1), Pb.readInt(payload, 2))
        Pb.readBytes(payload, 4)?.let { m.snapshot = parseSnapshot(it) }
        Pb.readBytes(payload, 3)?.let { sub ->
            m.set = Pb.iterFields(sub).filter { it.wire == Pb.WIRE_LEN }.associate { it.number to it.asBytes }
        }
        Pb.readBytes(payload, 5)?.let { m.event = Pb.readInt(it, 1, 0) to Pb.readInt(it, 2, 0) }
        for (fld in intArrayOf(6, 7)) Pb.readBytes(payload, fld)?.let { m.ack = Ack(Pb.readInt(it, 1, 0), Pb.readInt(it, 2, 0)) }
        return m
    }

    fun parseAck(payload: ByteArray): Ack? = parse(payload).ack

    data class ModuleMessage(val cmd: Int, val magic: Int, var language: Int? = null, var timeout: Int? = null) {
        val timeoutNever: Boolean? get() = timeout?.let { it > 20 }
    }

    fun parseModule(payload: ByteArray): ModuleMessage {
        val m = ModuleMessage(Pb.readInt(payload, 1), Pb.readInt(payload, 2))
        Pb.readBytes(payload, 3)?.let { m.language = Pb.readInt(it, 1, 0) }
        Pb.readBytes(payload, 4)?.let { val t = Pb.readVarint(it, 1, -1); if (t >= 0) m.timeout = t.toInt() }
        return m
    }
}
