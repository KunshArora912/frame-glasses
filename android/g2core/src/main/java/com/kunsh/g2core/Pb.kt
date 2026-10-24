package com.kunsh.g2core

import java.io.ByteArrayOutputStream

/**
 * Minimal protobuf wire codec, byte-identical to g2/native/pb.py (and open-g2-hub pb.dart).
 *
 * There is no generated code: every G2 message is built field by field in the order the official
 * app emits them. Negative int32/int64 values are written as their unsigned 64-bit two's
 * complement (10 bytes). Varint values are carried as [Long]; values >= 2^63 come back negative
 * (use [decodeSigned] / treat as unsigned where it matters).
 */
object Pb {
    const val WIRE_VARINT = 0
    const val WIRE_FIXED64 = 1
    const val WIRE_LEN = 2
    const val WIRE_FIXED32 = 5

    /** Protobuf varint; negatives as 64-bit two's complement (pb.py encode_varint). */
    fun encodeVarint(value: Long): ByteArray {
        val out = ByteArrayOutputStream(10)
        var v = value
        while (v and 0x7FL.inv() != 0L) {
            out.write(((v and 0x7F) or 0x80).toInt())
            v = v ushr 7
        }
        out.write(v.toInt())
        return out.toByteArray()
    }

    fun encodeVarint(value: Int): ByteArray = encodeVarint(value.toLong())

    /** (value, nextIndex). Throws [IndexOutOfBoundsException] on a truncated varint. */
    fun decodeVarint(data: ByteArray, start: Int = 0): Pair<Long, Int> {
        var result = 0L
        var shift = 0
        var i = start
        while (true) {
            if (i >= data.size) throw IndexOutOfBoundsException("truncated varint")
            val b = data[i].toInt() and 0xFF
            if (shift < 64) result = result or ((b and 0x7F).toLong() shl shift)
            i++
            if (b and 0x80 == 0) return result to i
            shift += 7
        }
    }

    /** A signed int32/int64 written as a 64-bit two's complement varint is already a signed Long. */
    fun decodeSigned(value: Long): Long = value

    fun encodeKey(field: Int, wire: Int): ByteArray = encodeVarint(((field.toLong()) shl 3) or wire.toLong())

    fun varintField(field: Int, value: Long): ByteArray = encodeKey(field, WIRE_VARINT) + encodeVarint(value)
    fun varintField(field: Int, value: Int): ByteArray = varintField(field, value.toLong())

    fun bytesField(field: Int, value: ByteArray): ByteArray =
        encodeKey(field, WIRE_LEN) + encodeVarint(value.size.toLong()) + value

    fun stringField(field: Int, value: String): ByteArray = bytesField(field, value.toByteArray(Charsets.UTF_8))
    fun messageField(field: Int, inner: ByteArray): ByteArray = bytesField(field, inner)

    fun floatField(field: Int, value: Float): ByteArray = encodeKey(field, WIRE_FIXED32) + le32(java.lang.Float.floatToRawIntBits(value))
    fun doubleField(field: Int, value: Double): ByteArray =
        encodeKey(field, WIRE_FIXED64) + le64(java.lang.Double.doubleToRawLongBits(value))

    fun packedVarintField(field: Int, values: List<Long>): ByteArray {
        val o = ByteArrayOutputStream()
        values.forEach { o.write(encodeVarint(it)) }
        return bytesField(field, o.toByteArray())
    }

    fun packedFloatField(field: Int, values: List<Float>): ByteArray {
        val o = ByteArrayOutputStream()
        values.forEach { o.write(le32(java.lang.Float.floatToRawIntBits(it))) }
        return bytesField(field, o.toByteArray())
    }

    internal fun le32(v: Int): ByteArray = byteArrayOf(v.toByte(), (v ushr 8).toByte(), (v ushr 16).toByte(), (v ushr 24).toByte())
    internal fun le64(v: Long): ByteArray = ByteArray(8) { (v ushr (8 * it)).toByte() }

    /** Port of iter_fields: stops at an unknown wire type or a truncated varint; slices clip like Python. */
    fun iterFields(pb: ByteArray): List<Field> {
        val out = ArrayList<Field>()
        var i = 0
        val n = pb.size
        while (i < n) {
            val field: Int
            val wire: Int
            var v = 0L
            try {
                val (key, j) = decodeVarint(pb, i)
                i = j
                field = (key ushr 3).toInt()
                wire = (key and 7).toInt()
                if (wire == WIRE_VARINT) {
                    val (vv, k) = decodeVarint(pb, i)
                    v = vv
                    i = k
                }
            } catch (e: IndexOutOfBoundsException) {
                return out
            }
            when (wire) {
                WIRE_VARINT -> out.add(Field(field, wire, v, null))
                WIRE_FIXED64 -> { out.add(Field(field, wire, 0, slice(pb, i, i + 8))); i += 8 }
                WIRE_LEN -> {
                    val (len, j) = try { decodeVarint(pb, i) } catch (e: IndexOutOfBoundsException) { return out }
                    i = j
                    val end = if (len < 0 || len > Int.MAX_VALUE.toLong()) Int.MAX_VALUE else (i.toLong() + len).coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
                    out.add(Field(field, wire, 0, slice(pb, i, end)))
                    i = end
                }
                WIRE_FIXED32 -> { out.add(Field(field, wire, 0, slice(pb, i, i + 4))); i += 4 }
                else -> return out
            }
        }
        return out
    }

    private fun slice(b: ByteArray, from: Int, to: Int): ByteArray {
        val a = from.coerceIn(0, b.size)
        val z = to.coerceIn(a, b.size)
        return b.copyOfRange(a, z)
    }

    fun readVarint(pb: ByteArray?, field: Int, default: Long = -1): Long {
        if (pb == null) return default
        for (f in iterFields(pb)) if (f.number == field && f.wire == WIRE_VARINT) return f.value
        return default
    }

    fun readVarintOrNull(pb: ByteArray?, field: Int): Long? {
        if (pb == null) return null
        for (f in iterFields(pb)) if (f.number == field && f.wire == WIRE_VARINT) return f.value
        return null
    }

    fun readInt(pb: ByteArray?, field: Int, default: Int = -1): Int = readVarint(pb, field, default.toLong()).toInt()

    fun readSigned(pb: ByteArray?, field: Int, default: Long = 0): Long = readVarintOrNull(pb, field) ?: default

    fun readString(pb: ByteArray?, field: Int, default: String = ""): String {
        if (pb == null) return default
        for (f in iterFields(pb)) if (f.number == field && f.wire == WIRE_LEN) return f.asStr
        return default
    }

    fun readBytes(pb: ByteArray?, field: Int): ByteArray? {
        if (pb == null) return null
        for (f in iterFields(pb)) if (f.number == field && f.wire == WIRE_LEN) return f.asBytes
        return null
    }

    fun readFloat(pb: ByteArray?, field: Int, default: Float = 0f): Float {
        if (pb == null) return default
        for (f in iterFields(pb)) if (f.number == field && f.wire == WIRE_FIXED32) return f.asFloat
        return default
    }

    fun readDouble(pb: ByteArray?, field: Int, default: Double = 0.0): Double {
        if (pb == null) return default
        for (f in iterFields(pb)) if (f.number == field && f.wire == WIRE_FIXED64) return f.asDouble
        return default
    }

    fun readMessages(pb: ByteArray?, field: Int): List<ByteArray> =
        if (pb == null) emptyList() else iterFields(pb).filter { it.number == field && it.wire == WIRE_LEN }.map { it.asBytes }

    fun readPackedVarints(pb: ByteArray?, field: Int): List<Long> {
        val raw = readBytes(pb, field) ?: return emptyList()
        val out = ArrayList<Long>()
        var i = 0
        while (i < raw.size) {
            val (v, j) = decodeVarint(raw, i)
            out.add(v)
            i = j
        }
        return out
    }

    fun hex(b: ByteArray): String {
        val sb = StringBuilder(b.size * 2)
        for (x in b) {
            val v = x.toInt() and 0xFF
            sb.append(HEX[v ushr 4]).append(HEX[v and 0xF])
        }
        return sb.toString()
    }

    fun unhex(s: String): ByteArray {
        val c = s.filter { !it.isWhitespace() }
        require(c.length % 2 == 0) { "odd hex length" }
        return ByteArray(c.length / 2) { ((Character.digit(c[2 * it], 16) shl 4) or Character.digit(c[2 * it + 1], 16)).toByte() }
    }

    private val HEX = "0123456789abcdef".toCharArray()
}

/** One decoded field: [value] for wire 0, [raw] otherwise. */
class Field(val number: Int, val wire: Int, val value: Long, private val raw: ByteArray?) {
    val asInt: Long get() = value
    val asBytes: ByteArray get() = raw ?: ByteArray(0)
    val asSigned: Long get() = value
    val asStr: String get() = String(asBytes, Charsets.UTF_8)
    val asFloat: Float
        get() {
            val b = asBytes
            require(b.size >= 4) { "truncated float" }
            return java.lang.Float.intBitsToFloat((b[0].toInt() and 0xFF) or ((b[1].toInt() and 0xFF) shl 8) or
                ((b[2].toInt() and 0xFF) shl 16) or ((b[3].toInt() and 0xFF) shl 24))
        }
    val asDouble: Double
        get() {
            val b = asBytes
            require(b.size >= 8) { "truncated double" }
            var v = 0L
            for (k in 0 until 8) v = v or ((b[k].toLong() and 0xFF) shl (8 * k))
            return java.lang.Double.longBitsToDouble(v)
        }

    override fun equals(other: Any?): Boolean =
        other is Field && other.number == number && other.wire == wire && other.value == value && asBytes.contentEquals(other.asBytes)

    override fun hashCode(): Int = number * 31 + wire
    override fun toString(): String = "Field($number, $wire, ${if (raw == null) value.toString() else Pb.hex(raw)})"
}

/** Growable protobuf buffer (the Writer of pb.py / Pb of pb.dart). Methods chain. */
class Writer {
    private val buf = ByteArrayOutputStream()

    fun add(raw: ByteArray): Writer = apply { buf.write(raw) }
    fun varint(field: Int, value: Long): Writer = add(Pb.varintField(field, value))
    fun varint(field: Int, value: Int): Writer = varint(field, value.toLong())
    fun int32(field: Int, value: Int): Writer = varint(field, value.toLong())
    fun int64(field: Int, value: Long): Writer = varint(field, value)
    fun bool(field: Int, value: Boolean): Writer = varint(field, if (value) 1L else 0L)
    fun string(field: Int, value: String): Writer = add(Pb.stringField(field, value))
    fun bytes(field: Int, value: ByteArray): Writer = add(Pb.bytesField(field, value))
    fun message(field: Int, inner: ByteArray): Writer = add(Pb.messageField(field, inner))
    fun message(field: Int, inner: Writer): Writer = message(field, inner.toBytes())
    fun float32(field: Int, value: Float): Writer = add(Pb.floatField(field, value))
    fun double(field: Int, value: Double): Writer = add(Pb.doubleField(field, value))
    fun packedVarints(field: Int, values: List<Long>): Writer = add(Pb.packedVarintField(field, values))
    fun packedFloats(field: Int, values: List<Float>): Writer = add(Pb.packedFloatField(field, values))
    fun repeated(field: Int, inners: List<ByteArray>): Writer = apply { inners.forEach { message(field, it) } }
    val size: Int get() = buf.size()
    fun toBytes(): ByteArray = buf.toByteArray()
}

/** Convenience view over one message: `Reader(pb)[2]` -> first field 2 (or null). */
class Reader(val pb: ByteArray) {
    val fields: List<Field> = Pb.iterFields(pb)
    operator fun get(number: Int): Field? = fields.firstOrNull { it.number == number }
    fun all(number: Int): List<Field> = fields.filter { it.number == number }
    fun varint(number: Int, default: Long = -1): Long = Pb.readVarint(pb, number, default)
    fun string(number: Int, default: String = ""): String = Pb.readString(pb, number, default)
    fun bytes(number: Int): ByteArray? = Pb.readBytes(pb, number)
    fun sub(number: Int): Reader? = Pb.readBytes(pb, number)?.let { Reader(it) }
}

/** Text helpers that mirror Python slicing on code points and UTF-8 byte caps. */
object Text {
    /** Python `s[:n]` on code points (dashboard._head). */
    fun head(s: String, n: Int): String {
        if (n <= 0) return ""
        val count = s.codePointCount(0, s.length)
        if (count <= n) return s
        return s.substring(0, s.offsetByCodePoints(0, n))
    }

    fun codePoints(s: String): Int = s.codePointCount(0, s.length)

    /** Cut to [maxBytes] UTF-8 bytes on a character boundary (dashboard.utf8_fit). */
    fun utf8Fit(text: String, maxBytes: Int): String {
        val data = text.toByteArray(Charsets.UTF_8)
        if (data.size <= maxBytes) return text
        var cut = maxOf(maxBytes, 0)
        while (cut > 0 && (data[cut].toInt() and 0xC0) == 0x80) cut--
        return String(data, 0, cut, Charsets.UTF_8)
    }

    fun utf8Len(s: String): Int = s.toByteArray(Charsets.UTF_8).size
}
