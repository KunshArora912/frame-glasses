"""Apply the boot-diagnostics patch to a frame-codebase worktree.
Usage: apply_diag_patch.py <worktree-root>
"""
import pathlib, sys

root = pathlib.Path(sys.argv[1])

def sub(path, old, new, count=1):
    p = root / path
    s = p.read_text(encoding="utf-8")
    assert old in s, f"anchor not found in {path}: {old[:60]!r}"
    s = s.replace(old, new, count)
    p.write_text(s, encoding="utf-8")

# ---------- linker: add a NOLOAD .noinit section after .bss ----------
sub("source/application/section_layout.ld",
    "\t.stack (NOLOAD) : {",
    "    .noinit (NOLOAD) : {\n        . = ALIGN(4);\n        *(.noinit .noinit*)\n        . = ALIGN(4);\n    } > APPLICATION_RAM\n\n\t.stack (NOLOAD) : {")

# ---------- main.c: boot diagnostics ----------
diag_code = r'''
// ---------------------------------------------------------------------------
// Boot diagnostics (wake-failure investigation). Records, for each boot, the
// reset reason and the furthest stage reached, in a RAM log that is kept
// through resets and System OFF (section retention is enabled in shutdown())
// and mirrored into GPREGRET2 (retained in System OFF, cleared by POR/BOR).
// GPREGRET2 layout: bit7=0 (never collides with the bootloader values
// 0xA8/0xB1), bits6:4 = boot sequence & 7, bits3:0 = stage.
// ---------------------------------------------------------------------------
#include "nrf_soc.h"
#include "nrf_sdm.h"

#define DIAG_MAGIC 0x4652444Cu
#define DIAG_ENTRIES 8

typedef struct
{
    uint32_t resetreas;
    uint16_t seq;
    uint8_t stage;
    uint8_t flags; // bit0 charging, bit1 case_detect, bit2 stay_awake
    uint8_t prev_gpregret2;
    uint8_t pad[3];
} diag_entry_t;

typedef struct
{
    uint32_t magic;
    uint16_t seq;
    uint16_t pad;
    diag_entry_t e[DIAG_ENTRIES];
} diag_log_t;

static diag_log_t diag_log __attribute__((section(".noinit")));
static diag_entry_t *diag_cur = NULL;

static void diag_gpregret2_set(uint8_t value)
{
    value &= 0x7F;
    uint8_t sd_enabled = 0;
    sd_softdevice_is_enabled(&sd_enabled);
    if (sd_enabled)
    {
        sd_power_gpregret_clr(1, 0xFF);
        sd_power_gpregret_set(1, value);
    }
    else
    {
        NRF_POWER->GPREGRET2 = value;
    }
}

void diag_stage(uint8_t stage)
{
    if (diag_cur)
    {
        diag_cur->stage = stage;
    }
    diag_gpregret2_set((uint8_t)(((diag_log.seq & 7) << 4) | (stage & 0x0F)));
}

void diag_flags(bool charging, bool case_detect, bool stay)
{
    if (diag_cur)
    {
        diag_cur->flags = (charging ? 1 : 0) | (case_detect ? 2 : 0) | (stay ? 4 : 0);
    }
}

static void diag_boot_begin(void)
{
    uint32_t reas = NRF_POWER->RESETREAS;
    NRF_POWER->RESETREAS = 0xFFFFFFFF;
    uint8_t prev = (uint8_t)NRF_POWER->GPREGRET2;

    if (diag_log.magic != DIAG_MAGIC)
    {
        memset(&diag_log, 0, sizeof(diag_log));
        diag_log.magic = DIAG_MAGIC;
        diag_log.seq = 0x1000; // high bit pattern marks a (re)initialised log
    }
    diag_log.seq++;
    diag_cur = &diag_log.e[diag_log.seq % DIAG_ENTRIES];
    memset(diag_cur, 0, sizeof(*diag_cur));
    diag_cur->resetreas = reas;
    diag_cur->seq = diag_log.seq;
    diag_cur->prev_gpregret2 = prev;
    diag_stage(1);
}

// Enable System OFF retention for the RAM section that holds the log.
static void diag_retain_ram(void)
{
    uint32_t addr = (uint32_t)&diag_log;
    if (addr >= 0x20010000u)
    {
        uint32_t section = (addr - 0x20010000u) / 0x8000u; // RAM8: 6 x 32K
        NRF_POWER->RAM[8].POWERSET = (1u << (16 + section)) | (1u << section);
    }
    else
    {
        uint32_t block = (addr - 0x20000000u) / 0x2000u; // RAM0-7: 2 x 4K
        uint32_t section = ((addr - 0x20000000u) % 0x2000u) / 0x1000u;
        NRF_POWER->RAM[block].POWERSET = (1u << (16 + section)) | (1u << section);
    }
}

int diag_format(char *buf, size_t len)
{
    int n = snprintf(buf, len, "seq=%u", diag_log.seq);
    for (int i = 0; i < DIAG_ENTRIES; i++)
    {
        int idx = (diag_log.seq - i) % DIAG_ENTRIES;
        diag_entry_t *e = &diag_log.e[idx];
        if (e->seq == 0)
        {
            continue;
        }
        n += snprintf(buf + n, len - n,
                      "\n#%u reas=0x%lx stage=%u chg=%u cd=%u stay=%u prevg2=0x%02x",
                      e->seq, (unsigned long)e->resetreas, e->stage,
                      e->flags & 1, (e->flags >> 1) & 1, (e->flags >> 2) & 1,
                      e->prev_gpregret2);
        if (n >= (int)len - 1)
        {
            break;
        }
    }
    return n;
}
'''

sub("source/application/main.c", "bool stay_awake = false;\n", "bool stay_awake = false;\n" + diag_code)
sub("source/application/main.c", '#include "nrfx_gpiote.h"', '#include <string.h>\n#include <stdio.h>\n#include "nrfx_gpiote.h"')

# shutdown(): mark stage and retain RAM before SYSTEM OFF
sub("source/application/main.c",
    "void shutdown(bool enable_imu_wakeup)\n{\n    nrfx_gpiote_trigger_disable(CASE_DETECT_PIN);\n",
    "void shutdown(bool enable_imu_wakeup)\n{\n    nrfx_gpiote_trigger_disable(CASE_DETECT_PIN);\n    diag_stage(stay_awake ? 12 : 11);\n")
sub("source/application/main.c",
    '    // Clear the reset reasons\n    NRF_POWER->RESETREAS = 0xF000F;\n\n    LOG("Going to sleep");\n',
    '    // Clear the reset reasons\n    NRF_POWER->RESETREAS = 0xF000F;\n\n    diag_retain_ram();\n    diag_stage(13);\n\n    LOG("Going to sleep");\n')

# hardware_setup stages
sub("source/application/main.c",
    "        // Connect AMUX to battery voltage\n        check_error(i2c_write(PMIC, 0x28, 0x0F, 0x03).fail);\n    }\n",
    "        // Connect AMUX to battery voltage\n        check_error(i2c_write(PMIC, 0x28, 0x0F, 0x03).fail);\n    }\n    diag_stage(2);\n")
sub("source/application/main.c",
    "        bool charging = charger_status.value;\n\n        if (charging)\n        {",
    "        bool charging = charger_status.value;\n        diag_flags(charging, case_detect_pin, stay_awake);\n        diag_stage(3);\n\n        if (charging)\n        {")
sub("source/application/main.c",
    "            if (case_detect_pin == true)\n            {\n                shutdown(false);\n            }",
    "            if (case_detect_pin == true)\n            {\n                diag_stage(9);\n                shutdown(false);\n            }")
sub("source/application/main.c",
    '                LOG("Factory reset");\n                bluetooth_unpair();\n                stay_awake = true;',
    '                LOG("Factory reset");\n                diag_stage(10);\n                bluetooth_unpair();\n                stay_awake = true;')
sub("source/application/main.c",
    "        set_power_rails(true);\n        nrfx_systick_delay_ms(5);\n",
    "        diag_stage(4);\n        set_power_rails(true);\n        nrfx_systick_delay_ms(5);\n")
sub("source/application/main.c",
    '            if (fpga_chip_id[0] != 0x81)\n            {\n                error_with_message("FPGA not found");\n            }\n        }\n    }\n',
    '            if (fpga_chip_id[0] != 0x81)\n            {\n                error_with_message("FPGA not found");\n            }\n        }\n        diag_stage(5);\n    }\n')
sub("source/application/main.c",
    "    // Configure the camera\n    {\n        // Wake up the camera",
    "    diag_stage(6);\n\n    // Configure the camera\n    {\n        // Wake up the camera")
sub("source/application/main.c",
    'int main(void)\n{\n    LOG("Frame firmware " BUILD_VERSION " (" GIT_COMMIT ")");\n\n    hardware_setup();\n\n    bluetooth_setup();\n',
    'int main(void)\n{\n    diag_boot_begin();\n\n    LOG("Frame firmware " BUILD_VERSION " (" GIT_COMMIT ")");\n\n    hardware_setup();\n    diag_stage(7);\n\n    bluetooth_setup();\n    diag_stage(8);\n')

# main.h prototypes
sub("source/application/main.h",
    "void shutdown(bool enable_imu_wakeup);",
    "void shutdown(bool enable_imu_wakeup);\nint diag_format(char *buf, size_t len);\nvoid diag_stage(uint8_t stage);")

# system.c: frame.boot_diag()
sub("source/application/lua_libraries/system.c",
    "static int lua_fpga_read(lua_State *L)",
    "static int lua_boot_diag(lua_State *L)\n{\n    char buf[512];\n    diag_format(buf, sizeof(buf));\n    lua_pushstring(L, buf);\n    return 1;\n}\n\nstatic int lua_fpga_read(lua_State *L)")
sub("source/application/lua_libraries/system.c",
    '    lua_pushcfunction(L, lua_fpga_read);\n    lua_setfield(L, -2, "fpga_read");\n',
    '    lua_pushcfunction(L, lua_boot_diag);\n    lua_setfield(L, -2, "boot_diag");\n\n    lua_pushcfunction(L, lua_fpga_read);\n    lua_setfield(L, -2, "fpga_read");\n')
print("patched")
