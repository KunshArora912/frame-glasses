"""fix4: the tap callback reference must not outlive the Lua VM.
Usage: apply_imu_fix4.py <worktree-root>

Bug (upstream too): imu.c keeps the registry reference of the Lua tap callback
in a C static. A VM reset (0x04, or main loop re-entry) creates a fresh
registry, but the static keeps the old integer. The next tap runs the hook,
lua_rawgeti() yields nil, pcall fails and luaL_error() kills whatever Lua is
running -- a freshly started main.lua dies with
"attempt to call a nil value (hook '?')" and the device falls back to the
pairing screen. Fix: forget the reference when the IMU library is opened for a
new VM, and only call it if it is actually a function.
"""
import pathlib, sys

root = pathlib.Path(sys.argv[1])

def sub(path, old, new):
    p = root / path
    s = p.read_text(encoding="utf-8")
    assert old in s, f"anchor not found in {path}: {old[:70]!r}"
    p.write_text(s.replace(old, new, 1), encoding="utf-8")

I = "source/application/lua_libraries/imu.c"
sub(I, """        lua_rawgeti(L, LUA_REGISTRYINDEX, lua_imu_callback_function);

        if (lua_pcall(L, 0, 0, 0) != LUA_OK)
        {
            luaL_error(L, "%s", lua_tostring(L, -1));
        }""",
"""        lua_rawgeti(L, LUA_REGISTRYINDEX, lua_imu_callback_function);

        if (!lua_isfunction(L, -1))
        {
            // Stale reference from a previous Lua VM
            lua_pop(L, 1);
            lua_imu_callback_function = 0;
            return;
        }

        if (lua_pcall(L, 0, 0, 0) != LUA_OK)
        {
            luaL_error(L, "%s", lua_tostring(L, -1));
        }""")
sub(I, """void lua_open_imu_library(lua_State *L)
{
""", """void lua_open_imu_library(lua_State *L)
{
    // A new VM has a new registry: any callback registered in the old one is gone
    lua_imu_callback_function = 0;

""")
print("imu fix4 applied")
