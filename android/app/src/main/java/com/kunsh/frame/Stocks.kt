package com.kunsh.frame

import com.kunsh.g2core.Dashboard
import java.util.Locale
import kotlin.coroutines.cancellation.CancellationException

/** One quote from a free, keyless source (Yahoo chart JSON or Stooq CSV). */
data class Quote(
    val symbol: String,
    val price: Double,
    val previousClose: Double? = null,
    val open: Double? = null,
    val high: Double? = null,
    val low: Double? = null,
    val volume: Long? = null,
    val currency: String? = null,
    val name: String? = null,
    val time: String? = null,
    val points: List<Double> = emptyList(),
    val source: String,
) {
    val change: Double? get() = previousClose?.let { price - it }
    val changePercent: Double? get() = previousClose?.takeIf { it != 0.0 }?.let { (price - it) / it * 100.0 }

    fun describe(): String {
        val sb = StringBuilder()
        sb.append(symbol).append(' ').append(String.format(Locale.US, "%.2f", price))
        currency?.let { sb.append(' ').append(it) }
        changePercent?.let { sb.append(String.format(Locale.US, " (%+.2f%% today)", it)) }
        if (high != null && low != null) sb.append(String.format(Locale.US, ", range %.2f-%.2f", low, high))
        time?.let { sb.append(", as of ").append(it) }
        sb.append(" [").append(source).append(']')
        return sb.toString()
    }

    /** The dashboard Stocks card message (g2core Dashboard.Stock, DECODED). */
    fun toDashboard(): Dashboard.Stock {
        val pct = changePercent
        val pts = if (points.size > 48) points.indices.step((points.size + 47) / 48).map { points[it] } else points
        return Dashboard.Stock(
            code = symbol, name = name ?: symbol, price = price.toFloat(), changePercent = pct?.toFloat(),
            dayHigh = high?.toFloat(), dayLow = low?.toFloat(), open = open?.toFloat(), volume = volume,
            trend = when { pct == null || pct == 0.0 -> 0; pct > 0 -> 1; else -> 2 },
            points = pts.map { it.toFloat() }.ifEmpty { null },
        )
    }
}

object Stocks {
    /** "aapl", " $AAPL " -> "AAPL"; null when it is not a plausible ticker. */
    fun normalize(symbol: String): String? {
        val s = symbol.trim().removePrefix("$").uppercase(Locale.US)
        return if (s.matches(Regex("[A-Z0-9.^=\\-]{1,15}"))) s else null
    }

    fun yahooUrl(sym: String) = "https://query1.finance.yahoo.com/v8/finance/chart/$sym?range=1d&interval=5m"
    /** Stooq wants lowercase and a market suffix; US tickers get ".us". */
    fun stooqUrl(sym: String): String {
        val s = sym.lowercase(Locale.US)
        val withMarket = if ('.' in s || s.startsWith("^")) s else "$s.us"
        return "https://stooq.com/q/l/?s=$withMarket&f=sd2t2ohlcv&h&e=csv"
    }

    /** Parse the Yahoo v8 chart JSON. Throws with a short reason when there is no usable quote. */
    fun parseYahoo(json: String, requested: String): Quote {
        val root = Json.obj(json)
        val chart = root["chart"].jObj() ?: throw IllegalArgumentException("no chart object")
        chart["error"].jObj()?.let { e -> throw IllegalArgumentException(e.str("description") ?: e.str("code") ?: "Yahoo error") }
        val res = chart["result"].jArr()?.firstOrNull().jObj() ?: throw IllegalArgumentException("no result for $requested")
        val meta = res["meta"].jObj() ?: throw IllegalArgumentException("no meta")
        val closes = res["indicators"].jObj()?.get("quote").jArr()?.firstOrNull().jObj()?.get("close").jArr()
            ?.mapNotNull { (it as? Number)?.toDouble() } ?: emptyList()
        val price = meta.num("regularMarketPrice") ?: closes.lastOrNull() ?: throw IllegalArgumentException("no price for $requested")
        val time = meta.num("regularMarketTime")?.toLong()?.let {
            java.time.Instant.ofEpochSecond(it).atZone(java.time.ZoneId.systemDefault())
                .format(java.time.format.DateTimeFormatter.ofPattern("MMM d HH:mm", Locale.US))
        }
        return Quote(
            symbol = meta.str("symbol") ?: requested,
            price = price,
            previousClose = meta.num("chartPreviousClose") ?: meta.num("previousClose"),
            open = meta.num("regularMarketOpen"),
            high = meta.num("regularMarketDayHigh"),
            low = meta.num("regularMarketDayLow"),
            volume = meta.num("regularMarketVolume")?.toLong(),
            currency = meta.str("currency"),
            name = meta.str("shortName") ?: meta.str("longName"),
            time = time,
            points = closes,
            source = "Yahoo",
        )
    }

    /**
     * Parse Stooq's CSV (`Symbol,Date,Time,Open,High,Low,Close,Volume` + one row). "N/D" = unknown symbol.
     * Stooq gives no previous close, so the change is unknown.
     */
    fun parseStooq(csv: String, requested: String): Quote {
        val lines = csv.trim().lines().map { it.trim() }.filter { it.isNotEmpty() }
        require(lines.size >= 2) { "empty Stooq reply" }
        val head = lines[0].split(',').map { it.trim().lowercase(Locale.US) }
        val row = lines[1].split(',').map { it.trim() }
        fun col(name: String): String? = head.indexOf(name).takeIf { it >= 0 }?.let { row.getOrNull(it) }?.takeIf { it.isNotEmpty() && it != "N/D" }
        val close = col("close")?.toDoubleOrNull() ?: throw IllegalArgumentException("no quote for $requested on Stooq")
        val sym = (col("symbol") ?: requested).uppercase(Locale.US).removeSuffix(".US")
        val date = col("date"); val time = col("time")
        return Quote(
            symbol = sym, price = close,
            open = col("open")?.toDoubleOrNull(), high = col("high")?.toDoubleOrNull(), low = col("low")?.toDoubleOrNull(),
            volume = col("volume")?.toDoubleOrNull()?.toLong(),
            time = listOfNotNull(date, time).joinToString(" ").ifEmpty { null },
            source = "Stooq",
        )
    }

    private val UA = mapOf("User-Agent" to "Mozilla/5.0 (Android) FrameCompanion", "Accept" to "*/*")

    /** Yahoo first, Stooq as fallback. Throws with both reasons when neither works. */
    suspend fun fetch(http: HttpPost, symbol: String): Quote {
        val sym = normalize(symbol) ?: throw IllegalArgumentException("'$symbol' is not a ticker symbol")
        val errors = ArrayList<String>()
        try {
            val r = http.get(yahooUrl(sym), UA)
            if (r.code == 200) return parseYahoo(r.body, sym)
            errors.add("Yahoo HTTP ${r.code}")
        } catch (e: CancellationException) { throw e } catch (e: Exception) { errors.add("Yahoo: ${e.message ?: e.javaClass.simpleName}") }
        try {
            val r = http.get(stooqUrl(sym), UA)
            if (r.code == 200) return parseStooq(r.body, sym)
            errors.add("Stooq HTTP ${r.code}")
        } catch (e: CancellationException) { throw e } catch (e: Exception) { errors.add("Stooq: ${e.message ?: e.javaClass.simpleName}") }
        throw IllegalStateException("no quote for $sym (${errors.joinToString("; ")})")
    }
}
