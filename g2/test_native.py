"""Byte-exact tests for g2.native against the vectors in docs/native-protocol.md, the open-g2-hub
Dart port and the even-g2-protocol community branches.  Run from C:/Users/Kunsh/Desktop/frame:

    .venv/Scripts/python.exe -m unittest g2.test_native

No hardware, no network: the transport tests use FakeTransport only.
"""
from __future__ import annotations

import asyncio
import random
import struct
import unittest

from g2.native import apps, dashboard, even_ai, framing, menu, notifications, pb, session, settings, transport
from g2.native.framing import FLAG_PLAIN, FLAG_REQUEST, FrameAssembler, MagicAllocator, auth_frame, crc16, frame_pb, parse_frame
from g2.native.pb import unhex

H = unhex


# ═══════════════════════════════════════════════════════════════════════════════════════
# pb
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestPb(unittest.TestCase):
    def test_varint_vectors(self):
        for v, want in ((0, "00"), (1, "01"), (127, "7f"), (128, "8001"), (300, "ac02"), (156, "9c01"), (65365, "d5fe03")):
            self.assertEqual(pb.encode_varint(v).hex(), want, v)
            self.assertEqual(pb.decode_varint(H(want)), (v, len(want) // 2))

    def test_negative_is_ten_bytes(self):
        # device.dart:124-131 / doc: tz -16 and -24 as 10-byte two's complement varints
        self.assertEqual(pb.encode_varint(-16).hex(), "f0ffffffffffffffff01")
        self.assertEqual(pb.encode_varint(-24).hex(), "e8ffffffffffffffff01")
        self.assertEqual(pb.encode_varint(-1).hex(), "ff" * 9 + "01")
        self.assertEqual(pb.decode_signed(pb.decode_varint(H("e8ffffffffffffffff01"))[0]), -24)

    def test_writer_reader_round_trip(self):
        w = (pb.Writer().varint(1, 300).int32(2, -5).bool(3, True).string(4, "héllo").bytes(5, b"\x00\xff")
             .float32(6, 1.5).double(7, 2.25).message(8, pb.Writer().varint(1, 7)).packed_varints(9, [1, 3, 2, 2])
             .repeated(10, [b"\x08\x01", b"\x08\x02"]).packed_floats(11, [1.0, -2.5]))
        b = w.to_bytes()
        r = pb.Reader(b)
        self.assertEqual(r.varint(1), 300)
        self.assertEqual(r[2].as_signed, -5)
        self.assertEqual(r.varint(3), 1)
        self.assertEqual(r.string(4), "héllo")
        self.assertEqual(r.bytes(5), b"\x00\xff")
        self.assertEqual(pb.read_float(b, 6), 1.5)
        self.assertEqual(pb.read_double(b, 7), 2.25)
        self.assertEqual(r.sub(8).varint(1), 7)
        self.assertEqual(pb.read_packed_varints(b, 9), [1, 3, 2, 2])
        self.assertEqual(pb.read_messages(b, 10), [b"\x08\x01", b"\x08\x02"])
        self.assertEqual(struct.unpack("<2f", r.bytes(11)), (1.0, -2.5))
        self.assertEqual(len(w), len(b))

    def test_wire_types_and_clipping(self):
        fields = list(pb.iter_fields(H("0d0000a841 11 0000000000000440 1203616263 1801".replace(" ", ""))))
        self.assertEqual([(f.number, f.wire) for f in fields], [(1, 5), (2, 1), (2, 2), (3, 0)])
        self.assertEqual(fields[0].as_float, 21.0)
        self.assertEqual(fields[1].as_double, 2.5)
        # a length past the end is clipped like Python slicing (pb.dart:303-304)
        self.assertEqual(pb.read_bytes(H("12ff01aabb"), 2), b"\xaa\xbb")
        # unknown wire type stops iteration without raising
        self.assertEqual(list(pb.iter_fields(H("0801 1c".replace(" ", "")))), [pb.Field(1, 0, 1)])

    def test_tree_dump(self):
        self.assertEqual(pb.Reader(dashboard.dashboard_init(0x66)).to_tree(), {1: 2, 2: 0x66, 4: {1: 1, 2: {1: 4, 2: 3, 3: "\x01\x02\x03", 4: 4, 5: "\x01\x03\x02\x02", 6: 1, 7: 1}, 3: {}}})


# ═══════════════════════════════════════════════════════════════════════════════════════
# framing
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestFraming(unittest.TestCase):
    def test_crc16_check_values(self):
        self.assertEqual(crc16(b"123456789"), 0x29B1)  # CRC-16/CCITT-FALSE
        self.assertEqual(framing.crc16_bytes(b"").hex(), "ffff")
        self.assertEqual(framing.crc16_bytes(session.PRELUDE_PAYLOAD).hex(), "a142")

    def test_prelude_frame_exact(self):
        self.assertEqual(session.PRELUDE_FRAME.hex(), "aa219213010101200802109c01220a1a081206120408001000a142")
        p = parse_frame(session.PRELUDE_FRAME)
        self.assertEqual((p.seq, p.length, p.total, p.num, p.sid, p.flag, p.crc_ok), (0x92, 0x13, 1, 1, 0x01, 0x20, True))
        self.assertEqual(p.length, len(p.payload) + 2)  # len = payload + CRC (framing.dart:38)

    def test_auth_frame_hub_and_community(self):
        # hub: seq == magic, flag 0x00, len 0x0C
        f = auth_frame(1)
        self.assertEqual(f.hex(), "aa21010c01018000080410011a0408011004cc56")
        self.assertTrue(parse_frame(f).crc_ok)
        # community teleprompter.py:70-72 / even_ai.py: seq 1, magic 0x0C
        p1 = H("aa21010c01018000" "0804100c1a0408011004")
        self.assertEqual(auth_frame(0x0C, seq=1), p1 + framing.crc16_bytes(p1[8:]))
        # magic >= 128 is a proper 2-byte varint (deviation from the raw byte of framing.dart:84)
        f = auth_frame(200)
        p = parse_frame(f)
        self.assertEqual(pb.read_varint(p.payload, 2), 200)
        self.assertEqual(p.length, len(p.payload) + 2)
        self.assertEqual(session.parse_device(p.payload).cmd, 4)
        with self.assertRaises(ValueError):
            auth_frame(0)

    def test_heartbeat_frame(self):
        hb = session.evenhub_heartbeat(0x65)
        self.assertEqual(hb.hex(), "080c106572020800")
        f = frame_pb(hb, 0xE0, FLAG_REQUEST, 11)[0]
        self.assertEqual(f[:8].hex(), "aa210b0a0101e020")
        self.assertEqual(f[8:-2], hb)

    def test_single_frame_len_and_flag(self):
        ka = session.keepalive(0x6B)
        f = framing.frame_single(ka, 0x80, FLAG_PLAIN, 0x0E)
        self.assertEqual(f.hex(), "aa210e08010180" + "00" + "080e106b6a00" + framing.crc16_bytes(ka).hex())
        self.assertEqual(f[3], len(ka) + 2)

    def test_multi_fragment_split(self):
        payload = bytes(range(256)) * 2  # 512 bytes -> 514 with CRC -> 3 packets of 232/232/50
        frames = frame_pb(payload, 0x01, FLAG_REQUEST, 0x2A)
        self.assertEqual(len(frames), 3)
        self.assertEqual([f[3] for f in frames], [232, 232, 50])
        self.assertTrue(all(f[2] == 0x2A for f in frames))  # same seq
        self.assertEqual([(f[4], f[5]) for f in frames], [(3, 1), (3, 2), (3, 3)])
        self.assertEqual(b"".join(f[8:] for f in frames), payload + framing.crc16_bytes(payload))

    def test_crc_straddles_packets(self):
        payload = bytes(231)  # 233 with CRC: packet 1 = 231 data + 1 CRC byte, packet 2 = 1 CRC byte
        frames = frame_pb(payload, 0x01, FLAG_REQUEST, 1)
        self.assertEqual([f[3] for f in frames], [232, 1])
        asm = FrameAssembler()
        self.assertIsNone(asm.feed(frames[0]))
        out = asm.feed(frames[1])
        p = parse_frame(out)
        self.assertEqual((p.length, p.payload, p.crc_ok, asm.crc_errors), (0xFF, payload, True, 0))

    def test_max_write_limits_chunk(self):
        frames = frame_pb(bytes(100), 0x01, FLAG_REQUEST, 1, max_write=60)
        self.assertEqual([f[3] for f in frames], [52, 50])

    def test_assembler_passthrough_and_errors(self):
        asm = FrameAssembler()
        single = frame_pb(b"\x08\x01", 0x09, FLAG_REQUEST, 3)[0]
        self.assertEqual(asm.feed(single), single)
        self.assertEqual(asm.feed(b"junk"), b"junk")
        a, b = frame_pb(bytes(300), 0x01, FLAG_REQUEST, 7)
        self.assertIsNone(asm.feed(b))  # out of order -> dropped
        self.assertIsNone(asm.feed(a))
        bad = bytearray(b)
        bad[-1] ^= 0xFF
        out = asm.feed(bytes(bad))
        self.assertIsNotNone(out)
        self.assertEqual(asm.crc_errors, 1)
        self.assertFalse(parse_frame(out).crc_ok)

    def test_parse_rx_and_ack_magic(self):
        rx = transport.rx_frame(0x01, H("08021066" "1a04" "08011000"))
        self.assertEqual(rx[1], 0x12)
        sid, payload = framing.parse_rx(rx)
        self.assertEqual((sid, payload.hex()), (0x01, "080210661a0408011000"))
        self.assertEqual(framing.read_ack_magic(payload), 0x66)
        self.assertEqual(framing.read_ack_magic(H("109c01")), 156)
        self.assertIsNone(framing.read_ack_magic(H("1a00")))
        self.assertEqual(framing.parse_rx(session.PRELUDE_FRAME), (None, b""))
        self.assertIsNone(parse_frame(b"\xaa\x12\x00"))

    def test_magic_allocator_wraps_never_zero(self):
        a = MagicAllocator()
        self.assertEqual(a.next_magic(), 101)  # hub starts at 100
        self.assertEqual(a.next_seq(), 11)
        seen = [a.next_magic() for _ in range(600)]
        self.assertNotIn(0, seen)
        self.assertEqual(max(seen), 255)
        self.assertEqual(seen[seen.index(255) + 1], 1)
        s = MagicAllocator(seq_start=254)
        self.assertEqual([s.next_seq(), s.next_seq(), s.next_seq()], [255, 0, 1])

    def test_property_round_trip(self):
        rng = random.Random(1234)
        for _ in range(300):
            n = rng.choice([0, 1, 2, 229, 230, 231, 232, 233, 463, 464, 465, rng.randrange(0, 1500)])
            payload = bytes(rng.randrange(256) for _ in range(n))
            sid, flag, seq = rng.randrange(256), rng.choice([0, 0x20]), rng.randrange(256)
            frames = frame_pb(payload, sid, flag, seq)
            self.assertEqual(len(frames), max(1, -(-(n + 2) // 232)))
            self.assertTrue(all(len(f) <= 240 for f in frames))
            asm = FrameAssembler()
            out = None
            for f in frames:
                self.assertIsNone(out)
                out = asm.feed(f)
            p = parse_frame(out)
            self.assertEqual((p.sid, p.flag, p.seq, p.payload, p.crc_ok, asm.crc_errors), (sid, flag, seq, payload, True, 0))


# ═══════════════════════════════════════════════════════════════════════════════════════
# session (prelude, keep-alive, plan)
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestSession(unittest.TestCase):
    def test_doc_section_1_vectors(self):
        self.assertEqual(session.auth_payload(0x0C).hex(), "0804100c1a0408011004")
        self.assertEqual(session.role_change(0x69).hex(), "0805106922020801")
        self.assertEqual(session.role_change(0x0E, session.ROLE_LEFT).hex(), "0805100e22020802")  # community p2
        self.assertEqual(session.app_sync_subscribe(0x72).hex(), "08001072")
        self.assertEqual(session.keepalive(0x6B).hex(), "080e106b6a00")
        self.assertEqual(session.onboarding_finish(0x20).hex(), "080110201a020804")
        self.assertEqual(session.wear_query(0x21).hex(), "080310212a020801")
        self.assertEqual(session.case_query(0x22).hex(), "080110221a00")
        self.assertEqual(session.PRELUDE_PAYLOAD.hex(), "0802109c01220a1a081206120408001000")

    def test_time_sync_vectors(self):
        self.assertEqual(session.time_sync(0x68, 1792497600, -16).hex(), "0880011068820811" "08c0afddd606" "10f0ffffffffffffffff01")
        # community "transaction id" E8 FF.. 01 is just tz -24 (UTC-6)
        self.assertTrue(session.time_sync(0x0F, 1700000000, -24).endswith(H("10e8ffffffffffffffff01")))
        self.assertEqual(session.time_sync(1, 0, 0).hex(), "0880011001820804080010" + "00")
        self.assertEqual(session.tz_units_from_offset(-4 * 3600), -16)
        self.assertEqual(session.tz_units_from_offset(5 * 3600 + 1800), 22)
        self.assertEqual(session.tz_units_from_offset(-14 * 3600 * 10), -128)
        d = session.parse_device(session.time_sync(5, 1792497600, -16))
        self.assertEqual((d.cmd, d.timestamp, d.timezone), (128, 1792497600, -16))
        with self.assertRaises(ValueError):
            session.time_sync(1, -1, 0)
        with self.assertRaises(ValueError):
            session.time_sync(1, 0, 200)

    def test_flag_rule(self):
        self.assertEqual(session.flag_for(0x80, session.auth_payload(1)), FLAG_PLAIN)
        self.assertEqual(session.flag_for(0x80, session.keepalive(1)), FLAG_PLAIN)
        self.assertEqual(session.flag_for(0x80, session.role_change(1)), FLAG_REQUEST)
        self.assertEqual(session.flag_for(0x80, session.time_sync(1, 0, 0)), FLAG_REQUEST)
        self.assertEqual(session.flag_for(0x01, session.PRELUDE_PAYLOAD), FLAG_REQUEST)

    def test_parsers(self):
        self.assertTrue(session.parse_auth_reply(H("08041001" "1a00")))
        self.assertFalse(session.parse_auth_reply(H("1a00")))
        self.assertFalse(session.parse_auth_reply(H("0804100122020800")))
        ka = session.parse_device(H("080e106b6a00"))
        self.assertTrue(ka.is_keepalive_echo)
        self.assertEqual(ka.name, "BASE_CONNECT_HEART_BEAT")
        c = session.parse_case(H("08011014" "1a0a" "083c1000180120012800"))
        self.assertEqual((c.battery, c.charging, c.lid, c.in_case, c.error), (60, 0, 1, 1, 0))
        self.assertIsNone(session.parse_case(H("08011014")))
        self.assertTrue(session.parse_wear_reply(H("08031013" "2a04" "08011001")))
        self.assertFalse(session.parse_wear_reply(H("08031013" "2a04" "08011000")))
        self.assertIsNone(session.parse_wear_reply(H("08011013" "1a02" "0804")))
        a = session.parse_app_sync(H("0801" "1a04" "08001007"))
        self.assertEqual((a.background, a.foreground), (0, 7))
        self.assertEqual(session.parse_onboarding(H("08011012" "1a04" "08041000")).process_id, 4)

    def test_connect_sequence_order(self):
        a = MagicAllocator()
        steps = session.connect_sequence(a.next_magic, now=1792497600, tz_units=-16)
        self.assertEqual([s[0] for s in steps], [0x80, 0x80, 0x09, 0x09, 0x10, 0x10, 0x81, 0x09, 0x04, 0x04])
        self.assertTrue(all(s[2] == "right" for s in steps))
        self.assertEqual([pb.read_varint(s[1], 2) for s in steps], list(range(101, 111)))  # one magic each
        self.assertEqual(steps[0][1].hex(), "0805106522020801")
        full = session.connect_sequence(MagicAllocator().next_magic, now=0, tz_units=0, full=True)
        self.assertEqual([s[0] for s in full], [0x80, 0x80, 0x07, 0x0C, 0x0D, 0x09, 0x09, 0x10, 0x10, 0x1F, 0x81, 0x04, 0x09, 0x04, 0x04])
        self.assertEqual(pb.Reader(full[2][1]).to_tree()[13], {1: 0, 2: 80, 4: 0})  # 0x07 cmd 10 {13:{1:0,2:80,4:0}}
        self.assertEqual(pb.Reader(full[3][1]).to_tree()[4], {1: 1, 2: 0})  # 0x0C cmd 2 {4:{1:1,2:0}}
        self.assertEqual(pb.Reader(full[9][1]).to_tree(), {1: 0, 2: 110, 3: {1: 1}})  # 0x1F cmd 0 {3:{1:1}}
        ks = session.keepalive_steps(MagicAllocator().next_magic)
        self.assertEqual([k[2] for k in ks], ["left", "right"])
        self.assertNotEqual(ks[0][1], ks[1][1])

    def test_minimal_dashboard_plan(self):
        plan = session.MinimalDashboardPlan(MagicAllocator(), now=1792497600, tz_units=-16, temperature_c=21, weather_type=1,
                                            weather_status="Sunny")
        steps = plan.steps()
        self.assertEqual([s.number for s in steps], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 16, 17, 18])
        self.assertEqual([s.side for s in steps[:2]], ["right", "left"])
        self.assertEqual([s.action for s in steps], ["send"] * 3 + ["start_heartbeat"] + ["send"] * 14 + ["stop_heartbeat"])
        self.assertEqual(steps[0].frame.hex(), "aa21010c01018000080410011a0408011004cc56")
        self.assertEqual(steps[0].expect, "auth")
        self.assertEqual(steps[2].frame, session.PRELUDE_FRAME)
        self.assertEqual(steps[2].wait_ms, 1500)
        init = steps[5]
        self.assertEqual((init.sid, init.expect, init.magic), (0x01, "ack", 101))
        self.assertEqual(init.payload.hex(), "08021065221b08011215080410031a0301020320042a0401030202300138011a00")
        self.assertEqual([s.sid for s in steps[6:17]], [0x0D, 0x80, 0x80, 0x09, 0x09, 0x10, 0x10, 0x81, 0x09, 0x04, 0x04])
        self.assertEqual([s.flag for s in steps[6:17]], [FLAG_REQUEST] * 11)
        self.assertEqual(steps[8].payload[:8].hex(), "0880011068820811")  # time sync with tz -16
        self.assertTrue(steps[8].payload.endswith(H("f0ffffffffffffffff01")))
        self.assertEqual(steps[9].expect, "snapshot")
        self.assertEqual(steps[10].payload.hex(), "0801106a" "1a0c" "4a0a" "0800" "1000" "1801" "2000" "2801")  # units 12 h, Celsius
        self.assertEqual(steps[14].payload.hex(), "0801106e" "1a1a" "5218" "0a06080010001800" "0a06080010011800" "0a06080010021800")
        self.assertEqual(steps[15].payload.hex(), "0801106f1a06080110011805")
        self.assertEqual(steps[16].payload.hex(), "0803107032020801")
        w = steps[17]
        self.assertEqual((w.sid, w.expect), (0x01, "ack"))
        self.assertEqual(w.payload.hex(), "0802107122211a1f0a1d0a1b0d0000a8411001180120808cbbc995342a0553756e6e7930004000")
        tuples = plan.as_tuples()
        self.assertEqual(len(tuples), 19)
        self.assertEqual(tuples[3], ("right", None, 0, None))
        # every seq is distinct across the framed steps and frames carry the right flag byte
        seqs = [s.frame[2] for s in steps if s.frame is not None]
        self.assertEqual(len(seqs), len(set(seqs)) + 1)  # the prelude frame is fixed at 0x92 and sent twice
        self.assertEqual([s.frame[7] for s in steps[:3]], [0x00, 0x00, 0x20])


# ═══════════════════════════════════════════════════════════════════════════════════════
# settings
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestSettings(unittest.TestCase):
    def test_doc_vectors(self):
        self.assertEqual(settings.read_settings(0x6A).hex(), "0802106a22020801")
        self.assertEqual(settings.brightness(0x7D, 20).hex(), "0801107d1a040a021014")
        self.assertEqual(settings.display_height(0x7E, 10).hex(), "0801107e1a041202080a")
        self.assertEqual(settings.display_distance(0x7F, 10).hex(), "0801107f1a041a02080a")
        self.assertEqual(settings.read_display_timeout(128).hex(), "08011080012200")
        self.assertEqual(settings.units(0x6B).hex(), "0801106b" "1a0c" "4a0a" "0800100018012000" "2801")
        self.assertEqual(settings.gestures(0x6E).hex(), "0801106e" "1a1a" "5218" "0a06080010001800" "0a06080010011800" "0a06080010021800")

    def test_every_set_leaf(self):
        t = lambda b: pb.Reader(b).to_tree()  # noqa: E731
        self.assertEqual(t(settings.auto_brightness(1, True)), {1: 1, 2: 1, 3: {1: {1: 1}}})
        self.assertEqual(t(settings.brightness_calibration(1, 10, 20)), {1: 1, 2: 1, 3: {1: {3: 10, 4: 20}}})
        self.assertEqual(t(settings.head_up(1, True, 30)), {1: 1, 2: 1, 3: {4: {1: 1, 2: 30}}})
        self.assertEqual(t(settings.head_up(1, False)), {1: 1, 2: 1, 3: {4: {1: 0}}})
        self.assertEqual(t(settings.wear_detection(1, 1)), {1: 1, 2: 1, 3: {5: {1: 1}}})
        self.assertEqual(t(settings.silent_mode(1, False)), {1: 1, 2: 1, 3: {6: {1: 0}}})
        self.assertEqual(t(settings.units(1, 0, 1, 0, 2, 2)), {1: 1, 2: 1, 3: {9: {1: 0, 2: 1, 3: 0, 4: 2, 5: 2}}})
        self.assertEqual(t(settings.dominant_hand(1, 1, b"\x01\x02\x03\x04\x05\x06")), {1: 1, 2: 1, 3: {11: {1: 1, 2: "\x01\x02\x03\x04\x05\x06"}}})
        self.assertEqual(t(settings.display_timeout(2, 10)), {1: 2, 2: 2, 4: {1: 10}})
        self.assertEqual(t(settings.display_timeout(2, "never")), {1: 2, 2: 2, 4: {1: 65365}})
        self.assertEqual(t(settings.language(3, "Korean")), {1: 0, 2: 3, 3: {1: 8}})
        self.assertEqual(t(settings.get_device_info(4, 5)), {1: 12, 2: 4, 11: {1: {1: 5}}})
        self.assertEqual(t(settings.quick_restart(5)), {1: 15, 2: 5, 14: {}})
        self.assertEqual(t(settings.factory_reset(6)), {1: 13, 2: 6, 12: {}})

    def test_range_validation(self):
        for bad in (lambda: settings.brightness(1, 0), lambda: settings.brightness(1, 51), lambda: settings.brightness(1, True),
                    lambda: settings.display_height(1, 21), lambda: settings.display_distance(1, -1), lambda: settings.head_up(1, 1, 61),
                    lambda: settings.units(1, unit_format=16), lambda: settings.gestures(1, []), lambda: settings.gestures(1, [(0, 1)]),
                    lambda: settings.dominant_hand(1, 2), lambda: settings.dominant_hand(1, 1, b"\x00"), lambda: settings.display_timeout(1, 7),
                    lambda: settings.language(1, 9), lambda: settings.language(1, "Klingon"), lambda: settings.get_device_info(1, 9),
                    lambda: settings.read_settings(1, 2), lambda: settings.wear_detection(1, 2)):
            with self.assertRaises(ValueError):
                bad()

    def test_snapshot_parse(self):
        snap = (pb.Writer().varint(1, 1).varint(2, 30).varint(3, 10).varint(4, 12).string(5, "1.2.3").string(6, "1.2.4").varint(7, 1)
                .varint(8, 25).varint(10, 1).varint(12, 87).varint(13, 1).varint(14, 0).varint(18, 1).varint(19, 2).varint(40, 9))
        payload = pb.Writer().varint(1, 2).varint(2, 0x6A).message(4, snap).to_bytes()
        m = settings.parse(payload)
        s = m.snapshot
        self.assertEqual((m.cmd, m.magic, m.name), (2, 0x6A, "READ"))
        self.assertEqual((s.battery, s.charging, s.display_height, s.display_distance, s.left_version, s.right_version),
                         (87, 1, 10, 12, "1.2.3", "1.2.4"))
        self.assertEqual((s.head_up, s.head_up_angle, s.brightness_level, s.brightness_auto, s.unread_count), (1, 25, 30, 1, 2))
        self.assertFalse(s.is_push)
        self.assertEqual(s.extra, {40: 9})
        push = settings.parse(pb.Writer().varint(2, 3).message(4, pb.Writer().varint(12, 50).varint(13, 0)).to_bytes())
        self.assertTrue(push.snapshot.is_push)
        self.assertEqual(push.snapshot.battery, 50)
        ack = settings.parse(H("08041005" "3204" "08011000"))
        self.assertEqual(ack.ack, {"package_id": 1, "flag": 0})
        self.assertTrue(ack.ok)
        self.assertEqual(settings.parse_ack(H("08041005" "3a04" "08011001")), {"package_id": 1, "flag": 1})
        echo = settings.parse(settings.head_up(1, True, 30))
        self.assertEqual(echo.set, {"head_up": {"enabled": 1, "angle": 30, "calibration_switch": 0, "calibration": 0}})
        ev = settings.parse(H("08031005" "2a04" "08001001"))
        self.assertEqual(ev.event, {"recalibration_status": 0, "silent_mode": 1})

    def test_module_and_device_info_parse(self):
        m = settings.parse_module(H("08011080012204" "08d5fe03"))
        self.assertEqual((m.timeout, m.timeout_never), (65365, True))
        self.assertEqual(settings.parse_module(H("0802100122020805")).timeout_never, False)
        self.assertEqual(settings.parse_module(H("080010011a020807")).language, 7)
        info = pb.Writer().message(1, pb.Writer().varint(1, 5).message(6, pb.Writer().varint(1, 25).varint(2, 3)))
        info.message(1, pb.Writer().varint(1, 8).message(9, pb.Writer().bytes(1, b"\x01\x02\x03\x04\x05\x06")))
        payload = pb.Writer().varint(1, 12).varint(2, 9).message(11, info).to_bytes()
        items = settings.parse_device_info(payload)
        self.assertEqual(items[0]["wakeup_angle"], 25)
        self.assertEqual(items[1]["ble_mac"], "01:02:03:04:05:06")
        self.assertEqual(session.parse_device(payload).info[0]["name"], "DEVICE_WAKEUP_ANGLE")


# ═══════════════════════════════════════════════════════════════════════════════════════
# dashboard
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestDashboard(unittest.TestCase):
    def test_init_and_layout(self):
        self.assertEqual(dashboard.dashboard_init(0x66).hex(), "08021066221b08011215080410031a0301020320042a0401030202300138011a00")
        lay = dashboard.dashboard_layout(0x67, [3, 1])
        tree = pb.Reader(lay).to_tree()
        self.assertNotIn(3, tree[4])  # field 2 only (oneof caveat)
        self.assertEqual(tree[4][2][5], "\x03\x01")
        self.assertEqual(dashboard.quicklist_display_enable(0x7A).hex(), "0802107a22130801120d0800100020012a0104300038001a00")

    def test_weather(self):
        self.assertEqual(dashboard.weather(0x73, 21.0, 1, 1, 1792497600000, "Sunny").hex(),
                         "0802107322211a1f0a1d0a1b0d0000a8411001180120808cbbc995342a0553756e6e7930004000")
        w = pb.Reader(dashboard.weather(1, -3.5, dashboard.TEMP_FAHRENHEIT, 9, 0, "x" * 40, 2, "rain", 1, "sun")).sub(4).sub(3).sub(1).sub(1)
        self.assertEqual(pb.read_float(w.pb, 1), -3.5)
        self.assertEqual((w.varint(2), w.varint(3), len(w.string(5)), w.varint(6), w.string(7), w.varint(8), w.string(9)), (2, 9, 32, 2, "rain", 1, "sun"))

    def test_calendar(self):
        s = dashboard.Schedule("Standup", "Room 1", "10:00 AM-10:30 AM", 1792500000)
        self.assertEqual(dashboard.calendar_event(0x74, s, 1, 0).hex(),
                         "08021074223808011a3412321a30080110001a2a12075374616e6475701a06526f6f6d2031221131303a303020414d2d31303a333020414d28a0c2ddd606")
        self.assertEqual(dashboard.calendar_none(0x75).hex(), "08021075220e08011a0a12081a06080010002001")
        multi = pb.Reader(dashboard.calendar_events(1, [s, dashboard.Schedule("B", end_ts=5)])).sub(4).sub(3).sub(2).sub(3)
        self.assertEqual((multi.varint(1), multi.varint(2), multi.varint(4)), (2, 0, 0))
        items = multi.all(3)
        self.assertEqual([pb.read_varint(i.as_bytes, 1) for i in items], [1, 2])
        self.assertEqual(pb.read_string(items[1].as_bytes, 2), "B")
        a = MagicAllocator()
        seq = dashboard.calendar_sequence(a.next_magic, [s] * 10)
        self.assertEqual(len(seq), 8)  # max 8 events
        self.assertEqual([pb.Reader(p).sub(4).sub(3).sub(2).sub(3).varint(2) for p in seq], list(range(8)))
        self.assertEqual(len(dashboard.calendar_sequence(a.next_magic, [])), 1)
        long = dashboard.Schedule("é" * 200)
        self.assertLessEqual(len(pb.read_bytes(pb.Reader(dashboard.calendar_event(1, long, 1, 0)).sub(4).sub(3).sub(2).sub(3).bytes(3), 2)), 175)

    def test_news_v3(self):
        self.assertEqual(dashboard.news_reset(0x76).hex(), "080d10767a020801")
        self.assertEqual(dashboard.news_fifo_count_request(0x77).hex(), "080710774a020801")
        p = dashboard.news_push(0x78, 2, dashboard.NewsItem("Title", "Src", "Body"), 1792497600000, session_total=5)
        self.assertEqual(pb.Reader(p).to_tree(), {1: 9, 2: 0x78, 11: {1: 0, 2: 0, 3: 5, 4: 2, 5: 2, 6: "Title", 7: 1792497600000, 8: "Src", 9: "Body"}})
        self.assertEqual(dashboard.news_fifo_count(H("08081077" "5202" "0805")), 5)
        self.assertEqual(dashboard.news_fifo_count(None), 5)
        self.assertEqual(dashboard.news_fifo_count(H("08081077" "5202" "0803")), 3)
        self.assertEqual(dashboard.news_status(H("080a1078" "6202" "2800")), 0)
        ev = dashboard.parse_news_event(H("080c1000" "7204" "08031002"))
        self.assertEqual((ev.event_id, ev.news_id, ev.event_name), (3, 2, "OPEN_ARTICLE"))
        self.assertTrue(dashboard.parse_news_event(H("080b1000")).refresh_requested)
        self.assertIsNone(dashboard.parse_news_event(H("08021000")))
        # V2 kept for parity: WidgetComponent.news in field 1 of field 2
        self.assertIn(1, pb.Reader(dashboard.news_v2_push(1, [(0, "t", 1, "s", "c")])).sub(4).sub(3).sub(2).to_tree())

    def test_acks(self):
        self.assertEqual(dashboard.ack_flag(H("08021066" "1a04" "08011000")), 0)
        self.assertEqual(dashboard.ack_flag(H("08021066" "1a04" "08011001")), 1)
        self.assertEqual(dashboard.ack_flag(H("08021066" "1a00")), 0)  # empty field 3 = success
        self.assertIsNone(dashboard.ack_flag(H("08021066")))
        self.assertIsNone(dashboard.ack_flag(None))

    def test_quicklist(self):
        tasks = [dashboard.Task(7, "Milk"), dashboard.Task(8, "Eggs", True)]
        p = dashboard.quicklist_full_update(0x20, tasks)
        self.assertEqual(p.hex(), "08021020" "222a" "08011002"
                         "1a11" "0807100018002000" "2a054d696c6b00" "3802"
                         "1a11" "0808100118012000" "2a054567677300" "3802")
        ticks = dashboard.parse_quicklist_ticks(p)
        self.assertEqual([(t.uid, t.completed, t.title) for t in ticks], [(7, False, "Milk"), (8, True, "Eggs")])
        self.assertEqual(dashboard.parse_quicklist_ticks(H("08011021" "1a06" "08071001180 1".replace(" ", ""))), [dashboard.TaskTick(7, 1, True, None)])
        self.assertEqual(dashboard.parse_quicklist_event(H("08031022" "2a04" "08021007")), (2, 7))
        self.assertIsNone(dashboard.parse_quicklist_event(p))
        many = [dashboard.Task(i, str(i)) for i in range(50)]
        self.assertEqual([t.uid for t in dashboard.page_around(many, dashboard.QL_EVENT_DOWN, 10)], list(range(11, 31)))
        self.assertEqual([t.uid for t in dashboard.page_around(many, dashboard.QL_EVENT_UP, 5)], list(range(0, 5)))
        add = pb.Reader(dashboard.quicklist_add_page(1, many)).sub(4)
        self.assertEqual((add.varint(1), add.varint(2), len(add.all(3))), (2, 20, 20))
        with self.assertRaises(ValueError):
            dashboard.quicklist_full_update(1, many)
        title = pb.read_bytes(pb.read_messages(pb.Reader(dashboard.quicklist_full_update(1, [dashboard.Task(1, "é" * 150)])).bytes(4), 3)[0], 5)
        self.assertEqual(len(title), 201)
        self.assertEqual(title[-1], 0)
        self.assertEqual(dashboard.quicklist_empty(5).hex(), "0802100522040801 1000".replace(" ", ""))
        seq = dashboard.tasks_sequence(MagicAllocator().next_magic, tasks)
        self.assertEqual([(s[0], s[2]) for s in seq], [(0x01, "ack"), (0x0C, "echo"), (0x01, "ack")])

    def test_stocks_health_watchface(self):
        p = dashboard.stock_push(1, {"code": "AAPL", "price": 190.5, "change_percent": -1.25, "trend": 2, "points": [1.0, 2.0]}, 2, 1)
        sw = pb.Reader(p).sub(4).sub(3).sub(2).sub(2)
        self.assertEqual((sw.varint(1), sw.varint(2)), (2, 1))
        st = sw.sub(3)
        self.assertEqual((st.string(1), pb.read_float(st.pb, 5), st.varint(13), st.varint(14), len(st.bytes(16))), ("AAPL", 190.5, 2, 2, 8))
        with self.assertRaises(ValueError):
            dashboard.stock_display_enable(1, [1, 3])
        h = dashboard.health_snapshot(1, steps=5000, step_goal=8000, heart_rate=70, heart_rate_avg=65.5, sleep_minutes=420)
        sub = pb.Reader(h).sub(4)
        self.assertEqual(pb.Reader(h).varint(1), 2)
        items = [pb.Reader(i.as_bytes).to_tree() for i in sub.all(2)]
        self.assertEqual(items[0][1], 2)
        self.assertEqual(items[0][2], 8000)
        self.assertEqual(items[1], {1: 4, 3: 0.0, 5: 420})
        self.assertEqual(items[2][4], 65.5)
        self.assertEqual(dashboard.watchface_info_request(0x7C).hex(), "0800107c1a020801")
        wf = dashboard.parse_watchface(H("08011005" "2209" "0801" "120131" "1a026162"))
        self.assertEqual((wf.name, wf.exists, wf.version, wf.hash), ("INFO_RESPONSE", 1, "1", "ab"))
        self.assertEqual(dashboard.parse_watchface(H("08061005" "4a02" "3000")).status, 0)


# ═══════════════════════════════════════════════════════════════════════════════════════
# menu
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestMenu(unittest.TestCase):
    def test_default_vector(self):
        self.assertEqual(menu.menu_push(0x7B).hex(), "0800107b1a200805120408002001120408002004120408002005120408002006120408002007")
        self.assertEqual(menu.parse_menu_reply(H("0801107b")), (1, 0x7B))

    def test_plugin_items_and_validation(self):
        p = menu.menu_push(1, [1, 4, 5, 6], [menu.PluginItem("My App", 10000)])
        items = pb.Reader(p).sub(3)
        self.assertEqual(items.varint(1), 5)
        last = pb.Reader(items.all(2)[-1].as_bytes).to_tree()
        self.assertEqual(last, {1: 1, 2: 1, 3: "My App", 4: 10000})
        long_name = pb.Reader(menu.plugin_item(menu.PluginItem("é" * 40, 1))).bytes(3)
        self.assertLessEqual(len(long_name), 31)
        self.assertEqual(menu.plugin_name("  \x01 "), "custom")
        self.assertIsNone(menu.validate([1, 4, 5, 6, 7]))
        self.assertIn("5 to 10", menu.validate([1, 4]))
        self.assertIn("duplicate", menu.validate([1, 1, 4, 5, 6]))
        self.assertIn("not a menu app", menu.validate([1, 2, 4, 5, 6]))
        with self.assertRaises(ValueError):
            menu.menu_push(1, [1, 4], strict=True)
        self.assertEqual(len(menu.menu_push(1, [1, 4])), len(menu.menu_push(1, [1, 4], strict=False)))

    def test_selection_events(self):
        e = menu.parse_selection_event(0x0D, H("0801" "1a04" "08001006"))
        self.assertEqual((e.kind, e.app_id, e.app_name), ("foreground", 6, "Teleprompt"))
        e = menu.parse_selection_event(0xE0, H("08111005" "a20103" "08904e"))
        self.assertEqual((e.kind, e.app_id), ("plugin", 10000))
        self.assertEqual(menu.parse_selection_event(0x08, H("08011005" "1a00")).detail, "MENU_STARTUP")
        self.assertEqual(menu.parse_selection_event(0x06, H("08a2011005")).app_name, "Teleprompt")
        self.assertEqual(menu.parse_selection_event(0x07, H("08011005" "1a02" "0801")).detail, "WAKE_UP")
        self.assertIsNone(menu.parse_selection_event(0x07, H("08011005" "1a02" "0803")))
        self.assertIsNone(menu.parse_selection_event(0x09, H("08021005")))


# ═══════════════════════════════════════════════════════════════════════════════════════
# notifications
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestNotifications(unittest.TestCase):
    def test_crc32_variants(self):
        self.assertEqual(notifications.crc32c(b"123456789"), 0xE3069283)  # standard CRC-32C
        self.assertEqual(notifications.crc32_efs(b"123456789"), 0xC052A8C8)  # crc.dart:424 check value
        self.assertEqual(notifications.crc32_efs(b""), 0)
        self.assertEqual(notifications.crc32c(b""), 0)

    def test_ctrl_and_whitelist(self):
        self.assertEqual(notifications.notification_ctrl(0x10).hex(), "080110101a06080110011805")
        self.assertEqual(notifications.notification_ctrl(0x10, dnd=True, disp_time=7).hex(), "080110101a08080110011807" "2801")
        self.assertEqual(notifications.whitelist_disable(0x11).hex(), "0803101132020801")
        m = notifications.parse_notification(notifications.notification_ctrl(0x10))
        self.assertEqual((m.cmd, m.enable, m.auto_display, m.disp_time, m.dnd), (1, 1, 1, 5, 0))
        m = notifications.parse_notification(H("08031011" "3a07" "08d20910011800"))
        self.assertEqual((m.whitelist_crc, m.check_status), (1234, 1))
        ios = pb.Reader(notifications.ios_register(1, "com.x", "X")).sub(4)
        self.assertEqual((ios.string(1), ios.string(2)), ("com.x", "X"))

    def test_json_exact(self):
        j = notifications.notification_json("Title", "Body", "Sub", now=1700000000, utc_offset_s=3600, msg_id=42)
        self.assertEqual(j, b'{"android_notification":{"msg_id":42,"action":0,"app_identifier":"com.even.sg","title":"Title",'
                            b'"subtitle":"Sub","message":"Body","time_s":1700000000,"date":"20231114T231320","display_name":"Even Realities"}}')
        j = notifications.notification_json("é" * 100, "x" * 600, display_name="n" * 50, app_identifier="a" * 80, now=0, utc_offset_s=0)
        import json as _json
        d = _json.loads(j)["android_notification"]
        self.assertEqual((len(d["title"].encode()), len(d["message"]), len(d["display_name"]), len(d["app_identifier"])), (62, 511, 31, 63))
        self.assertIn('"q\\"x"', notifications.notification_json('q"x', "", now=0, utc_offset_s=0).decode())

    def test_start_header_hub_equals_community(self):
        data = b"{}" * 20
        hub = notifications.efs_start(0x10, data)
        self.assertEqual(hub[:8].hex(), "aa21105f0101c400")
        self.assertEqual(len(hub), 8 + 93 + 2)
        body = hub[8:-2]
        self.assertEqual(body[0], 0)
        self.assertEqual(struct.unpack_from("<III", body, 1), (1, len(data), notifications.crc32_efs(data)))
        self.assertEqual(body[13:13 + 26], b"user/notify_whitelist.json")
        self.assertEqual(body[13 + 26:], bytes(80 - 26))
        self.assertEqual(notifications.community_file_check(data, 0x10), hub)
        self.assertEqual(framing.crc16_bytes(body), hub[-2:])

    def test_hub_data_packets_rule(self):
        for n, want in ((100, [102]), (232, [232, 2]), (231, [231, 2]), (233, [232, 3]), (464, [232, 232, 2]), (0, [2])):
            pk = notifications.efs_data_packets(0x49, bytes(n))
            self.assertEqual([f[3] for f in pk], want, n)
            self.assertEqual([(f[4], f[5]) for f in pk], [(len(pk), i + 1) for i in range(len(pk))])
            self.assertTrue(all(f[6] == 0xC5 and f[7] == 0x00 for f in pk))
            self.assertEqual(b"".join(f[8:] for f in pk), bytes(n) + framing.crc16_bytes(bytes(n)))
        self.assertEqual(notifications.efs_data_prefix(0x49).hex(), "aa21490301 01c400 01".replace(" ", "") + framing.crc16_bytes(b"\x01").hex())
        self.assertEqual(notifications.efs_result_check(0xDA).hex(), "aa21da030101c40002" + framing.crc16_bytes(b"\x02").hex())
        steps = notifications.hub_notification_steps(MagicAllocator().next_seq, bytes(5000))
        self.assertEqual([s.name for s in steps], ["start", "data", "data", "result_check"])
        self.assertEqual([s.expect_cid for s in steps], [0, 1, 1, 2])
        self.assertEqual(steps[1].frames[0][2], steps[1].frames[1][2])  # prefix and packets share the seq
        self.assertEqual(len(steps[1].frames), 1 + 18)  # 4096 / 232 -> 18 packets (4096 % 232 = 152)
        self.assertEqual(notifications.parse_file_reply(b"\x01\x00"), (1, 0))
        self.assertIsNone(notifications.parse_file_reply(b"\x01"))

    def test_community_frames(self):
        data = notifications.notification_json("Python", "Hello!", "Test", app_identifier="com.google.android.gm",
                                               display_name="Gmail", now=1700000000, utc_offset_s=0, msg_id=12345)
        frames = notifications.community_notification_frames(data)
        self.assertEqual(len(frames), 4)
        self.assertEqual(frames[0][:8].hex(), "aa21105f0101c400")
        self.assertEqual(frames[1].hex(), "aa21490301 01c40001".replace(" ", "") + framing.crc16_bytes(b"\x01").hex())
        self.assertEqual(frames[2][:8], bytes([0xAA, 0x21, 0x49, len(data) + 2, 1, 1, 0xC5, 0x00]))
        self.assertEqual(frames[2][8:-2], data)
        self.assertEqual(frames[3][:9].hex(), "aa21da030101c40002")
        with self.assertRaises(ValueError):
            notifications.community_notification_frames(bytes(300))
        self.assertEqual([f[3] for f in notifications.community_data(bytes(300))], [236, 68])


# ═══════════════════════════════════════════════════════════════════════════════════════
# even ai
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestEvenAI(unittest.TestCase):
    def test_hub_vectors(self):
        self.assertEqual(even_ai.enter(129).hex(), "08011081011a020802")
        self.assertEqual(even_ai.exit_(5).hex(), "080110051a020803")
        self.assertEqual(even_ai.heartbeat(6, 3).hex(), "080910065a020803")
        self.assertEqual(even_ai.ask(7, "Hi").hex(), "080310072a04" "22024869")
        self.assertEqual(even_ai.analyse(8).hex(), "080410083200")
        self.assertEqual(even_ai.reply(9, "ok", True).hex(), "080510093a06" "22026f6b" "3001")
        self.assertEqual(even_ai.reply(9, "ok", False).hex(), "080510093a04" "22026f6b")
        self.assertEqual(pb.Reader(even_ai.config_captured(1)).to_tree()[13], {1: 0, 2: 80, 4: 0})
        self.assertEqual(pb.Reader(even_ai.config(1)).to_tree()[13], {1: 0, 2: 80})
        self.assertEqual(even_ai.evenhub_audio_control(2, True).hex(), "080f1002" "920102" "0801")

    def test_community_vectors(self):
        def pkt(seq, payload):
            return framing.add_crc(bytes([0xAA, 0x21, seq, len(payload) + 2, 1, 1, 0x07, 0x20]) + payload)
        self.assertEqual(pkt(8, even_ai.enter(100)).hex(), "aa21080a01010720080110641a0208025ca9")
        self.assertEqual(pkt(9, even_ai.ask(101, "What is 2 + 2?", community=True)).hex(),
                         "aa21091e01010720080310652a16080010001800220e576861742069732032202b20323fc72b")
        self.assertEqual(pkt(10, even_ai.reply(102, "The answer is 4!", True, community=True)).hex(),
                         "aa210a2001010720080510663a18080010001800221054686520616e737765722069732034218432")
        # their captured glasses REPLY "20": aa12c708010107000805103b3a00a8e4
        rx = H("aa12c708010107000805103b3a00a8e4")
        p = parse_frame(rx)
        self.assertEqual((p.sid, p.flag, p.crc_ok), (0x07, 0x00, True))
        m = even_ai.parse(p.payload)
        self.assertEqual((m.cmd, m.magic, m.name), (5, 59, "REPLY"))

    def test_chunks_and_parse(self):
        text = "é" * 600  # 1200 bytes
        chunks = even_ai.reply_chunks(text)
        self.assertEqual([len(c) for c in chunks], [512, 512, 176])
        self.assertTrue(all(c.decode("utf-8") for c in chunks))
        self.assertEqual(even_ai.reply_chunks(""), [b""])
        seq = even_ai.reply_sequence(MagicAllocator().next_magic, text)
        self.assertEqual([pb.read_varint(pb.read_bytes(p, 7), 6) for p in seq], [-1, -1, 1])
        with self.assertRaises(ValueError):
            even_ai.reply(1, bytes(513), True)
        self.assertTrue(even_ai.parse(H("08011005" "1a02" "0801")).is_wake_up)
        self.assertTrue(even_ai.parse(H("08081005" "5202" "0802")).is_stream_complete)
        self.assertEqual(even_ai.parse(H("08021005" "2202" "0802")).value_name, "VAD_END")
        self.assertEqual(even_ai.parse(H("08a1011005" "6202" "0803")).error, 3)
        self.assertEqual(even_ai.parse(even_ai.ask(1, "Q")).text, b"Q")

    def test_mic_packet_layout(self):
        frames = [bytes([i]) * 40 for i in range(5)]
        raw = b"".join(frames) + struct.pack("<hh", -1200, 90) + bytes([0xAB])
        self.assertEqual(len(raw), 205)
        p = even_ai.parse_mic_packet(raw)
        self.assertEqual(p.frames, tuple(frames))
        self.assertEqual((p.signal_ratio, p.direction, p.counter, len(p.lc3)), (-1200, 90, 0xAB, 200))
        self.assertIsNone(even_ai.parse_mic_packet(raw[:199]))
        short = even_ai.parse_mic_packet(raw[:200])
        self.assertEqual((short.counter, short.direction), (None, None))


# ═══════════════════════════════════════════════════════════════════════════════════════
# apps
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestApps(unittest.TestCase):
    def test_teleprompter_hub(self):
        st = apps.tp_start(1, 3, 25, mode=apps.TP_MODE_MANUAL, scroll_interval_ms=0, countdown_s=3)
        self.assertEqual(pb.Reader(st).to_tree(), {1: 1, 2: 1, 3: {1: 1, 2: {1: 1, 4: 3, 5: 25, 6: 567, 8: 3, 9: 9}}})
        pg = apps.tp_page(2, 1, ["a", "b"])
        self.assertEqual(pb.Reader(pg).to_tree(), {1: 3, 2: 2, 5: {1: 1, 2: 2, 3: "a\nb \n"}})
        self.assertEqual(apps.tp_page(2, 0, []).hex(), "080310022a04" "1a02" "200a")  # zero pageId/lineCount omitted
        self.assertEqual(apps.tp_heartbeat(3, 2, 5).hex(), "08ff0110036a040802 1005".replace(" ", ""))
        self.assertEqual(apps.tp_close(4).hex(), "080110041a020804")
        with self.assertRaises(ValueError):
            apps.tp_page(1, 0, ["x" * 1100])
        pages = apps.tp_paginate("word " * 100)
        self.assertEqual(len(pages), 2)
        self.assertTrue(all(len(l) <= 28 for p in pages for l in p))
        self.assertEqual(sum(len(p) for p in pages), 20)  # 5 words of "word" per 28-char line
        m = apps.tp_parse(H("08a4011005" "5202" "0803"))
        self.assertEqual((m.name, m.page), ("PAGE_DATA_REQUEST", 3))
        self.assertTrue(apps.tp_parse(H("08aa011005" "8a0100")).is_close)
        self.assertTrue(apps.tp_parse(H("08a1011005" "3a02" "0804")).is_close)
        self.assertEqual(apps.tp_parse(H("08a6011005" "6202" "0801")).error, 1)
        fl = pb.Reader(apps.tp_file_list(1, [("id1", "Speech")], menu_mode=1)).sub(4)
        self.assertEqual(pb.Reader(fl.all(1)[0].as_bytes).to_tree(), {1: "id1", 2: "Speech"})

    def test_teleprompter_community(self):
        self.assertEqual(len(apps.COMMUNITY_TP_DISPLAY_CONFIG), 106)
        cfg = apps.community_tp_display_config(8, 100)
        self.assertEqual(cfg[:8].hex(), "aa2108" + f"{106 + 6 + 2:02x}" + "01010e20")
        self.assertEqual(cfg[8:14].hex(), "08021064226a")
        init = apps.community_tp_init(9, 101, 140, True)
        self.assertEqual(init[6:8].hex(), "0620")
        self.assertEqual(pb.Reader(init[8:-2]).to_tree(), {1: 1, 2: 101, 3: {1: 1, 2: {1: 1, 2: 0, 3: 0, 4: 267, 5: 2665, 6: 230, 7: 1294, 8: 5, 9: 0}}})
        self.assertTrue(parse_frame(init).crc_ok)
        page = apps.community_tp_page(10, 102, 0, "hello")
        self.assertEqual(pb.Reader(page[8:-2]).to_tree(), {1: 3, 2: 102, 5: {1: 0, 2: 10, 3: "\nhello"}})
        self.assertEqual(apps.community_tp_marker(11, 103)[8:-2].hex(), "08ff0110676a0408001006")
        sync = apps.community_tp_sync(12, 104)
        self.assertEqual(sync[6:8].hex(), "8000")
        self.assertEqual(sync[8:-2], session.keepalive(104))
        pages = apps.community_tp_format("one two three")
        self.assertEqual(len(pages), 14)
        self.assertTrue(all(p.endswith(" \n") and p.count("\n") == 10 for p in pages))
        frames = apps.community_tp_frames("x")
        self.assertEqual([f[6] for f in frames], [0x0E, 0x06] + [0x06] * 10 + [0x06, 0x80] + [0x06] * 4)
        self.assertEqual([f[2] for f in frames], list(range(8, 8 + len(frames))))

    def test_translate(self):
        op = apps.tr_open(1, "en", "de", lang_list=[("EN", "English"), ("DE", "Deutsch")])
        o = pb.Reader(op).sub(3)
        self.assertEqual((o.varint(1), o.string(2), o.string(6), o.string(7)), (1, "EN>DE", "EN", "DE"))
        self.assertEqual([pb.Reader(f.as_bytes).to_tree() for f in o.all(5)], [{1: "EN", 2: "English"}, {1: "DE", 2: "Deutsch"}])
        self.assertEqual(pb.Reader(apps.tr_text(2, " Hallo ", "Hello", True, "A")).to_tree(), {1: 2, 2: 2, 4: {1: "Hallo", 2: "Hello", 4: 1, 5: "A"}})
        self.assertEqual(apps.tr_text(2, "a", " ", False).hex(), "08021002" "2206" "0a0161" "120120")
        self.assertEqual(apps.tr_heartbeat(3).hex(), "08ff0110034200")
        self.assertEqual(apps.tr_close(4).hex(), "080110041a020802")
        self.assertEqual(apps.tr_lang_switch_response(5).hex(), "08a5011005" "5200")
        self.assertEqual(apps.tr_parse(H("08a2011005" "3a02" "0801")).error, 1)
        self.assertTrue(apps.tr_parse(H("08a1011005" "2a02" "0802")).is_close)
        self.assertEqual(apps.tr_parse(H("08a4011005" "4a04" "0a024652")).lang_key, "FR")

    def test_navigation(self):
        bi = apps.nav_basic_info(1, "turn right", "200 m", "Main St", eta="12:30", method=2, counter=3)
        self.assertEqual(pb.Reader(bi).to_tree(), {1: 7, 2: 1, 5: {1: 3, 2: "200 m", 3: "Main St", 4: "--", 5: "--", 6: "ETA: 12:30", 7: "--", 8: 2, 10: 3}})
        self.assertEqual(apps.nav_start(2).hex(), "080510021a00")
        self.assertEqual(apps.nav_heartbeat(3).hex(), "080010031a00")
        self.assertEqual(pb.Reader(apps.nav_location_list(4, ["Home", ""], [1, 2])).to_tree(), {1: 2, 2: 4, 4: {1: 2, 2: ["Home", "--"], 3: "\x01\x02"}})
        self.assertEqual(apps.nav_direction_index("UTURN"), 10)
        with self.assertRaises(ValueError):
            apps.nav_direction_index("sideways")
        with self.assertRaises(ValueError):
            apps.nav_location_list(1, ["x"] * 21)
        sel = apps.nav_parse(H("08041005" "420a" "0a04486f6d65" "1801" "2001"))
        self.assertEqual((sel.location, sel.value, sel.method), ("Home", 1, 1))
        self.assertTrue(apps.nav_parse(H("080d1005" "1a00")).is_exit)
        self.assertTrue(apps.nav_parse(H("08141005" "1a00")).is_exit)
        self.assertEqual(apps.nav_parse(H("08121005" "5a02" "0802")).method, 2)
        self.assertEqual(len(apps.nav_text("x" * 100)), 60)


# ═══════════════════════════════════════════════════════════════════════════════════════
# transport + Glasses (FakeTransport only)
# ═══════════════════════════════════════════════════════════════════════════════════════


class TestTransport(unittest.IsolatedAsyncioTestCase):
    def _glasses(self, responder=None):
        t = transport.FakeTransport(responder or transport.fake_glasses_responder())
        g = transport.Glasses(t, MagicAllocator(), clock=lambda: 1792497600.0, tz_units=-16)
        g.time_scale = 0.01
        return t, g

    def test_protocol_and_gateway_event_parsing(self):
        self.assertIsInstance(transport.FakeTransport(), transport.Transport)
        gw = transport.GatewayRawTransport("http://127.0.0.1:8765/")
        self.assertIsInstance(gw, transport.Transport)
        self.assertEqual(gw.ws_url, "ws://127.0.0.1:8765/ws")
        self.assertEqual(gw.parse_event({"kind": "glasses.raw_packet", "data": {"source": "L", "hex": "aa12"}}), ("left", b"\xaa\x12"))
        self.assertEqual(gw.parse_event({"source": "R", "hex": "AA12"}), ("right", b"\xaa\x12"))
        self.assertIsNone(gw.parse_event({"kind": "glasses.touch", "data": {"gesture": "tap"}}))
        self.assertIsNone(gw.parse_event({"kind": "glasses.raw_packet", "data": {"source": "?", "hex": "aa"}}))

    async def test_minimal_plan_end_to_end(self):
        t, g = self._glasses()
        async with g:
            plan = session.MinimalDashboardPlan(g.alloc, now=1792497600, tz_units=-16, temperature_c=21, weather_type=1, weather_status="Sunny")
            results = await g.run_plan(plan)
            self.assertEqual(len(results), 19)
            # every step that expected a reply got one
            for st, reply in results:
                if st.expect:
                    self.assertIsNotNone(reply, st.label)
            self.assertFalse(g.heartbeat_running)  # step 18 stopped it
            self.assertGreaterEqual(g.hb_count, 1)  # it ran between steps 4 and 18
            self.assertEqual(dashboard.ack_flag(results[5][1].payload), 0)
            self.assertEqual(dashboard.ack_flag(results[17][1].payload), 0)
            self.assertEqual(g.snapshot.battery, 87)
            self.assertEqual((g.status["battery"], g.status["worn"], g.status["case_battery"]), (87, True, 60))
        # wire order: auth R, auth L, prelude, [heartbeats...], prelude, init, 0x0D, role, time, read, units, ob, wear, case, gest, ctrl, wl, weather
        sids = [(s, parse_frame(f).sid) for s, f, _ in t.writes if parse_frame(f).sid != 0xE0]
        self.assertEqual(sids[:3], [("right", 0x80), ("left", 0x80), ("right", 0x01)])
        self.assertEqual([x[1] for x in sids[3:]], [0x01, 0x01, 0x0D, 0x80, 0x80, 0x09, 0x09, 0x10, 0x10, 0x81, 0x09, 0x04, 0x04, 0x01])
        self.assertTrue(all(s == "right" for s, _ in sids[2:]))
        self.assertEqual(t.writes[0][1], auth_frame(1))
        self.assertEqual(t.writes[2][1], session.PRELUDE_FRAME)
        hb = [f for _, f, _ in t.writes if parse_frame(f).sid == 0xE0]
        self.assertTrue(hb and all(parse_frame(f).payload[:2] == b"\x08\x0c" for f in hb))
        magics = [framing.read_ack_magic(parse_frame(f).payload) for _, f, _ in t.writes[3:]]
        self.assertEqual(len(magics), len(set(magics)))

    async def test_plan_without_replies_times_out_cleanly(self):
        t, g = self._glasses(responder=lambda *a: None)
        async with g:
            steps = session.MinimalDashboardPlan(g.alloc, now=0, tz_units=0).steps()[:1]
            steps[0].timeout_ms = 20
            results = await g.run_plan(steps)
            self.assertIsNone(results[0][1])

    async def test_pushes(self):
        t, g = self._glasses()
        async with g:
            self.assertEqual(await g.set_weather(21, 1, "Sunny"), 0)
            self.assertFalse(g.heartbeat_running)
            self.assertEqual(await g.set_calendar([dashboard.Schedule("A", end_ts=1), dashboard.Schedule("B", end_ts=2)]), [0, 0])
            statuses = await g.push_news([dashboard.NewsItem("T", "S", "C")])
            self.assertEqual(statuses, [0] * 5)
            news = [p for p in t.payloads("right", 0x01) if dashboard.read_cmd(p) == 9]
            self.assertEqual([pb.read_varint(pb.read_bytes(p, 11), 4) for p in news], [0, 1, 2, 3, 4])
            self.assertEqual(pb.read_varint(pb.read_bytes(news[0], 11), 7), (1792497600 - 16 * 900) * 1000)  # local wall-clock ms
            self.assertTrue(await g.set_tasks([dashboard.Task(1, "Milk")]))
            ql = t.payloads("right", 0x0C)
            self.assertEqual(len(ql), 1)
            self.assertTrue(await g.set_menu([1, 4, 5, 6, 7]))
            self.assertEqual(t.payloads("right", 0x03)[0][4:], menu.menu_push(0)[4:])
            snap = await g.read_settings()
            self.assertEqual((snap.battery, snap.display_height, snap.left_version), (87, 10, "1.2.3"))
            self.assertTrue(await g.keepalive_once())
            ka = [(s, parse_frame(f)) for s, f, _ in t.writes if parse_frame(f).sid == 0x80]
            self.assertEqual([s for s, _ in ka], ["left", "right"])
            self.assertTrue(all(p.flag == 0x00 for _, p in ka))
            self.assertIsNotNone(g.last_keepalive_echo)

    async def test_fragmented_inbound_and_listeners(self):
        t, g = self._glasses(responder=lambda *a: None)
        seen = []
        async with g:
            g.on(lambda side, p: seen.append((side, p.sid, p.payload)))
            big = pb.Writer().varint(1, 2).varint(2, 5).bytes(9, bytes(400)).to_bytes()
            for f in frame_pb(big, 0x09, 0x00, 7):
                rx = bytearray(f)
                rx[1] = 0x12
                t.push("right", bytes(rx))
            fut = g._wait(lambda s, p: p.sid == 0x09)
            p = await asyncio.wait_for(fut, 1)
            self.assertEqual(p.payload, big)
            self.assertEqual(seen[0][:2], ("right", 0x09))

    async def test_notify_hub_form(self):
        def responder(side, p, raw):
            if p.sid == 0xC4 or p.sid == 0xC5:
                cid = p.payload[0] if p.sid == 0xC4 else None
                if p.sid == 0xC5 and p.num == p.total:
                    cid = 1
                if cid is not None and not (p.sid == 0xC4 and cid == 1):
                    return [(side, transport.rx_frame(0xC4, bytes([cid, 0])))]
            return None
        t, g = self._glasses(responder)
        async with g:
            ok = await g.notify("Hi", "There", form="hub", timeout=1)
            self.assertTrue(ok)
            self.assertEqual([f[6] for _, f, _ in t.file_writes], [0xC4, 0xC4, 0xC5, 0xC4])
            self.assertEqual(t.file_writes[0][1][:8].hex()[6:8], "5f")
            ok = await g.notify("Hi", "There", form="community")
            self.assertTrue(ok)
            self.assertEqual(len(t.file_writes), 8)
            last = parse_frame(t.writes[-1][1])
            self.assertEqual((t.writes[-1][0], last.sid, last.flag, last.payload[:2]), ("left", 0x80, 0x00, b"\x08\x0e"))

    async def test_even_ai_sequence(self):
        t, g = self._glasses()
        async with g:
            await g.even_ai_answer("Q", "A" * 600)
            cmds = [pb.read_varint(p, 1) for p in t.payloads("right", 0x07)]
            self.assertEqual(cmds, [1, 3, 4, 5, 5])
            await g.even_ai_answer("Q", "A", community=True)
            self.assertEqual([pb.read_varint(p, 1) for p in t.payloads("right", 0x07)][5:], [1, 3, 5])


if __name__ == "__main__":
    unittest.main()
