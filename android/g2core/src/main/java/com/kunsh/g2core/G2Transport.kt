package com.kunsh.g2core

import kotlinx.coroutines.flow.Flow

/** The G2 GATT characteristics a frame travels on (suffixes of 00002760-08c2-11e1-9073-0e8ac72eXXXX). */
enum class G2Char(val writeSuffix: String?, val notifySuffix: String) {
    /** 0x5401 write / 0x5402 notify: every AA frame */
    COMMAND("5401", "5402"),
    /** 0x6402 notify only: raw LC3 packets from the left arm */
    MIC(null, "6402"),
    /** 0x7401 write / 0x7402 notify: EvenFile (notification JSON) */
    FILE("7401", "7402"),
}

/** One notification from an arm. */
class G2Inbound(val side: Side, val char: G2Char, val data: ByteArray) {
    override fun toString(): String = "G2Inbound($side, $char, ${Pb.hex(data)})"
}

/**
 * What [G2Session] needs from a link: write raw frames to an arm and observe raw notifications. [G2Ble] implements it
 * over android.bluetooth; tests use a fake. Re-assembly, ack matching and timing live in [G2Session].
 */
interface G2Transport {
    /** Write one raw frame to [side] ([Side.BOTH] = both arms) on [char]. Suspends until queued/written. */
    suspend fun write(side: Side, frame: ByteArray, char: G2Char = G2Char.COMMAND)

    /** Every inbound notification, all arms and characteristics; hot, never completes by itself. */
    val inbound: Flow<G2Inbound>

    /** Largest ATT write (MTU - 3) both arms accept; [Framing.framePb] uses min(maxWrite - 8, 232) per packet. */
    val maxWrite: Int get() = Framing.DEFAULT_MAX_WRITE
}
