-- Device-side app for the Frame AI assistant.
--
-- Does four things: renders accumulated text lines, forwards taps, streams
-- microphone audio on command, and captures a photo on command.
--
-- Every message type here is already proven elsewhere in this project; this
-- app only combines them. Codes MUST match assistant.py.

local data = require('data.min')
local plain_text = require('plain_text.min')
local audio = require('audio.min')
local camera = require('camera.min')
local code = require('code.min')
local tap = require('tap.min')
local sprite = require('sprite.min')

-- host -> device
TEXT_MSG = 0x0a          -- TxPlainText: one line, accumulated
CLEAR_MSG = 0x43         -- TxCode: wipe accumulated lines
AUDIO_CTRL_MSG = 0x30    -- [cmd, rate_code, bit_depth]
TAP_CTRL_MSG = 0x41      -- TxCode: 1 = forward taps
CAPTURE_MSG = 0x0d       -- TxCaptureSettings
SPRITE_MSG = 0x20        -- TxSprite: show an image

-- Glyphs are 48px tall and the fit check (y + 48 <= 400) fails SILENTLY,
-- so anything placed below this just disappears.
local MAX_Y = 352

local lines = {}
local streaming = false
local taps_on = false

-- The FPGA clears the write buffer on every show(), so a frame must be
-- redrawn in full -- drawing only the newest line would wipe the rest.
local function redraw()
    for _, ln in ipairs(lines) do
        frame.display.text(ln.string, ln.x, ln.y,
                           { color = ln.color, spacing = ln.spacing })
    end
    frame.display.show()
end

function app_loop()
    lines = {}
    frame.display.text('ready - tap to ask', 14, 150)
    frame.display.show()

    -- start_frame_app(await_print=True) blocks until this print arrives
    print('Frame app is running')

    frame.imu.tap_callback(function()
        if taps_on then
            pcall(tap.send_tap, 'single')
        end
    end)

    while true do
        local rc, err = pcall(function()
            -- Pump audio FIRST so it keeps priority: the mic FIFO holds only
            -- 2.048 s and never recovers once it laps.
            if streaming then
                for _ = 1, 20 do
                    local n = audio.read_and_send_audio()
                    if n == nil then streaming = false break end
                    if n == 0 then break end
                end
            end

            local items = data.process_raw_items()

            for _, item in ipairs(items) do
                local flag, raw = item[1], item[2]

                if flag == TEXT_MSG then
                    local msg = plain_text.parse_plain_text(raw)
                    if msg.y > MAX_Y then msg.y = MAX_Y end
                    lines[#lines + 1] = msg
                    redraw()

                elseif flag == CLEAR_MSG then
                    lines = {}
                    frame.display.text(' ', 1, 1)
                    frame.display.show()

                elseif flag == AUDIO_CTRL_MSG then
                    local cmd = string.byte(raw, 1)
                    if cmd == 1 then
                        local rate = 8000
                        if string.byte(raw, 2) == 1 then rate = 16000 end
                        audio.start { sample_rate = rate,
                                      bit_depth = string.byte(raw, 3) }
                        streaming = true
                    else
                        audio.stop()
                        streaming = false
                    end

                elseif flag == TAP_CTRL_MSG then
                    taps_on = (code.parse_code(raw).value == 1)

                elseif flag == SPRITE_MSG then
                    local spr = sprite.parse_sprite(raw)
                    sprite.set_palette(spr.num_colors, spr.palette_data)
                    frame.display.bitmap(1, 1, spr.width, spr.num_colors, 0,
                                         spr.pixel_data)
                    frame.display.show()
                    lines = {}          -- the image owns the screen now

                elseif flag == CAPTURE_MSG then
                    camera.capture_and_send(camera.parse_capture_settings(raw))
                    -- drain whatever audio piled up during the capture
                    if streaming then
                        for _ = 1, 40 do
                            local n = audio.read_and_send_audio()
                            if n == nil then streaming = false break end
                            if n == 0 then break end
                        end
                    end
                end
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
