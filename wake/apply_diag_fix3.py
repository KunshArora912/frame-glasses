"""fix3: make the boot-log formatter bounds-safe and add a raw dump.
Usage: apply_diag_fix3.py <worktree-root>

Why: after a System OFF sleep on the clean fix2 build, frame.boot_diag()
started returning an empty string. The old formatter computed `len - n` after
n could already exceed len, so a log with implausible entries overflowed the
caller's stack buffer. Entries are now sanity-checked, the length math is
clamped, each entry records the RAM[8] power/retention register seen at boot,
and frame.boot_diag_raw() returns the retained struct as hex.
"""
import pathlib, sys

root = pathlib.Path(sys.argv[1])

def sub(path, old, new):
    p = root / path
    s = p.read_text(encoding="utf-8")
    assert old in s, f"anchor not found in {path}: {old[:70]!r}"
    p.write_text(s.replace(old, new, 1), encoding="utf-8")

M = "source/application/main.c"

# entry: use the pad bytes for the RAM8 power register and a magic-ok flag
sub(M, "    uint8_t prev_gpregret2;\n    uint8_t pad[3];\n} diag_entry_t;",
       "    uint8_t prev_gpregret2;\n    uint8_t ram8_hi; // NRF_POWER->RAM[8].POWER bits 23:16 (retention) at boot\n    uint8_t ram8_lo; // bits 7:0 (power)\n    uint8_t magic_ok; // 1 if the retained log survived into this boot\n} diag_entry_t;")

sub(M, "static void diag_boot_begin(void)\n{\n    uint32_t reas = NRF_POWER->RESETREAS;",
       "static void diag_boot_begin(void)\n{\n    uint32_t ram8 = NRF_POWER->RAM[8].POWER;\n    bool magic_ok = (diag_log.magic == DIAG_MAGIC);\n    uint32_t reas = NRF_POWER->RESETREAS;")
sub(M, "    diag_cur->prev_gpregret2 = prev;\n    diag_stage(1);\n}",
       "    diag_cur->prev_gpregret2 = prev;\n    diag_cur->ram8_hi = (uint8_t)(ram8 >> 16);\n    diag_cur->ram8_lo = (uint8_t)ram8;\n    diag_cur->magic_ok = magic_ok ? 1 : 0;\n    diag_stage(1);\n}")

old_fmt_start = "int diag_format(char *buf, size_t len)\n{"
p = root / M
s = p.read_text(encoding="utf-8")
a = s.index(old_fmt_start)
b = s.index("\n}\n", a) + 3
new_fmt = r'''int diag_format(char *buf, size_t len)
{
    if (len == 0)
    {
        return 0;
    }
    size_t n = 0;
    int w = snprintf(buf, len, "seq=%u", diag_log.seq);
    if (w < 0)
    {
        buf[0] = 0;
        return 0;
    }
    n = (size_t)w < len ? (size_t)w : len - 1;

    for (int i = 0; i < DIAG_ENTRIES && n < len - 1; i++)
    {
        int idx = (diag_log.seq - i) % DIAG_ENTRIES;
        diag_entry_t *e = &diag_log.e[idx];
        if (e->seq == 0)
        {
            continue;
        }
        if (e->stage > 15 || (uint16_t)(diag_log.seq - e->seq) >= DIAG_ENTRIES)
        {
            w = snprintf(buf + n, len - n, "\n#? corrupt entry");
        }
        else
        {
            w = snprintf(buf + n, len - n,
                         "\n#%u reas=0x%lx stage=%u chg=%u cd=%u stay=%u prevg2=0x%02x ram8=%02x%02x%s",
                         e->seq, (unsigned long)e->resetreas, e->stage,
                         e->flags & 1, (e->flags >> 1) & 1, (e->flags >> 2) & 1,
                         e->prev_gpregret2, e->ram8_hi, e->ram8_lo,
                         e->magic_ok ? "" : " (log reinit)");
        }
        if (w < 0)
        {
            break;
        }
        n += (size_t)w;
        if (n >= len)
        {
            n = len - 1;
            break;
        }
    }
    buf[n] = 0;
    return (int)n;
}

int diag_raw(char *buf, size_t len)
{
    const uint8_t *p = (const uint8_t *)&diag_log;
    size_t n = 0;
    for (size_t i = 0; i < sizeof(diag_log) && n + 3 < len; i++)
    {
        n += (size_t)snprintf(buf + n, len - n, "%02x", p[i]);
    }
    buf[n] = 0;
    return (int)n;
}
'''
s = s[:a] + new_fmt + s[b:]
p.write_text(s, encoding="utf-8")

sub("source/application/main.h", "int diag_format(char *buf, size_t len);",
    "int diag_format(char *buf, size_t len);\nint diag_raw(char *buf, size_t len);")

S = "source/application/lua_libraries/system.c"
sub(S, "static int lua_boot_diag(lua_State *L)\n{\n    char buf[512];\n    diag_format(buf, sizeof(buf));\n    lua_pushstring(L, buf);\n    return 1;\n}",
       "static int lua_boot_diag(lua_State *L)\n{\n    char buf[800];\n    diag_format(buf, sizeof(buf));\n    lua_pushstring(L, buf);\n    return 1;\n}\n\nstatic int lua_boot_diag_raw(lua_State *L)\n{\n    char buf[256];\n    diag_raw(buf, sizeof(buf));\n    lua_pushstring(L, buf);\n    return 1;\n}")
sub(S, '    lua_pushcfunction(L, lua_boot_diag);\n    lua_setfield(L, -2, "boot_diag");',
       '    lua_pushcfunction(L, lua_boot_diag);\n    lua_setfield(L, -2, "boot_diag");\n\n    lua_pushcfunction(L, lua_boot_diag_raw);\n    lua_setfield(L, -2, "boot_diag_raw");')
print("diag fix3 applied")
