"""Developer tool for the G2 launcher: make a new app, preview it on the laptop,
push it to the glasses, design raw layouts, check the stack.

    python g2/dev.py new weather              # scaffold g2/apps/weather.py from the template
    python g2/dev.py list                     # apps the launcher will show, in menu order
    python g2/dev.py preview weather          # run the app against fakes; print screens, save PNGs
    python g2/dev.py preview weather --gestures tap,swipe_down,tap
    python g2/dev.py push weather             # open it on the glasses now (launcher must be running)
    python g2/dev.py push home                # back to the home screen
    python g2/dev.py layout screen.json       # send a layout file straight to the glasses
    python g2/dev.py layout screen.json --watch   # resend whenever the file changes
    python g2/dev.py render screen.json       # PNG of a layout file, no glasses needed
    python g2/dev.py doctor                   # gateway, arms, speech, decoder, apps
    python g2/dev.py test                     # run the launcher and app tests

The running launcher (python g2/run.py) reloads a changed app file within two
seconds, so the loop is: edit, save, look at the glasses. `push` just tells it
which app to open. Everything stays on this laptop.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APPS = HERE / "apps"
PREVIEWS = HERE / "previews"
OPEN_REQUEST = HERE / "logs" / "open_app.txt"
GATEWAY = os.environ.get("G2_GATEWAY", "http://127.0.0.1:8765")
GESTURE_ALIASES = {"tap": "single_tap", "double": "double_tap", "dtap": "double_tap", "up": "swipe_up",
                   "down": "swipe_down", "long": "long_press_release"}


def _post(path: str, body: dict, base: str = GATEWAY) -> dict:
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"accepted": False, "http": e.code, "error": e.read()[:300].decode(errors="replace")}
    except Exception as e:
        return {"accepted": False, "error": f"{type(e).__name__}: {e}"}


def _status(base: str = GATEWAY) -> dict | None:
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ------------------------------------------------------------------ new
def cmd_new(a) -> int:
    name = re.sub(r"[^a-z0-9_]", "_", a.name.lower()).strip("_")
    if not name or name[0].isdigit():
        print("name must start with a letter: letters, digits, underscores")
        return 2
    dest = APPS / f"{name}.py"
    if dest.exists() and not a.force:
        print(f"{dest} exists (use --force to overwrite)")
        return 1
    title = a.title or name.replace("_", " ").title()
    cls = re.sub(r"[^A-Za-z0-9]", "", title) or "MyApp"
    src = (APPS / "_template.py").read_text(encoding="utf-8")
    src = src.replace("class MyApp(App):", f"class {cls}(App):")
    src = src.replace('name = "My App"', f'name = "{title}"')
    src = src.replace('blurb = "one line that says what it does"', f'blurb = "{a.blurb}"')
    src = src.replace("TEMPLATE: copy me to g2/apps/<yourname>.py (or run: python g2/dev.py new <yourname>).",
                      f"{title}: {a.blurb}")
    dest.write_text(src, encoding="utf-8")
    print(f"created {dest}")
    print(f"next:  python g2/dev.py preview {name}      (screens on the laptop)")
    print(f"       python g2/dev.py push {name}         (open it on the glasses)")
    return 0


# ----------------------------------------------------------------- list
def cmd_list(a) -> int:
    from g2 import apps as registry
    for app in registry.discover():
        flag = "  (hidden)" if app.hidden else ""
        print(f"{app.order:4d}  {app.name:<14} {getattr(app, 'module', '?'):<14} {app.blurb}{flag}")
    return 0


# -------------------------------------------------------------- preview
async def _preview(a) -> int:
    from g2.fakes import FakeGateway, FakeSpeech, FakeBrain, Clock
    from g2.launcher import Launcher
    from g2 import apps as registry
    from g2.render import save
    from notes.store import Store
    import tempfile

    apps = registry.discover()
    app = next((x for x in apps if x.key == a.name.lower().replace(" ", "_") or getattr(x, "module", "") == a.name), None)
    if app is None:
        print(f"no app named {a.name}; try: python g2/dev.py list")
        return 1
    gw = FakeGateway()
    speech = FakeSpeech(gw, text=a.say)
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "thoughts.json")
    la = Launcher(gw, speech, FakeBrain(a.answer), store, apps=apps, clock=Clock(),
                  transcripts_dir=Path(tmp.name) / "transcripts", data_dir=Path(tmp.name))
    await la.open(app.key)
    gestures = [GESTURE_ALIASES.get(g.strip(), g.strip()) for g in a.gestures.split(",") if g.strip()] if a.gestures else []
    frames: list[tuple[str, list[dict]]] = [("open", gw.layouts[-1] if gw.layouts else [])]
    for g in gestures:
        await gw.emit("touch", g, 1)
        if la.busy is not None and not la.busy.done():
            try:
                await asyncio.wait_for(la.busy, timeout=5)
            except Exception:
                pass
        if gw.layouts:
            frames.append((g, gw.layouts[-1]))
    out_dir = PREVIEWS / app.key
    problems: list[str] = []
    for i, (label, elements) in enumerate(frames):
        text = "\n".join(e.get("text", "") for e in elements if e.get("type") == "text")
        print(f"\n--- {i}: after {label} ---")
        print("+" + "-" * 48 + "+")
        for row, line in enumerate(text.split("\n")[:12]):
            marker = "!" if len(line) > 48 or row >= 10 else "|"
            print(f"{marker}{line[:48]:<48}{marker}")
            if len(line) > 48:
                problems.append(f"frame {i}: line {row} is {len(line)} chars (max 48): {line[:30]}...")
        if len(text.split("\n")) > 10:
            problems.append(f"frame {i}: {len(text.split(chr(10)))} rows (max 10)")
        print("+" + "-" * 48 + "+")
        if not a.no_png:
            p = save(elements, out_dir / f"{i:02d}_{label}.png")
            print(f"png: {p}")
    await la.stop()
    tmp.cleanup()
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  " + p)
        return 1
    print("\nall frames fit the 48 x 10 grid")
    return 0


def cmd_preview(a) -> int:
    return asyncio.run(_preview(a))


# ----------------------------------------------------------------- push
def cmd_push(a) -> int:
    st = _status(a.gateway)
    if st is None:
        print(f"gateway not answering at {a.gateway}; start it with: python g2/run.py")
        return 1
    g = st.get("glasses", {})
    if g.get("parked"):
        # the launcher is parked behind the native dashboard: wake our page
        # first, so its page_create event lands before the open request
        _post("/api/page", {"create": True}, a.gateway)
        time.sleep(1.0)
    OPEN_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = OPEN_REQUEST.with_suffix(".tmp")
    tmp.write_text(a.name.strip(), encoding="utf-8")
    os.replace(tmp, OPEN_REQUEST)                      # atomic: the launcher never reads a half-written file
    print(f"asked the launcher to open '{a.name}' (it checks every 2 s; glasses {g.get('phase')}, "
          f"battery {g.get('battery_level')}%)")
    if not g.get("ready"):
        print("note: the glasses are not connected right now; it opens when they are")
    return 0


# --------------------------------------------------------------- layout
def _load_layout(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "elements" in data:
        return data["elements"]
    if isinstance(data, dict) and "text" in data:
        from g2.render import text_to_elements
        return text_to_elements(data["text"])
    if isinstance(data, list):
        return data
    raise ValueError("layout file must be a list of elements, {\"elements\": [...]}, or {\"text\": \"...\"}")


def _check_layout(elements: list[dict]) -> list[str]:
    errs = []
    texts = [e for e in elements if e.get("type") == "text"]
    if len(texts) > 8:
        errs.append("more than 8 text elements")
    if sum(1 for e in texts if e.get("capture_events")) > 1:
        errs.append("more than one capture_events element")
    for e in elements:
        n = e.get("container_name", "")
        if len(n) > 16:
            errs.append(f"container_name {n!r} longer than 16")
        if e.get("type") == "text" and len(str(e.get("text", "")).encode()) > 1000:
            errs.append(f"text in {n or '?'} over 1000 bytes")
        x, y, w, h = (int(e.get(k, 0)) for k in ("x", "y", "width", "height"))
        if x + w > 576 or y + h > 288:
            errs.append(f"{n or '?'} runs off the canvas ({x}+{w} x {y}+{h})")
    return errs


def cmd_layout(a) -> int:
    path = Path(a.file)
    last = None
    while True:
        try:
            elements = _load_layout(path)
            errs = _check_layout(elements)
            if errs:
                print("layout problems: " + "; ".join(errs))
            else:
                r = _post("/api/display", {"elements": elements}, a.gateway)
                print(f"{time.strftime('%H:%M:%S')} sent {len(elements)} elements -> {r.get('mode') or r}")
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}")
        if not a.watch:
            return 0
        while True:
            time.sleep(0.5)
            try:
                m = path.stat().st_mtime
            except OSError:
                continue
            if m != last:
                last = m
                break


def cmd_render(a) -> int:
    from g2.render import save
    elements = _load_layout(Path(a.file))
    errs = _check_layout(elements)
    out = Path(a.out) if a.out else PREVIEWS / (Path(a.file).stem + ".png")
    save(elements, out)
    print(f"png: {out}")
    if errs:
        print("layout problems: " + "; ".join(errs))
        return 1
    return 0


# --------------------------------------------------------------- doctor
def cmd_doctor(a) -> int:
    ok = True

    def line(label, good, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok' if good else '!!'}] {label:<18} {detail}")

    print("G2 stack check")
    st = _status(a.gateway)
    line("gateway", st is not None, a.gateway if st else f"not answering at {a.gateway} (python g2/run.py)")
    if st:
        g = st["glasses"]
        line("glasses", bool(g.get("ready")), f"{g.get('phase')}  L {'on' if g['left'].get('connected') else 'off'}  "
             f"R {'on' if g['right'].get('connected') else 'off'}  fw {g.get('firmware_version')}  battery {g.get('battery_level')}%")
        srv = st.get("server", {})
        line("park on exit", bool(srv.get("park_on_exit")), "native dashboard stays visible after a double tap" if srv.get("park_on_exit") else "off: gateway recreates its page at once")
        line("raw events", bool(srv.get("raw_events")), "needed by g2/native")
    from g2.lc3dec import library_path
    line("LC3 decoder", library_path() is not None, str(library_path() or "build it: see g2/README.md"))
    try:
        import faster_whisper  # noqa: F401
        line("speech model", True, "faster-whisper installed")
    except Exception as exc:
        line("speech model", False, f"faster-whisper missing ({exc})")
    try:
        from g2 import apps as registry
        apps = registry.discover()
        line("apps", bool(apps), ", ".join(x.name for x in apps))
    except Exception as exc:
        line("apps", False, str(exc))
    line("launcher request", True, f"{OPEN_REQUEST} ({'pending' if OPEN_REQUEST.exists() else 'idle'})")
    return 0 if ok else 1


def cmd_test(a) -> int:
    import subprocess
    return subprocess.call([sys.executable, "-m", "unittest", "g2.test_launcher", *(["-v"] if a.verbose else [])], cwd=str(ROOT))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--gateway", default=GATEWAY)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new", help="scaffold an app"); p.add_argument("name"); p.add_argument("--title"); p.add_argument("--blurb", default="what it does, in a few words"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_new)
    p = sub.add_parser("list", help="apps in menu order"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("preview", help="run an app against fakes and save PNGs"); p.add_argument("name"); p.add_argument("--gestures", default="", help="comma list: tap,down,up,double,long"); p.add_argument("--say", default="what time is it", help="what the fake microphone hears"); p.add_argument("--answer", default="It is late. Go to sleep.", help="what the fake brain answers"); p.add_argument("--no-png", action="store_true"); p.set_defaults(fn=cmd_preview)
    p = sub.add_parser("push", help="open an app on the glasses now"); p.add_argument("name"); p.set_defaults(fn=cmd_push)
    p = sub.add_parser("layout", help="send a layout JSON file to the glasses"); p.add_argument("file"); p.add_argument("--watch", action="store_true"); p.set_defaults(fn=cmd_layout)
    p = sub.add_parser("render", help="PNG of a layout JSON file"); p.add_argument("file"); p.add_argument("--out"); p.set_defaults(fn=cmd_render)
    p = sub.add_parser("doctor", help="check the stack"); p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("test", help="run the tests"); p.add_argument("-v", dest="verbose", action="store_true"); p.set_defaults(fn=cmd_test)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
