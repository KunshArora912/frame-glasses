"""Run device/main.lua against a fake `frame` table and check its behaviour.

    .venv/Scripts/python.exe device/test_main_lua.py

Simulated clock: frame.sleep(s) advances time by s. Taps are injected by
calling the registered tap callback at chosen times.
"""
import sys
from pathlib import Path
from lupa import LuaRuntime

SRC = (Path(__file__).parent / "main.lua").read_text(encoding="utf-8")


class Stop(Exception):
    pass


def run(charging, taps_at, horizon, battery=87.0, connected=False):
    L = LuaRuntime(unpack_returned_tuples=True, encoding="latin-1")
    state = {"t": 0.0, "draws": [], "texts": [], "prints": [], "slept": False,
             "stay": True, "tap_cb": None, "pending": sorted(taps_at)}
    g = L.globals()

    def sleep(s=None):
        if s is None:
            state["slept"] = True
            raise Stop()
        # fire any taps that fall inside this step
        end = state["t"] + s
        while state["pending"] and state["pending"][0] <= end:
            state["t"] = state["pending"].pop(0)
            state["tap_cb"]()
        state["t"] = end
        if end > horizon:
            raise Stop()

    frame = L.table_from({
        "sleep": sleep,
        "stay_awake": lambda v=None: state.__setitem__("stay", v),
        "battery_level": lambda: battery,
        "amux": lambda sel: 0.79 if charging else 0.12,
        "time": L.table_from({"utc": lambda: state["t"]}),
        "bluetooth": L.table_from({"is_connected": lambda: connected}),
        "imu": L.table_from({"tap_callback": lambda f: state.__setitem__("tap_cb", f)}),
        "display": L.table_from({
            "text": lambda s, x, y, opt=None: state["texts"].append((state["t"], s, x, y)),
            "bitmap": lambda x, y, w, c, off, data: state["texts"].append((state["t"], f"<bar {w}x{len(data)*8//w} off={off}>", x, y)),
            "show": lambda: state["draws"].append((state["t"], tuple(t[1] for t in state["texts"] if t[0] == state["t"]))),
        }),
    })
    g.frame = frame
    g.print = lambda *a: state["prints"].append(" ".join(str(x) for x in a))
    try:
        L.execute(SRC)
    except Exception as e:
        if not isinstance(e, Stop) and "Stop" not in str(type(e)) and "Stop" not in str(e):
            raise
    return state


fails = 0
def check(name, cond, info=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  -- {info}"))
    if not cond:
        fails += 1

# 1. wake on charger: status screen drawn immediately with % and Charging
s = run(charging=True, taps_at=[], horizon=5)
first = s["draws"][0][1] if s["draws"] else ()
check("banner printed", any("main.lua" in p for p in s["prints"]), s["prints"])
check("status drawn at once", bool(s["draws"]) and s["draws"][0][0] < 0.2, s["draws"][:1])
check("shows percent", any("87%" in x for x in first), first)
check("shows Charging", any("Charging" in x for x in first), first)
check("shows bar", any("<bar" in x for x in first), first)
check("never sleeps on charger", not s["slept"])

# 2. wake taps during boot are ignored (3 taps at 0.2 s)
s = run(charging=False, taps_at=[0.2, 0.3, 0.4], horizon=3)
check("boot taps ignored", all("Assistant" not in x for _, d in s["draws"] for x in d))

# 3. on battery: 1 tap -> menu, 1 tap -> down, 2 taps -> launch prints
s = run(charging=False, taps_at=[3.0, 5.0, 7.0, 7.2], horizon=9)
menu = [d for _, d in s["draws"] if any("Assistant" in x for x in d)]
check("menu shown after 1 tap", bool(menu), s["draws"][-3:])
check("cursor moved to Shell", any("> Shell" in x for d in menu for x in d), menu[-1:] if menu else None)
check("launch printed", "launch:shell" in s["prints"], s["prints"])
check("starting screen", any("Starting Shell" in x for _, d in s["draws"] for x in d))
check("says needs laptop when unlinked", any("needs the laptop" in x for _, d in s["draws"] for x in d))

# 4. on battery: blank after 20 s idle, sleep after 300 s
s = run(charging=False, taps_at=[], horizon=400)
blank_draws = [t for t, d in s["draws"] if d == ()]
check("blanked after ~20 s", blank_draws and 20 <= blank_draws[0] <= 22, blank_draws[:2])
check("slept after 300 s", s["slept"] and s["stay"] is False and 300 <= s["t"] <= 302, (s["slept"], s["stay"], s["t"]))

# 5. on battery, a tap after blanking re-shows
s = run(charging=False, taps_at=[30.0], horizon=33)
after = [d for t, d in s["draws"] if t > 30.5]
check("tap re-shows after blank", any(d for d in after), after[:2])

# 6. on charger the picture jitters
s = run(charging=True, taps_at=[], horizon=40)
xs = {t[2] for t in s["texts"] if t[1] == "87%"}
check("jitter moves text on charger", len(xs) >= 2, xs)

# 7. low battery bar colour (RED index 3 -> offset 2) and no amux (stock firmware)
s = run(charging=False, taps_at=[], horizon=1, battery=15.0)
check("red bar when low", any("off=2" in x for _, d in s["draws"] for x in d), s["draws"][:1])

print("\nFAILS:", fails)
sys.exit(1 if fails else 0)
