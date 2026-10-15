-- Device-side Frame app: capture photos on request and stream them to the host.
--
-- Paired with ../live_camera.py. CAPTURE_SETTINGS_MSG must match the host.
--
-- Image chunks go back as 0x07 (non-final) / 0x08 (final), which is what
-- RxPhoto expects by default -- those flags live inside camera.min.lua.

local data = require('data.min')
local camera = require('camera.min')

-- must match CAPTURE_SETTINGS_MSG in live_camera.py
CAPTURE_SETTINGS_MSG = 0x0d

-- Loop sleep. Kept short because per-frame latency, not bandwidth, limits
-- frame rate at small resolutions (2 kB frames took 0.64s each at 0.1s sleep).
local LOOP_SLEEP = 0.025

-- Auto-exposure wants stepping about every 100ms, so only run it every Nth
-- iteration rather than on every pass.
local AUTOEXP_EVERY = 4

function app_loop()
    -- start_frame_app(await_print=True) blocks until this print arrives.
    print('Frame app is running')

    local tick = 0

    while true do
        local rc, err = pcall(function()
            tick = tick + 1

            local items = data.process_raw_items()

            for _, item in ipairs(items) do
                local flag, raw = item[1], item[2]

                if flag == CAPTURE_SETTINGS_MSG then
                    -- blocks until the whole JPEG has been pushed to the host
                    camera.capture_and_send(camera.parse_capture_settings(raw))
                end
            end

            -- The auto-exposure algorithm is iterative: it converges over many
            -- calls. Capturing before it settles yields near-black frames.
            if camera.is_auto_exp and tick % AUTOEXP_EVERY == 0 then
                camera.run_auto_exposure()
            end

            frame.sleep(LOOP_SLEEP)
        end)

        if rc == false then
            print('app_loop error: ' .. tostring(err))
            break
        end
    end
end

app_loop()
