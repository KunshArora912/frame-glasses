"""Apply the undock-wake fix on top of the diagnostics patch.
Usage: apply_wake_fix.py <worktree-root>

What it changes (all in source/application/main.c):
 1. shutdown(): tear down the nrfx GPIOTE driver before arming wake senses, so
    its PORT handler can no longer flip the sense polarity behind our back.
 2. shutdown(): arm CASE_DETECT to wake on a *change* of its current level,
    and arm the IMU interrupt as SENSE_HIGH (INTA is push-pull active-high).
 3. shutdown(): when docked (charger present), keep the PMIC AMUX routed to the
    CHGIN voltage monitor and arm the nRF LPCOMP on AIN3 to wake when that
    voltage falls (undock). Skip the PMIC main-bias low-power mode in that case
    so the AMUX buffer keeps working. Undocked sleeps are unchanged.
 4. hardware_setup(): debounce the "charging" read (re-read after 300 ms) so a
    wake caused by undocking is not misread as "still docked".
 5. hardware_setup(): make sure LPCOMP is off at boot.
"""
import pathlib, sys

root = pathlib.Path(sys.argv[1])

def sub(path, old, new):
    p = root / path
    s = p.read_text(encoding="utf-8")
    assert old in s, f"anchor not found in {path}: {old[:70]!r}"
    p.write_text(s.replace(old, new, 1), encoding="utf-8")

M = "source/application/main.c"

# includes
sub(M, '#include "nrfx_gpiote.h"', '#include "nrfx_gpiote.h"\n#include "hal/nrf_lpcomp.h"')

# helper: arm LPCOMP on AIN3 (PMIC AMUX) to wake on a downward crossing
sub(M, "void shutdown(bool enable_imu_wakeup)\n{", r'''
// Arm the low-power comparator on AIN3 (PMIC AMUX output) so that the charger
// input voltage falling below ~5/16 VDD (~0.56 V at the AMUX, i.e. CHGIN gone)
// wakes the nRF from System OFF via ANADETECT. Returns false if LPCOMP never
// became ready.
static bool arm_undock_wake_lpcomp(void)
{
    nrf_lpcomp_disable(NRF_LPCOMP);
    nrf_lpcomp_config_t cfg = {
        .reference = NRF_LPCOMP_REF_SUPPLY_5_16,
        .detection = NRF_LPCOMP_DETECT_DOWN,
        .hyst = NRF_LPCOMP_HYST_ENABLED,
    };
    nrf_lpcomp_configure(NRF_LPCOMP, &cfg);
    nrf_lpcomp_input_select(NRF_LPCOMP, NRF_LPCOMP_INPUT_3);
    nrf_lpcomp_event_clear(NRF_LPCOMP, NRF_LPCOMP_EVENT_READY);
    nrf_lpcomp_event_clear(NRF_LPCOMP, NRF_LPCOMP_EVENT_DOWN);
    nrf_lpcomp_event_clear(NRF_LPCOMP, NRF_LPCOMP_EVENT_UP);
    nrf_lpcomp_event_clear(NRF_LPCOMP, NRF_LPCOMP_EVENT_CROSS);
    nrf_lpcomp_enable(NRF_LPCOMP);
    nrf_lpcomp_task_trigger(NRF_LPCOMP, NRF_LPCOMP_TASK_START);
    for (int i = 0; i < 100; i++)
    {
        if (nrf_lpcomp_event_check(NRF_LPCOMP, NRF_LPCOMP_EVENT_READY))
        {
            break;
        }
        nrfx_systick_delay_ms(1);
    }
    bool ready = nrf_lpcomp_event_check(NRF_LPCOMP, NRF_LPCOMP_EVENT_READY);
    // Discard any crossing recorded while settling; only a real fall after
    // this point should wake us.
    nrf_lpcomp_event_clear(NRF_LPCOMP, NRF_LPCOMP_EVENT_DOWN);
    nrf_lpcomp_event_clear(NRF_LPCOMP, NRF_LPCOMP_EVENT_CROSS);
    LOG("LPCOMP armed ready=%d above_ref=%lu", ready,
        (unsigned long)nrf_lpcomp_result_get(NRF_LPCOMP));
    return ready;
}

void shutdown(bool enable_imu_wakeup)
{''')

# 1. tear down GPIOTE after the stay_awake early return
sub(M, '''        nrfx_gpiote_trigger_enable(CASE_DETECT_PIN, true);
        return;
    }

    uint8_t display_power_save[1] = {0x92};''', '''        nrfx_gpiote_trigger_enable(CASE_DETECT_PIN, true);
        return;
    }

    // Fully stop the GPIOTE driver. Its PORT-event handler would otherwise run
    // when we arm a sense below on a pin already at the sensed level, and
    // invert the sense polarity before SYSTEMOFF (nrfx next_sense_cond_call_handler).
    nrfx_gpiote_uninit();

    uint8_t display_power_save[1] = {0x92};''')

# 3. PMIC: keep AMUX on CHGIN when docked, skip LPM in that case
sub(M, '''    // Disconnect AMUX
    check_error(i2c_write(PMIC, 0x28, 0x0F, 0x00).fail);

    // Put PMIC main bias into low power mode
    check_error(i2c_write(PMIC, 0x10, 0x20, 0x20).fail);
''', '''    // Docked (charger present)? Then keep the AMUX routed to the CHGIN voltage
    // monitor so the LPCOMP can wake us when the charger goes away. Otherwise
    // disconnect the AMUX as before.
    i2c_response_t dock_status = i2c_read(PMIC, 0x03, 0x0C);
    bool docked_for_sleep = (dock_status.fail == false) && (dock_status.value != 0);
    if (docked_for_sleep)
    {
        check_error(i2c_write(PMIC, 0x28, 0x0F, 0x01).fail);
    }
    else
    {
        check_error(i2c_write(PMIC, 0x28, 0x0F, 0x00).fail);

        // Put PMIC main bias into low power mode (only when the AMUX is idle)
        check_error(i2c_write(PMIC, 0x10, 0x20, 0x20).fail);
    }
''')

# 2. sense polarity by level; IMU SENSE_HIGH; arm LPCOMP when docked
sub(M, '''    for (uint8_t pin = 0; pin < 16; pin++)
    {
        nrf_gpio_cfg_default(NRF_GPIO_PIN_MAP(0, pin));
        nrf_gpio_cfg_default(NRF_GPIO_PIN_MAP(0, pin + 16));
        nrf_gpio_cfg_default(NRF_GPIO_PIN_MAP(1, pin));
    }

    nrf_gpio_cfg_sense_input(CASE_DETECT_PIN,
                             NRF_GPIO_PIN_NOPULL,
                             NRF_GPIO_PIN_SENSE_LOW);

    if (enable_imu_wakeup)
    {
        nrf_gpio_cfg_sense_input(IMU_INTERRUPT_PIN,
                                 NRF_GPIO_PIN_NOPULL,
                                 NRF_GPIO_PIN_SENSE_LOW);
    }
''', '''    // Read the case detect level before the pins are defaulted (input disconnect)
    nrf_gpio_cfg_input(CASE_DETECT_PIN, NRF_GPIO_PIN_NOPULL);
    nrfx_systick_delay_ms(1);
    bool case_detect_level = nrf_gpio_pin_read(CASE_DETECT_PIN);

    for (uint8_t pin = 0; pin < 16; pin++)
    {
        nrf_gpio_cfg_default(NRF_GPIO_PIN_MAP(0, pin));
        nrf_gpio_cfg_default(NRF_GPIO_PIN_MAP(0, pin + 16));
        nrf_gpio_cfg_default(NRF_GPIO_PIN_MAP(1, pin));
    }

    // Wake when the case detect line changes from its current level: docked
    // (high) -> wake on low (undock or pairing button); undocked (low) -> wake
    // on high (dock).
    nrf_gpio_cfg_sense_input(CASE_DETECT_PIN,
                             NRF_GPIO_PIN_NOPULL,
                             case_detect_level ? NRF_GPIO_PIN_SENSE_LOW
                                               : NRF_GPIO_PIN_SENSE_HIGH);

    if (enable_imu_wakeup)
    {
        // INTA is configured push-pull active-high in imu.c
        nrf_gpio_cfg_sense_input(IMU_INTERRUPT_PIN,
                                 NRF_GPIO_PIN_NOPULL,
                                 NRF_GPIO_PIN_SENSE_HIGH);
    }

    if (docked_for_sleep)
    {
        arm_undock_wake_lpcomp();
    }
''')

# 4. debounce the charging read at boot; 5. LPCOMP off at boot
sub(M, '''        // Check if the device is docked by reading STAT_CHG_B
        i2c_response_t charger_status = i2c_read(PMIC, 0x03, 0x0C);
        check_error(charger_status.fail);
        bool charging = charger_status.value;
''', '''        // Check if the device is docked by reading STAT_CHG_B. If it says
        // "charger present", re-check after a short delay: right after an
        // undock the charger input is still decaying/debouncing.
        i2c_response_t charger_status = i2c_read(PMIC, 0x03, 0x0C);
        check_error(charger_status.fail);
        bool charging = charger_status.value;
        if (charging)
        {
            nrfx_systick_delay_ms(300);
            charger_status = i2c_read(PMIC, 0x03, 0x0C);
            check_error(charger_status.fail);
            charging = charger_status.value;
            case_detect_pin = nrf_gpio_pin_read(CASE_DETECT_PIN);
        }
''')
sub(M, '''    // Configure watchdog
    {
        init_watchdog();
    }
''', '''    // Make sure the low power comparator (undock wake source) is off
    {
        nrf_lpcomp_disable(NRF_LPCOMP);
    }

    // Configure watchdog
    {
        init_watchdog();
    }
''')
print("wake fix applied")
