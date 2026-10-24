package com.kunsh.g2core

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import java.util.Collections

/** Responder: (side written to, parsed frame, raw frame) -> replies to queue as (side, rx frame), or null. */
typealias Responder = (Side, ParsedFrame, ByteArray) -> List<Pair<Side, ByteArray>>?

/** Build a glasses -> app frame (`AA 12 ...`) for fakes (transport.rx_frame). */
fun rxFrame(sid: Int, payload: ByteArray, seq: Int = 1, flag: Int = 0x00): ByteArray =
    Framing.framePb(payload, sid, flag, seq)[0].also { it[1] = 0x12 }

/** All fragments of a glasses -> app message. */
fun rxFrames(sid: Int, payload: ByteArray, seq: Int = 1, flag: Int = 0x00): List<ByteArray> =
    Framing.framePb(payload, sid, flag, seq).onEach { it[1] = 0x12 }

/** Port of transport.FakeTransport: records every write and feeds scripted replies. */
class FakeTransport(var responder: Responder? = null) : G2Transport {
    data class Write(val side: Side, val frame: ByteArray, val char: G2Char)

    val writes: MutableList<Write> = Collections.synchronizedList(ArrayList())
    private val q = MutableSharedFlow<G2Inbound>(extraBufferCapacity = 4096)
    override val inbound: Flow<G2Inbound> = q

    fun push(side: Side, frame: ByteArray, char: G2Char = G2Char.COMMAND) {
        check(q.tryEmit(G2Inbound(side, char, frame)))
    }

    override suspend fun write(side: Side, frame: ByteArray, char: G2Char) {
        writes.add(Write(side, frame.copyOf(), char))
        val r = responder ?: return
        val p = Framing.parseFrame(frame) ?: return
        r(side, p, frame)?.forEach { (s, f) -> push(s, f) }
    }

    fun commandWrites(): List<Write> = writes.filter { it.char == G2Char.COMMAND }
    fun fileWrites(): List<Write> = writes.filter { it.char == G2Char.FILE }

    /** Re-assembled payloads written, optionally filtered by side / sid. */
    fun payloads(side: Side? = null, sid: Int? = null): List<ByteArray> {
        val asm = mapOf(Side.LEFT to FrameAssembler(), Side.RIGHT to FrameAssembler(), Side.BOTH to FrameAssembler())
        val out = ArrayList<ByteArray>()
        for (w in commandWrites()) {
            val data = asm.getValue(w.side).feed(w.frame) ?: continue
            val p = Framing.parseFrame(data) ?: continue
            if ((side != null && w.side != side) || (sid != null && p.sid != sid)) continue
            out.add(p.payload)
        }
        return out
    }
}

/**
 * Port of transport.fake_glasses_responder: auth reply ending `1a 00`, dashboard acks flag 0, settings snapshot,
 * wear/case replies, keep-alive echo on the right arm, news FIFO count / status, menu + quicklist echoes.
 */
fun fakeGlassesResponder(battery: Int = 87, charging: Int = 0, worn: Int = 1, caseBattery: Int = 60, fw: String = "1.2.3", newsSlots: Int = 5): Responder =
    resp@{ side, p, _ ->
        val cmd = Dashboard.readCmd(p.payload)
        val magic = Framing.readAckMagic(p.payload) ?: return@resp null
        val head = Pb.varintField(1, cmd) + Pb.varintField(2, magic)
        fun r(sid: Int, payload: ByteArray) = listOf(side to rxFrame(sid, payload))
        if (p.sid == 0x80 && cmd == 4) return@resp r(0x80, head + Pb.messageField(3, ByteArray(0)))
        if (p.sid == 0x80 && cmd == 14) return@resp if (side == Side.RIGHT) r(0x80, head + Pb.messageField(13, ByteArray(0))) else null
        if (side != Side.RIGHT) return@resp null
        when {
            p.sid == 0x01 && cmd == 2 -> r(0x01, head + Pb.messageField(3, Pb.varintField(1, 1) + Pb.varintField(2, 0)))
            p.sid == 0x01 && cmd == 13 -> r(0x01, Pb.varintField(1, 14) + Pb.varintField(2, magic) + Pb.messageField(16, ByteArray(0)))
            p.sid == 0x01 && cmd == 7 -> r(0x01, Pb.varintField(1, 8) + Pb.varintField(2, magic) + Pb.messageField(10, Pb.varintField(1, newsSlots)))
            p.sid == 0x01 && cmd == 9 -> r(0x01, Pb.varintField(1, 10) + Pb.varintField(2, magic) + Pb.messageField(12, Pb.varintField(5, 0)))
            p.sid == 0x01 -> null
            p.sid == 0x09 && cmd == 2 -> {
                val snap = Writer().varint(1, 1).varint(2, 30).varint(3, 10).varint(4, 10).string(5, fw).string(6, fw).varint(7, 1).varint(8, 25)
                    .varint(10, 1).varint(12, battery).varint(13, charging).varint(14, 0).varint(18, 1).varint(19, 2)
                r(0x09, head + Pb.messageField(4, snap.toBytes()))
            }
            p.sid == 0x09 -> r(0x09, head + Pb.messageField(6, Pb.varintField(1, 1) + Pb.varintField(2, 0)))
            p.sid == 0x10 && cmd == 3 -> r(0x10, head + Pb.messageField(5, Pb.varintField(1, 1) + Pb.varintField(2, worn)))
            p.sid == 0x81 -> r(0x81, head + Pb.messageField(3, Pb.varintField(1, caseBattery) + Pb.varintField(2, 0)))
            p.sid == 0x03 -> r(0x03, Pb.varintField(1, 1) + Pb.varintField(2, magic))
            p.sid == 0x0C -> r(0x0C, p.payload)
            p.sid in setOf(0x04, 0x05, 0x06, 0x07, 0x08, 0x0D, 0x10, 0x20) -> r(p.sid, head)
            else -> null
        }
    }
