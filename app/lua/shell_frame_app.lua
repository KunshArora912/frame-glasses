-- The Frame shell: one device app that hosts every other app.
--
-- Until now each program uploaded its own Lua, which meant ~6 s of upload
-- every time you switched tasks and no way to move between them on the
-- glasses themselves. This app is uploaded ONCE and stays resident; the
-- host switches what it draws.
--
-- The device deliberately knows nothing about what the apps mean. It can
-- draw a menu, draw a page of text, draw a sprite, and report taps. Every
-- decision lives on the host, where it can be tested without hardware.
--
-- Panel facts that shape this file:
--   * the FPGA clears the write buffer on every show(), so each frame is
--     redrawn IN FULL
--   * glyphs are 48 px tall and the fit check (y + 48 <= 400) fails
--     SILENTLY, so nothing may be drawn below y = 352

local data = require('data.min')
local audio = require('audio.min')
local imu = require('imu.min')
local sprite = require('sprite.min')
local code = require('code.min')
local tap = require('tap.min')

-- host -> device
MENU_MSG = 0x60          -- [count][selected][len][label] x count
PAGE_MSG = 0x61          -- [flags][len][line] x up to 7
SPRITE_MSG = 0x20        -- TxSprite, drawn top-left of the text
CLEAR_MSG = 0x43         -- TxCode: blank the panel
TAP_CTRL_MSG = 0x41      -- TxCode: 1 = forward taps
AUDIO_CTRL_MSG = 0x30    -- [cmd, rate_code, bit_depth]
IMU_CTRL_MSG = 0x40      -- TxCode: 1 = stream accelerometer

local ROW_H = 52
local TOP = 6
local MAX_Y = 352
local LEFT = 14

local mode = 'idle'
local menu = { items = {}, selected = 1 }
local page = { lines = {}, flags = 0 }
local image = nil
local taps_on = false
local streaming = false
local stream_imu = false
local tick = 0

local function draw_menu()
    -- The selected row is marked with a caret rather than a filled bar:
    -- inverse video on this panel costs a full-width fill and reads as a
    -- glitch at 20 degrees of FOV.
    local y = TOP
    for i, label in ipairs(menu.items) do
        if y > MAX_Y then break end
        if i == menu.selected then
            frame.display.text('> ' .. label, LEFT, y, { color = 'WHITE' })
        else
            frame.display.text('  ' .. label, LEFT, y, { color = 'GREY' })
        end
        y = y + ROW_H
    end
end

local function draw_page()
    local y = TOP
    -- flags bit 0: the first line is a heading, drawn in the accent colour
    local heading = (page.flags & 0x01) ~= 0
    -- flags bit 1: centre-ish emphasis, used by the teleprompter
    local emphasis = (page.flags & 0x02) ~= 0

    for i, line in ipairs(page.lines) do
        if y > MAX_Y then break end
        if line ~= '' then
            local colour = 'WHITE'
            if heading and i == 1 then
                colour = 'SKYBLUE'
            elseif emphasis and i ~= 2 then
                colour = 'GREY'
            end
            frame.display.text(line, LEFT, y, { color = colour })
        end
        y = y + ROW_H
    end
end

local function redraw()
    if image ~= nil then
        frame.display.bitmap(1, 1, image.width, image.num_colors, 0,
                             image.pixel_data)
    end
    if mode == 'menu' then
        draw_menu()
    elseif mode == 'page' then
        draw_page()
    end
    frame.display.show()
end

-- Length-prefixed strings throughout, so a label or a line may contain any
-- character without escaping.
local function read_strings(raw, start, count)
    local out = {}
    local pos = start
    for i = 1, count do
        local n = string.byte(raw, pos) or 0
        out[i] = (n > 0) and string.sub(raw, pos + 1, pos + n) or ''
        pos = pos + 1 + n
    end
    return out
end

function app_loop()
    frame.display.text('frame shell ready', LEFT, 150)
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
            -- Pump audio FIRST, before anything else gets a turn. The
            -- microphone FIFO holds only 2.048 s and never recovers once
            -- it laps, so a redraw must never delay a read.
            if streaming then
                for _ = 1, 20 do
                    local n = audio.read_and_send_audio()
                    if n == nil then streaming = false break end
                    if n == 0 then break end
                end
            end

            -- Roughly 10 Hz of accelerometer while an app wants it. The
            -- loop runs ~1 ms when idle and slower while audio pumps, so
            -- this is approximate; the host timestamps each sample on
            -- arrival and never relies on the rate.
            tick = tick + 1
            if stream_imu and tick % 100 == 0 then
                pcall(imu.send_imu_data)
            end

            local items = data.process_raw_items()

            for _, item in ipairs(items) do
                local flag, raw = item[1], item[2]

                if flag == MENU_MSG then
                    local count = string.byte(raw, 1) or 0
                    menu.selected = string.byte(raw, 2) or 1
                    menu.items = read_strings(raw, 3, count)
                    image = nil
                    mode = 'menu'
                    redraw()

                elseif flag == PAGE_MSG then
                    page.flags = string.byte(raw, 1) or 0
                    local count = string.byte(raw, 2) or 0
                    page.lines = read_strings(raw, 3, count)
                    mode = 'page'
                    redraw()

                elseif flag == SPRITE_MSG then
                    -- The host always follows a sprite with the page or
                    -- menu that belongs beside it, so do not repaint here.
                    local spr = sprite.parse_sprite(raw)
                    sprite.set_palette(spr.num_colors, spr.palette_data)
                    image = spr

                elseif flag == CLEAR_MSG then
                    image = nil
                    menu.items = {}
                    page.lines = {}
                    mode = 'idle'
                    frame.display.text(' ', 1, 1)
                    frame.display.show()

                elseif flag == TAP_CTRL_MSG then
                    taps_on = (code.parse_code(raw).value == 1)

                elseif flag == IMU_CTRL_MSG then
                    stream_imu = (code.parse_code(raw).value == 1)

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
