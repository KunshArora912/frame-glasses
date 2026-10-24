package com.kunsh.g2core

import com.kunsh.g2core.Pb.hex
import com.kunsh.g2core.Pb.unhex
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.Parameterized

/**
 * Golden vectors exported from the Python library (g2/tools/export_vectors.py -> src/test/resources/vectors.json).
 * One JUnit test per vector: the Kotlin builder is called with the same inputs and must produce the same bytes.
 */
@RunWith(Parameterized::class)
class VectorsTest(private val name: String, private val fn: String, private val args: Map<String, Any?>, private val out: Any?) {

    companion object {
        @JvmStatic
        @Parameterized.Parameters(name = "{0}")
        fun data(): List<Array<Any?>> {
            val text = VectorsTest::class.java.classLoader!!.getResourceAsStream("vectors.json")!!.readBytes().toString(Charsets.UTF_8)
            @Suppress("UNCHECKED_CAST")
            val list = MiniJson.parse(text) as List<Map<String, Any?>>
            return list.map { arrayOf(it["name"], it["fn"], it["args"], it["out"]) }
        }
    }

    // ── arg helpers ────────────────────────────────────────────────────────────────────
    private fun i(k: String): Int = (args[k] as Number).toInt()
    private fun l(k: String): Long = (args[k] as Number).toLong()
    private fun f(k: String): Float = (args[k] as Number).toFloat()
    private fun d(k: String): Double = (args[k] as Number).toDouble()
    private fun s(k: String): String = args[k] as String
    private fun b(k: String): Boolean = args[k] as Boolean
    private fun h(k: String): ByteArray = unhex(args[k] as String)
    private fun iOpt(k: String): Int? = (args[k] as Number?)?.toInt()
    private fun sOpt(k: String): String? = args[k] as String?
    private fun has(k: String) = args.containsKey(k)
    private fun iOr(k: String, def: Int) = if (has(k)) i(k) else def
    private fun sOr(k: String, def: String) = if (has(k)) s(k) else def

    @Suppress("UNCHECKED_CAST")
    private fun list(k: String): List<Any?> = args[k] as List<Any?>

    @Suppress("UNCHECKED_CAST")
    private fun maps(k: String): List<Map<String, Any?>> = args[k] as List<Map<String, Any?>>

    private fun ints(k: String): List<Int> = list(k).map { (it as Number).toInt() }

    private fun schedule(m: Map<String, Any?>) = Dashboard.Schedule(
        m["title"] as String, (m["location"] as String?) ?: "", (m["time_str"] as String?) ?: "", (m["end_ts"] as Number?)?.toLong() ?: 0,
        (m["schedule_id"] as Number?)?.toLong())

    private fun task(m: Map<String, Any?>) = Dashboard.Task((m["uid"] as Number).toLong(), m["title"] as String, (m["completed"] as Boolean?) ?: false,
        (m["timestamp"] as Number?)?.toLong() ?: 0, (m["ts_type"] as Number?)?.toInt() ?: Dashboard.QL_TS_TIME)

    @Suppress("UNCHECKED_CAST")
    private fun stock(m: Map<String, Any?>) = Dashboard.Stock(
        code = m["code"] as String?, marketSuffix = m["market_suffix"] as String?, marketCap = (m["market_cap"] as Number?)?.toDouble(),
        changePercent = (m["change_percent"] as Number?)?.toFloat(), price = (m["price"] as Number?)?.toFloat(), name = m["name"] as String?,
        dayHigh = (m["day_high"] as Number?)?.toFloat(), dayLow = (m["day_low"] as Number?)?.toFloat(), open = (m["open"] as Number?)?.toFloat(),
        volume = (m["volume"] as Number?)?.toLong(), marketValue = (m["market_value"] as Number?)?.toDouble(),
        peRatio = (m["pe_ratio"] as Number?)?.toFloat(), trend = (m["trend"] as Number?)?.toInt(), pointTotal = (m["point_total"] as Number?)?.toInt(),
        darkPrice = (m["dark_price"] as Number?)?.toFloat(), points = (m["points"] as List<Any?>?)?.map { (it as Number).toFloat() })

    private fun hexes(v: List<ByteArray>) = v.map { hex(it) }
    private fun stepsJson(steps: List<Step>): List<Map<String, Any?>> = steps.map { st ->
        mapOf("number" to st.number.toLong(), "label" to st.label, "action" to st.action.name.lowercase(), "side" to st.side.wire,
            "sid" to st.sid?.toLong(), "magic" to st.magic?.toLong(), "frames" to hexes(st.frames), "wait_ms" to st.waitMs,
            "expect" to st.expect?.label, "timeout_ms" to st.timeoutMs)
    }

    private fun alloc() = MagicAllocator()

    /** Compute the Kotlin result in the same shape as the JSON "out". */
    private fun run(): Any? = when (fn) {
        // pb
        "pb.encode_varint" -> hex(Pb.encodeVarint(l("value")))
        "pb.writer_mixed" -> hex(Writer().varint(1, 300).int32(2, -5).bool(3, true).string(4, "héllo €").bytes(5, byteArrayOf(0, -1)).float32(6, 1.5f)
            .double(7, 2.25).message(8, Writer().varint(1, 7)).packedVarints(9, listOf(1L, 3L, 2L, 2L, 300L)).packedFloats(11, listOf(1.0f, -2.5f)).toBytes())
        "pb.float_field" -> hex(Pb.floatField(i("field"), d("value").toFloat()))
        // framing
        "framing.crc16" -> String.format("%04x", Framing.crc16(h("data")))
        "framing.frame_pb" -> hexes(Framing.framePb(h("pb"), i("sid"), i("flag"), i("seq"), i("max_write")))
        "framing.auth_frame" -> hex(Framing.authFrame(i("magic"), iOpt("seq")))
        "framing.add_crc" -> hex(Framing.addCrc(h("packet")))
        "framing.assemble" -> {
            val asm = FrameAssembler()
            val outs = list("packets").map { asm.feed(unhex(it as String)) }
            require(outs.dropLast(1).all { it == null })
            val last = outs.last()!!
            mapOf("assembled" to hex(last), "payload" to hex(Framing.parseFrame(last)!!.payload), "crc_errors" to asm.crcErrors.toLong())
        }
        "framing.assemble_seq" -> {
            val asm = FrameAssembler()
            val outs = list("packets").map { asm.feed(unhex(it as String))?.let { o -> hex(o) } }
            mapOf("outs" to outs, "crc_errors" to asm.crcErrors.toLong())
        }
        "framing.read_ack_magic" -> Framing.readAckMagic(h("pb"))?.toLong()
        // session
        "session.prelude_payload" -> hex(Session.PRELUDE_PAYLOAD)
        "session.prelude_frame" -> hex(Session.PRELUDE_FRAME)
        "session.auth_payload" -> hex(Session.authPayload(i("magic")))
        "session.role_change" -> hex(Session.roleChange(i("magic"), i("role")))
        "session.time_sync" -> hex(Session.timeSync(i("magic"), l("unix_s"), i("tz_units")))
        "session.tz_units_from_offset" -> Session.tzUnitsFromOffset(i("offset_seconds")).toLong()
        "session.keepalive" -> hex(Session.keepalive(i("magic")))
        "session.keepalive_frame" -> hex(Session.keepaliveFrame(i("magic"), i("seq")))
        "session.evenhub_heartbeat" -> hex(Session.evenhubHeartbeat(i("magic")))
        "session.evenhub_heartbeat_frame" -> hexes(Framing.framePb(Session.evenhubHeartbeat(i("magic")), 0xE0, 0x20, i("seq")))
        "session.app_sync_subscribe" -> hex(Session.appSyncSubscribe(i("magic")))
        "session.onboarding_finish" -> hex(Session.onboardingFinish(i("magic")))
        "session.wear_query" -> hex(Session.wearQuery(i("magic")))
        "session.case_query" -> hex(Session.caseQuery(i("magic")))
        "session.connect_sequence" -> Session.connectSequence(alloc()::nextMagic, l("now"), i("tz_units"), b("full"))
            .map { listOf(it.sid.toLong(), hex(it.payload), it.side.wire) }
        "session.minimal_dashboard_plan" -> stepsJson(MinimalDashboardPlan(alloc(), l("now"), i("tz_units"), f("temperature_c"), i("weather_type"),
            s("weather_status")).steps())
        // settings
        "settings.read_settings" -> hex(Settings.readSettings(i("magic"), i("setting_type")))
        "settings.brightness" -> hex(Settings.brightness(i("magic"), i("level")))
        "settings.auto_brightness" -> hex(Settings.autoBrightness(i("magic"), b("on")))
        "settings.brightness_calibration" -> hex(Settings.brightnessCalibration(i("magic"), i("left"), i("right")))
        "settings.display_height" -> hex(Settings.displayHeight(i("magic"), i("level")))
        "settings.display_distance" -> hex(Settings.displayDistance(i("magic"), i("level")))
        "settings.head_up" -> hex(Settings.headUp(i("magic"), b("enabled"), iOpt("angle")))
        "settings.wear_detection" -> hex(Settings.wearDetection(i("magic"), b("on")))
        "settings.silent_mode" -> hex(Settings.silentMode(i("magic"), b("on")))
        "settings.units" -> hex(Settings.units(i("magic"), i("unit_format"), i("distance_unit"), i("time_format"), i("date_format"), i("temperature_unit")))
        "settings.gestures" -> hex(Settings.gestures(i("magic"), list("entries").map { e ->
            val x = (e as List<*>).map { (it as Number).toInt() }; Triple(x[0], x[1], x[2]) }))
        "settings.dominant_hand" -> hex(Settings.dominantHand(i("magic"), i("hand"), sOpt("ring_mac")?.let { unhex(it) }))
        "settings.app_ack" -> hex(Settings.appAck(i("magic"), i("package_id"), i("flag")))
        "settings.display_timeout" -> hex(Settings.displayTimeout(i("magic"), iOpt("seconds")))
        "settings.read_display_timeout" -> hex(Settings.readDisplayTimeout(i("magic")))
        "settings.language" -> hex(Settings.language(i("magic"), i("index")))
        "settings.get_device_info" -> hex(Settings.getDeviceInfo(i("magic"), i("item")))
        "settings.quick_restart" -> hex(Settings.quickRestart(i("magic")))
        "settings.factory_reset" -> hex(Settings.factoryReset(i("magic")))
        "settings.unpair" -> hex(Settings.unpair(i("magic")))
        // dashboard
        "dashboard.dashboard_init" -> hex(Dashboard.dashboardInit(i("magic")))
        "dashboard.dashboard_layout" -> hex(Dashboard.dashboardLayout(i("magic"), ints("widget_order")))
        "dashboard.display_enable" -> hex(Dashboard.displayEnable(i("magic"), ints("widget_types")))
        "dashboard.quicklist_display_enable" -> hex(Dashboard.quicklistDisplayEnable(i("magic")))
        "dashboard.weather" -> hex(Dashboard.weather(i("magic"), Dashboard.Weather(f("temperature_c"), i("unit"), i("weather_type"), l("update_time_ms"),
            s("status"), iOr("rainfall_icon", 0), sOr("rainfall_hint", ""), iOr("sunset_select", 0), sOr("sunset_hint", "")), iOpt("package_id")))
        "dashboard.calendar_event" -> hex(Dashboard.calendarEvent(i("magic"), (args["schedule"] as Map<String, Any?>?)?.let { schedule(it) },
            i("total"), i("index"), i("authority")))
        "dashboard.calendar_events" -> hex(Dashboard.calendarEvents(i("magic"), maps("schedules").map { schedule(it) }))
        "dashboard.calendar_sequence" -> hexes(Dashboard.calendarSequence(alloc()::nextMagic, maps("schedules").map { schedule(it) }))
        "dashboard.news_reset" -> hex(Dashboard.newsReset(i("magic")))
        "dashboard.news_fifo_count_request" -> hex(Dashboard.newsFifoCountRequest(i("magic")))
        "dashboard.news_push" -> hex(Dashboard.newsPush(i("magic"), i("slot"), Dashboard.NewsItem(s("title"), s("source"), s("content")),
            l("report_time_local_ms"), i("session_total")))
        "dashboard.quicklist_full_update" -> hex(Dashboard.quicklistFullUpdate(i("magic"), maps("tasks").map { task(it) }))
        "dashboard.quicklist_add_page" -> hex(Dashboard.quicklistAddPage(i("magic"), maps("tasks").map { task(it) }, i("first_index")))
        "dashboard.quicklist_empty" -> hex(Dashboard.quicklistEmpty(i("magic")))
        "dashboard.tasks_sequence" -> Dashboard.tasksSequence(alloc()::nextMagic, maps("tasks").map { task(it) })
            .map { listOf(it.sid.toLong(), hex(it.payload), it.expect) }
        "dashboard.stock_push" -> hex(Dashboard.stockPush(i("magic"), stock(args["stock"] as Map<String, Any?>), i("total"), i("index")))
        "dashboard.stock_display_enable" -> hex(Dashboard.stockDisplayEnable(i("magic")))
        "dashboard.health_snapshot" -> hex(Dashboard.healthSnapshot(i("magic"), steps = f("steps"), stepGoal = i("step_goal"), heartRate = f("heart_rate"),
            heartRateAvg = f("heart_rate_avg"), sleepMinutes = i("sleep_minutes")))
        "dashboard.watchface_info_request" -> hex(Dashboard.watchfaceInfoRequest(i("magic")))
        // menu
        "menu.menu_push" -> hex(Menu.menuPush(i("magic"), ints("builtin_ids"),
            maps("plugins").map { Menu.PluginItem(it["name"] as String, (it["app_id"] as Number).toInt(), (it["icon"] as Number).toInt()) }))
        // notifications
        "notifications.crc32_efs" -> String.format("%08x", Notifications.crc32Efs(h("data")))
        "notifications.crc32c" -> String.format("%08x", Notifications.crc32c(h("data")))
        "notifications.notification_ctrl" -> hex(Notifications.notificationCtrl(i("magic"), b("enable"), b("auto_display"), i("disp_time"), b("dnd")))
        "notifications.whitelist_disable" -> hex(Notifications.whitelistDisable(i("magic"), b("disable")))
        "notifications.ios_register" -> hex(Notifications.iosRegister(i("magic"), s("app_id"), s("display_name")))
        "notifications.notification_json" -> hex(Notifications.notificationJson(s("title"), s("message"), s("subtitle"), s("display_name"),
            s("app_identifier"), (args["msg_id"] as Number?)?.toLong(), l("now"), i("utc_offset_s")))
        "notifications.efs_start" -> hex(Notifications.efsStart(i("seq"), h("data")))
        "notifications.efs_data_prefix" -> hex(Notifications.efsDataPrefix(i("seq")))
        "notifications.efs_result_check" -> hex(Notifications.efsResultCheck(i("seq")))
        "notifications.efs_data_packets" -> hexes(Notifications.efsDataPackets(i("seq"), h("block")))
        "notifications.hub_notification_steps" -> Notifications.hubNotificationSteps(alloc()::nextSeq, h("data")).map {
            mapOf("name" to it.name, "frames" to hexes(it.frames), "expect_cid" to it.expectCid.toLong(), "offset" to it.offset.toLong()) }
        "notifications.community_notification_frames" -> hexes(Notifications.communityNotificationFrames(h("data")))
        "notifications.community_data" -> hexes(Notifications.communityData(h("data")))
        "notifications.community_file_check" -> hex(Notifications.communityFileCheck(h("data"), i("seq")))
        // even ai
        "even_ai.enter" -> hex(EvenAi.enter(i("magic")))
        "even_ai.exit_" -> hex(EvenAi.exit(i("magic")))
        "even_ai.heartbeat" -> hex(EvenAi.heartbeat(i("magic"), i("count")))
        "even_ai.config" -> hex(EvenAi.config(i("magic"), i("voice_switch"), i("stream_speed")))
        "even_ai.config_captured" -> hex(EvenAi.configCaptured(i("magic")))
        "even_ai.ask" -> hex(EvenAi.ask(i("magic"), s("text"), b("community")))
        "even_ai.analyse" -> hex(EvenAi.analyse(i("magic")))
        "even_ai.reply" -> hex(EvenAi.reply(i("magic"), s("chunk"), b("final"), b("community")))
        "even_ai.reply_chunks" -> mapOf("chunks" to hexes(EvenAi.replyChunks(s("text"), i("limit"))))
        "even_ai.reply_sequence" -> hexes(EvenAi.replySequence(alloc()::nextMagic, s("text")))
        "even_ai.vad_info" -> hex(EvenAi.vadInfo(i("magic"), i("status")))
        "even_ai.prompt" -> hex(EvenAi.prompt(i("magic"), i("code")))
        "even_ai.event" -> hex(EvenAi.event(i("magic"), i("event_id")))
        "even_ai.evenhub_audio_control" -> hex(EvenAi.evenhubAudioControl(i("magic"), b("enable")))
        // apps
        "apps.tp_start" -> hex(Apps.tpStart(i("magic"), i("total_pages"), i("total_lines"), i("mode"), i("start_page"), i("start_line"),
            i("scroll_interval_ms"), i("countdown_s")))
        "apps.tp_page" -> hex(Apps.tpPage(i("magic"), i("page_id"), list("lines").map { it as String }))
        "apps.tp_heartbeat" -> hex(Apps.tpHeartbeat(i("magic"), i("page_id"), i("line_id")))
        "apps.tp_close" -> hex(Apps.tpClose(i("magic")))
        "apps.tp_file_list" -> hex(Apps.tpFileList(i("magic"), list("files").map { val x = it as List<*>; (x[0] as String).toByteArray() to x[1] as String },
            iOpt("menu_mode")))
        "apps.tp_paginate" -> mapOf("pages" to Apps.tpPaginate(s("script")))
        "apps.community_tp_frames" -> hexes(Apps.communityTpFrames(s("text")))
        "apps.tr_open" -> hex(Apps.trOpen(i("magic"), s("src_lang"), s("dst_lang"),
            langList = list("lang_list").map { val x = it as List<*>; x[0] as String to x[1] as String }))
        "apps.tr_text" -> hex(Apps.trText(i("magic"), s("src_text"), s("dst_text"), b("final"), sOpt("speaker")?.toByteArray()))
        "apps.tr_heartbeat" -> hex(Apps.trHeartbeat(i("magic")))
        "apps.tr_close" -> hex(Apps.trClose(i("magic")))
        "apps.nav_basic_info" -> hex(Apps.navBasicInfo(i("magic"), s("direction"), s("distance"), s("road"), eta = s("eta"), method = i("method"),
            counter = i("counter")))
        "apps.nav_start" -> hex(Apps.navStart(i("magic")))
        "apps.nav_location_list" -> hex(Apps.navLocationList(i("magic"), list("names").map { it as String }, ints("icons")))
        "apps.nav_error_info" -> hex(Apps.navErrorInfo(i("magic"), s("message"), i("index")))
        else -> throw AssertionError("no Kotlin dispatch for $fn")
    }

    @Test
    fun matchesPython() {
        assertEquals("$name ($fn)", out, run())
    }
}
