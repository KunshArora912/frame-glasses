# Even Realities G2, app-free and offline

Goal: use the G2 glasses without the Even phone app or Even's cloud, and keep
every byte of data (transcripts, notes, audio) on our own machines.

## Why this works

The G2 has no Wi-Fi and no cellular radio. Each temple is a Bluetooth LE
peripheral (Ambiq Apollo510 MCU + EM9305 BLE controller). The only path from the
glasses to Even's servers is their phone app, which relays Bluetooth traffic to
the Even Hub cloud and to Microsoft/Azure speech services for the voice
features. Remove the app from the equation and the glasses cannot reach
anything but the device we connect them to.

Consequences:
- Do not install (or uninstall) the Even app (`com.even.sg`) on the phone.
- The official Even Hub SDK is out: those apps run inside the Even app and sign
  in to Even Hub Cloud.
- We talk to the glasses directly over BLE, on stock firmware, using the
  community protocol work (below). No Even account is needed; the arms accept a
  connection from any central and the "authentication" is an unsecured
  7-packet application handshake.
- Firmware updates would normally come through the app. `jimrandomh/g2flash`
  can fetch Even's firmware from their CDN and flash it without the app, if we
  ever want one. Until then we stay on 2.3.2.14.

## Reference projects (cloned under `vendor/`, gitignored; `bash setup.sh` refetches)

| Project | What it gives us | License |
| --- | --- | --- |
| `men-g2-ble-gateway` (gpsnmeajp) | Python/bleak gateway ported from MentraOS: connects both arms, heartbeat, fast text, layouts with text+image containers, mic PCM over WebSocket, touch events, battery/firmware, HTTP + WebSocket + MCP APIs, browser UI. No outbound network calls. | MIT |
| `g2-kit-unofficial` (Commute773) | TypeScript/Bun transport + the best written protocol docs: envelope framing, container lifecycle, image pipeline, LC3 mic stream, settings, gotchas. | see repo |
| `even-g2-protocol` (i-soxi) | First public RE: packet format, CRC, teleprompter text path, protobuf guesses. | unstated |
| `open-g2-hub` (ithinkthisiscool) | Offline replacement hub (Python + Flutter): native widgets (news/tasks/calendar), "Hey Even" mic capture routed to a local or chosen LLM, notes from the 2.3.1 Even app RE. | MIT |

## Protocol in one paragraph

Both arms expose vendor service base `00002760-08c2-11e1-9073-0e8ac72eXXXX`:
`5401` write (commands), `5402` notify (replies/events), `6450`/`6402` mic
stream. Frames are `AA 21 seq len total num sid flag payload crc16`, CRC-16/CCITT-FALSE
over the payload only, little-endian. Payloads are protobuf. A session prelude
(sid 0x80 config arrays, sid 0x01 prelude to the right arm) then a heartbeat
(sid 0xE0 cmd 12 to the right arm every ~4 s) keeps the glasses from declaring
the connection lost. Display UI uses the same container model as the official
SDK: 576x288 canvas, 16 grey levels, up to 8 text/list + 4 image containers,
images as 4-bit BMP tiles of 288x144, in-place text updates <= 1000 bytes.

## Running the gateway (first hardware step)

1. Glasses out of the case, not connected to any phone. If the phone had them,
   turn phone Bluetooth off; the arms only advertise when free.
2. `.venv/Scripts/python.exe g2/vendor/men-g2-ble-gateway/gateway_server.py --no-gui`
   It scans, connects both arms, runs the prelude, saves the arm addresses to
   `g2/vendor/.../config/gateway.yaml` (gitignored), and serves
   http://127.0.0.1:8765 with a browser UI.
3. `python gateway_cli.py send-text --text "hello"`, `mic --on`, `events`.

## Mic audio

The glasses stream LC3 (10 ms frames, 40 bytes, 16 kHz mono) from the left arm;
the gateway forwards five frames per WebSocket event. Decoding uses Google's
liblc3, which the gateway carries as a submodule. There is no C compiler on this
laptop, so the DLL is built with zig from pip:

    .venv/Scripts/python.exe -m pip install ziglang
    cd g2/vendor/men-g2-ble-gateway && git submodule update --init --depth 1 liblc3
    cd liblc3 && ../../../../.venv/Scripts/python.exe -m ziglang cc -O3 -std=c11 -shared -Iinclude src/*.c -o liblc3.dll -lm

`python g2/mic_record.py [seconds] [out.wav]` then records to `g2/recordings/`
(gitignored). First hardware run 2026-10-23: both arms connected and
authenticated on firmware 2.3.2.14, fast text visible, taps received, 186 mic
packets decoded to 9.3 s of audio.

## Our code

Lives next to this file, not in `vendor/`: `mic_record.py` so far.
