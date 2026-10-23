"""Record the G2 microphone to a WAV file through the local gateway.

    python g2/mic_record.py [seconds] [out.wav]

Needs gateway_server.py running (see g2/README.md) and liblc3.dll built inside
g2/vendor/men-g2-ble-gateway/liblc3 (python -m ziglang cc ... see README).
The glasses send LC3 frames (10 ms, 40 bytes, 16 kHz mono); each gateway event
carries five of them. Output: 16 kHz signed 16-bit mono PCM. Nothing leaves the
laptop.
"""
import asyncio, base64, ctypes.util, json, pathlib, sys, time, wave

HERE = pathlib.Path(__file__).resolve().parent
LC3_DIR = HERE / "vendor" / "men-g2-ble-gateway" / "liblc3"
GATEWAY = "http://127.0.0.1:8765"
FRAME_BYTES, FRAME_US, RATE = 40, 10_000, 16_000


def load_decoder():
    sys.path.insert(0, str(LC3_DIR / "python"))
    orig = ctypes.util.find_library
    ctypes.util.find_library = lambda n: "msvcrt" if n == "c" else orig(n)   # Windows quirk
    try:
        import lc3
    finally:
        ctypes.util.find_library = orig
    dll = next((p for p in (LC3_DIR / "liblc3.dll", LC3_DIR / "liblc3.so", LC3_DIR / "liblc3.dylib") if p.exists()), None)
    if dll is None:
        sys.exit(f"liblc3 not built in {LC3_DIR}; see g2/README.md")
    return lc3.Decoder(FRAME_US, RATE, num_channels=1, libpath=str(dll))


async def record(seconds: float, out: pathlib.Path) -> None:
    import aiohttp
    dec = load_decoder()
    pcm = bytearray()
    packets = 0
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(GATEWAY + "/ws") as ws:
            await s.post(GATEWAY + "/api/display", json={"text": f"Recording {int(seconds)} s\nspeak now"})
            await s.post(GATEWAY + "/api/mic", json={"enabled": True})
            t0 = time.time()
            try:
                while time.time() - t0 < seconds:
                    try:
                        m = await asyncio.wait_for(ws.receive(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if m.type != aiohttp.WSMsgType.TEXT:
                        continue
                    e = json.loads(m.data)
                    if e.get("kind") != "glasses.mic_audio":
                        continue
                    raw = base64.b64decode(e["data"]["data_base64"])[: e["data"].get("frame_size", FRAME_BYTES * 5)]
                    for i in range(0, len(raw) - FRAME_BYTES + 1, FRAME_BYTES):
                        pcm += dec.decode(raw[i:i + FRAME_BYTES], bit_depth=16)
                    packets += 1
            finally:
                await s.post(GATEWAY + "/api/mic", json={"enabled": False})
                await s.post(GATEWAY + "/api/display", json={"text": "Saved."})
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(bytes(pcm))
    n = len(pcm) // 2
    peak = rms = 0.0
    if n:
        import array
        a = array.array("h", bytes(pcm))
        peak = max(abs(x) for x in a) / 32768
        rms = (sum(x * x for x in a) / n) ** 0.5 / 32768
    print(f"{out}: {packets} packets, {n / RATE:.2f} s audio, peak {peak:.3f}, rms {rms:.4f}")


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 8
    dest = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "recordings" / time.strftime("g2_%Y%m%d_%H%M%S.wav")
    asyncio.run(record(secs, dest))
