package com.kunsh.g2core

import java.io.ByteArrayOutputStream

/**
 * Frame envelope `AA <dir> <seq> <len> <total> <num> <SID> <flag> <payload> [CRC16]` (port of
 * g2/native/framing.py, itself a port of open-g2-hub framing.dart).
 *
 * `len` is the number of bytes after the 8-byte header in this packet (payload chunk including
 * whatever part of the trailing CRC lands in it). The CRC-16/CCITT-FALSE is computed over the
 * whole protobuf once, appended little-endian, and the result is cut into chunks of at most 232
 * bytes; the CRC may straddle two packets.
 */
object Framing {
    const val DIR_TX = 0x21
    const val DIR_RX = 0x12
    const val FLAG_REQUEST = 0x20
    const val FLAG_PLAIN = 0x00
    const val CHUNK_SIZE = 232
    const val ASSEMBLED_LEN = 0xFF
    const val HEADER_LEN = 8
    const val DEFAULT_MAX_WRITE = 240

    /** CRC-16/CCITT-FALSE. crc16("123456789") == 0x29B1. */
    fun crc16(data: ByteArray, init: Int = 0xFFFF, from: Int = 0, to: Int = data.size): Int {
        var crc = init
        for (i in from until to) {
            crc = crc xor ((data[i].toInt() and 0xFF) shl 8)
            repeat(8) {
                crc = if (crc and 0x8000 != 0) ((crc shl 1) xor 0x1021) and 0xFFFF else (crc shl 1) and 0xFFFF
            }
        }
        return crc
    }

    /** The CRC as the two wire bytes, little-endian. */
    fun crc16Bytes(data: ByteArray): ByteArray {
        val c = crc16(data)
        return byteArrayOf((c and 0xFF).toByte(), ((c shr 8) and 0xFF).toByte())
    }

    /** Wrap [pb] in one or more frames (framing.py frame_pb). */
    fun framePb(pb: ByteArray, sid: Int, flag: Int, seq: Int, maxWrite: Int = DEFAULT_MAX_WRITE): List<ByteArray> {
        val size = minOf(maxWrite - HEADER_LEN, CHUNK_SIZE)
        require(size >= 1) { "max_write too small" }
        val payload = pb + crc16Bytes(pb)
        val total = maxOf(1, (payload.size + size - 1) / size)
        return (0 until total).map { i ->
            val a = minOf(i * size, payload.size)
            val z = minOf((i + 1) * size, payload.size)
            val chunk = payload.copyOfRange(a, z)
            byteArrayOf(0xAA.toByte(), DIR_TX.toByte(), seq.toByte(), chunk.size.toByte(), total.toByte(), (i + 1).toByte(),
                sid.toByte(), flag.toByte()) + chunk
        }
    }

    fun frameSingle(pb: ByteArray, sid: Int, flag: Int, seq: Int): ByteArray {
        val f = framePb(pb, sid, flag, seq)
        require(f.size == 1) { "payload of ${pb.size} bytes needs ${f.size} packets" }
        return f[0]
    }

    /** packet + CRC16 (LE) of everything after the 8-byte header. */
    fun addCrc(packet: ByteArray): ByteArray = packet + crc16Bytes(packet.copyOfRange(HEADER_LEN, packet.size))

    /**
     * The one-packet authentication frame `AA 21 <seq> <len> 01 01 80 00` + `{1: 4, 2: magic, 3: {1: 1, 2: 4}}` + CRC.
     * [seq] defaults to [magic]. Magic 128..255 is a proper 2-byte varint (deviation from the Dart's raw byte).
     */
    fun authFrame(magic: Int, seq: Int? = null): ByteArray {
        require(magic in 1..255) { "auth magic must be 1..255" }
        val m = if (magic < 0x80) byteArrayOf(magic.toByte()) else byteArrayOf(((magic and 0x7F) or 0x80).toByte(), (magic shr 7).toByte())
        val pb = byteArrayOf(0x08, 0x04, 0x10) + m + byteArrayOf(0x1A, 0x04, 0x08, 0x01, 0x10, 0x04)
        val s = seq ?: magic
        val pkt = byteArrayOf(0xAA.toByte(), DIR_TX.toByte(), s.toByte(), (pb.size + 2).toByte(), 0x01, 0x01, 0x80.toByte(), FLAG_PLAIN.toByte()) + pb
        return addCrc(pkt)
    }

    /** (start, end) of the protobuf inside one AA frame. */
    fun payloadSpan(frame: ByteArray): Pair<Int, Int> {
        val ln = frame[3].toInt() and 0xFF
        if (ln == ASSEMBLED_LEN) return HEADER_LEN to frame.size - 2
        return HEADER_LEN to HEADER_LEN + maxOf(ln - 2, 0)
    }

    /** Any AA frame (either direction) or null. For a complete frame the CRC is verified when [checkCrc]. */
    fun parseFrame(frame: ByteArray, checkCrc: Boolean = true): ParsedFrame? {
        if (frame.size < 10 || (frame[0].toInt() and 0xFF) != 0xAA) return null
        val (a, b0) = payloadSpan(frame)
        val b = minOf(b0, frame.size)
        val payload = frame.copyOfRange(a, maxOf(a, b))
        val total = frame[4].toInt() and 0xFF
        val num = frame[5].toInt() and 0xFF
        val len = frame[3].toInt() and 0xFF
        var crcOk: Boolean? = null
        if (total <= 1 || len == ASSEMBLED_LEN) {
            if (checkCrc) {
                val got = frame.copyOfRange(minOf(b, frame.size), minOf(b + 2, frame.size))
                crcOk = got.contentEquals(crc16Bytes(payload))
            }
        }
        return ParsedFrame(frame[1].toInt() and 0xFF, frame[2].toInt() and 0xFF, len, total, num, frame[6].toInt() and 0xFF,
            frame[7].toInt() and 0xFF, payload, crcOk)
    }

    /** (sid, payload) for a glasses -> app frame, else (null, empty). */
    fun parseRx(frame: ByteArray): Pair<Int?, ByteArray> {
        if (frame.size >= 10 && (frame[0].toInt() and 0xFF) == 0xAA && (frame[1].toInt() and 0xFF) == DIR_RX) {
            val (a, b) = payloadSpan(frame)
            return (frame[6].toInt() and 0xFF) to frame.copyOfRange(a, minOf(maxOf(a, b), frame.size))
        }
        return null to ByteArray(0)
    }

    /** Field 2 of `{1: cmd, 2: magic, ...}` (or a message starting directly with field 2), else null. */
    fun readAckMagic(pb: ByteArray): Int? {
        return try {
            if (pb.size >= 2 && (pb[0].toInt() and 0xFF) == 0x10) return Pb.decodeVarint(pb, 1).first.toInt()
            if (pb.size < 2 || (pb[0].toInt() and 0xFF) != 0x08) return null
            val (_, i) = Pb.decodeVarint(pb, 1)
            if (i >= pb.size || (pb[i].toInt() and 0xFF) != 0x10) return null
            Pb.decodeVarint(pb, i + 1).first.toInt()
        } catch (e: IndexOutOfBoundsException) {
            null
        }
    }
}

data class ParsedFrame(
    val direction: Int,
    val seq: Int,
    val length: Int,
    val total: Int,
    val num: Int,
    val sid: Int,
    val flag: Int,
    val payload: ByteArray,
    /** null when the frame is a fragment (CRC not in this packet) or checking was off. */
    val crcOk: Boolean?,
) {
    val isRx: Boolean get() = direction == Framing.DIR_RX
    val isFragment: Boolean get() = total > 1 && length != Framing.ASSEMBLED_LEN

    override fun equals(other: Any?): Boolean = other is ParsedFrame && other.direction == direction && other.seq == seq &&
        other.length == length && other.total == total && other.num == num && other.sid == sid && other.flag == flag &&
        other.payload.contentEquals(payload) && other.crcOk == crcOk

    override fun hashCode(): Int = (sid * 31 + seq) * 31 + payload.contentHashCode()
}

/**
 * Joins multi-packet notifications back into one synthetic frame `AA <dir> <seq> FF 01 01 <sid> <flag> <payload> <crc>`.
 * [feed] returns the bytes to hand on (single packets pass straight through) or null while incomplete. Fragments are
 * keyed by (sid, seq); an out-of-order fragment drops the partial message. Not thread-safe: use one per arm.
 */
class FrameAssembler {
    private val parts = HashMap<Int, MutableList<ByteArray>>()
    var crcErrors = 0
        private set

    fun feed(data: ByteArray): ByteArray? {
        if (data.size < Framing.HEADER_LEN || (data[0].toInt() and 0xFF) != 0xAA) return data
        val total = data[4].toInt() and 0xFF
        val num = data[5].toInt() and 0xFF
        if (total <= 1) return data
        val key = ((data[6].toInt() and 0xFF) shl 8) or (data[2].toInt() and 0xFF)
        val ln = data[3].toInt() and 0xFF
        val chunk = data.copyOfRange(Framing.HEADER_LEN, minOf(Framing.HEADER_LEN + ln, data.size))
        val existing = parts[key]
        if (num == 1) {
            parts[key] = mutableListOf(chunk)
        } else if (existing != null && existing.size == num - 1) {
            existing.add(chunk)
        } else {
            parts.remove(key)
            return null
        }
        if (num < total) return null
        val o = ByteArrayOutputStream()
        parts.remove(key)!!.forEach { o.write(it) }
        val body = o.toByteArray()
        if (body.size < 2 || !body.copyOfRange(body.size - 2, body.size).contentEquals(Framing.crc16Bytes(body.copyOfRange(0, body.size - 2)))) {
            crcErrors++
        }
        return byteArrayOf(0xAA.toByte(), data[1], data[2], Framing.ASSEMBLED_LEN.toByte(), 1, 1, data[6], data[7]) + body
    }

    fun reset() = parts.clear()
}

/**
 * Message magic 1..255, wrapping, never 0; frame seq 0..255 wrapping. The hub starts at 100 so the first magic is
 * 101; seq starts at 10 so the first seq is 11. Thread-safe.
 */
class MagicAllocator(magicStart: Int = 100, seqStart: Int = 10) {
    private var magicValue = magicStart % 255
    private var seqValue = seqStart and 0xFF

    @Synchronized
    fun nextMagic(): Int {
        magicValue = magicValue % 255 + 1
        return magicValue
    }

    @Synchronized
    fun nextSeq(): Int {
        seqValue = (seqValue + 1) and 0xFF
        return seqValue
    }

    val magic: Int @Synchronized get() = magicValue
    val seq: Int @Synchronized get() = seqValue
}
