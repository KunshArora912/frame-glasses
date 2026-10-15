-- Device-side Frame app: stream microphone audio continuously AND send
-- photos on request, sharing the one Bluetooth link.
--
-- Paired with ../live_av.py.
--
-- Budget (measured): the link sustains ~25 kB/s. 8 kHz x 16 bit audio is
-- 16 kB/s, leaving ~9 kB/s for images. capture_and_send() blocks for the
-- whole JPEG transfer, during which audio accumulates in the device's ~32 kB
-- buffer -- at 16 kB/s a 0.6s capture buffers ~10 kB, comfortably inside it,
-- so photos do not punch holes in the audio.

local data = require('data.min')
local audio = require('audio.min')
local camera = require('camera.min')

-- must match live_av.py
AUDIO_CTRL_MSG = 0x30
CAPTURE_SETTINGS_MSG = 0x0d

local MAX_SENDS_PER_PASS = 20

local streaming = false
local tick = 0

function app_loop()
    print('Frame app is running')

    while true do
        local rc, err = pcall(function()
            tick = tick + 1

            -- Pump audio FIRST so it keeps priority over everything else.
            if streaming then
                for _ = 1, MAX_SENDS_PER_PASS do
                    local n = audio.read_and_send_audio()
                    if n == nil then streaming = false break end
                    if n == 0 then break end
                end
            end

            local items = data.process_raw_items()

            for _, item in ipairs(items) do
                local flag, raw = item[1], item[2]

                if flag == AUDIO_CTRL_MSG then
                    local cmd = string.byte(raw, 1)
                    if cmd == 1 then
                        local rate = 8000
                        if string.byte(raw, 2) == 1 then rate = 16000 end
                        local depth = string.byte(raw, 3)
                        audio.start { sample_rate = rate, bit_depth = depth }
                        streaming = true
                        print('audio start ' .. rate .. '/' .. depth)
                    else
                        audio.stop()
                        streaming = false
                        print('audio stop')
                    end

                elseif flag == CAPTURE_SETTINGS_MSG then
                    -- Blocks for the whole JPEG transfer; audio buffers meanwhile.
                    camera.capture_and_send(camera.parse_capture_settings(raw))

                    -- Drain whatever audio piled up during the capture.
                    if streaming then
                        for _ = 1, MAX_SENDS_PER_PASS * 4 do
                            local n = audio.read_and_send_audio()
                            if n == nil then streaming = false break end
                            if n == 0 then break end
                        end
                    end
                end
            end

            -- Auto-exposure wants stepping ~every 100ms. Skip it while audio is
            -- streaming on the odd pass to keep the audio pump hot.
            if camera.is_auto_exp and tick % 40 == 0 then
                camera.run_auto_exposure()
            end

            frame.sleep(0.001)
        end)

        if rc == false then
            print('app_loop error: ' .. tostring(err))
            break
        end
    end
end

app_loop()
