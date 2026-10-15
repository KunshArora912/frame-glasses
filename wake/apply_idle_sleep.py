"""Add idle sleep on top of the wake fix.
Usage: apply_idle_sleep.py <worktree-root>

Behaviour: if the glasses are off the cradle, have no Bluetooth connection and
have not been tapped for `idle_sleep_seconds` (default 300, 0 disables), they
go to sleep with tap-wake armed. Docked glasses never idle-sleep. Lua API:
frame.idle_sleep([seconds]) gets/sets the timeout.
"""
import pathlib, sys

root = pathlib.Path(sys.argv[1])

def sub(path, old, new):
    p = root / path
    s = p.read_text(encoding="utf-8")
    assert old in s, f"anchor not found in {path}: {old[:70]!r}"
    p.write_text(s.replace(old, new, 1), encoding="utf-8")

# --- time.c: monotonic uptime next to the settable UTC clock ---
T = "source/application/lua_libraries/time.c"
sub(T, "static uint64_t utc_time_ms = 0;", "static uint64_t utc_time_ms = 0;\nstatic volatile uint64_t uptime_ms = 0;\n\nuint64_t time_uptime_ms(void)\n{\n    return uptime_ms;\n}")
sub(T, "    utc_time_ms++;", "    utc_time_ms++;\n    uptime_ms++;")

# --- main.h: prototypes and the tunable ---
H = "source/application/main.h"
sub(H, "void diag_stage(uint8_t stage);", "void diag_stage(uint8_t stage);\nuint64_t time_uptime_ms(void);\nvoid idle_activity(void);\nvoid idle_sleep_poll(void);\nextern uint32_t idle_sleep_seconds;")

# --- main.c: idle logic ---
M = "source/application/main.c"
sub(M, "void shutdown(bool enable_imu_wakeup)\n{", r'''
// ---------------------------------------------------------------------------
// Idle sleep: off the cradle, no BLE connection and no tap for
// idle_sleep_seconds -> sleep with tap-wake armed. 0 disables.
// ---------------------------------------------------------------------------
void shutdown(bool enable_imu_wakeup);
uint64_t time_uptime_ms(void);

uint32_t idle_sleep_seconds = 300;
static volatile uint64_t idle_last_activity_ms = 0;
static bool idle_check_in_progress = false;

void idle_activity(void)
{
    idle_last_activity_ms = time_uptime_ms();
}

void idle_sleep_poll(void)
{
    if (idle_sleep_seconds == 0 || idle_check_in_progress)
    {
        return;
    }

    uint64_t now = time_uptime_ms();

    if (bluetooth_is_connected())
    {
        idle_last_activity_ms = now;
        return;
    }

    if (now - idle_last_activity_ms < (uint64_t)idle_sleep_seconds * 1000u)
    {
        return;
    }

    idle_check_in_progress = true;

    // Never idle-sleep on the cradle: that is the "stay on while charging" case
    i2c_response_t charger_status = i2c_read(PMIC, 0x03, 0x0C);
    if (charger_status.fail == false && charger_status.value != 0)
    {
        idle_last_activity_ms = now;
        idle_check_in_progress = false;
        return;
    }

    LOG("Idle for %lu s, sleeping", (unsigned long)idle_sleep_seconds);

    // Idle sleep applies regardless of the docked stay-awake preference
    stay_awake = false;
    shutdown(true);

    // Only reached if shutdown() returned
    idle_check_in_progress = false;
}

void shutdown(bool enable_imu_wakeup)
{''')

# main loop: poll alongside the watchdog reload
sub(M, "        reload_watchdog(NULL, NULL);\n\n        run_lua(bluetooth_is_paired());", "        reload_watchdog(NULL, NULL);\n        idle_sleep_poll();\n\n        run_lua(bluetooth_is_paired());")

# --- watchdog.c: the Lua hook is the frequent thread-context poll point ---
W = "source/application/watchdog.c"
sub(W, "void reload_watchdog(lua_State *L, lua_Debug *ar)\n{\n    nrfx_wdt_channel_feed(&watchdog, NRF_WDT_RR0);\n}",
       "void reload_watchdog(lua_State *L, lua_Debug *ar)\n{\n    nrfx_wdt_channel_feed(&watchdog, NRF_WDT_RR0);\n    if (L != NULL)\n    {\n        idle_sleep_poll();\n    }\n}")
ws = (root / W).read_text(encoding="utf-8")
if '#include "main.h"' not in ws:
    sub(W, '#include "nrfx_wdt.h"', '#include "nrfx_wdt.h"\n#include "main.h"')

# --- imu.c: a tap is activity ---
I = "source/application/lua_libraries/imu.c"
sub(I, "void imu_tap_interrupt_handler(nrfx_gpiote_pin_t unused_gptiote_pin,\n                               nrfx_gpiote_trigger_t unused_gptiote_trigger,\n                               void *unused_gptiote_context_pointer)\n{",
       "void imu_tap_interrupt_handler(nrfx_gpiote_pin_t unused_gptiote_pin,\n                               nrfx_gpiote_trigger_t unused_gptiote_trigger,\n                               void *unused_gptiote_context_pointer)\n{\n    idle_activity();")
is_ = (root / I).read_text(encoding="utf-8")
if '#include "main.h"' not in is_:
    sub(I, '#include "nrfx_gpiote.h"', '#include "nrfx_gpiote.h"\n#include "main.h"')

# --- system.c: frame.idle_sleep([seconds]) ---
S = "source/application/lua_libraries/system.c"
sub(S, "static int lua_boot_diag(lua_State *L)", '''static int lua_idle_sleep(lua_State *L)
{
    if (lua_gettop(L) > 1)
    {
        return luaL_error(L, "expected 0 or 1 arguments");
    }
    if (lua_gettop(L) == 1)
    {
        lua_Integer seconds = luaL_checkinteger(L, 1);
        if (seconds < 0)
        {
            return luaL_error(L, "seconds must be >= 0");
        }
        idle_sleep_seconds = (uint32_t)seconds;
        idle_activity();
        return 0;
    }
    lua_pushinteger(L, idle_sleep_seconds);
    return 1;
}

static int lua_boot_diag(lua_State *L)''')
sub(S, '    lua_pushcfunction(L, lua_boot_diag);', '    lua_pushcfunction(L, lua_idle_sleep);\n    lua_setfield(L, -2, "idle_sleep");\n\n    lua_pushcfunction(L, lua_boot_diag);')
print("idle sleep applied")
