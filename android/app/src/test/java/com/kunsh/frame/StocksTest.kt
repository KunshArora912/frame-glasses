package com.kunsh.frame

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class StocksTest {
    private val yahoo = """
        {"chart":{"result":[{"meta":{"currency":"USD","symbol":"AAPL","exchangeName":"NMS","regularMarketPrice":251.2,
        "regularMarketTime":1792526400,"regularMarketDayHigh":252.3,"regularMarketDayLow":249.05,"regularMarketVolume":41234567,
        "shortName":"Apple Inc.","chartPreviousClose":248.0,"previousClose":248.0},
        "timestamp":[1792502400,1792502700,1792503000],
        "indicators":{"quote":[{"close":[249.5,null,251.2],"open":[249.0,249.5,250.0],"volume":[1,2,3]}]}}],"error":null}}
    """.trimIndent()

    private val yahooError = """{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found, symbol may be delisted"}}}"""

    private val stooq = "Symbol,Date,Time,Open,High,Low,Close,Volume\r\nAAPL.US,2026-10-23,22:00:09,250.1,252.3,249.05,251.2,41234567\r\n"
    private val stooqNd = "Symbol,Date,Time,Open,High,Low,Close,Volume\nZZZZ.US,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"

    @Test
    fun parsesYahoo() {
        val q = Stocks.parseYahoo(yahoo, "AAPL")
        assertEquals("AAPL", q.symbol)
        assertEquals(251.2, q.price, 1e-9)
        assertEquals(248.0, q.previousClose!!, 1e-9)
        assertEquals(1.2903, q.changePercent!!, 1e-3)
        assertEquals(252.3, q.high!!, 1e-9)
        assertEquals(249.05, q.low!!, 1e-9)
        assertEquals(41234567L, q.volume)
        assertEquals("USD", q.currency)
        assertEquals("Apple Inc.", q.name)
        assertEquals(listOf(249.5, 251.2), q.points)        // nulls dropped
        assertEquals("Yahoo", q.source)
        assertTrue(q.describe().startsWith("AAPL 251.20 USD (+1.29% today)"))
        val d = q.toDashboard()
        assertEquals("AAPL", d.code)
        assertEquals(1, d.trend)
        assertEquals(251.2f, d.price!!, 1e-4f)
    }

    @Test
    fun yahooErrorIsReported() {
        try { Stocks.parseYahoo(yahooError, "ZZZZ"); fail() } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("delisted"))
        }
    }

    @Test
    fun parsesStooq() {
        val q = Stocks.parseStooq(stooq, "AAPL")
        assertEquals("AAPL", q.symbol)
        assertEquals(251.2, q.price, 1e-9)
        assertEquals(250.1, q.open!!, 1e-9)
        assertEquals(252.3, q.high!!, 1e-9)
        assertEquals(249.05, q.low!!, 1e-9)
        assertEquals(41234567L, q.volume)
        assertEquals("2026-10-23 22:00:09", q.time)
        assertNull(q.changePercent)                          // Stooq has no previous close
        assertEquals(0, q.toDashboard().trend)
        assertEquals("Stooq", q.source)
    }

    @Test
    fun stooqUnknownSymbol() {
        try { Stocks.parseStooq(stooqNd, "ZZZZ"); fail() } catch (e: IllegalArgumentException) { assertTrue(e.message!!.contains("ZZZZ")) }
    }

    @Test
    fun urlsAndNormalize() {
        assertEquals("AAPL", Stocks.normalize(" \$aapl "))
        assertEquals("BRK.B", Stocks.normalize("brk.b"))
        assertNull(Stocks.normalize("not a ticker"))
        assertEquals("https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=5m", Stocks.yahooUrl("AAPL"))
        assertEquals("https://stooq.com/q/l/?s=aapl.us&f=sd2t2ohlcv&h&e=csv", Stocks.stooqUrl("AAPL"))
        assertEquals("https://stooq.com/q/l/?s=vod.uk&f=sd2t2ohlcv&h&e=csv", Stocks.stooqUrl("VOD.UK"))
    }

    @Test
    fun fetchFallsBackToStooq() = runBlocking {
        val http = FakeHttp()
        http.getHandler = { url -> if ("yahoo" in url) HttpPost.Result(429, "Too Many Requests") else HttpPost.Result(200, stooq) }
        val q = Stocks.fetch(http, "aapl")
        assertEquals("Stooq", q.source)
        assertEquals(2, http.gets.size)
    }

    @Test
    fun fetchReportsBothFailures() = runBlocking {
        val http = FakeHttp()
        http.getHandler = { url -> if ("yahoo" in url) HttpPost.Result(200, yahooError) else HttpPost.Result(200, stooqNd) }
        try { Stocks.fetch(http, "ZZZZ"); fail() } catch (e: IllegalStateException) {
            assertTrue(e.message!!.contains("Yahoo")); assertTrue(e.message!!.contains("Stooq"))
        }
    }
}
