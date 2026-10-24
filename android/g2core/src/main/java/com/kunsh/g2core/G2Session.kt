package com.kunsh.g2core

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import kotlin.coroutines.cancellation.CancellationException

/**
 * Drive the native firmware through any [G2Transport] (port of g2/native/transport.py `Glasses`).
 *
 * [start] begins reading frames (re-assembled per arm, routed to waiters, listeners and [frames]); [send] writes and
 * optionally waits for the matching reply on (sid, magic); [startup] runs [HubStartupPlan]; the helpers reproduce the
 * hub's push patterns. The EvenHub heartbeat is OFF by default (we never show an EvenHub page, and the native dashboard
 * only redraws pushed content while no heartbeat is being sent); set [Config.heartbeatForPushes] to bracket pushes with
 * it like the Python `Glasses` does.
 */
/** startup steps that would change what the owner configured in Even's app */
val G2_USER_SETTING_STEPS = setOf("dashboard init", "units", "onboarding FINISH", "gestures default")

class G2Session(
    private val transport: G2Transport,
    parentScope: CoroutineScope,
    val alloc: MagicAllocator = MagicAllocator(),
    /** wall clock, milliseconds since the epoch */
    private val clockMs: () -> Long = System::currentTimeMillis,
    /** fixed time zone in 15-minute units; null = this device's zone */
    private val tzUnits: Int? = null,
    val config: Config = Config(),
    private val log: (String) -> Unit = {},
) {
    data class Config(
        /** Leave what the owner set in Even's app alone on connect: skip dashboard init (widget layout),
         *  units, onboarding FINISH and "gestures default" (would overwrite long-press settings). Default on. */
        val preserveUserSettings: Boolean = true,
        /** start/stop the EvenHub heartbeat around dashboard pushes (Python Glasses behaviour). Default off. */
        val heartbeatForPushes: Boolean = false,
        /** run the prelude + heartbeat bracket during [startup] (MinimalDashboardPlan steps 3-5/18). Default off. */
        val startupHeartbeat: Boolean = false,
        /** start the 60 s keep-alive loop after a successful [startup] */
        val keepalive: Boolean = true,
        /** weather pushed at the end of [startup], or null */
        val startupWeather: Dashboard.Weather? = null,
        /** built-in menu ids pushed at the end of [startup] (the gateway sends its launch menu), or null */
        val startupMenu: List<Int>? = null,
        val notificationDispTime: Int = 5,
        /** multiply every protocol sleep (tests use 0.01); reply timeouts are not scaled */
        val timeScale: Double = 1.0,
    )

    private val scope = CoroutineScope(parentScope.coroutineContext + SupervisorJob(parentScope.coroutineContext[Job]))
    private val asm = mapOf(Side.LEFT to FrameAssembler(), Side.RIGHT to FrameAssembler())

    private class Waiter(val pred: (Side, ParsedFrame) -> Boolean, val result: CompletableDeferred<ParsedFrame>)

    private val waiters = CopyOnWriteArrayList<Waiter>()
    private val listeners = CopyOnWriteArrayList<(Side, ParsedFrame) -> Unit>()
    private val _frames = MutableSharedFlow<Pair<Side, ParsedFrame>>(extraBufferCapacity = 256)

    /** Every inbound (re-assembled) frame with its arm. */
    val frames: SharedFlow<Pair<Side, ParsedFrame>> = _frames.asSharedFlow()

    /** Raw 0x6402 packets from the left arm (five 40-byte LC3 frames + trailer each). */
    val micPackets: Flow<EvenAi.MicPacket> =
        transport.inbound.filter { it.char == G2Char.MIC && it.side == Side.LEFT }.map { EvenAi.parseMicPacket(it.data) }.filter { it != null }
            .map { it!! }

    private var reader: Job? = null
    private var hb: Job? = null
    private var ka: Job? = null

    @Volatile var hbCount = 0
        private set
    @Volatile var lastKeepaliveEchoMs: Long? = null
        private set
    @Volatile var missedKeepalives = 0
        private set
    @Volatile var snapshot: Settings.Snapshot? = null
        private set

    /** Live status merged from snapshots / case / wear / app-sync / module replies (battery, worn, case_battery, ...). */
    val status: MutableMap<String, Any> = ConcurrentHashMap()

    /** Called when the keep-alive watchdog sees two missed right-arm echoes. */
    @Volatile var onLinkLost: ((String) -> Unit)? = null

    private suspend fun sleepMs(ms: Long) {
        if (ms > 0) delay((ms * config.timeScale).toLong().coerceAtLeast(1))
    }

    // ── lifecycle ──────────────────────────────────────────────────────────────────────

    @Synchronized
    fun start() {
        if (reader != null) return
        reader = scope.launch(start = CoroutineStart.UNDISPATCHED) {
            transport.inbound.collect { inb ->
                if (inb.char == G2Char.MIC) return@collect
                val side = if (inb.side == Side.LEFT) Side.LEFT else Side.RIGHT
                val data = asm.getValue(side).feed(inb.data) ?: return@collect
                val p = Framing.parseFrame(data) ?: return@collect
                dispatch(side, p)
            }
        }
    }

    suspend fun stop() {
        stopHeartbeat()
        ka?.cancelAndJoin(); ka = null
        reader?.cancelAndJoin(); reader = null
    }

    /** Register [fn] for every inbound frame (called after waiters). */
    fun on(fn: (Side, ParsedFrame) -> Unit) { listeners.add(fn) }

    /** Forget partial fragments (call after an arm reconnects). */
    fun resetAssembly(side: Side) { if (side != Side.RIGHT) asm.getValue(Side.LEFT).reset(); if (side != Side.LEFT) asm.getValue(Side.RIGHT).reset() }

    private fun dispatch(side: Side, p: ParsedFrame) {
        track(side, p)
        for (w in waiters) {
            if (w.result.isCompleted) { waiters.remove(w); continue }
            val hit = try { w.pred(side, p) } catch (e: Exception) { false }
            if (hit) { waiters.remove(w); w.result.complete(p) }
        }
        for (fn in listeners) {
            try { fn(side, p) } catch (e: Exception) { log("listener error: $e") }
        }
        _frames.tryEmit(side to p)
    }

    private fun track(side: Side, p: ParsedFrame) {
        try {
            when (p.sid) {
                Session.SID_SETTINGS -> Settings.parse(p.payload).snapshot?.let { s -> snapshot = s; status.putAll(s.asMap()) }
                Session.SID_CASE -> Session.parseCase(p.payload)?.battery?.let { status["case_battery"] = it }
                Session.SID_ONBOARDING -> Session.parseWearReply(p.payload)?.let { status["worn"] = it }
                Session.SID_APP_SYNC -> Session.parseAppSync(p.payload).foreground?.let { status["foreground"] = it }
                Session.SID_DEVICE -> if (side == Side.RIGHT && Session.parseDevice(p.payload).isKeepaliveEcho) lastKeepaliveEchoMs = clockMs()
                Session.SID_MODULE -> Settings.parseModule(p.payload).timeout?.let { status["display_timeout"] = it }
            }
        } catch (e: Exception) {
            log("decode error sid=0x${Integer.toHexString(p.sid)}: $e")
        }
    }

    // ── waiting ────────────────────────────────────────────────────────────────────────

    private fun register(pred: (Side, ParsedFrame) -> Boolean): Waiter = Waiter(pred, CompletableDeferred()).also { waiters.add(it) }

    /** Wait for the next inbound frame matching [pred] (registered immediately; await the returned deferred). */
    fun expectFrame(pred: (Side, ParsedFrame) -> Boolean): CompletableDeferred<ParsedFrame> = register(pred).result

    companion object {
        /** Port of Glasses._predicate: the reply side is RIGHT for BOTH; (sid, magic) matched for ack/echo/snapshot/reply. */
        fun predicate(expect: Expect, side: Side, sid: Int?, magic: Int?): (Side, ParsedFrame) -> Boolean {
            val want = if (side == Side.BOTH) Side.RIGHT else side
            return when (expect) {
                is Expect.Match -> { _, p -> expect.pred(p) }
                is Expect.Sid -> { _, p -> p.sid == expect.sid }
                Expect.Auth -> { s, p -> s == want && p.sid == Session.SID_DEVICE && Session.parseAuthReply(p.payload) }
                Expect.Ack, Expect.Echo, Expect.Reply, Expect.Snapshot -> { s, p ->
                    s == want && p.sid == sid && (magic == null || Framing.readAckMagic(p.payload) == magic) &&
                        (expect != Expect.Snapshot || Settings.parse(p.payload).snapshot != null)
                }
            }
        }
    }

    /**
     * Write [frames] (fragments 50 ms apart) to [side] and, when [expect] is given, wait up to [timeoutMs] for the matching
     * reply. sid/magic default to those of fragment 1 (fields 1-2 sit at its start even when the message is fragmented).
     * Returns the reply or null on timeout.
     */
    suspend fun send(side: Side, frames: List<ByteArray>, expect: Expect? = null, timeoutMs: Long = 3000, sid: Int? = null,
                     magic: Int? = null, char: G2Char = G2Char.COMMAND): ParsedFrame? {
        val first = frames.firstOrNull()?.let { Framing.parseFrame(it) }
        val s = sid ?: first?.sid
        val m = magic ?: first?.let { Framing.readAckMagic(it.payload) }
        val w = expect?.let { register(predicate(it, side, s, m)) }
        try {
            frames.forEachIndexed { n, f ->
                if (n > 0) sleepMs(Session.PACKET_GAP_MS)
                transport.write(side, f, char)
            }
            if (w == null) return null
            val r = withTimeoutOrNull(timeoutMs) { w.result.await() }
            if (r == null) log("no reply (sid=0x${Integer.toHexString(s ?: 0)} magic=$m expect=${expect.label})")
            return r
        } finally {
            if (w != null) { waiters.remove(w); w.result.cancel() }
        }
    }

    /** Frame [payload] for [sid] with the app's flag rule and send. BOTH = left fire-and-forget, then right (awaited). */
    suspend fun sendPb(sid: Int, payload: ByteArray, side: Side = Side.RIGHT, expect: Expect? = null, timeoutMs: Long = 3000,
                       flag: Int? = null): ParsedFrame? {
        val f = flag ?: Session.flagFor(sid, payload)
        val mw = transport.maxWrite
        if (side == Side.BOTH) {
            send(Side.LEFT, Framing.framePb(payload, sid, f, alloc.nextSeq(), mw))
            return send(Side.RIGHT, Framing.framePb(payload, sid, f, alloc.nextSeq(), mw), expect, timeoutMs, sid)
        }
        return send(side, Framing.framePb(payload, sid, f, alloc.nextSeq(), mw), expect, timeoutMs, sid)
    }

    /** Send to the right arm and wait for the reply echoing (sid, magic). */
    suspend fun request(sid: Int, payload: ByteArray, timeoutMs: Long = 4000): ParsedFrame? = sendPb(sid, payload, Side.RIGHT, Expect.Ack, timeoutMs)

    // ── EvenHub heartbeat ──────────────────────────────────────────────────────────────

    /** (Re)start the SID 0xE0 heartbeat every 4 s. Returns true if started now. */
    @Synchronized
    fun startHeartbeat(): Boolean {
        if (hb?.isActive == true) return false
        hb = scope.launch {
            while (isActive) {
                try {
                    sendPb(Session.SID_EVENHUB, Session.evenhubHeartbeat(alloc.nextMagic()))
                    hbCount++
                } catch (e: CancellationException) {
                    throw e
                } catch (e: Exception) {
                    log("heartbeat write failed: $e")
                }
                sleepMs(Session.HEARTBEAT_INTERVAL_MS)
            }
        }
        return true
    }

    /** Stop it: the native dashboard takes the screen back ~5 s later. */
    @Synchronized
    fun stopHeartbeat(): Boolean {
        val t = hb
        hb = null
        if (t == null || !t.isActive) return false
        t.cancel()
        return true
    }

    val heartbeatRunning: Boolean get() = hb?.isActive == true

    /** Start the heartbeat if needed and give the glasses 2 s to see it. */
    suspend fun ensureHeartbeat(settleMs: Long = 2000) { if (startHeartbeat()) sleepMs(settleMs) }

    private suspend fun pushBegin(): Boolean = if (config.heartbeatForPushes) { ensureHeartbeat(); true } else false
    private fun pushEnd(started: Boolean, show: Boolean) { if (started && show) stopHeartbeat() }

    // ── plans ──────────────────────────────────────────────────────────────────────────

    suspend fun runStep(st: Step): ParsedFrame? {
        when (st.action) {
            StepAction.START_HEARTBEAT -> { startHeartbeat(); sleepMs(st.waitMs); return null }
            StepAction.STOP_HEARTBEAT -> { stopHeartbeat(); sleepMs(st.waitMs); return null }
            StepAction.SEND -> {
                val reply = send(st.side, st.frames, st.expect, st.timeoutMs, st.sid, st.magic)
                sleepMs(st.waitMs)
                return reply
            }
        }
    }

    /** Execute a plan; a step that expected a reply and got none has a null reply. */
    suspend fun runPlan(plan: List<Step>, stopOnFailure: Boolean = false): List<Pair<Step, ParsedFrame?>> {
        val out = ArrayList<Pair<Step, ParsedFrame?>>()
        for (st in plan) {
            val r = runStep(st)
            out.add(st to r)
            log("step ${st.number} ${st.label}: ${if (r != null) "reply" else if (st.expect != null) "no reply" else "sent"}")
            if (stopOnFailure && st.expect != null && r == null) break
        }
        return out
    }

    data class StartupResult(val steps: List<Pair<Step, ParsedFrame?>>) {
        val authenticated: Boolean get() = steps.firstOrNull { it.first.expect == Expect.Auth }?.second != null
        val missing: List<Step> get() = steps.filter { it.first.expect != null && it.second == null }.map { it.first }
        val ok: Boolean get() = missing.isEmpty()
    }

    fun startupPlan(): List<Step> = HubStartupPlan(alloc, clockMs() / 1000, tzUnits, config.startupHeartbeat, config.startupWeather,
        config.startupMenu, dispTime = config.notificationDispTime).steps()
        .filter { !config.preserveUserSettings || it.label !in G2_USER_SETTING_STEPS }

    /**
     * The startup sequence for a fresh connection of both arms: auth L, auth R, dashboard init, 0x0D subscribe, role, time
     * sync, settings READ, units, onboarding FINISH, wear, case, gestures, notification CTRL + whitelist off (+ weather /
     * menu when configured). Stops at a failed auth. Starts the keep-alive loop when [Config.keepalive].
     */
    suspend fun startup(): StartupResult {
        start()
        resetAssembly(Side.BOTH)
        val plan = startupPlan()
        val results = ArrayList<Pair<Step, ParsedFrame?>>()
        for (st in plan) {
            val r = runStep(st)
            results.add(st to r)
            log("startup ${st.number} ${st.label}: ${if (r != null) "reply" else if (st.expect != null) "no reply" else "sent"}")
            if (st.expect == Expect.Auth && r == null) break      // no point pushing to an arm that did not authenticate
        }
        if (heartbeatRunning && !config.heartbeatForPushes) stopHeartbeat()
        val res = StartupResult(results)
        if (res.authenticated && config.keepalive) startKeepalive()
        return res
    }

    /** Minimal re-init after one arm reconnected (port of client.py `_run_side_reconnect_init`, native builders). */
    suspend fun reinitSide(side: Side): Boolean {
        resetAssembly(side)
        if (side == Side.LEFT) {
            val m = alloc.nextMagic()
            send(Side.LEFT, listOf(Framing.authFrame(m, alloc.nextSeq())))
            return true
        }
        val m = alloc.nextMagic()
        val ok = send(Side.RIGHT, listOf(Framing.authFrame(m, alloc.nextSeq())), Expect.Auth, 5000) != null
        sleepMs(Session.SETTLE_MS)
        val now = clockMs() / 1000
        for (st in Session.connectSequence(alloc::nextMagic, now, tzUnits, dispTime = config.notificationDispTime)) {
            sendPb(st.sid, st.payload, st.side)
            sleepMs(Session.CONNECT_SEQUENCE_GAP_MS)
        }
        return ok
    }

    /** The captured post-auth housekeeping (Session.connectSequence), 150 ms apart. */
    suspend fun connectSequence(full: Boolean = false) {
        for (st in Session.connectSequence(alloc::nextMagic, clockMs() / 1000, tzUnits, full)) {
            sendPb(st.sid, st.payload, st.side)
            sleepMs(Session.CONNECT_SEQUENCE_GAP_MS)
        }
    }

    // ── settings ───────────────────────────────────────────────────────────────────────

    /** Settings READ basic -> Snapshot (battery, charging, height, distance, fw, head-up, ...). */
    suspend fun readSettings(timeoutMs: Long = 3000): Settings.Snapshot? {
        val r = sendPb(Session.SID_SETTINGS, Settings.readSettings(alloc.nextMagic()), Side.RIGHT, Expect.Snapshot, timeoutMs)
        return r?.let { Settings.parse(it.payload).snapshot }
    }

    /** Send one SET/module payload (built with [Settings], magic from [alloc]) and wait for its ack/echo. */
    suspend fun applySetting(payload: ByteArray, sid: Int = Session.SID_SETTINGS, timeoutMs: Long = 3000): ParsedFrame? =
        sendPb(sid, payload, Side.RIGHT, Expect.Ack, timeoutMs)

    /** Convenience: build with a fresh magic, e.g. `applySetting { Settings.brightness(it, 20) }`. */
    suspend fun applySetting(sid: Int = Session.SID_SETTINGS, timeoutMs: Long = 3000, build: (Int) -> ByteArray): ParsedFrame? =
        applySetting(build(alloc.nextMagic()), sid, timeoutMs)

    /** Hub "refresh status" burst: settings READ, wear, case, timeout READ, 150 ms apart. */
    suspend fun refreshStatus(): Map<String, Any> {
        sendPb(Session.SID_SETTINGS, Settings.readSettings(alloc.nextMagic())); sleepMs(Session.CONNECT_SEQUENCE_GAP_MS)
        sendPb(Session.SID_ONBOARDING, Session.wearQuery(alloc.nextMagic())); sleepMs(Session.CONNECT_SEQUENCE_GAP_MS)
        sendPb(Session.SID_CASE, Session.caseQuery(alloc.nextMagic())); sleepMs(Session.CONNECT_SEQUENCE_GAP_MS)
        sendPb(Settings.SID_MODULE, Settings.readDisplayTimeout(alloc.nextMagic())); sleepMs(Session.CONNECT_SEQUENCE_GAP_MS)
        sleepMs(500)
        return HashMap(status)
    }

    // ── dashboard pushes ───────────────────────────────────────────────────────────────

    private suspend fun dash(payload: ByteArray, timeoutMs: Long = 4000): Int? {
        val r = request(Session.SID_DASHBOARD, payload, timeoutMs) ?: return null
        return Dashboard.ackFlag(r.payload) ?: 0
    }

    /** Weather status bar (LIVE). Returns the ack flag (0 = success) or null. */
    suspend fun setWeather(temperatureC: Float, weatherType: Int = 0, status: String = "", fahrenheitDisplay: Boolean = false,
                           show: Boolean = true): Int? =
        setWeather(Dashboard.Weather(temperatureC, if (fahrenheitDisplay) Dashboard.TEMP_FAHRENHEIT else Dashboard.TEMP_CELSIUS, weatherType,
            clockMs(), status), show)

    suspend fun setWeather(weather: Dashboard.Weather, show: Boolean = true): Int? {
        val hbStarted = pushBegin()
        val w = if (weather.updateTimeMs == 0L) weather.copy(updateTimeMs = clockMs()) else weather
        val flag = dash(Dashboard.weather(alloc.nextMagic(), w))
        pushEnd(hbStarted, show)
        return flag
    }

    /** One message per event (or the "none" message), each awaited (LIVE). */
    suspend fun setCalendar(schedules: List<Dashboard.Schedule>, show: Boolean = true): List<Int?> {
        val hbStarted = pushBegin()
        val flags = Dashboard.calendarSequence(alloc::nextMagic, schedules).map { dash(it) }
        pushEnd(hbStarted, show)
        return flags
    }

    /** News V3 FIFO: reset -> count -> per slot (LIVE). Returns the newsStatus per slot. */
    suspend fun pushNews(items: List<Dashboard.NewsItem>, show: Boolean = true): List<Int?> {
        if (items.isEmpty()) return emptyList()
        val hbStarted = pushBegin()
        request(Session.SID_DASHBOARD, Dashboard.newsReset(alloc.nextMagic()))
        sleepMs(500)
        val r = request(Session.SID_DASHBOARD, Dashboard.newsFifoCountRequest(alloc.nextMagic()))
        val count = Dashboard.newsFifoCount(r?.payload)
        sleepMs(200)
        val now = clockMs() / 1000
        val tz = tzUnits ?: Session.localTzUnits(now)
        val reportMs = (now + tz * 900L) * 1000
        val out = ArrayList<Int?>()
        for (slot in 0 until count) {
            val it = items[slot % items.size]
            val rr = request(Session.SID_DASHBOARD, Dashboard.newsPush(alloc.nextMagic(), slot, it, reportMs, count))
            out.add(Dashboard.newsStatus(rr?.payload))
            sleepMs(150)
        }
        pushEnd(hbStarted, show)
        return out
    }

    /** Quicklist: init -> 300 ms -> FULL_UPDATE (echo) -> display-enable (LIVE). */
    suspend fun setTasks(tasks: List<Dashboard.Task>, show: Boolean = true): Boolean {
        val hbStarted = pushBegin()
        var ok = true
        Dashboard.tasksSequence(alloc::nextMagic, tasks).forEachIndexed { i, st ->
            val r = sendPb(st.sid, st.payload, Side.RIGHT, Expect.of(st.expect), 4000)
            ok = ok && r != null && (st.expect != "ack" || (Dashboard.ackFlag(r.payload) ?: 0) == 0)
            if (i == 0) sleepMs(300)
        }
        pushEnd(hbStarted, show)
        return ok
    }

    /** Stocks card (DECODED, never seen on glasses): stock display-enable, then one push per stock. Returns the ack flags. */
    suspend fun setStocks(stocks: List<Dashboard.Stock>, show: Boolean = true): List<Int?> {
        val hbStarted = pushBegin()
        val out = ArrayList<Int?>()
        out.add(dash(Dashboard.stockDisplayEnable(alloc.nextMagic())))
        stocks.forEachIndexed { i, s -> out.add(dash(Dashboard.stockPush(alloc.nextMagic(), s, stocks.size, i))) }
        pushEnd(hbStarted, show)
        return out
    }

    /** Whole-menu replacement (LIVE for built-ins); true when the cmd-1 reply echoed the magic. */
    suspend fun setMenu(builtinIds: List<Int> = Menu.DEFAULT_MENU, plugins: List<Menu.PluginItem> = emptyList(), timeoutMs: Long = 4000): Boolean =
        request(Menu.SID_MENU, Menu.menuPush(alloc.nextMagic(), builtinIds, plugins, strict = true), timeoutMs) != null

    // ── notifications ──────────────────────────────────────────────────────────────────

    suspend fun enableNotifications(dispTime: Int = 5) {
        sendPb(Session.SID_NOTIFICATION, Notifications.notificationCtrl(alloc.nextMagic(), dispTime = dispTime))
        sleepMs(Session.CONNECT_SEQUENCE_GAP_MS)
        sendPb(Session.SID_NOTIFICATION, Notifications.whitelistDisable(alloc.nextMagic()))
    }

    enum class NotifyForm { HUB, COMMUNITY }

    /**
     * Send a notification card. HUB = START/DATA/RESULT_CHECK with awaited `[cid, rsp]` replies (DECODED/UNTESTED);
     * COMMUNITY = the Jan-2026 community sequence, fire and forget with 0.3 s gaps, then a LEFT keep-alive (COMMUNITY).
     * Frames go to the EvenFile characteristic 0x7401 of the right arm.
     */
    suspend fun notify(title: String, message: String, subtitle: String = "", form: NotifyForm = NotifyForm.HUB, timeoutMs: Long = 8000,
                       displayName: String = "Even Realities", appIdentifier: String = "com.even.sg", msgId: Long? = null): Boolean {
        val data = Notifications.notificationJson(title, message, subtitle, displayName, appIdentifier, msgId, clockMs() / 1000,
            tzUnits?.let { it * 900 })
        if (form == NotifyForm.COMMUNITY) {
            val frames = Notifications.communityNotificationFrames(data)
            val gaps = listOf(300L, 100L) + List(frames.size - 3) { 50L } + listOf(300L)
            frames.zip(gaps).forEach { (f, g) -> transport.write(Side.RIGHT, f, G2Char.FILE); sleepMs(g) }
            sendPb(Session.SID_DEVICE, Session.keepalive(alloc.nextMagic()), Side.LEFT)
            return true
        }
        for (st in Notifications.hubNotificationSteps(alloc::nextSeq, data)) {
            var done = false
            for (attempt in 0 until 4) {
                val w = register { s, p ->
                    s == Side.RIGHT && (p.sid == Notifications.SID_FILE_CMD || p.sid == Notifications.SID_FILE_DATA) &&
                        Notifications.parseFileReply(p.payload)?.first == st.expectCid
                }
                var rsp: Int? = null
                try {
                    for (f in st.frames) { transport.write(Side.RIGHT, f, G2Char.FILE); sleepMs(10) }
                    rsp = withTimeoutOrNull(timeoutMs) { w.result.await() }?.let { Notifications.parseFileReply(it.payload)?.second }
                } finally {
                    waiters.remove(w); w.result.cancel()
                }
                if (rsp == 0) { done = true; break }
                if (st.name != "data" || (rsp != null && rsp !in Notifications.RSP_RETRY)) {
                    log("EFS ${st.name}: ${rsp?.let { Notifications.RSP_NAMES[it] ?: it.toString() } ?: "timeout"}")
                    return false
                }
                if (rsp == 5) sleepMs(5000)
            }
            if (!done) return false
        }
        return true
    }

    // ── keep-alive ─────────────────────────────────────────────────────────────────────

    /** LEFT keep-alive, 2 s, RIGHT keep-alive; true when the right arm echoed within 5 s. */
    suspend fun keepaliveOnce(): Boolean {
        sendPb(Session.SID_DEVICE, Session.keepalive(alloc.nextMagic()), Side.LEFT)
        sleepMs(Session.KEEPALIVE_LEFT_LEAD_MS)
        return sendPb(Session.SID_DEVICE, Session.keepalive(alloc.nextMagic()), Side.RIGHT, Expect.Echo, Session.KEEPALIVE_ECHO_WAIT_MS) != null
    }

    /** Every [intervalMs]: keep-alive both arms (left first); hourly time re-sync; two missed echoes -> [onLinkLost] and return. */
    suspend fun keepaliveLoop(intervalMs: Long = Session.KEEPALIVE_INTERVAL_MS, leftLeadMs: Long = Session.KEEPALIVE_LEFT_LEAD_MS,
                              echoWaitMs: Long = Session.KEEPALIVE_ECHO_WAIT_MS, timeSyncEveryMs: Long = Session.TIME_SYNC_INTERVAL_MS) {
        var lastSync = clockMs()
        missedKeepalives = 0
        while (true) {
            sleepMs(intervalMs)
            val r = try {
                sendPb(Session.SID_DEVICE, Session.keepalive(alloc.nextMagic()), Side.LEFT)
                sleepMs(leftLeadMs)
                sendPb(Session.SID_DEVICE, Session.keepalive(alloc.nextMagic()), Side.RIGHT, Expect.Echo, echoWaitMs)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                log("keep-alive write failed: $e")
                null
            }
            if (r != null) missedKeepalives = 0 else {
                missedKeepalives++
                log("keep-alive: no echo from the right arm ($missedKeepalives/2)")
                if (missedKeepalives >= 2) { onLinkLost?.invoke("keep-alive watchdog"); return }
            }
            if (clockMs() - lastSync > timeSyncEveryMs) {
                try {
                    sendPb(Session.SID_DEVICE, Session.timeSync(alloc.nextMagic(), clockMs() / 1000, tzUnits))
                } catch (e: CancellationException) { throw e } catch (e: Exception) { log("time sync failed: $e") }
                lastSync = clockMs()
            }
        }
    }

    @Synchronized
    fun startKeepalive() {
        if (ka?.isActive == true) return
        ka = scope.launch { keepaliveLoop() }
    }

    fun stopKeepalive() { ka?.cancel(); ka = null }

    // ── Even AI ────────────────────────────────────────────────────────────────────────

    /** ENTER -> ASK -> ANALYSE -> REPLY chunks (or the community minimal path). With [heartbeat] the 2 s Even AI heartbeat runs meanwhile. */
    suspend fun evenAiAnswer(question: String, answer: String, community: Boolean = false, heartbeat: Boolean = false, exitAfter: Boolean = false) {
        val m = alloc::nextMagic
        sendPb(EvenAi.SID_EVEN_AI, EvenAi.enter(m()))
        val hbJob = if (heartbeat && !community) scope.launch {
            var n = 0
            while (isActive) { sleepMs(EvenAi.HEARTBEAT_INTERVAL_MS); sendPb(EvenAi.SID_EVEN_AI, EvenAi.heartbeat(m(), ++n)) }
        } else null
        try {
            sleepMs(300)
            sendPb(EvenAi.SID_EVEN_AI, EvenAi.ask(m(), question, community))
            if (community) {
                sleepMs(1000)
                sendPb(EvenAi.SID_EVEN_AI, EvenAi.reply(m(), answer, true, community = true))
                return
            }
            sleepMs(300)
            sendPb(EvenAi.SID_EVEN_AI, EvenAi.analyse(m()))
            for (p in EvenAi.replySequence(m, answer)) {
                sendPb(EvenAi.SID_EVEN_AI, p)
                sleepMs(EvenAi.REPLY_PACE_MS)
            }
            if (exitAfter) sendPb(EvenAi.SID_EVEN_AI, EvenAi.exit(m()))
        } finally {
            hbJob?.cancel()
        }
    }

    /** Enable/disable the mic stream without an Even AI session (SID 0xE0 cmd 15, LIVE). */
    suspend fun setMicEnabled(enable: Boolean) {
        sendPb(Session.SID_EVENHUB, EvenAi.evenhubAudioControl(alloc.nextMagic(), enable))
    }
}
