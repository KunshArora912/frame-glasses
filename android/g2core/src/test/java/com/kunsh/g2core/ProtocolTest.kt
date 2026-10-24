package com.kunsh.g2core

import com.kunsh.g2core.Pb.hex
import com.kunsh.g2core.Pb.unhex
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Random

/** Parsers, validation and invariants (ports of the non-vector checks in g2/test_native.py). */
class ProtocolTest {
    private val H = ::unhex

    // ── pb ────────────────────────────────────────────────────────────────────────────
    @Test fun varintRoundTripAndNegatives() {
        for (v in listOf(0L, 1, 127, 128, 300, 156, 65365, 0xFFFFFFFFL, Long.MAX_VALUE, -1, -16, -24)) {
            val e = Pb.encodeVarint(v)
            assertEquals(v, Pb.decodeVarint(e).first)
            assertEquals(e.size, Pb.decodeVarint(e).second)
        }
        assertEquals("f0ffffffffffffffff01", hex(Pb.encodeVarint(-16)))
        assertEquals(10, Pb.encodeVarint(-1).size)
    }

    @Test fun writerReaderRoundTrip() {
        val b = Writer().varint(1, 300).int32(2, -5).bool(3, true).string(4, "héllo").bytes(5, byteArrayOf(0, -1)).float32(6, 1.5f).double(7, 2.25)
            .message(8, Writer().varint(1, 7)).packedVarints(9, listOf(1L, 3L, 2L, 2L)).repeated(10, listOf(H("0801"), H("0802"))).toBytes()
        val r = Reader(b)
        assertEquals(300L, r.varint(1)); assertEquals(-5L, r[2]!!.asSigned); assertEquals(1L, r.varint(3)); assertEquals("héllo", r.string(4))
        assertArrayEquals(byteArrayOf(0, -1), r.bytes(5)); assertEquals(1.5f, Pb.readFloat(b, 6)); assertEquals(2.25, Pb.readDouble(b, 7), 0.0)
        assertEquals(7L, r.sub(8)!!.varint(1)); assertEquals(listOf(1L, 3L, 2L, 2L), Pb.readPackedVarints(b, 9))
        assertEquals(listOf("0801", "0802"), Pb.readMessages(b, 10).map { hex(it) })
    }

    @Test fun iterFieldsClipsAndStops() {
        val fields = Pb.iterFields(H("0d0000a841110000000000000440120361626318 01".replace(" ", "")))
        assertEquals(listOf(1 to 5, 2 to 1, 2 to 2, 3 to 0), fields.map { it.number to it.wire })
        assertEquals(21.0f, fields[0].asFloat); assertEquals(2.5, fields[1].asDouble, 0.0)
        assertEquals("aabb", hex(Pb.readBytes(H("12ff01aabb"), 2)!!))     // length past the end is clipped
        assertEquals(1, Pb.iterFields(H("08011c")).size)                    // unknown wire type stops
        assertEquals(0, Pb.iterFields(H("08")).size)                        // truncated varint stops
    }

    // ── framing ───────────────────────────────────────────────────────────────────────
    @Test fun crcCheckValues() {
        assertEquals(0x29B1, Framing.crc16("123456789".toByteArray()))
        assertEquals("ffff", hex(Framing.crc16Bytes(ByteArray(0))))
        assertEquals(0xC052A8C8L, Notifications.crc32Efs("123456789".toByteArray()))
        assertEquals(0xE3069283L, Notifications.crc32c("123456789".toByteArray()))
    }

    @Test fun preludeFrameParses() {
        val p = Framing.parseFrame(Session.PRELUDE_FRAME)!!
        assertEquals(listOf(0x92, 0x13, 1, 1, 0x01, 0x20), listOf(p.seq, p.length, p.total, p.num, p.sid, p.flag))
        assertEquals(true, p.crcOk)
        assertEquals(p.length, p.payload.size + 2)
    }

    @Test fun authFrameMagicValidation() {
        assertThrows(IllegalArgumentException::class.java) { Framing.authFrame(0) }
        assertThrows(IllegalArgumentException::class.java) { Framing.authFrame(256) }
        val p = Framing.parseFrame(Framing.authFrame(200))!!
        assertEquals(200L, Pb.readVarint(p.payload, 2)); assertEquals(true, p.crcOk); assertEquals(4, Session.parseDevice(p.payload).cmd)
    }

    @Test fun framingPropertyRoundTrip() {
        val rng = Random(1234)
        repeat(300) {
            val sizes = intArrayOf(0, 1, 2, 229, 230, 231, 232, 233, 463, 464, 465, rng.nextInt(1500))
            val n = sizes[rng.nextInt(sizes.size)]
            val payload = ByteArray(n).also { rng.nextBytes(it) }
            val sid = rng.nextInt(256); val flag = if (rng.nextBoolean()) 0 else 0x20; val seq = rng.nextInt(256)
            val frames = Framing.framePb(payload, sid, flag, seq)
            assertEquals(maxOf(1, (n + 2 + 231) / 232), frames.size)
            assertTrue(frames.all { it.size <= 240 })
            val asm = FrameAssembler()
            var out: ByteArray? = null
            for (f in frames) { assertNull(out); out = asm.feed(f) }
            val p = Framing.parseFrame(out!!)!!
            assertEquals(listOf(sid, flag, seq), listOf(p.sid, p.flag, p.seq))
            assertArrayEquals(payload, p.payload); assertEquals(true, p.crcOk); assertEquals(0, asm.crcErrors)
        }
    }

    @Test fun maxWriteLimitsChunk() {
        assertEquals(listOf(52, 50), Framing.framePb(ByteArray(100), 1, 0x20, 1, maxWrite = 60).map { it[3].toInt() })
        assertThrows(IllegalArgumentException::class.java) { Framing.framePb(ByteArray(1), 1, 0, 1, maxWrite = 8) }
    }

    @Test fun assemblerPassthrough() {
        val asm = FrameAssembler()
        val single = Framing.framePb(H("0801"), 0x09, 0x20, 3)[0]
        assertArrayEquals(single, asm.feed(single))
        assertArrayEquals("junk".toByteArray(), asm.feed("junk".toByteArray()))
    }

    @Test fun parseRxAndAckMagic() {
        val rx = rxFrame(0x01, H("080210661a0408011000"))
        assertEquals(0x12, rx[1].toInt())
        val (sid, payload) = Framing.parseRx(rx)
        assertEquals(0x01, sid); assertEquals("080210661a0408011000", hex(payload))
        assertEquals(0x66, Framing.readAckMagic(payload))
        assertNull(Framing.parseRx(Session.PRELUDE_FRAME).first)
        assertNull(Framing.parseFrame(H("aa1200")))
    }

    @Test fun magicAllocatorWrapsNeverZero() {
        val a = MagicAllocator()
        assertEquals(101, a.nextMagic()); assertEquals(11, a.nextSeq())
        val seen = List(600) { a.nextMagic() }
        assertFalse(0 in seen); assertEquals(255, seen.max()); assertEquals(1, seen[seen.indexOf(255) + 1])
        val s = MagicAllocator(seqStart = 254)
        assertEquals(listOf(255, 0, 1), List(3) { s.nextSeq() })
    }

    // ── session ───────────────────────────────────────────────────────────────────────
    @Test fun flagRule() {
        assertEquals(0x00, Session.flagFor(0x80, Session.authPayload(1)))
        assertEquals(0x00, Session.flagFor(0x80, Session.keepalive(1)))
        assertEquals(0x20, Session.flagFor(0x80, Session.roleChange(1)))
        assertEquals(0x20, Session.flagFor(0x80, Session.timeSync(1, 0, 0)))
        assertEquals(0x20, Session.flagFor(0x01, Session.PRELUDE_PAYLOAD))
    }

    @Test fun timeSyncValidationAndParse() {
        assertThrows(IllegalArgumentException::class.java) { Session.timeSync(1, -1, 0) }
        assertThrows(IllegalArgumentException::class.java) { Session.timeSync(1, 0, 200) }
        val d = Session.parseDevice(Session.timeSync(5, 1792497600, -16))
        assertEquals(128, d.cmd); assertEquals(1792497600L, d.timestamp); assertEquals(-16L, d.timezone)
    }

    @Test fun sessionParsers() {
        assertTrue(Session.parseAuthReply(H("080410011a00")))
        assertFalse(Session.parseAuthReply(H("1a00")))
        assertFalse(Session.parseAuthReply(H("0804100122020800")))
        val ka = Session.parseDevice(H("080e106b6a00"))
        assertTrue(ka.isKeepaliveEcho); assertEquals("BASE_CONNECT_HEART_BEAT", ka.name)
        val c = Session.parseCase(H("080110141a0a083c1000180120012800"))!!
        assertEquals(listOf(60, 0, 1, 1, 0), listOf(c.battery, c.charging, c.lid, c.inCase, c.error))
        assertNull(Session.parseCase(H("08011014")))
        assertEquals(true, Session.parseWearReply(H("080310132a0408011001")))
        assertEquals(false, Session.parseWearReply(H("080310132a0408011000")))
        assertNull(Session.parseWearReply(H("080110131a020804")))
        val a = Session.parseAppSync(H("08011a0408001007"))
        assertEquals(0, a.background); assertEquals(7, a.foreground)
        assertEquals(4, Session.parseOnboarding(H("080110121a0408041000")).processId)
    }

    @Test fun hubStartupPlanShape() {
        val steps = HubStartupPlan(MagicAllocator(), now = 1792497600, tzUnits = -16).steps()
        assertEquals(listOf(Side.LEFT, Side.RIGHT), steps.take(2).map { it.side })
        assertEquals(Expect.Auth, steps[1].expect)
        assertNull(steps[0].expect)
        assertTrue(steps.none { it.action != StepAction.SEND })
        assertEquals(listOf(0x80, 0x80, 0x01, 0x0D, 0x80, 0x80, 0x09, 0x09, 0x10, 0x10, 0x81, 0x09, 0x04, 0x04), steps.map { it.sid })
        val hb = HubStartupPlan(MagicAllocator(), now = 0, tzUnits = 0, heartbeat = true, menu = Menu.DEFAULT_MENU).steps()
        assertEquals(StepAction.START_HEARTBEAT, hb[3].action)
        assertEquals(StepAction.STOP_HEARTBEAT, hb.last().action)
        assertEquals(0x03, hb[hb.size - 2].sid)
        // every frame of a plan carries a distinct seq except the fixed prelude
        val seqs = hb.mapNotNull { it.frame?.get(2) }
        assertEquals(seqs.size - 1, seqs.toSet().size)
    }

    // ── settings ──────────────────────────────────────────────────────────────────────
    @Test fun settingsRangeValidation() {
        val bad = listOf<() -> Unit>({ Settings.brightness(1, 0) }, { Settings.brightness(1, 51) }, { Settings.displayHeight(1, 21) },
            { Settings.displayDistance(1, -1) }, { Settings.headUp(1, true, 61) }, { Settings.units(1, unitFormat = 16) },
            { Settings.gestures(1, emptyList()) }, { Settings.dominantHand(1, 2) }, { Settings.dominantHand(1, 1, ByteArray(1)) },
            { Settings.displayTimeout(1, 7) }, { Settings.language(1, 9) }, { Settings.language(1, "Klingon") }, { Settings.getDeviceInfo(1, 9) },
            { Settings.readSettings(1, 2) })
        for (f in bad) assertThrows(IllegalArgumentException::class.java) { f() }
        assertEquals(hex(Settings.language(3, 8)), hex(Settings.language(3, "korean")))
    }

    @Test fun snapshotParse() {
        val snap = Writer().varint(1, 1).varint(2, 30).varint(3, 10).varint(4, 12).string(5, "1.2.3").string(6, "1.2.4").varint(7, 1).varint(8, 25)
            .varint(10, 1).varint(12, 87).varint(13, 1).varint(14, 0).varint(18, 1).varint(19, 2).varint(40, 9)
        val m = Settings.parse(Writer().varint(1, 2).varint(2, 0x6A).message(4, snap).toBytes())
        val s = m.snapshot!!
        assertEquals(listOf<Any?>(2, 0x6A, "READ"), listOf(m.cmd, m.magic, m.name))
        assertEquals(listOf<Any?>(87, 1, 10, 12, "1.2.3", "1.2.4"), listOf(s.battery, s.charging, s.displayHeight, s.displayDistance, s.leftVersion, s.rightVersion))
        assertEquals(listOf<Any?>(1, 25, 30, 1, 2), listOf(s.headUp, s.headUpAngle, s.brightnessLevel, s.brightnessAuto, s.unreadCount))
        assertFalse(s.isPush); assertEquals(9L, s.extra[40])
        val push = Settings.parse(Writer().varint(2, 3).message(4, Writer().varint(12, 50).varint(13, 0)).toBytes())
        assertTrue(push.snapshot!!.isPush); assertEquals(50, push.snapshot!!.battery)
        assertEquals(Settings.Ack(1, 0), Settings.parse(H("0804100532040801 1000".replace(" ", ""))).ack)
        assertEquals(true, Settings.parse(H("08041005320408011000")).ok)
        assertEquals(Settings.Ack(1, 1), Settings.parseAck(H("080410053a0408011001")))
        assertEquals(0 to 1, Settings.parse(H("080310052a0408001001")).event)
    }

    @Test fun moduleParse() {
        val m = Settings.parseModule(H("0801108001220408d5fe03"))
        assertEquals(65365, m.timeout); assertEquals(true, m.timeoutNever)
        assertEquals(false, Settings.parseModule(H("0802100122020805")).timeoutNever)
        assertEquals(7, Settings.parseModule(H("080010011a020807")).language)
    }

    // ── dashboard ─────────────────────────────────────────────────────────────────────
    @Test fun dashboardAcks() {
        assertEquals(0, Dashboard.ackFlag(H("080210661a0408011000")))
        assertEquals(1, Dashboard.ackFlag(H("080210661a0408011001")))
        assertEquals(0, Dashboard.ackFlag(H("080210661a00")))
        assertNull(Dashboard.ackFlag(H("08021066")))
        assertNull(Dashboard.ackFlag(null))
    }

    @Test fun newsParsers() {
        assertEquals(5, Dashboard.newsFifoCount(H("0808107752020805")))
        assertEquals(5, Dashboard.newsFifoCount(null))
        assertEquals(3, Dashboard.newsFifoCount(H("0808107752020803")))
        assertEquals(0, Dashboard.newsStatus(H("080a107862022800")))
        val ev = Dashboard.parseNewsEvent(H("080c100072040803 1002".replace(" ", "")))!!
        assertEquals(listOf<Any?>(3, 2, "OPEN_ARTICLE"), listOf(ev.eventId, ev.newsId, ev.eventName))
        assertTrue(Dashboard.parseNewsEvent(H("080b1000"))!!.refreshRequested)
        assertNull(Dashboard.parseNewsEvent(H("08021000")))
    }

    @Test fun quicklistParsersAndPaging() {
        val tasks = listOf(Dashboard.Task(7, "Milk"), Dashboard.Task(8, "Eggs", true))
        val p = Dashboard.quicklistFullUpdate(0x20, tasks)
        assertEquals(listOf(Triple(7L, false, "Milk"), Triple(8L, true, "Eggs")), Dashboard.parseQuicklistTicks(p).map { Triple(it.uid, it.completed, it.title) })
        assertEquals(listOf(Dashboard.TaskTick(7, 1, true, null)), Dashboard.parseQuicklistTicks(H("080110211a06080710011801")))
        assertEquals(2 to 7L, Dashboard.parseQuicklistEvent(H("080310222a0408021007")))
        assertNull(Dashboard.parseQuicklistEvent(p))
        val many = List(50) { Dashboard.Task(it.toLong(), "$it") }
        assertEquals((11L..30L).toList(), Dashboard.pageAround(many, Dashboard.QL_EVENT_DOWN, 10).map { it.uid })
        assertEquals((0L..4L).toList(), Dashboard.pageAround(many, Dashboard.QL_EVENT_UP, 5).map { it.uid })
        assertTrue(Dashboard.pageAround(many, Dashboard.QL_EVENT_UP, 999).isEmpty())
        assertThrows(IllegalArgumentException::class.java) { Dashboard.quicklistFullUpdate(1, many) }
        val title = Pb.readBytes(Pb.readMessages(Reader(Dashboard.quicklistFullUpdate(1, listOf(Dashboard.Task(1, "é".repeat(150))))).bytes(4), 3)[0], 5)!!
        assertEquals(201, title.size); assertEquals(0, title.last().toInt())
    }

    @Test fun calendarCapsAndSequence() {
        val long = Dashboard.Schedule("é".repeat(200))
        val t = Pb.readBytes(Reader(Dashboard.calendarEvent(1, long, 1, 0)).sub(4)!!.sub(3)!!.sub(2)!!.sub(3)!!.bytes(3), 2)!!
        assertTrue(t.size <= 175)
        assertTrue(String(t, Charsets.UTF_8).endsWith("..."))
        assertEquals(8, Dashboard.calendarSequence(MagicAllocator()::nextMagic, List(10) { long }).size)
        assertThrows(IllegalArgumentException::class.java) { Dashboard.stockDisplayEnable(1, listOf(1, 3)) }
    }

    // ── menu / notifications / even ai / apps ─────────────────────────────────────────
    @Test fun menuValidationAndSelection() {
        assertNull(Menu.validate(listOf(1, 4, 5, 6, 7)))
        assertTrue(Menu.validate(listOf(1, 4))!!.contains("5 to 10"))
        assertTrue(Menu.validate(listOf(1, 1, 4, 5, 6))!!.contains("duplicate"))
        assertTrue(Menu.validate(listOf(1, 2, 4, 5, 6))!!.contains("not a menu app"))
        assertThrows(IllegalArgumentException::class.java) { Menu.menuPush(1, listOf(1, 4), strict = true) }
        assertEquals("custom", Menu.pluginName("  \u0001 "))
        assertEquals(1 to 0x7B, Menu.parseMenuReply(H("0801107b")))
        val e = Menu.parseSelectionEvent(0x0D, H("08011a0408001006"))!!
        assertEquals(listOf<Any?>("foreground", 6, "Teleprompt"), listOf(e.kind, e.appId, e.appName))
        assertEquals("plugin" to 10000, Menu.parseSelectionEvent(0xE0, H("08111005a2010308904e"))!!.let { it.kind to it.appId })
        assertEquals("MENU_STARTUP", Menu.parseSelectionEvent(0x08, H("080110051a00"))!!.detail)
        assertEquals("Teleprompt", Menu.parseSelectionEvent(0x06, H("08a2011005"))!!.appName)
        assertEquals("WAKE_UP", Menu.parseSelectionEvent(0x07, H("080110051a020801"))!!.detail)
        assertNull(Menu.parseSelectionEvent(0x07, H("080110051a020803")))
        assertNull(Menu.parseSelectionEvent(0x09, H("08021005")))
    }

    @Test fun notificationParseAndLimits() {
        val m = Notifications.parseNotification(Notifications.notificationCtrl(0x10))
        assertEquals(listOf<Any?>(1, 1, 1, 5, 0), listOf(m.cmd, m.enable, m.autoDisplay, m.dispTime, m.dnd))
        val w = Notifications.parseNotification(H("080310113a0708d20910011800"))
        assertEquals(1234L, w.whitelistCrc); assertEquals(1, w.checkStatus)
        assertThrows(IllegalArgumentException::class.java) { Notifications.communityNotificationFrames(ByteArray(300)) }
        assertEquals(listOf(236, 68), Notifications.communityData(ByteArray(300)).map { it[3].toInt() and 0xFF })
        assertEquals(1 to 0, Notifications.parseFileReply(byteArrayOf(1, 0)))
        assertNull(Notifications.parseFileReply(byteArrayOf(1)))
        val hub = Notifications.efsStart(0x10, "{}".repeat(20).toByteArray())
        assertArrayEquals(hub, Notifications.communityFileCheck("{}".repeat(20).toByteArray(), 0x10))
        assertEquals(8 + 93 + 2, hub.size)
        val steps = Notifications.hubNotificationSteps(MagicAllocator()::nextSeq, ByteArray(5000))
        assertEquals(listOf("start", "data", "data", "result_check"), steps.map { it.name })
        assertEquals(1 + 18, steps[1].frames.size)
    }

    @Test fun notificationJsonIsValidAndCapped() {
        val j = String(Notifications.notificationJson("é".repeat(100), "x".repeat(600), displayName = "n".repeat(50), appIdentifier = "a".repeat(80),
            now = 0, utcOffsetS = 0), Charsets.UTF_8)
        @Suppress("UNCHECKED_CAST")
        val d = (MiniJson.parse(j) as Map<String, Any?>)["android_notification"] as Map<String, Any?>
        assertEquals(62, (d["title"] as String).toByteArray().size)
        assertEquals(511, (d["message"] as String).length)
        assertEquals(31, (d["display_name"] as String).length)
        assertEquals(63, (d["app_identifier"] as String).length)
        assertEquals("19700101T000000", d["date"])
        assertTrue(String(Notifications.notificationJson("q\"x", "", now = 0, utcOffsetS = 0)).contains("\"q\\\"x\""))
    }

    @Test fun evenAiParseAndLimits() {
        assertThrows(IllegalArgumentException::class.java) { EvenAi.reply(1, ByteArray(513), true) }
        assertTrue(EvenAi.parse(H("080110051a020801")).isWakeUp)
        assertTrue(EvenAi.parse(H("0808100552020802")).isStreamComplete)
        assertEquals(EvenAi.CMD_VAD_INFO, EvenAi.parse(H("0802100522020802")).cmd)
        assertEquals(3, EvenAi.parse(H("08a101100562020803")).error)
        assertEquals("Q", String(EvenAi.parse(EvenAi.ask(1, "Q")).text!!))
        val rx = Framing.parseFrame(H("aa12c708010107000805103b3a00a8e4"))!!
        assertEquals(listOf<Any?>(0x07, 0x00, true), listOf(rx.sid, rx.flag, rx.crcOk))
        val m = EvenAi.parse(rx.payload)
        assertEquals(listOf<Any?>(5, 59, "REPLY"), listOf(m.cmd, m.magic, m.name))
        assertEquals(listOf(512, 512, 176), EvenAi.replyChunks("é".repeat(600)).map { it.size })
        assertEquals(listOf(-1L, -1L, 1L), EvenAi.replySequence(MagicAllocator()::nextMagic, "é".repeat(600)).map { Pb.readVarint(Pb.readBytes(it, 7), 6) })
    }

    @Test fun micPacketLayout() {
        val frames = List(5) { i -> ByteArray(40) { i.toByte() } }
        val raw = frames.fold(ByteArray(0)) { a, b -> a + b } + byteArrayOf(0x50, 0xFB.toByte(), 90, 0, 0xAB.toByte())
        val p = EvenAi.parseMicPacket(raw)!!
        assertEquals(-1200, p.signalRatio); assertEquals(90, p.direction); assertEquals(0xAB, p.counter); assertEquals(200, p.lc3.size)
        assertArrayEquals(frames[3], p.frames[3])
        assertNull(EvenAi.parseMicPacket(raw.copyOf(199)))
        assertNull(EvenAi.parseMicPacket(raw.copyOf(200))!!.counter)
    }

    @Test fun appsParsersAndCommunityTp() {
        assertEquals(106, Apps.COMMUNITY_TP_DISPLAY_CONFIG.size)
        assertEquals(true, Framing.parseFrame(Apps.communityTpInit(9, 101, 140, true))!!.crcOk)
        val tm = Apps.tpParse(H("08a40110055202 0803".replace(" ", "")))
        assertEquals(3, tm.page)
        assertTrue(Apps.tpParse(H("08aa0110058a0100")).isClose)
        assertTrue(Apps.tpParse(H("08a10110053a020804")).isClose)
        assertEquals(1, Apps.tpParse(H("08a601100562020801")).error)
        assertEquals(1, Apps.trParse(H("08a20110053a020801")).error)
        assertTrue(Apps.trParse(H("08a10110052a020802")).isClose)
        assertEquals("FR", Apps.trParse(H("08a40110054a040a024652")).langKey)
        val sel = Apps.navParse(H("08041005420a0a04486f6d6518012001"))
        assertEquals(Triple("Home", 1, 1), Triple(sel.location, sel.value, sel.method))
        assertTrue(Apps.navParse(H("080d10051a00")).isExit)
        assertEquals(2, Apps.navParse(H("081210055a020802")).method)
        assertEquals(10, Apps.navDirectionIndex("UTURN"))
        assertThrows(IllegalArgumentException::class.java) { Apps.navDirectionIndex("sideways") }
        assertThrows(IllegalArgumentException::class.java) { Apps.tpPage(1, 0, listOf("x".repeat(1100))) }
        assertEquals(14, Apps.communityTpFormat("one two three").size)
    }

    @Test fun g2BleNameAndSerialHelpers() {
        assertEquals(Side.LEFT, G2Ble.sideFromName("Even G2_AB_L_XYZ"))
        assertEquals(Side.RIGHT, G2Ble.sideFromName("even g2_ab_r_xyz"))
        assertNull(G2Ble.sideFromName("Even G2_AB"))
        assertEquals("XYZ", G2Ble.serialFromName("Even G2_AB_L_XYZ"))
        assertEquals("S1234567890ABC", G2Ble.serialFromManufacturer("S1234567890ABC".toByteArray() + ByteArray(6)))
        assertNull(G2Ble.serialFromManufacturer(ByteArray(5)))
        assertEquals("00002760-08c2-11e1-9073-0e8ac72e5401", G2Ble.CHAR_WRITE.toString())
    }

    @Test fun textHelpers() {
        assertEquals("ab", Text.head("abc", 2)); assertEquals("😀", Text.head("😀😀", 1))
        assertEquals("é", Text.utf8Fit("éé", 3)); assertEquals("", Text.utf8Fit("é", 1))
        assertNotNull(Text.utf8Fit("", 0))
    }
}
