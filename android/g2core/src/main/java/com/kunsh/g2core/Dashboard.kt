package com.kunsh.g2core

import com.kunsh.g2core.Pb.messageField
import com.kunsh.g2core.Pb.varintField

/**
 * The native dashboard: SID 0x01 (weather, calendar, news, stocks, display settings), SID 0x0C quicklist/tasks,
 * SID 0x0E health, SID 0x1F watchface (port of g2/native/dashboard.py). Every builder returns the protobuf payload.
 */
object Dashboard {
    const val SID_DASHBOARD = 0x01
    const val SID_QUICKLIST = 0x0C
    const val SID_HEALTH = 0x0E
    const val SID_WATCHFACE = 0x1F

    const val CMD_DASHBOARD_RECEIVE = 2
    const val CMD_APP_REQUEST_NEWS_INFO = 7
    const val CMD_APP_SEND_NEWS_DATA = 9
    const val CMD_DEV_REQUEST_NEWS_UPGRADE = 11
    const val CMD_DEV_NOTIFY_NEWS_EVENT = 12
    const val CMD_APP_REQUEST_CLEAR_ALL_DATA = 13

    const val WIDGET_NEWS = 1
    const val WIDGET_STOCK = 2
    const val WIDGET_SCHEDULE = 3
    const val WIDGET_QUICKLIST = 4
    const val WIDGET_HEALTH = 5
    const val TEMP_CELSIUS = 1
    const val TEMP_FAHRENHEIT = 2
    val WEATHER_TYPES = listOf("Unknown", "Sunny", "Cloudy", "Drizzle", "Heavy drizzle", "Rain", "Heavy rain", "Thunderstorm",
        "Thunder", "Snow", "Mist", "Fog", "Dust", "Squalls", "Tornado", "Freezing rain", "Clear night")

    const val CAL_TITLE_MAX_BYTES = 175
    const val CAL_LOCATION_MAX_BYTES = 282
    const val CAL_TIME_MAX_BYTES = 62
    const val CAL_MAX_EVENTS = 8

    /** calendar limit_bytes: cut to max-3 bytes and append '...'. */
    fun limitBytes(text: String, maxBytes: Int): String =
        if (Text.utf8Len(text) <= maxBytes) text else Text.utf8Fit(text, maxBytes - 3) + "..."

    fun displaySetting(widgetOrder: List<Int>, statusOrder: List<Int> = emptyList(), displayMode: Int = 0, halfDay: Int = 0,
                       temperatureUnit: Int = 0): ByteArray {
        val w = Writer().varint(1, displayMode).varint(2, statusOrder.size)
        if (statusOrder.isNotEmpty()) w.packedVarints(3, statusOrder.map { it.toLong() })
        w.varint(4, widgetOrder.size)
        if (widgetOrder.isNotEmpty()) w.packedVarints(5, widgetOrder.map { it.toLong() })
        return w.varint(6, halfDay).varint(7, temperatureUnit).toBytes()
    }

    /** DashboardReceiveFromApp `{1 packageId?, 2 DisplaySetting?, 3 DashboardContent}`; field 3 always written. */
    fun receiveFromApp(packageId: Int?, content: ByteArray, setting: ByteArray? = null): ByteArray {
        val w = Writer()
        if (packageId != null) w.varint(1, packageId)
        if (setting != null) w.message(2, setting)
        return w.message(3, content).toBytes()
    }

    /** DashboardDataPackage `{1 cmd, 2 magic, 4 receive?}`. */
    fun pkg(cmd: Int, magic: Int, receive: ByteArray? = null): ByteArray {
        val w = Writer().varint(1, cmd).varint(2, magic)
        if (receive != null) w.message(4, receive)
        return w.toBytes()
    }

    /** Base settings, LIVE: display_mode 4, status [1,2,3], widgets [NEWS, SCHEDULE, STOCK, STOCK], 12 h, Celsius. */
    fun dashboardInit(magic: Int, packageId: Int = 1): ByteArray {
        val ds = displaySetting(listOf(WIDGET_NEWS, WIDGET_SCHEDULE, WIDGET_STOCK, WIDGET_STOCK), listOf(1, 2, 3), 4, 1, TEMP_CELSIUS)
        return pkg(CMD_DASHBOARD_RECEIVE, magic, receiveFromApp(packageId, ByteArray(0), ds))
    }

    /** Custom widget order, EXPERIMENTAL; field 2 only (oneof caveat). */
    fun dashboardLayout(magic: Int, widgetOrder: List<Int>, displayMode: Int = 4, halfDay: Int = 1, temperatureUnit: Int = TEMP_CELSIUS,
                        packageId: Int = 1): ByteArray {
        val ds = displaySetting(widgetOrder, listOf(1, 2, 3), displayMode, halfDay, temperatureUnit)
        return pkg(CMD_DASHBOARD_RECEIVE, magic, Writer().varint(1, packageId).message(2, ds).toBytes())
    }

    fun displayEnable(magic: Int, widgetTypes: List<Int>, packageId: Int = 1): ByteArray =
        pkg(CMD_DASHBOARD_RECEIVE, magic, receiveFromApp(packageId, ByteArray(0), displaySetting(widgetTypes)))

    /** LIVE. */
    fun quicklistDisplayEnable(magic: Int, packageId: Int = 1): ByteArray = displayEnable(magic, listOf(WIDGET_QUICKLIST), packageId)

    // ── weather (LIVE) ─────────────────────────────────────────────────────────────────

    data class Weather(
        val temperatureC: Float,
        val unit: Int = TEMP_CELSIUS,
        val weatherType: Int = 0,
        val updateTimeMs: Long = 0,
        val status: String = "",
        val rainfallIcon: Int = 0,
        val rainfallHint: String = "",
        val sunsetSelect: Int = 0,
        val sunsetHint: String = "",
    )

    fun weatherStatus(w: Weather): ByteArray {
        val b = Writer().float32(1, w.temperatureC).varint(2, w.unit).varint(3, w.weatherType).varint(4, w.updateTimeMs)
        if (w.status.isNotEmpty()) b.string(5, Text.head(w.status, 32))
        b.varint(6, w.rainfallIcon)
        if (w.rainfallHint.isNotEmpty()) b.string(7, Text.head(w.rainfallHint, 32))
        b.varint(8, w.sunsetSelect)
        if (w.sunsetHint.isNotEmpty()) b.string(9, Text.head(w.sunsetHint, 32))
        return b.toBytes()
    }

    /** Weather status-bar push, LIVE: `{1:2, 2:m, 4:{3:{1:{1: WeatherStatus}}}}` with no packageId by default. */
    fun weather(magic: Int, w: Weather, packageId: Int? = null): ByteArray {
        val content = messageField(1, messageField(1, weatherStatus(w)))
        return pkg(CMD_DASHBOARD_RECEIVE, magic, receiveFromApp(packageId, content))
    }

    fun weather(magic: Int, temperatureC: Float, unit: Int = TEMP_CELSIUS, weatherType: Int = 0, updateTimeMs: Long = 0, status: String = ""): ByteArray =
        weather(magic, Weather(temperatureC, unit, weatherType, updateTimeMs, status))

    // ── calendar (LIVE) ────────────────────────────────────────────────────────────────

    data class Schedule(
        val title: String,
        val location: String = "",
        /** "10:00 AM-10:30 AM", "Tmr 09:00-09:30", "All day" */
        val timeStr: String = "",
        /** unix seconds */
        val endTs: Long = 0,
        /** only the multi-event form writes field 1 */
        val scheduleId: Long? = null,
    )

    private fun scheduleBytes(s: Schedule, withId: Boolean): ByteArray {
        val w = Writer()
        if (withId) w.varint(1, s.scheduleId ?: 0)
        if (s.title.isNotEmpty()) w.string(2, limitBytes(s.title, CAL_TITLE_MAX_BYTES))
        if (s.location.isNotEmpty()) w.string(3, limitBytes(s.location, CAL_LOCATION_MAX_BYTES))
        if (s.timeStr.isNotEmpty()) w.string(4, limitBytes(s.timeStr, CAL_TIME_MAX_BYTES))
        return w.varint(5, s.endTs).toBytes()
    }

    /** ONE event per message as the official app sends it (LIVE). "No events" = calendarNone. */
    fun calendarEvent(magic: Int, schedule: Schedule?, total: Int, index: Int, authority: Int = 0, packageId: Int = 1): ByteArray {
        val w = Writer().varint(1, total).varint(2, index)
        if (schedule != null) w.message(3, scheduleBytes(schedule, false))
        if (total == 0) w.varint(4, authority)
        val content = messageField(2, messageField(3, w.toBytes()))
        return pkg(CMD_DASHBOARD_RECEIVE, magic, receiveFromApp(packageId, content))
    }

    fun calendarNone(magic: Int, packageId: Int = 1): ByteArray = calendarEvent(magic, null, 0, 0, 1, packageId)

    /** Multi-event in one message (LIVE per the doc, no vector): ScheduleWidget `{1 total, 2 startIndex, 3 Schedule..., 4 authority}`. */
    fun calendarEvents(magic: Int, schedules: List<Schedule>, total: Int? = null, startIndex: Int = 0, authority: Int = 0, packageId: Int = 1): ByteArray {
        val w = Writer().varint(1, total ?: schedules.size).varint(2, startIndex)
        schedules.forEachIndexed { i, s0 ->
            val s = if (s0.scheduleId == null) s0.copy(scheduleId = (i + 1).toLong()) else s0
            w.message(3, scheduleBytes(s, true))
        }
        w.varint(4, authority)
        val content = messageField(2, messageField(3, w.toBytes()))
        return pkg(CMD_DASHBOARD_RECEIVE, magic, receiveFromApp(packageId, content))
    }

    /** One message per event (max 8) or the "none" message; each awaited for ack flag 0. */
    fun calendarSequence(magicFn: () -> Int, schedules: List<Schedule>): List<ByteArray> {
        if (schedules.isEmpty()) return listOf(calendarNone(magicFn()))
        val n = minOf(schedules.size, CAL_MAX_EVENTS)
        return (0 until n).map { calendarEvent(magicFn(), schedules[it], n, it) }
    }

    // ── news (V3 FIFO, LIVE) ───────────────────────────────────────────────────────────

    data class NewsItem(val title: String, val source: String = "", val content: String = "")

    fun newsReset(magic: Int): ByteArray =
        varintField(1, CMD_APP_REQUEST_CLEAR_ALL_DATA) + varintField(2, magic) + messageField(15, varintField(1, 1))

    fun newsFifoCountRequest(magic: Int): ByteArray =
        varintField(1, CMD_APP_REQUEST_NEWS_INFO) + varintField(2, magic) + messageField(9, varintField(1, 1))

    fun newsPush(magic: Int, slot: Int, item: NewsItem, reportTimeLocalMs: Long, sessionTotal: Int = 1, sessionId: Int = 0, sessionStatus: Int = 0): ByteArray {
        val w = Writer().varint(1, sessionId).varint(2, sessionStatus).varint(3, sessionTotal).varint(4, slot).varint(5, slot)
        if (item.title.isNotEmpty()) w.string(6, item.title)
        w.varint(7, reportTimeLocalMs)
        if (item.source.isNotEmpty()) w.string(8, item.source)
        if (item.content.isNotEmpty()) w.string(9, item.content)
        return varintField(1, CMD_APP_SEND_NEWS_DATA) + varintField(2, magic) + messageField(11, w.toBytes())
    }

    fun newsFifoCount(reply: ByteArray?, default: Int = 5): Int {
        val resp = Pb.readBytes(reply ?: return default, 10) ?: return default
        val v = Pb.readVarint(resp, 1, default.toLong()).toInt()
        return if (v == 0) default else v
    }

    fun newsStatus(reply: ByteArray?): Int? {
        if (reply == null) return null
        val resp = Pb.readBytes(reply, 12) ?: return 0
        return Pb.readInt(resp, 5, 0)
    }

    val NEWS_EVENT_NAMES = mapOf(0 to "EXPAND_LIST", 1 to "BACK_TO_WIDGET", 2 to "SCROLL", 3 to "OPEN_ARTICLE", 4 to "BACK_TO_LIST")

    data class NewsEvent(val cmd: Int, val eventId: Int? = null, val newsId: Int? = null) {
        val refreshRequested: Boolean get() = cmd == CMD_DEV_REQUEST_NEWS_UPGRADE
        val eventName: String? get() = eventId?.let { NEWS_EVENT_NAMES[it] ?: it.toString() }
    }

    /** Glasses -> app news events on SID 0x01: cmd 11 (refresh) and cmd 12 `{14: {1 eventID, 2 newsId}}`. */
    fun parseNewsEvent(payload: ByteArray): NewsEvent? {
        val cmd = Pb.readInt(payload, 1, 0)
        if (cmd == CMD_DEV_REQUEST_NEWS_UPGRADE) return NewsEvent(cmd)
        if (cmd == CMD_DEV_NOTIFY_NEWS_EVENT) {
            val ev = Pb.readBytes(payload, 14) ?: return NewsEvent(cmd)
            return NewsEvent(cmd, Pb.readInt(ev, 1, 0), Pb.readInt(ev, 2, 0))
        }
        return null
    }

    // ── acks ───────────────────────────────────────────────────────────────────────────

    /** Result flag of a Dashboard_Receive ack `{3: {1 packageId, 2 flag}}`; null when no field 3. Empty field 3 = success. */
    fun ackFlag(reply: ByteArray?): Int? {
        val respond = Pb.readBytes(reply ?: return null, 3) ?: return null
        return Pb.readInt(respond, 2, 0)
    }

    fun readCmd(payload: ByteArray): Int = Pb.readInt(payload, 1, 0)
    fun readMagic(payload: ByteArray): Int = Pb.readInt(payload, 2, -1)

    // ── quicklist / tasks (SID 0x0C, LIVE) ─────────────────────────────────────────────

    const val QL_CMD_ITEM = 1
    const val QL_CMD_MULT_ITEMS = 2
    const val QL_CMD_EVENT = 3
    const val QL_FULL_UPDATE = 1
    const val QL_ADD = 2
    const val QL_TS_DATETIME = 0
    const val QL_TS_DATE = 1
    const val QL_TS_TIME = 2
    const val QL_TITLE_MAX_BYTES = 200
    const val QL_MAX_ITEMS = 20
    const val QL_EVENT_UP = 1
    const val QL_EVENT_DOWN = 2

    data class Task(val uid: Long, val title: String, val completed: Boolean = false, val timestamp: Long = 0, val tsType: Int = QL_TS_TIME)

    private fun titleBytes(title: String): ByteArray = Text.utf8Fit(title, QL_TITLE_MAX_BYTES).toByteArray(Charsets.UTF_8) + byteArrayOf(0)

    fun quicklistItem(t: Task, index: Int): ByteArray {
        val w = Writer().varint(1, t.uid).varint(2, index).varint(3, if (t.completed) 1 else 0).varint(4, t.timestamp)
        if (t.title.isNotEmpty()) w.bytes(5, titleBytes(t.title))
        return w.varint(7, t.tsType).toBytes()
    }

    private fun multItems(items: List<Task>, dataType: Int, total: Int? = null, firstIndex: Int = 0): ByteArray {
        val w = Writer().varint(1, dataType).varint(2, total ?: items.size)
        items.forEachIndexed { i, t -> w.message(3, quicklistItem(t, firstIndex + i)) }
        return w.toBytes()
    }

    /** `{1: 2, 2: m, 4: {1: 1 FULL_UPDATE, 2: n, 3: Item...}}` LIVE; <=20 items. */
    fun quicklistFullUpdate(magic: Int, tasks: List<Task>): ByteArray {
        require(tasks.size <= QL_MAX_ITEMS) { "at most $QL_MAX_ITEMS items per packet, got ${tasks.size}" }
        return varintField(1, QL_CMD_MULT_ITEMS) + varintField(2, magic) + messageField(4, multItems(tasks, QL_FULL_UPDATE))
    }

    fun quicklistAddPage(magic: Int, tasks: List<Task>, firstIndex: Int = 0): ByteArray =
        varintField(1, QL_CMD_MULT_ITEMS) + varintField(2, magic) + messageField(4, multItems(tasks.take(QL_MAX_ITEMS), QL_ADD, firstIndex = firstIndex))

    fun quicklistEmpty(magic: Int): ByteArray =
        varintField(1, QL_CMD_MULT_ITEMS) + varintField(2, magic) + messageField(4, multItems(emptyList(), QL_FULL_UPDATE, total = 0))

    data class SeqItem(val sid: Int, val payload: ByteArray, val expect: String)

    /** LIVE push order: dashboard init (ack) -> 300 ms -> FULL_UPDATE (echo) -> quicklist display-enable (ack). */
    fun tasksSequence(magicFn: () -> Int, tasks: List<Task>): List<SeqItem> = listOf(
        SeqItem(SID_DASHBOARD, dashboardInit(magicFn()), "ack"),
        SeqItem(SID_QUICKLIST, quicklistFullUpdate(magicFn(), tasks.take(QL_MAX_ITEMS)), "echo"),
        SeqItem(SID_DASHBOARD, quicklistDisplayEnable(magicFn()), "ack"),
    )

    data class TaskTick(val uid: Long, val index: Int, val completed: Boolean, val title: String?)

    fun parseQuicklistTicks(payload: ByteArray): List<TaskTick> {
        val raws = when (Pb.readInt(payload, 1)) {
            QL_CMD_ITEM -> listOfNotNull(Pb.readBytes(payload, 3))
            QL_CMD_MULT_ITEMS -> Pb.readMessages(Pb.readBytes(payload, 4), 3)
            else -> emptyList()
        }
        return raws.map { raw ->
            val t = Pb.readBytes(raw, 5)
            val title = t?.let { b -> var e = b.size; while (e > 0 && b[e - 1].toInt() == 0) e--; String(b, 0, e, Charsets.UTF_8) }
            TaskTick(Pb.readVarint(raw, 1, 0), Pb.readInt(raw, 2, 0), Pb.readVarint(raw, 3, 0) != 0L, title)
        }
    }

    /** cmd 3 `{5: {1 event, 2 pivotUid}}` -> (event, pivotUid) or null. */
    fun parseQuicklistEvent(payload: ByteArray): Pair<Int, Long>? {
        if (Pb.readInt(payload, 1) != QL_CMD_EVENT) return null
        val ev = Pb.readBytes(payload, 5) ?: return null
        return Pb.readInt(ev, 1, 0) to Pb.readVarint(ev, 2, 0)
    }

    fun pageAround(tasks: List<Task>, event: Int, pivotUid: Long): List<Task> {
        val idx = tasks.indexOfFirst { it.uid == pivotUid }
        if (idx < 0) return emptyList()
        return if (event == QL_EVENT_UP) tasks.subList(maxOf(0, idx - QL_MAX_ITEMS), idx).toList()
        else tasks.subList(minOf(idx + 1, tasks.size), minOf(idx + 1 + QL_MAX_ITEMS, tasks.size)).toList()
    }

    // ── stocks (DECODED, never seen on glasses) ────────────────────────────────────────

    const val STOCK_TREND_FLAT = 0
    const val STOCK_TREND_UP = 1
    const val STOCK_TREND_DOWN = 2
    const val STOCK_CODE_MAX_BYTES = 63
    const val STOCK_NAME_MAX_BYTES = 127

    data class Stock(
        val code: String? = null,
        val marketSuffix: String? = null,
        val marketCap: Double? = null,
        val changePercent: Float? = null,
        val price: Float? = null,
        val name: String? = null,
        val dayHigh: Float? = null,
        val dayLow: Float? = null,
        val open: Float? = null,
        val volume: Long? = null,
        val marketValue: Double? = null,
        val peRatio: Float? = null,
        val trend: Int? = null,
        val pointTotal: Int? = null,
        val darkPrice: Float? = null,
        val points: List<Float>? = null,
    )

    private fun stockStr(v: String, isName: Boolean): String =
        if (isName) Text.utf8Fit(Text.head(v, 126), STOCK_NAME_MAX_BYTES) else Text.utf8Fit(Text.head(v, 62), STOCK_CODE_MAX_BYTES)

    fun stockMessage(s: Stock): ByteArray {
        val pts = s.points
        val pointTotal = s.pointTotal ?: if (!pts.isNullOrEmpty()) pts.size else null
        val w = Writer()
        s.code?.let { w.string(1, stockStr(it, false)) }
        s.marketSuffix?.let { w.string(2, stockStr(it, false)) }
        s.marketCap?.let { w.double(3, it) }
        s.changePercent?.let { w.float32(4, it) }
        s.price?.let { w.float32(5, it) }
        s.name?.let { w.string(6, stockStr(it, true)) }
        s.dayHigh?.let { w.float32(7, it) }
        s.dayLow?.let { w.float32(8, it) }
        s.open?.let { w.float32(9, it) }
        s.volume?.let { w.varint(10, it) }
        s.marketValue?.let { w.double(11, it) }
        s.peRatio?.let { w.float32(12, it) }
        s.trend?.let { w.varint(13, it) }
        pointTotal?.let { w.varint(14, it) }
        s.darkPrice?.let { w.float32(15, it) }
        if (!pts.isNullOrEmpty()) w.packedFloats(16, pts)
        return w.toBytes()
    }

    /** DECODED: `{1:2, 2:m, 4:{3:{2:{2: StockWidget{1 total, 2 num, 3 Stock}}}}}`; card shows only if widget order contains 2. */
    fun stockPush(magic: Int, stock: Stock, total: Int = 1, index: Int = 0, packageId: Int? = null): ByteArray {
        val widget = varintField(1, total) + varintField(2, index) + messageField(3, stockMessage(stock))
        val content = messageField(2, messageField(2, widget))
        return pkg(CMD_DASHBOARD_RECEIVE, magic, receiveFromApp(packageId, content))
    }

    fun stockDisplayEnable(magic: Int, widgetOrder: List<Int> = listOf(WIDGET_NEWS, WIDGET_SCHEDULE, WIDGET_STOCK, WIDGET_STOCK),
                           packageId: Int = 1, displayMode: Int = 4): ByteArray {
        require(WIDGET_STOCK in widgetOrder) { "widget_order must contain WIDGET_STOCK (2) or the card never shows" }
        val ds = displaySetting(widgetOrder, listOf(1, 2, 3), displayMode, 1, TEMP_CELSIUS)
        return pkg(CMD_DASHBOARD_RECEIVE, magic, receiveFromApp(packageId, ByteArray(0), ds))
    }

    // ── health (SID 0x0E, DECODED) ─────────────────────────────────────────────────────

    const val HEALTH_CMD_MULT = 2
    const val HEALTH_ALL = 1
    const val HEALTH_STEPS = 2
    const val HEALTH_CALORIES = 3
    const val HEALTH_SLEEP = 4
    const val HEALTH_HEART_RATE = 5
    const val HEALTH_SPO2 = 6
    const val HEALTH_PRODUCTIVITY = 9
    const val HEALTH_MAX_ITEMS = 8

    fun healthSingle(dataType: Int, value: Float? = null, goal: Int? = null, avg: Float? = null, duration: Int? = null,
                     trend: Int? = null, error: Int? = null): ByteArray {
        val w = Writer().varint(1, dataType)
        if (goal != null && goal != 0) w.varint(2, goal)
        if (value != null) w.float32(3, value)
        if (avg != null) w.float32(4, avg)
        if (duration != null && duration != 0) w.varint(5, duration)
        if (error != null && error != 0) w.varint(6, error)
        if (trend != null && trend != 0) w.varint(7, trend)
        return w.toBytes()
    }

    fun healthPush(magic: Int, items: List<ByteArray>, dataType: Int = HEALTH_ALL): ByteArray {
        val w = Writer().varint(1, dataType)
        items.take(HEALTH_MAX_ITEMS).forEach { w.message(2, it) }
        return varintField(1, HEALTH_CMD_MULT) + varintField(2, magic) + messageField(4, w.toBytes())
    }

    fun healthSnapshot(magic: Int, steps: Float? = null, stepGoal: Int? = null, calories: Float? = null, calorieGoal: Int? = null,
                       sleep: Float? = null, sleepMinutes: Int? = null, heartRate: Float? = null, heartRateAvg: Float? = null,
                       spo2: Float? = null, spo2Avg: Float? = null, productivity: Float? = null, productivityTrend: Int? = null): ByteArray {
        val items = ArrayList<ByteArray>()
        if (steps != null) items.add(healthSingle(HEALTH_STEPS, steps, stepGoal))
        if (calories != null) items.add(healthSingle(HEALTH_CALORIES, calories, calorieGoal))
        if (sleep != null || (sleepMinutes != null && sleepMinutes != 0)) {
            items.add(healthSingle(HEALTH_SLEEP, if (sleep == null || sleep == 0f) 0f else sleep, duration = sleepMinutes))
        }
        if (heartRate != null) items.add(healthSingle(HEALTH_HEART_RATE, heartRate, avg = heartRateAvg))
        if (spo2 != null) items.add(healthSingle(HEALTH_SPO2, spo2, avg = spo2Avg))
        if (productivity != null) items.add(healthSingle(HEALTH_PRODUCTIVITY, productivity, trend = productivityTrend))
        return healthPush(magic, items)
    }

    // ── watchface (SID 0x1F, read only) ────────────────────────────────────────────────

    /** READ ONLY `{1: 0, 2: m, 3: {1: 1}}`. No upload builder on purpose. */
    fun watchfaceInfoRequest(magic: Int): ByteArray = varintField(1, 0) + varintField(2, magic) + messageField(3, varintField(1, 1))
}
