package com.kunsh.g2core

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.Timeout

/** G2Session against FakeTransport + fakeGlassesResponder (port of TestTransport in g2/test_native.py). */
class G2SessionTest {
    @get:Rule val timeout: Timeout = Timeout.seconds(30)

    private val now = 1792497600L

    private fun <T> withSession(responder: Responder? = fakeGlassesResponder(), config: G2Session.Config = G2Session.Config(keepalive = false, timeScale = 0.01),
                                body: suspend (FakeTransport, G2Session) -> T): T = runBlocking {
        val t = FakeTransport(responder)
        val sc = CoroutineScope(coroutineContext + Job())
        val g = G2Session(t, sc, MagicAllocator(), { now * 1000 }, -16, config)
        g.start()
        try {
            withTimeout(20_000) { body(t, g) }
        } finally {
            g.stop()
            sc.cancel()
        }
    }

    private fun sid(w: FakeTransport.Write) = Framing.parseFrame(w.frame)!!.sid

    @Test
    fun startupPlanEndToEnd() = withSession { t, g ->
        val res = g.startup()
        assertTrue(res.authenticated)
        assertTrue("missing replies: ${res.missing.map { it.label }}", res.ok)
        assertFalse(g.heartbeatRunning)
        assertEquals(0, g.hbCount)
        val w = t.commandWrites()
        assertEquals(listOf(Side.LEFT to 0x80, Side.RIGHT to 0x80), w.take(2).map { it.side to sid(it) })
        assertEquals(listOf(0x01, 0x0D, 0x80, 0x80, 0x09, 0x09, 0x10, 0x10, 0x81, 0x09, 0x04, 0x04), w.drop(2).map { sid(it) })
        assertTrue(w.drop(2).all { it.side == Side.RIGHT })
        assertTrue(w.none { sid(it) == 0xE0 })
        assertEquals(listOf(0x00, 0x00), w.take(2).map { Framing.parseFrame(it.frame)!!.flag })
        assertTrue(w.drop(2).all { Framing.parseFrame(it.frame)!!.flag == 0x20 })
        val magics = w.map { Framing.readAckMagic(Framing.parseFrame(it.frame)!!.payload) }
        assertEquals(magics.size, magics.toSet().size)
        assertEquals(87, g.snapshot!!.battery)
        assertEquals(87, g.status["battery"]); assertEquals(true, g.status["worn"]); assertEquals(60, g.status["case_battery"])
        // dashboard init payload is the LIVE one with the allocated magic
        assertEquals(Pb.hex(Dashboard.dashboardInit(Framing.readAckMagic(Framing.parseFrame(w[2].frame)!!.payload)!!)),
            Pb.hex(Framing.parseFrame(w[2].frame)!!.payload))
    }

    @Test
    fun startupWithHeartbeatWeatherAndMenu() = withSession(config = G2Session.Config(keepalive = false, timeScale = 0.01, startupHeartbeat = true,
        startupWeather = Dashboard.Weather(21f, weatherType = 1, status = "Sunny"), startupMenu = Menu.DEFAULT_MENU)) { t, g ->
        val res = g.startup()
        assertTrue(res.ok)
        assertFalse(g.heartbeatRunning)
        assertTrue(g.hbCount >= 1)
        val sids = t.commandWrites().map { sid(it) }.filter { it != 0xE0 }
        assertEquals(listOf(0x80, 0x80, 0x01, 0x01, 0x01, 0x0D, 0x80, 0x80, 0x09, 0x09, 0x10, 0x10, 0x81, 0x09, 0x04, 0x04, 0x01, 0x03), sids)
        assertTrue(t.commandWrites()[2].frame.contentEquals(Session.PRELUDE_FRAME))
        val weather = t.payloads(Side.RIGHT, 0x01).last()
        assertEquals(21f, Pb.readFloat(Reader(weather).sub(4)!!.sub(3)!!.sub(1)!!.sub(1)!!.pb, 1))
        assertEquals(now * 1000, Reader(weather).sub(4)!!.sub(3)!!.sub(1)!!.sub(1)!!.varint(4))
    }

    @Test
    fun startupStopsWhenRightArmDoesNotAuthenticate() = withSession(responder = null) { t, g ->
        val plan = g.startupPlan().map { if (it.expect == Expect.Auth) it.copy(timeoutMs = 30) else it }
        val r = g.runPlan(plan, stopOnFailure = true)
        assertEquals(2, r.size)
        assertNull(r[1].second)
        assertEquals(2, t.commandWrites().size)
    }

    @Test
    fun replyMatchedOnSidAndMagicNotJustSid() = withSession(responder = null) { t, g ->
        val m = 0x42
        val payload = Settings.brightness(m, 20)
        t.responder = { side, _, _ ->
            listOf(
                side to rxFrame(0x09, Pb.varintField(1, 4) + Pb.varintField(2, m + 1)),     // wrong magic
                side to rxFrame(0x01, Pb.varintField(1, 4) + Pb.varintField(2, m)),         // wrong sid
                Side.LEFT to rxFrame(0x09, Pb.varintField(1, 4) + Pb.varintField(2, m)),    // wrong arm
                side to rxFrame(0x09, Pb.varintField(1, 4) + Pb.varintField(2, m) + Pb.messageField(6, Pb.varintField(2, 0))),
            )
        }
        val r = g.applySetting(payload)
        assertNotNull(r)
        assertEquals(m, Framing.readAckMagic(r!!.payload))
        assertEquals(0x09, r.sid)
        assertEquals(0, Settings.parseAck(r.payload)!!.flag)
    }

    @Test
    fun noReplyTimesOutToNull() = withSession(responder = null) { _, g ->
        val t0 = System.currentTimeMillis()
        assertNull(g.request(0x01, Dashboard.dashboardInit(5), timeoutMs = 50))
        assertTrue(System.currentTimeMillis() - t0 < 5000)
    }

    @Test
    fun bothSidesLeftFireAndForgetThenWaitOnRight() = withSession(responder = null) { t, g ->
        t.responder = { side, p, _ ->
            val m = Framing.readAckMagic(p.payload)!!
            listOf(side to rxFrame(p.sid, Pb.varintField(1, 1) + Pb.varintField(2, m)))
        }
        val r = g.sendPb(0x10, Session.onboardingFinish(9), Side.BOTH, Expect.Ack, 1000)
        assertNotNull(r)
        assertEquals(listOf(Side.LEFT, Side.RIGHT), t.commandWrites().map { it.side })
        assertTrue(t.commandWrites()[0].frame[2] != t.commandWrites()[1].frame[2])   // each arm gets its own seq
    }

    @Test
    fun fragmentedRequestMatchesMagicFromFirstFragment() = withSession { t, g ->
        val m = 77
        val big = Dashboard.calendarEvents(m, List(4) { Dashboard.Schedule("Event $it " + "x".repeat(60), "Room", "10:00 AM-11:00 AM", 1792500000) })
        assertTrue(big.size > 232)
        val r = g.request(0x01, big, 1000)
        assertNotNull(r)
        assertEquals(m, Framing.readAckMagic(r!!.payload))
        assertTrue(t.commandWrites().size >= 2)
        assertEquals(t.commandWrites().size, Framing.parseFrame(t.commandWrites()[0].frame)!!.total)
    }

    @Test
    fun fragmentedInboundIsReassembledForWaitersAndListeners() = withSession(responder = null) { t, g ->
        val seen = ArrayList<Pair<Side, Int>>()
        g.on { side, p -> seen.add(side to p.sid) }
        val big = Writer().varint(1, 2).varint(2, 5).bytes(9, ByteArray(400)).toBytes()
        val w = g.expectFrame { _, p -> p.sid == 0x09 }
        rxFrames(0x09, big, 7).forEach { t.push(Side.RIGHT, it) }
        val p = withTimeout(1000) { w.await() }
        assertTrue(p.payload.contentEquals(big))
        assertEquals(Framing.ASSEMBLED_LEN, p.length)
        assertEquals(true, p.crcOk)
        delay(10)
        assertEquals(Side.RIGHT to 0x09, seen.first())
    }

    @Test
    fun pushHelpersWithoutHeartbeat() = withSession { t, g ->
        assertEquals(0, g.setWeather(21f, 1, "Sunny"))
        assertFalse(g.heartbeatRunning)
        assertEquals(listOf(0, 0), g.setCalendar(listOf(Dashboard.Schedule("A", endTs = 1), Dashboard.Schedule("B", endTs = 2))))
        assertEquals(List(5) { 0 }, g.pushNews(listOf(Dashboard.NewsItem("T", "S", "C"))))
        val news = t.payloads(Side.RIGHT, 0x01).filter { Dashboard.readCmd(it) == 9 }
        assertEquals(listOf(0L, 1L, 2L, 3L, 4L), news.map { Pb.readVarint(Pb.readBytes(it, 11), 4) })
        assertEquals((now - 16 * 900) * 1000, Pb.readVarint(Pb.readBytes(news[0], 11), 7))
        assertTrue(g.setTasks(listOf(Dashboard.Task(1, "Milk"))))
        assertEquals(1, t.payloads(Side.RIGHT, 0x0C).size)
        assertTrue(g.setMenu(listOf(1, 4, 5, 6, 7)))
        assertEquals(Pb.hex(Menu.menuPush(0).copyOfRange(4, Menu.menuPush(0).size)),
            Pb.hex(t.payloads(Side.RIGHT, 0x03)[0].let { it.copyOfRange(4, it.size) }))
        assertEquals(listOf(0, 0, 0), g.setStocks(listOf(Dashboard.Stock(code = "AAPL", price = 190.5f), Dashboard.Stock(code = "MSFT", price = 400f))))
        assertTrue(t.commandWrites().none { sid(it) == 0xE0 })
        assertEquals(0, g.hbCount)
    }

    @Test
    fun pushHelpersWithHeartbeatBracket() = withSession(config = G2Session.Config(keepalive = false, timeScale = 0.01, heartbeatForPushes = true)) { t, g ->
        assertEquals(0, g.setWeather(21f, 1, "Sunny"))
        assertFalse(g.heartbeatRunning)
        assertTrue(t.commandWrites().any { sid(it) == 0xE0 })
        val hb = t.commandWrites().first { sid(it) == 0xE0 }
        assertEquals("080c", Pb.hex(Framing.parseFrame(hb.frame)!!.payload.copyOfRange(0, 2)))
    }

    @Test
    fun readSettingsAndKeepalive() = withSession { t, g ->
        val snap = g.readSettings()!!
        assertEquals(87, snap.battery); assertEquals(10, snap.displayHeight); assertEquals("1.2.3", snap.leftVersion)
        assertTrue(g.keepaliveOnce())
        val ka = t.commandWrites().filter { sid(it) == 0x80 }
        assertEquals(listOf(Side.LEFT, Side.RIGHT), ka.map { it.side })
        assertTrue(ka.all { Framing.parseFrame(it.frame)!!.flag == 0x00 })
        assertNotNull(g.lastKeepaliveEchoMs)
    }

    @Test
    fun keepaliveWatchdogCallsLinkLostAfterTwoMisses() = withSession(responder = null) { t, g ->
        var lost: String? = null
        g.onLinkLost = { lost = it }
        g.keepaliveLoop(intervalMs = 10, leftLeadMs = 10, echoWaitMs = 20)
        assertEquals("keep-alive watchdog", lost)
        assertEquals(2, g.missedKeepalives)
        assertEquals(4, t.commandWrites().size)
    }

    @Test
    fun notifyHubForm() = withSession(responder = null) { t, g ->
        t.responder = { side, p, _ ->
            var cid: Int? = if (p.sid == 0xC4) p.payload[0].toInt() else null
            if (p.sid == 0xC5 && p.num == p.total) cid = 1
            if (cid != null && !(p.sid == 0xC4 && cid == 1)) listOf(side to rxFrame(0xC4, byteArrayOf(cid.toByte(), 0))) else null
        }
        assertTrue(g.notify("Hi", "There", timeoutMs = 1000))
        assertEquals(listOf(0xC4, 0xC4, 0xC5, 0xC4), t.fileWrites().map { sid(it) })
        assertEquals(0x5F, t.fileWrites()[0].frame[3].toInt())
        assertTrue(t.fileWrites().all { it.side == Side.RIGHT })
    }

    @Test
    fun notifyHubFormFailsOnStartTimeout() = withSession(responder = null) { _, g ->
        assertFalse(g.notify("Hi", "There", timeoutMs = 30))
    }

    @Test
    fun notifyCommunityForm() = withSession(responder = null) { t, g ->
        assertTrue(g.notify("Hi", "There", form = G2Session.NotifyForm.COMMUNITY))
        assertEquals(4, t.fileWrites().size)
        val last = t.commandWrites().last()
        val p = Framing.parseFrame(last.frame)!!
        assertEquals(Side.LEFT, last.side)
        assertEquals(0x80, p.sid); assertEquals(0x00, p.flag); assertEquals("080e", Pb.hex(p.payload.copyOfRange(0, 2)))
    }

    @Test
    fun evenAiSequence() = withSession { t, g ->
        g.evenAiAnswer("Q", "A".repeat(600))
        assertEquals(listOf(1L, 3L, 4L, 5L, 5L), t.payloads(Side.RIGHT, 0x07).map { Pb.readVarint(it, 1) })
        g.evenAiAnswer("Q", "A", community = true)
        assertEquals(listOf(1L, 3L, 5L), t.payloads(Side.RIGHT, 0x07).drop(5).map { Pb.readVarint(it, 1) })
        g.evenAiAnswer("Q", "A", exitAfter = true)
        val last = t.payloads(Side.RIGHT, 0x07).last()
        assertEquals(EvenAi.CTRL_EXIT, EvenAi.parse(last).value)
    }

    @Test
    fun reinitRightArmSendsAuthThenHousekeeping() = withSession { t, g ->
        assertTrue(g.reinitSide(Side.RIGHT))
        val sids = t.commandWrites().map { sid(it) }
        assertEquals(listOf(0x80, 0x80, 0x80, 0x09, 0x09, 0x10, 0x10, 0x81, 0x09, 0x04, 0x04), sids)
        assertTrue(g.reinitSide(Side.LEFT))
        assertEquals(Side.LEFT, t.commandWrites().last().side)
    }

    @Test
    fun micPacketsAreExposedFromTheLeftArm() = withSession(responder = null) { t, g ->
        val pkt = ByteArray(200) { it.toByte() } + byteArrayOf(0x50, 0xFB.toByte(), 90, 0, 0xAB.toByte())
        val p = coroutineScope {
            val got = async(start = CoroutineStart.UNDISPATCHED) { g.micPackets.first() }
            t.push(Side.LEFT, pkt, G2Char.MIC)
            withTimeout(1000) { got.await() }
        }
        assertEquals(5, p.frames.size); assertEquals(-1200, p.signalRatio); assertEquals(90, p.direction); assertEquals(0xAB, p.counter)
    }
}
