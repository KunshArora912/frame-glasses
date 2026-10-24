package com.kunsh.frame

import android.annotation.SuppressLint
import android.content.Context
import android.content.SharedPreferences
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import com.kunsh.g2core.Dashboard
import com.kunsh.g2core.EvenAi
import com.kunsh.g2core.G2Ble
import com.kunsh.g2core.G2Session
import com.kunsh.g2core.Session
import com.kunsh.g2core.Settings
import com.kunsh.g2core.Side
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Date
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap
import kotlin.coroutines.cancellation.CancellationException

/**
 * Phone hub mode: this phone holds both G2 arms over BLE (g2core) with no laptop. Even's native dashboard stays the only
 * UI on the glasses: no EvenHub page, no menu push, no widget push or display-enable unless the user switched that
 * feature on in this app. Process-wide singleton (like [G2Link]) so the notification listener reaches it; owned and
 * started by [FrameService].
 */
@SuppressLint("StaticFieldLeak")
object PhoneHub {

    const val KEY_MODE = "hub_mode"              // "laptop" (default) | "phone"
    const val KEY_PHONE_ON = "phone_hub_on"
    const val KEY_BOND = "phone_hub_bond"        // the arms needed createBond() last time
    const val KEY_NOTIF = "phone_notif_on"
    const val KEY_FEAT_STOCKS = "feat_stocks"
    const val KEY_TICKERS = "feat_tickers"
    const val KEY_FEAT_CAL = "feat_calendar"
    const val KEY_FEAT_IDEAS = "feat_ideas"
    const val FEED_INTERVAL_MS = 15 * 60_000L
    const val STATUS_INTERVAL_MS = 60_000L
    const val MAX_TICKERS = 5
    const val NOT_FOUND = "Glasses not found. They are probably connected to something else (e.g. the laptop gateway), " +
        "in the closed case, or out of range. Stop the laptop gateway (only one host can hold the glasses), then tap Connect."

    data class State(val wanted: Boolean, val text: String, val left: String = "-", val right: String = "-",
                     val battery: Int? = null, val charging: Boolean? = null, val ready: Boolean = false)

    private lateinit var ctx: Context
    private lateinit var prefs: SharedPreferences
    var log: (String) -> Unit = {}
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val clock = SimpleDateFormat("HH:mm", Locale.US)

    private val _state = MutableLiveData(State(false, "off"))
    val state: LiveData<State> get() = _state
    private val _display = MutableLiveData<G2Link.Display?>(null)
    val display: LiveData<G2Link.Display?> get() = _display
    private val _ai = MutableLiveData("Even AI: waiting for the glasses (pick Even AI on the glasses)")
    val ai: LiveData<String> get() = _ai
    private val _lastNotif = MutableLiveData("none yet")
    val lastNotif: LiveData<String> get() = _lastNotif
    private val _feeds = MutableLiveData<Map<String, String>>(emptyMap())
    val feeds: LiveData<Map<String, String>> get() = _feeds

    @Volatile var wanted = false; private set
    @Volatile private var ble: G2Ble? = null
    @Volatile private var g2: G2Session? = null
    @Volatile private var ready = false
    private var loop: Job? = null
    private var linkJobs = ArrayList<Job>()
    private val feedJobs = ConcurrentHashMap<String, Job>()
    private val pushLock = Mutex()

    lateinit var keys: ApiKeyStore; private set
    lateinit var ideas: IdeaStore; private set
    lateinit var calendar: AndroidCalendar; private set
    private lateinit var speech: PhoneSpeech
    private val http = OkHttpPost()
    private lateinit var agent: ClaudeAgent
    private lateinit var turns: EvenAiTurns

    val active: Boolean get() = wanted && ready

    fun init(context: Context) {
        if (::ctx.isInitialized) return
        ctx = context.applicationContext
        prefs = ctx.getSharedPreferences(FrameService.PREFS, Context.MODE_PRIVATE)
        keys = ApiKeyStore(ctx)
        ideas = IdeaStore(java.io.File(ctx.filesDir, "ideas.jsonl"))
        calendar = AndroidCalendar(ctx)
        speech = PhoneSpeech(ctx) { log(it) }
        val tools = PhoneTools(calendar, ideas, http, onIdea = { if (feature(KEY_FEAT_IDEAS) && active) pushIdeas() else null })
        agent = ClaudeAgent(http, tools.tools(), log = { log(it) })
        turns = EvenAiTurns(scope,
            send = { p -> (g2 ?: throw IllegalStateException("glasses not connected")).sendPb(EvenAi.SID_EVEN_AI, p) },
            magic = { g2?.alloc?.nextMagic() ?: 1 },
            listen = { speech.listen() },
            answer = { q -> askClaude(q) },
            log = { log(it) },
            onPhase = { ph -> _ai.postValue("Even AI: $ph" + lastQa()) })
    }

    private fun lastQa(): String {
        val q = turns.lastQuestion; val a = turns.lastAnswer
        return if (q.isEmpty() && a.isEmpty()) "" else "\nQ: ${q.ifEmpty { "-" }}\nA: ${a.ifEmpty { "-" }}"
    }

    // ── prefs ──────────────────────────────────────────────────────────────────────────

    var mode: String
        get() = prefs.getString(KEY_MODE, "laptop") ?: "laptop"
        set(v) { prefs.edit().putString(KEY_MODE, v).apply() }

    fun feature(key: String): Boolean = prefs.getBoolean(key, false)
    var notificationsOn: Boolean
        get() = prefs.getBoolean(KEY_NOTIF, true)
        set(v) { prefs.edit().putBoolean(KEY_NOTIF, v).apply() }
    var tickers: List<String>
        get() = (prefs.getString(KEY_TICKERS, "") ?: "").split(',', ' ').mapNotNull { Stocks.normalize(it) }.distinct().take(MAX_TICKERS)
        set(v) { prefs.edit().putString(KEY_TICKERS, v.joinToString(",")).apply() }
    val resumeWanted: Boolean get() = prefs.getBoolean(KEY_PHONE_ON, false)
    val speechEngine: String get() = speech.engineName()

    private fun post(s: State) = _state.postValue(s)
    private fun setText(t: String) { val c = _state.value ?: State(wanted, t); post(c.copy(wanted = wanted, text = t)) }

    // ── lifecycle ──────────────────────────────────────────────────────────────────────

    fun start() {
        if (wanted && loop?.isActive == true) return
        wanted = true
        prefs.edit().putBoolean(KEY_PHONE_ON, true).apply()
        post(State(true, "starting"))
        loop = scope.launch { connectLoop() }
    }

    /** [forget] = false keeps the "resume when the app opens" flag (service destroyed, not a user stop). */
    fun stop(forget: Boolean = true) {
        wanted = false
        if (forget) prefs.edit().putBoolean(KEY_PHONE_ON, false).apply()
        turns.cancel()
        loop?.cancel(); loop = null
        scope.launch(NonCancellable) { teardown() }
        post(State(false, "off"))
        _display.postValue(null)
    }

    private suspend fun connectLoop() {
        var backoff = 5_000L
        while (wanted) {
            val held = try { connectOnce() } catch (e: CancellationException) { throw e } catch (e: Exception) {
                log("phone hub: ${e.message}"); setText("error: ${e.message}"); false
            }
            if (!wanted) break
            if (held) backoff = 5_000L
            val s = backoff / 1000
            val cur = _state.value?.text ?: ""
            setText(if (held) "link lost; reconnecting in ${s}s" else "$cur\n(retry in ${s}s)")
            delay(backoff)
            backoff = (backoff * 2).coerceAtMost(60_000L)
        }
    }

    /** One scan -> connect -> startup -> hold cycle. True when the glasses were held for a while (then lost). */
    private suspend fun connectOnce(): Boolean {
        setText("scanning for the glasses (15 s)")
        val probe = G2Ble(ctx, scope, log = { log(it) })
        val pair = try { probe.scanForPair(15_000) } finally { probe.disconnect() }
        if (pair == null) { log("phone hub: no G2 advertising"); setText(NOT_FOUND); return false }
        log("phone hub: found G2 pair")
        val order = if (prefs.getBoolean(KEY_BOND, false)) listOf(true) else listOf(false, true)
        for (bond in order) {
            setText(if (bond) "connecting (bonding first; accept a pairing prompt if one appears)" else "connecting both arms")
            val b = G2Ble(ctx, scope, G2Ble.Config(bondBeforeConnect = bond), log = { log(it) })
            ble = b
            linkJobs.add(scope.launch {
                b.state.collect { m ->
                    val c = _state.value ?: State(wanted, "")
                    post(c.copy(left = m[Side.LEFT]?.name?.lowercase() ?: "-", right = m[Side.RIGHT]?.name?.lowercase() ?: "-"))
                }
            })
            b.connect(pair)
            if (!b.awaitReady(30_000)) {
                log("phone hub: arms not ready${if (bond) " (with bonding)" else " without bonding"}")
                teardown(); continue
            }
            val s = G2Session(b, scope, config = G2Session.Config(heartbeatForPushes = false), log = { log(it) })
            g2 = s
            val lost = CompletableDeferred<String>()
            s.onLinkLost = { why -> lost.complete(why) }
            s.on { _, p -> onFrame(s, p) }
            setText("starting session")
            val res = s.startup()
            if (!res.authenticated) {
                log("phone hub: auth failed${if (bond) " (with bonding)" else " without bonding"}")
                teardown(); continue
            }
            if (res.missing.isNotEmpty()) log("phone hub: startup steps without reply: ${res.missing.joinToString { it.label }}")
            prefs.edit().putBoolean(KEY_BOND, bond).apply()
            ready = true
            linkJobs.add(scope.launch { b.readyEvents.collect { side -> if (ready) { log("phone hub: $side arm back; re-init"); s.reinitSide(side) } } })
            linkJobs.add(scope.launch { statusLoop(s) })
            setText("connected")
            log("phone hub: connected (${if (bond) "bonded" else "no bond"})")
            startFeeds()
            val t0 = System.currentTimeMillis()
            val why = lost.await()
            log("phone hub: link lost ($why)")
            teardown()
            return System.currentTimeMillis() - t0 > 60_000
        }
        setText("Could not open a session with the glasses. If the laptop gateway is running, stop it and tap Connect again.")
        return false
    }

    private suspend fun teardown() {
        ready = false
        feedJobs.values.forEach { it.cancel() }; feedJobs.clear()
        linkJobs.forEach { it.cancel() }; linkJobs.clear()
        val s = g2; g2 = null
        try { s?.stop() } catch (_: Exception) { }
        ble?.disconnect(); ble = null
        _display.postValue(null)
    }

    // ── inbound ────────────────────────────────────────────────────────────────────────

    private var snap = Settings.Snapshot()

    private fun onFrame(s: G2Session, p: com.kunsh.g2core.ParsedFrame) {
        when (p.sid) {
            EvenAi.SID_EVEN_AI -> turns.onFrame(p.payload)
            Session.SID_SETTINGS -> Settings.parse(p.payload).snapshot?.let { mergeSnapshot(it) }
        }
    }

    /** A READ reply replaces (absent proto3 fields are 0); an unsolicited push only overwrites what it carries. */
    @Synchronized
    private fun mergeSnapshot(n: Settings.Snapshot) {
        snap = if (!n.isPush) n else snap.copy(
            brightnessLevel = n.brightnessLevel ?: snap.brightnessLevel, displayHeight = n.displayHeight ?: snap.displayHeight,
            displayDistance = n.displayDistance ?: snap.displayDistance, battery = n.battery ?: snap.battery,
            charging = n.charging ?: snap.charging, brightnessAuto = n.brightnessAuto ?: snap.brightnessAuto)
        val d = G2Link.Display(snap.displayHeight ?: 0, snap.displayDistance ?: 0, snap.brightnessLevel ?: 0, (snap.brightnessAuto ?: 0) != 0,
            snap.battery ?: 0, (snap.charging ?: 0) != 0)
        _display.postValue(d)
        val c = _state.value ?: State(wanted, "")
        post(c.copy(battery = snap.battery, charging = snap.charging?.let { it != 0 }, ready = ready))
    }

    private suspend fun statusLoop(s: G2Session) {
        while (scope.isActive) {
            try { s.readSettings() } catch (e: CancellationException) { throw e } catch (e: Exception) { log("status read failed: ${e.message}") }
            delay(STATUS_INTERVAL_MS)
        }
    }

    // ── display settings (Display card) ────────────────────────────────────────────────

    private val pendingSets = ConcurrentHashMap<String, Int>()
    private var setJob: Job? = null

    /** Height/distance 0..20, brightness 1..50, auto 0/1; at most every 300 ms, last value wins, then re-read. */
    @Synchronized
    fun applySet(key: String, value: Int) {
        if (!active) return
        pendingSets[key] = value
        if (setJob?.isActive == true) return
        setJob = scope.launch {
            while (pendingSets.isNotEmpty()) {
                delay(300)
                val batch = HashMap(pendingSets)
                batch.forEach { (k, v) -> pendingSets.remove(k, v) }
                val s = g2 ?: return@launch
                for ((k, v) in batch) {
                    try {
                        val r = when (k) {
                            "height" -> s.applySetting { Settings.displayHeight(it, v.coerceIn(0, 20)) }
                            "distance" -> s.applySetting { Settings.displayDistance(it, v.coerceIn(0, 20)) }
                            "brightness" -> s.applySetting { Settings.brightness(it, v.coerceIn(1, 50)) }
                            "auto" -> s.applySetting { Settings.autoBrightness(it, v != 0) }
                            else -> null
                        }
                        log("display $k=$v ${if (r != null) "acked" else "no ack"}")
                    } catch (e: CancellationException) { throw e } catch (e: Exception) { log("display $k=$v failed: ${e.message}") }
                }
            }
            g2?.readSettings()
        }
    }

    // ── notifications ──────────────────────────────────────────────────────────────────

    private data class Notif(val pkg: String, val app: String, val title: String, val text: String, val key: String)

    private val queue = LinkedHashMap<String, Notif>()
    private val recent = HashMap<String, Long>()
    private var sender: Job? = null
    @Volatile private var hubFormFails = 0

    /** From [G2NotificationListener] while this hub holds the glasses. Silent and duplicate notifications are dropped. */
    fun notify(pkg: String, app: String, title: String, text: String, key: String, silent: Boolean) {
        if (!active || !notificationsOn || silent) return
        if (title.isBlank() && text.isBlank()) return
        val now = System.currentTimeMillis()
        synchronized(queue) {
            val sig = "$key|$title|$text"
            recent.entries.removeAll { now - it.value > 10_000 }
            if (recent.containsKey(sig)) return
            recent[sig] = now
            queue.remove(key)
            queue[key] = Notif(pkg, app, title.take(256), text.take(1000), key)
            if (sender?.isActive == true) return
            sender = scope.launch { sendLoop() }
        }
    }

    private suspend fun sendLoop() {
        while (true) {
            val batch = synchronized(queue) { val l = queue.values.toList(); queue.clear(); l }
            if (batch.isEmpty()) return
            val s = g2 ?: return
            val (title, subtitle, message, display, appId) = if (batch.size == 1) {
                val n = batch[0]
                Card(n.title.ifBlank { n.app }, if (n.title.isNotBlank()) n.app else "", n.text.ifBlank { n.title }, n.app, n.pkg)
            } else {
                val apps = batch.map { it.app }.distinct()
                Card("${batch.size} notifications", apps.joinToString(", "),
                    batch.takeLast(5).joinToString("\n") { "${it.app}: ${it.title}" + if (it.text.isNotEmpty()) " - ${it.text}" else "" },
                    if (apps.size == 1) apps[0] else "Phone", if (apps.size == 1) batch.last().pkg else "com.even.sg")
            }
            val result = pushLock.withLock { sendCard(s, title, subtitle, message, display, appId) }
            _lastNotif.postValue("$display: ${title.take(50)}  ${clock.format(Date())}  ($result)")
            delay(3000)
        }
    }

    private data class Card(val title: String, val subtitle: String, val message: String, val display: String, val appId: String)

    private var msgId = 0L

    /** Hub form first (acked); after two hub failures in a row, the community form only (it has no ack). */
    private suspend fun sendCard(s: G2Session, title: String, subtitle: String, message: String, display: String, appId: String): String {
        msgId = (msgId + 1) and 0x7FFFFFFF
        val dn = display.take(31)
        if (hubFormFails < 2) {
            val ok = try { s.notify(title, message, subtitle, G2Session.NotifyForm.HUB, displayName = dn, appIdentifier = appId, msgId = msgId) }
            catch (e: CancellationException) { throw e } catch (e: Exception) { log("notify hub form: ${e.message}"); false }
            if (ok) { hubFormFails = 0; log("notification: hub form ACKED ($title)"); return "hub form acked" }
            hubFormFails++
            log("notification: hub form got no ack ($hubFormFails); sending the community form")
        }
        return try {
            s.notify(title, message, subtitle, G2Session.NotifyForm.COMMUNITY, displayName = dn, appIdentifier = appId, msgId = msgId)
            log("notification: community form sent, fire-and-forget ($title)")
            "community form sent, no ack in that form"
        } catch (e: CancellationException) { throw e } catch (e: Exception) { log("notify community form: ${e.message}"); "failed: ${e.message}" }
    }

    // ── Even AI -> Claude ──────────────────────────────────────────────────────────────

    private suspend fun askClaude(q: String): String {
        val key = keys.get() ?: return "Set the Claude API key in the phone app first."
        val a = agent.ask(key, q)
        log("Claude: ${a.model ?: "no model"}${if (a.toolCalls.isNotEmpty()) ", tools ${a.toolCalls}" else ""}${a.error?.let { ", $it" } ?: ""}")
        return a.text
    }

    fun cancelAi() = turns.cancel()

    // ── dashboard features (all off by default) ────────────────────────────────────────

    private fun feedText(k: String, v: String) {
        val m = HashMap(_feeds.value ?: emptyMap()); m[k] = v; _feeds.postValue(m)
    }

    /** Switch a feature; ON pushes once now (when connected) and then on its schedule; OFF just stops pushing. */
    fun setFeature(key: String, on: Boolean) {
        prefs.edit().putBoolean(key, on).apply()
        if (!on) { feedJobs.remove(key)?.cancel(); feedText(key, "off (nothing pushed; the glasses keep whatever they show)"); return }
        if (active) startFeed(key) else feedText(key, "on; pushes once the phone hub is connected")
    }

    private fun startFeeds() { for (k in listOf(KEY_FEAT_STOCKS, KEY_FEAT_CAL, KEY_FEAT_IDEAS)) if (feature(k)) startFeed(k) }

    private fun startFeed(key: String) {
        feedJobs.remove(key)?.cancel()
        feedJobs[key] = scope.launch {
            when (key) {
                KEY_FEAT_IDEAS -> feedText(key, pushIdeas())
                else -> while (isActive && feature(key) && active) {
                    feedText(key, if (key == KEY_FEAT_STOCKS) pushStocks() else pushCalendar())
                    delay(FEED_INTERVAL_MS)
                }
            }
        }
    }

    fun refreshStocksNow() { if (feature(KEY_FEAT_STOCKS) && active) startFeed(KEY_FEAT_STOCKS) }

    private fun stamp() = clock.format(Date())
    private fun acks(flags: List<Int?>) = flags.joinToString(",") { it?.toString() ?: "none" }

    private suspend fun pushStocks(): String {
        val s = g2 ?: return "not connected"
        val list = tickers
        if (list.isEmpty()) return "on, but no tickers set"
        val quotes = ArrayList<Quote>(); val errs = ArrayList<String>()
        for (t in list) try { quotes.add(Stocks.fetch(http, t)) } catch (e: CancellationException) { throw e } catch (e: Exception) { errs.add("$t: ${e.message}") }
        if (quotes.isEmpty()) return "no quotes at ${stamp()} (${errs.joinToString("; ")})"
        val flags = pushLock.withLock { s.setStocks(quotes.map { it.toDashboard() }) }
        val what = quotes.joinToString("; ") { q -> String.format(Locale.US, "%s %.2f%s", q.symbol, q.price, q.changePercent?.let { String.format(Locale.US, " %+.2f%%", it) } ?: "") }
        log("stocks pushed: $what acks ${acks(flags)}")
        return "pushed at ${stamp()}: $what (acks ${acks(flags)}, 0 = ok)" + if (errs.isNotEmpty()) "; failed ${errs.joinToString()}" else ""
    }

    private suspend fun pushCalendar(): String {
        val s = g2 ?: return "not connected"
        if (!AndroidCalendar.canRead(ctx)) return "calendar permission not granted"
        val z = ZoneId.systemDefault()
        val today = LocalDate.now(z)
        val end = today.plusDays(2).atStartOfDay(z).toInstant().toEpochMilli()
        val events = try { calendar.list(System.currentTimeMillis(), end) } catch (e: Exception) { return "calendar read failed: ${e.message}" }
        val sched = calendarSchedules(events, today, z)
        val flags = pushLock.withLock { s.setCalendar(sched) }
        val what = if (sched.isEmpty()) "no events" else sched.joinToString("; ") { "${it.timeStr} ${it.title}" }
        log("calendar pushed: $what acks ${acks(flags)}")
        return "pushed at ${stamp()}: $what (acks ${acks(flags)}, 0 = ok)"
    }

    /** Today + tomorrow, at most 8 events, "10:00 AM-10:30 AM" / "Tmr 9:00 AM-9:30 AM" / "All day" (Even's format). */
    fun calendarSchedules(events: List<CalendarStore.Event>, today: LocalDate, z: ZoneId): List<Dashboard.Schedule> {
        val f = DateTimeFormatter.ofPattern("h:mm a", Locale.US)
        return events.take(Dashboard.CAL_MAX_EVENTS).map { e ->
            val startDay = if (e.allDay) Instant.ofEpochMilli(e.startMs).atZone(ZoneId.of("UTC")).toLocalDate()
            else Instant.ofEpochMilli(e.startMs).atZone(z).toLocalDate()
            val prefix = if (startDay.isAfter(today)) "Tmr " else ""
            val time = if (e.allDay) "All day" else
                Instant.ofEpochMilli(e.startMs).atZone(z).format(f) + "-" + Instant.ofEpochMilli(e.endMs).atZone(z).format(f)
            Dashboard.Schedule(e.title, e.location, prefix + time, endTs = e.endMs / 1000)
        }
    }

    /** Latest ideas (newest first, <= 20) to the quick list. Only called while "Ideas on dashboard" is on. */
    suspend fun pushIdeas(): String {
        val s = g2 ?: return "not connected"
        val list = ideas.latest(20)
        val ok = pushLock.withLock { s.setTasks(list.map { Dashboard.Task(it.id, it.text) }) }
        log("ideas pushed to the quick list: ${list.size} (${if (ok) "acked" else "not acked"})")
        return "pushed at ${stamp()}: ${list.size} ideas to the quick list (${if (ok) "acked" else "NOT acked"})" +
            (list.firstOrNull()?.let { "; newest: ${it.text.take(40)}" } ?: "")
    }
}
