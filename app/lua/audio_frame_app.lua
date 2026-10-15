-- Device-side Frame app: stream microphone PCM to the host on demand.
--
-- Paired with ../live_audio.py. Control message payload is 3 bytes:
--   [1] command   1 = start, 0 = stop
--   [2] rate code 0 = 8000 Hz, 1 = 16000 Hz
--   [3] bit depth 8 or 16
--
-- Audio chunks go back as 0x05 (non-final) / 0x06 (final), which is what
-- RxAudio expects by default -- those flags live inside audio.min.lua.

local data = require('data.min')
local audio = require('audio.min')

-- must match AUDIO_CTRL_MSG in live_audio.py
AUDIO_CTRL_MSG = 0x30

-- Pump at most this many MTU-sized reads per loop pass. The device buffer is
-- only ~32k, so read_and_send_audio() has to be called far more often than
-- once per pass or the buffer overflows and the audio gets gaps.
local MAX_SENDS_PER_PASS = 20

local streaming = false

function app_loop()
    -- start_frame_app(await_print=True) blocks until this print arrives.
    print('Frame app is running')

    while true do
        local rc, err = pcall(function()
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
                end
            end

            if streaming then
                for _ = 1, MAX_SENDS_PER_PASS do
                    local n = audio.read_and_send_audio()
                    if n == nil then
                        -- microphone stopped; end-of-stream already sent
                        streaming = false
                        break
                    end
                    if n == 0 then
                        -- buffer drained for now
                        break
                    end
                end
            end

            -- Keep this tiny: the mic buffer fills in realtime.
            frame.sleep(0.001)
        end)

        if rc == false then
            print('app_loop error: ' .. tostring(err))
            break
        end
    end
end

app_loop()
