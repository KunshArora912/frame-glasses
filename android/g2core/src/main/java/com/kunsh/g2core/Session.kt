package com.kunsh.g2core

import com.kunsh.g2core.Pb.encodeKey
import com.kunsh.g2core.Pb.encodeVarint
import com.kunsh.g2core.Pb.messageField
import com.kunsh.g2core.Pb.varintField
import java.util.TimeZone

/** Which arm a frame goes to / came from. */
enum class Side(val wire: String) { LEFT("left"), RIGHT("right"), BOTH("both") }

/**
 * Session prelude, keep-alive, time sync and the minimal offline dashboard plan (port of g2/native/session.py).
 * Everything here is pure: builders return protobuf payloads (or ready frames), parsers take payloads.
 */
object Session {
    const val SID_DASHBOARD = 0x01
    const val SID_MENU = 0x03
    const val SID_NOTIFICATION = 0x04
    const val SID_EVEN_AI = 0x07
    const val SID_SETTINGS = 0x09
    const val SID_QUICKLIST = 0x0C
    const val SID_APP_SYNC = 0x0D
    const val SID_ONBOARDING = 0x10
    const val SID_WATCHFACE = 0x1F
    const val SID_MODULE = 0x20
    const val SID_DEVICE = 0x80
    const val SID_CASE = 0x81
    const val SID_EVENHUB = 0xE0

    const val CMD_AUTH = 4
    const val CMD_ROLE_CHANGE = 5
    const val CMD_UNPAIR = 9
    const val CMD_EXCEPTION = 10
    const val CMD_SET_INFO = 11
    const val CMD_GET_INFO = 12
    const val CMD_FACTORY_RESET = 13
    const val CMD_KEEPALIVE = 14
    const val CMD_QUICK_RESTART = 15
    const val CMD_TIME_SYNC = 128
    const val CMD_AUD_CONTROL = 129
    val DEV_CMD_NAMES = mapOf(4 to "AUTHENTICATION", 5 to "PIPE_ROLE_CHANGE", 6 to "RING_CONNECT_INFO", 7 to "BLE_CONNECT_PARAM",
        8 to "DISCONNECT_INFO", 9 to "UNPAIR_INFO", 10 to "COMMAND_EXCEPTION", 11 to "SET_DEVICE_INFO", 12 to "GET_DEVICE_INFO",
        13 to "RESTORE_TO_FACTORY_SETTINGS", 14 to "BASE_CONNECT_HEART_BEAT", 15 to "QUICK_RESTART", 128 to "TIME_SYNC",
        129 to "AUD_CONTROL", 255 to "COMMAND_ERROR")
    val DEV_FIELD = mapOf(4 to 3, 5 to 4, 6 to 5, 7 to 6, 8 to 7, 9 to 8, 10 to 9, 11 to 10, 12 to 11, 13 to 12, 14 to 13, 15 to 14,
        128 to 128, 129 to 129)
    private val DEV_RESULT_FIELD = mapOf(4 to 3, 5 to 2, 6 to 5, 7 to 5, 8 to 3, 9 to 3, 10 to 2, 11 to 2, 12 to 2, 13 to 1, 14 to 1,
        15 to 1, 128 to 3, 129 to 3)
    const val ROLE_BOTH = 0
    const val ROLE_RIGHT = 1
    const val ROLE_LEFT = 2
    const val PHONE_IOS = 3
    const val PHONE_ANDROID = 4
    const val TZ_UNIT_MINUTES = 15
    const val TZ_MIN = -128
    const val TZ_MAX = 127

    const val OB_CMD_CONFIG = 1
    const val OB_CMD_EVENT = 3
    const val OB_START_UP = 1
    const val OB_TOUCH = 2
    const val OB_HEAD_UP = 3
    const val OB_FINISH = 4
    const val OB_EVENT_WEAR_STATUS = 1
    const val CASE_CMD_INFO = 1

    const val PRELUDE_MAGIC = 156
    const val PRELUDE_SEQ = 0x92
    const val EVENHUB_HEARTBEAT_CMD = 12

    // timing (ms)
    const val HEARTBEAT_INTERVAL_MS = 4000L
    const val PACKET_GAP_MS = 50L
    const val CONNECT_SEQUENCE_GAP_MS = 150L
    const val PRELUDE_DRAIN_MS = 1500L
    const val SETTLE_MS = 300L
    const val KEEPALIVE_INTERVAL_MS = 60_000L
    const val KEEPALIVE_LEFT_LEAD_MS = 2000L
    const val KEEPALIVE_ECHO_WAIT_MS = 5000L
    const val TIME_SYNC_INTERVAL_MS = 3_600_000L
    const val DASHBOARD_SETTLE_MS = 5000L

    /** 0x00 only for 0x80 cmd 4 (auth) and cmd 14 (keep-alive); 0x20 for everything else. */
    fun flagFor(sid: Int, payload: ByteArray): Int {
        if (sid == SID_DEVICE) {
            val c = Pb.readVarint(payload, 1, -1)
            if (c == CMD_AUTH.toLong() || c == CMD_KEEPALIVE.toLong()) return Framing.FLAG_PLAIN
        }
        return Framing.FLAG_REQUEST
    }

    fun devicePackage(cmd: Int, magic: Int, inner: ByteArray): ByteArray =
        varintField(1, cmd) + varintField(2, magic) + messageField(DEV_FIELD.getValue(cmd), inner)

    /** `{1: 4, 2: magic, 3: {1: 1, 2: 4}}`; the frame form is [Framing.authFrame] (flag 0x00). */
    fun authPayload(magic: Int, phoneType: Int = PHONE_ANDROID): ByteArray =
        devicePackage(CMD_AUTH, magic, varintField(1, 1) + varintField(2, phoneType))

    /** `{1: 5, 2: m, 4: {1: role}}` 1 RIGHT / 2 LEFT (LIVE). */
    fun roleChange(magic: Int, role: Int = ROLE_RIGHT): ByteArray {
        require(role in 0..2) { "role must be one of [0, 1, 2], got $role" }
        return devicePackage(CMD_ROLE_CHANGE, magic, varintField(1, role))
    }

    /** UTC offset in seconds -> 15-minute units, truncated toward zero, clamped -128..127. */
    fun tzUnitsFromOffset(offsetSeconds: Int): Int = (offsetSeconds / (TZ_UNIT_MINUTES * 60)).coerceIn(TZ_MIN, TZ_MAX)

    fun localTzUnits(unixS: Long = System.currentTimeMillis() / 1000, zone: TimeZone = TimeZone.getDefault()): Int =
        tzUnitsFromOffset(zone.getOffset(unixS * 1000) / 1000)

    /** `{1: 128, 2: m, 128: {1: unix_s (uint32), 2: tz int32 15-min units}}` (LIVE). Re-send hourly. */
    fun timeSync(magic: Int, unixS: Long? = null, tzUnits: Int? = null): ByteArray {
        val secs = unixS ?: (System.currentTimeMillis() / 1000)
        require(secs in 0..0xFFFFFFFFL) { "unix seconds out of uint32 range: $secs" }
        val tz = tzUnits ?: localTzUnits(secs)
        require(tz in TZ_MIN..TZ_MAX) { "tz_units must be in $TZ_MIN..$TZ_MAX, got $tz" }
        val inner = varintField(1, secs) + encodeKey(2, 0) + encodeVarint(tz.toLong())
        return devicePackage(CMD_TIME_SYNC, magic, inner)
    }

    /** `{1: 14, 2: m, 13: {}}`; flag 0x00; every 60 s LEFT first, RIGHT 2 s later; the right lens echoes it (LIVE). */
    fun keepalive(magic: Int): ByteArray = devicePackage(CMD_KEEPALIVE, magic, ByteArray(0))

    fun keepaliveFrame(magic: Int, seq: Int): ByteArray = Framing.framePb(keepalive(magic), SID_DEVICE, Framing.FLAG_PLAIN, seq)[0]

    /** `{1: 2, 2: 156, 4: {3: {2: {2: {1: 0, 2: 0}}}}}`: an empty schedule widget with fixed magic 156. */
    fun preludePayload(): ByteArray {
        val f4 = messageField(3, messageField(2, messageField(2, varintField(1, 0) + varintField(2, 0))))
        return varintField(1, 2) + varintField(2, PRELUDE_MAGIC) + messageField(4, f4)
    }

    val PRELUDE_PAYLOAD: ByteArray get() = preludePayload()
    val PRELUDE_FRAME: ByteArray get() = Framing.framePb(preludePayload(), SID_DASHBOARD, Framing.FLAG_REQUEST, PRELUDE_SEQ)[0]

    /** `{1: 12, 2: m, 14: {1: 0}}` on SID 0xE0 every 4 s, fire-and-forget (LIVE). */
    fun evenhubHeartbeat(magic: Int): ByteArray =
        varintField(1, EVENHUB_HEARTBEAT_CMD) + varintField(2, magic) + messageField(14, varintField(1, 0))

    /** SID 0x0D `{1: 0, 2: m}` (LIVE); the glasses then push `{3: {1 bg, 2 fg}}`. */
    fun appSyncSubscribe(magic: Int): ByteArray = varintField(1, 0) + varintField(2, magic)

    fun onboardingConfig(magic: Int, processId: Int = OB_FINISH): ByteArray {
        require(processId in 1..4) { "process_id must be one of [1, 2, 3, 4], got $processId" }
        return varintField(1, OB_CMD_CONFIG) + varintField(2, magic) + messageField(3, varintField(1, processId))
    }

    fun onboardingFinish(magic: Int): ByteArray = onboardingConfig(magic, OB_FINISH)

    fun wearQuery(magic: Int): ByteArray = varintField(1, OB_CMD_EVENT) + varintField(2, magic) + messageField(5, varintField(1, OB_EVENT_WEAR_STATUS))

    fun caseQuery(magic: Int): ByteArray = varintField(1, CASE_CMD_INFO) + varintField(2, magic) + messageField(3, ByteArray(0))

    data class SeqStep(val sid: Int, val payload: ByteArray, val side: Side)

    /** The official app's post-auth housekeeping, 150 ms apart, all RIGHT. */
    fun connectSequence(magicFn: () -> Int, now: Long? = null, tzUnits: Int? = null, full: Boolean = false,
                        notificationsEnable: Boolean = true, dispTime: Int = 5): List<SeqStep> {
        val seq = ArrayList<SeqStep>()
        fun add(sid: Int, p: ByteArray) = seq.add(SeqStep(sid, p, Side.RIGHT))
        add(SID_DEVICE, roleChange(magicFn(), ROLE_RIGHT))
        add(SID_DEVICE, timeSync(magicFn(), now, tzUnits))
        if (full) {
            add(SID_EVEN_AI, EvenAi.configCaptured(magicFn()))
            add(SID_QUICKLIST, Dashboard.quicklistEmpty(magicFn()))
            add(SID_APP_SYNC, appSyncSubscribe(magicFn()))
        }
        add(SID_SETTINGS, Settings.readSettings(magicFn()))
        add(SID_SETTINGS, Settings.units(magicFn()))
        add(SID_ONBOARDING, onboardingFinish(magicFn()))
        add(SID_ONBOARDING, wearQuery(magicFn()))
        if (full) add(SID_WATCHFACE, Dashboard.watchfaceInfoRequest(magicFn()))
        add(SID_CASE, caseQuery(magicFn()))
        if (full) {
            add(SID_NOTIFICATION, varintField(1, 1) + varintField(2, magicFn()) +
                messageField(3, varintField(1, 1) + varintField(2, 0) + varintField(3, 5) + varintField(5, 0)))
        }
        add(SID_SETTINGS, Settings.gestures(magicFn()))
        if (notificationsEnable) {
            add(SID_NOTIFICATION, Notifications.notificationCtrl(magicFn(), dispTime = dispTime))
            add(SID_NOTIFICATION, Notifications.whitelistDisable(magicFn()))
        }
        return seq
    }

    fun keepaliveSteps(magicFn: () -> Int): List<SeqStep> =
        listOf(SeqStep(SID_DEVICE, keepalive(magicFn()), Side.LEFT), SeqStep(SID_DEVICE, keepalive(magicFn()), Side.RIGHT))

    // ── parsers ────────────────────────────────────────────────────────────────────────

    /** The hub's check: >= 6 bytes and ending `1a 00`. */
    fun parseAuthReply(payload: ByteArray): Boolean =
        payload.size >= 6 && payload[payload.size - 2].toInt() == 0x1A && payload[payload.size - 1].toInt() == 0x00

    data class DeviceMessage(val cmd: Int, val magic: Int, var result: Int = 0, var authenticated: Boolean? = null, var timestamp: Long? = null,
                             var timezone: Long? = null, var role: Int? = null, var failedCmd: Int? = null) {
        val name: String get() = DEV_CMD_NAMES[cmd] ?: "cmd=$cmd"
        val isKeepaliveEcho: Boolean get() = cmd == CMD_KEEPALIVE
    }

    fun parseDevice(payload: ByteArray): DeviceMessage {
        val m = DeviceMessage(Pb.readInt(payload, 1), Pb.readInt(payload, 2))
        val fld = DEV_FIELD[m.cmd]
        val sub = if (fld != null) Pb.readBytes(payload, fld) else null
        if (sub != null) {
            DEV_RESULT_FIELD[m.cmd]?.let { m.result = Pb.readInt(sub, it, 0) }
            when {
                m.cmd == CMD_AUTH -> m.authenticated = Pb.readVarint(sub, 1, 0) == 1L
                m.cmd == CMD_TIME_SYNC && sub.isNotEmpty() -> { m.timestamp = Pb.readVarint(sub, 1, 0); m.timezone = Pb.readSigned(sub, 2, 0) }
                m.cmd == CMD_ROLE_CHANGE && sub.isNotEmpty() -> m.role = Pb.readInt(sub, 1, 0)
                m.cmd == CMD_EXCEPTION -> m.failedCmd = Pb.readInt(sub, 1, 0)
            }
        }
        return m
    }

    data class OnboardingMessage(val cmd: Int, val magic: Int, var processId: Int? = null, var event: Int? = null, var eventParam: Int? = null,
                                 var error: Int = 0) {
        val worn: Boolean? get() = if (event != OB_EVENT_WEAR_STATUS) null else eventParam == 1
    }

    fun parseOnboarding(payload: ByteArray): OnboardingMessage {
        val m = OnboardingMessage(Pb.readInt(payload, 1), Pb.readInt(payload, 2))
        Pb.readBytes(payload, 3)?.let { m.processId = Pb.readInt(it, 1, 0); m.error = Pb.readInt(it, 2, 0) }
        Pb.readBytes(payload, 4)?.let { m.error = Pb.readInt(it, 1, 0) }
        Pb.readBytes(payload, 5)?.let { m.event = Pb.readInt(it, 1, 0); m.eventParam = Pb.readInt(it, 2, 0); m.error = Pb.readInt(it, 3, 0) }
        return m
    }

    fun parseWearReply(payload: ByteArray): Boolean? = parseOnboarding(payload).worn

    data class CaseInfo(val magic: Int, val battery: Int?, val charging: Int?, val lid: Int?, val inCase: Int?, val error: Int)

    fun parseCase(payload: ByteArray): CaseInfo? {
        val info = Pb.readBytes(payload, 3) ?: return null
        fun opt(f: Int): Int? = Pb.readVarint(info, f, -1).let { if (it < 0) null else it.toInt() }
        return CaseInfo(Pb.readInt(payload, 2), opt(1), opt(2), opt(3), opt(4), Pb.readInt(info, 5, 0))
    }

    data class AppSync(val cmd: Int, val magic: Int, var background: Int? = null, var foreground: Int? = null)

    fun parseAppSync(payload: ByteArray): AppSync {
        val m = AppSync(Pb.readInt(payload, 1), Pb.readInt(payload, 2))
        Pb.readBytes(payload, 3)?.let { m.background = Pb.readInt(it, 1, 0); m.foreground = Pb.readInt(it, 2, 0) }
        return m
    }

    fun parseSnapshot(payload: ByteArray): Settings.Snapshot? = Settings.parse(payload).snapshot
}

/** What a step waits for after writing. */
sealed class Expect {
    /** a SID 0x80 reply ending `1a 00` on the step's side */
    object Auth : Expect()
    /** reply on (sid, magic) from the step's side (RIGHT for BOTH) */
    object Ack : Expect()
    object Echo : Expect()
    object Reply : Expect()
    /** like Ack but the SID 0x09 reply must carry a settings snapshot */
    object Snapshot : Expect()
    data class Sid(val sid: Int) : Expect()
    class Match(val pred: (ParsedFrame) -> Boolean) : Expect()

    val label: String
        get() = when (this) {
            Auth -> "auth"; Ack -> "ack"; Echo -> "echo"; Reply -> "reply"; Snapshot -> "snapshot"; is Sid -> "sid"; is Match -> "match"
        }

    companion object {
        fun of(s: String?): Expect? = when (s) {
            null -> null; "auth" -> Auth; "ack" -> Ack; "echo" -> Echo; "reply" -> Reply; "snapshot" -> Snapshot
            else -> throw IllegalArgumentException("unknown expect $s")
        }
    }
}

enum class StepAction { SEND, START_HEARTBEAT, STOP_HEARTBEAT }

/** One step of a plan; for SEND the frames are written in order to [side], then [expect] is awaited, then [waitMs] slept. */
data class Step(
    val label: String,
    val action: StepAction = StepAction.SEND,
    val side: Side = Side.RIGHT,
    val sid: Int? = null,
    val payload: ByteArray? = null,
    val flag: Int = Framing.FLAG_REQUEST,
    val magic: Int? = null,
    val frames: List<ByteArray> = emptyList(),
    val waitMs: Long = 0,
    val expect: Expect? = null,
    val timeoutMs: Long = 3000,
    val number: Int = 0,
) {
    val frame: ByteArray? get() = frames.firstOrNull()
}

/**
 * Steps 1-18 of native-protocol.md section 8 (port of session.MinimalDashboardPlan). [steps] consumes magics/seqs from
 * [alloc] on every call: call it once per connection.
 */
class MinimalDashboardPlan(
    val alloc: MagicAllocator,
    val now: Long? = null,
    val tzUnits: Int? = null,
    val temperatureC: Float = 21f,
    val weatherType: Int = 1,
    val weatherStatus: String = "Sunny",
    val temperatureUnit: Int = Dashboard.TEMP_CELSIUS,
    val timeFormat12h: Boolean = true,
    val dispTime: Int = 5,
    val authMagicStart: Int = 1,
) {
    private fun send(number: Int, label: String, sid: Int, payload: ByteArray, magic: Int?, side: Side = Side.RIGHT, waitMs: Long = 150,
                     expect: Expect? = null, timeoutMs: Long = 3000, flag: Int? = null): Step {
        val f = flag ?: Session.flagFor(sid, payload)
        return Step(label, StepAction.SEND, side, sid, payload, f, magic, Framing.framePb(payload, sid, f, alloc.nextSeq()), waitMs, expect,
            timeoutMs, number)
    }

    fun steps(): List<Step> {
        val a = alloc
        val nowS = now ?: (System.currentTimeMillis() / 1000)
        val tz = tzUnits ?: Session.localTzUnits(nowS)
        val s = ArrayList<Step>()
        var m = authMagicStart
        s.add(Step("auth R", StepAction.SEND, Side.RIGHT, Session.SID_DEVICE, Session.authPayload(m), Framing.FLAG_PLAIN, m,
            listOf(Framing.authFrame(m)), 0, Expect.Auth, 5000, 1))
        m += 1
        s.add(Step("auth L", StepAction.SEND, Side.LEFT, Session.SID_DEVICE, Session.authPayload(m), Framing.FLAG_PLAIN, m,
            listOf(Framing.authFrame(m)), 300, null, 0, 2))
        s.add(Step("prelude", StepAction.SEND, Side.RIGHT, Session.SID_DASHBOARD, Session.PRELUDE_PAYLOAD, Framing.FLAG_REQUEST,
            Session.PRELUDE_MAGIC, listOf(Session.PRELUDE_FRAME), 1500, null, 0, 3))
        s.add(Step("start EvenHub heartbeat (4 s)", StepAction.START_HEARTBEAT, Side.RIGHT, Session.SID_EVENHUB, number = 4))
        s.add(Step("prelude again", StepAction.SEND, Side.RIGHT, Session.SID_DASHBOARD, Session.PRELUDE_PAYLOAD, Framing.FLAG_REQUEST,
            Session.PRELUDE_MAGIC, listOf(Session.PRELUDE_FRAME), 1500, null, 0, 5))
        var mm = a.nextMagic()
        s.add(send(6, "dashboard init", Session.SID_DASHBOARD, Dashboard.dashboardInit(mm), mm, waitMs = 0, expect = Expect.Ack, timeoutMs = 4000))
        mm = a.nextMagic(); s.add(send(7, "0x0D subscribe", Session.SID_APP_SYNC, Session.appSyncSubscribe(mm), mm))
        mm = a.nextMagic(); s.add(send(8, "role RIGHT", Session.SID_DEVICE, Session.roleChange(mm, Session.ROLE_RIGHT), mm))
        mm = a.nextMagic(); s.add(send(9, "time sync", Session.SID_DEVICE, Session.timeSync(mm, nowS, tz), mm))
        mm = a.nextMagic(); s.add(send(10, "settings READ", Session.SID_SETTINGS, Settings.readSettings(mm), mm, expect = Expect.Snapshot))
        mm = a.nextMagic()
        s.add(send(11, "units", Session.SID_SETTINGS, Settings.units(mm, timeFormat = if (timeFormat12h) 1 else 0, temperatureUnit = temperatureUnit), mm))
        mm = a.nextMagic(); s.add(send(12, "onboarding FINISH", Session.SID_ONBOARDING, Session.onboardingFinish(mm), mm))
        mm = a.nextMagic(); s.add(send(13, "wear query", Session.SID_ONBOARDING, Session.wearQuery(mm), mm, expect = Expect.Reply))
        mm = a.nextMagic(); s.add(send(14, "case query", Session.SID_CASE, Session.caseQuery(mm), mm, expect = Expect.Reply))
        mm = a.nextMagic(); s.add(send(15, "gestures default", Session.SID_SETTINGS, Settings.gestures(mm), mm))
        mm = a.nextMagic(); s.add(send(16, "notification CTRL", Session.SID_NOTIFICATION, Notifications.notificationCtrl(mm, dispTime = dispTime), mm))
        mm = a.nextMagic(); s.add(send(16, "whitelist off", Session.SID_NOTIFICATION, Notifications.whitelistDisable(mm), mm))
        mm = a.nextMagic()
        val w = Dashboard.weather(mm, Dashboard.Weather(temperatureC, temperatureUnit, weatherType, nowS * 1000, weatherStatus))
        s.add(send(17, "weather", Session.SID_DASHBOARD, w, mm, waitMs = 0, expect = Expect.Ack, timeoutMs = 4000))
        s.add(Step("stop EvenHub heartbeat (dashboard shows in ~5 s)", StepAction.STOP_HEARTBEAT, Side.RIGHT, Session.SID_EVENHUB, number = 18))
        return s
    }
}

/**
 * The phone-hub startup plan run by [G2Session.startup]: MinimalDashboardPlan steps 6-16 with the gateway's auth order
 * (client.py `_run_init_sequence`: auth LEFT fire-and-forget, then auth RIGHT awaited) and, by default, NO EvenHub
 * prelude/heartbeat (we never show an EvenHub page, and the native dashboard only redraws pushed content while no
 * heartbeat runs). With [heartbeat] = true the prelude + heartbeat bracket of MinimalDashboardPlan is reproduced.
 * Optional extras at the end: weather push and the gateway's launch-menu push.
 */
class HubStartupPlan(
    val alloc: MagicAllocator,
    val now: Long? = null,
    val tzUnits: Int? = null,
    val heartbeat: Boolean = false,
    val weather: Dashboard.Weather? = null,
    val menu: List<Int>? = null,
    val temperatureUnit: Int = Dashboard.TEMP_CELSIUS,
    val timeFormat12h: Boolean = true,
    val dispTime: Int = 5,
    val notificationsEnable: Boolean = true,
) {
    private fun send(number: Int, label: String, sid: Int, payload: ByteArray, magic: Int?, waitMs: Long = Session.CONNECT_SEQUENCE_GAP_MS,
                     expect: Expect? = null, timeoutMs: Long = 3000): Step {
        val f = Session.flagFor(sid, payload)
        return Step(label, StepAction.SEND, Side.RIGHT, sid, payload, f, magic, Framing.framePb(payload, sid, f, alloc.nextSeq()), waitMs, expect,
            timeoutMs, number)
    }

    fun steps(): List<Step> {
        val a = alloc
        val nowS = now ?: (System.currentTimeMillis() / 1000)
        val tz = tzUnits ?: Session.localTzUnits(nowS)
        val s = ArrayList<Step>()
        var m = a.nextMagic()
        s.add(Step("auth L", StepAction.SEND, Side.LEFT, Session.SID_DEVICE, Session.authPayload(m), Framing.FLAG_PLAIN, m,
            listOf(Framing.authFrame(m, a.nextSeq())), 200, null, 0, 1))
        m = a.nextMagic()
        s.add(Step("auth R", StepAction.SEND, Side.RIGHT, Session.SID_DEVICE, Session.authPayload(m), Framing.FLAG_PLAIN, m,
            listOf(Framing.authFrame(m, a.nextSeq())), Session.SETTLE_MS, Expect.Auth, 5000, 2))
        if (heartbeat) {
            s.add(Step("prelude", StepAction.SEND, Side.RIGHT, Session.SID_DASHBOARD, Session.PRELUDE_PAYLOAD, Framing.FLAG_REQUEST,
                Session.PRELUDE_MAGIC, listOf(Session.PRELUDE_FRAME), Session.PRELUDE_DRAIN_MS, null, 0, 3))
            s.add(Step("start EvenHub heartbeat (4 s)", StepAction.START_HEARTBEAT, Side.RIGHT, Session.SID_EVENHUB, number = 4))
            s.add(Step("prelude again", StepAction.SEND, Side.RIGHT, Session.SID_DASHBOARD, Session.PRELUDE_PAYLOAD, Framing.FLAG_REQUEST,
                Session.PRELUDE_MAGIC, listOf(Session.PRELUDE_FRAME), Session.PRELUDE_DRAIN_MS, null, 0, 5))
        }
        var mm = a.nextMagic()
        s.add(send(6, "dashboard init", Session.SID_DASHBOARD, Dashboard.dashboardInit(mm), mm, waitMs = 0, expect = Expect.Ack, timeoutMs = 4000))
        mm = a.nextMagic(); s.add(send(7, "0x0D subscribe", Session.SID_APP_SYNC, Session.appSyncSubscribe(mm), mm))
        mm = a.nextMagic(); s.add(send(8, "role RIGHT", Session.SID_DEVICE, Session.roleChange(mm, Session.ROLE_RIGHT), mm))
        mm = a.nextMagic(); s.add(send(9, "time sync", Session.SID_DEVICE, Session.timeSync(mm, nowS, tz), mm))
        mm = a.nextMagic(); s.add(send(10, "settings READ", Session.SID_SETTINGS, Settings.readSettings(mm), mm, expect = Expect.Snapshot))
        mm = a.nextMagic()
        s.add(send(11, "units", Session.SID_SETTINGS, Settings.units(mm, timeFormat = if (timeFormat12h) 1 else 0, temperatureUnit = temperatureUnit), mm))
        mm = a.nextMagic(); s.add(send(12, "onboarding FINISH", Session.SID_ONBOARDING, Session.onboardingFinish(mm), mm))
        mm = a.nextMagic(); s.add(send(13, "wear query", Session.SID_ONBOARDING, Session.wearQuery(mm), mm, expect = Expect.Reply))
        mm = a.nextMagic(); s.add(send(14, "case query", Session.SID_CASE, Session.caseQuery(mm), mm, expect = Expect.Reply))
        mm = a.nextMagic(); s.add(send(15, "gestures default", Session.SID_SETTINGS, Settings.gestures(mm), mm))
        if (notificationsEnable) {
            mm = a.nextMagic(); s.add(send(16, "notification CTRL", Session.SID_NOTIFICATION, Notifications.notificationCtrl(mm, dispTime = dispTime), mm))
            mm = a.nextMagic(); s.add(send(16, "whitelist off", Session.SID_NOTIFICATION, Notifications.whitelistDisable(mm), mm))
        }
        if (weather != null) {
            mm = a.nextMagic()
            val w = if (weather.updateTimeMs == 0L) weather.copy(updateTimeMs = nowS * 1000) else weather
            s.add(send(17, "weather", Session.SID_DASHBOARD, Dashboard.weather(mm, w), mm, waitMs = 0, expect = Expect.Ack, timeoutMs = 4000))
        }
        if (menu != null) {
            mm = a.nextMagic()
            s.add(send(19, "menu", Session.SID_MENU, Menu.menuPush(mm, menu, strict = true), mm, expect = Expect.Ack, timeoutMs = 4000))
        }
        if (heartbeat) {
            s.add(Step("stop EvenHub heartbeat (dashboard shows in ~5 s)", StepAction.STOP_HEARTBEAT, Side.RIGHT, Session.SID_EVENHUB, number = 18))
        }
        return s
    }
}
