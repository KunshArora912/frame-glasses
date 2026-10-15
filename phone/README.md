# Frame phone companion

A web page the phone opens in Chrome. It talks to the glasses over Web
Bluetooth, shows battery and charging from the wake screen's status line,
has a Lua console, and can relay the Bluetooth link to the laptop so every
Python app in this project runs through the phone while you're out.

Frame has no Wi-Fi. Its only radio is Bluetooth Low Energy, and it keeps
exactly one bond. Bonding the phone unbonds the laptop; after that the
laptop reaches the glasses through the phone (relay) or you reset and
re-pair the laptop.

## Pieces

| file | runs on | job |
|---|---|---|
| `phone/index.html`, `app.js` | phone (Chrome) | Bluetooth link, status, console, relay bridge |
| `relay_server.py` | laptop | serves the page, bridges phone <-> apps on port 8765 |
| `frame_relay.py` | laptop | `python frame_relay.py <app.py>` runs an app through the phone |
| `frame_host.py --relay` | laptop | launcher companion, through the phone |
| `phone_sim.py` | laptop | stands in for the phone (laptop radio) to test the relay |

## One-time: bond the phone

1. Dock the glasses, hold the cradle pin, undock while holding, release.
   This erases the laptop bond and reformats the filesystem (main.lua goes
   with it; reinstall later through the relay).
2. On the laptop: `python relay_server.py`.
3. Open the page on the phone (see "Reaching the page"), tap **Connect**,
   pick "Frame xx", accept the Android pairing prompt.
4. Status appears once main.lua is on the glasses. To put it there through
   the phone: tap **Link** on the page, then on the laptop
   `python frame_relay.py install_main.py`.

## Reaching the page (Web Bluetooth needs a secure origin)

Pick one:

- **USB, zero setup.** Plug the phone in with USB debugging on, run
  `adb reverse tcp:8765 tcp:8765`, open `http://localhost:8765` on the phone.
  `localhost` counts as secure. Relay URL: `ws://localhost:8765/phone`.
- **Same Wi-Fi.** Open `http://<laptop-ip>:8765`. Chrome blocks Web
  Bluetooth on plain http, so add that origin at
  `chrome://flags/#unsafely-treat-insecure-origin-as-secure` and relaunch.
  Relay URL: `ws://<laptop-ip>:8765/phone`.
- **Anywhere (Tailscale).** Log the laptop in (`tailscale up`, browser
  login), enable HTTPS certificates in the tailnet admin, then
  `tailscale serve --bg 8765`. Install Tailscale on the phone and open
  `https://<laptop>.<tailnet>.ts.net`. Relay URL:
  `wss://<laptop>.<tailnet>.ts.net/phone`. The page works as an installed
  PWA ("Add to Home screen").

Keep the page in the foreground while relaying; it holds a screen wake
lock, but Android may still suspend a background tab's Bluetooth.

## Using it

- Status card: battery, charging, firmware, from `status:` lines main.lua
  prints every 10 s while a host is connected.
- Console: Lua goes straight to the REPL. Press **Break** first if main.lua
  is running (it loops), **Wake screen** afterwards to restart it.
- Relay: tap **Link**. Then on the laptop `python frame_host.py --relay`,
  and pick apps from the glasses' menu or the buttons on the page. Any app
  can also be run directly: `python frame_relay.py assistant.py`.

## Testing without a phone

```
python relay_server.py           # terminal 1
python phone_sim.py              # terminal 2: laptop radio pretends to be the phone
python frame_relay.py hello.py   # terminal 3: an app, through the relay
```

## Protocol

JSON over WebSocket, payloads base64. See the docstring in `relay_server.py`.
Back-pressure is per write: the phone acknowledges each write (`wrote`)
before the app's next one is sent, so Lua uploads and data streams keep
their ordering.
