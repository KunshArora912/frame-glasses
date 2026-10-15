-- Device-side app for showcase.py: handles sprites, sprite movement,
-- rasterised text blocks, IMU streaming, tap events and camera capture.
--
-- Message codes MUST match showcase.py.

local data = require('data.min')
local sprite = require('sprite.min')
local sprite_coords = require('sprite_coords.min')
local text_sprite_block = require('text_sprite_block.min')
local imu = require('imu.min')
local tap = require('tap.min')
local camera = require('camera.min')
local code = require('code.min')

-- host -> device
SPRITE_MSG = 0x20
COORDS_MSG = 0x21
TEXT_BLOCK_MSG = 0x23
IMU_CTRL_MSG = 0x40
TAP_CTRL_MSG = 0x41
CAPTURE_MSG = 0x0d
AUTOEXP_SET_MSG = 0x0e
MANUALEXP_SET_MSG = 0x0f
EXP_CTRL_MSG = 0x42

-- the most recently received sprite, so COORDS can redraw it elsewhere
local held = nil
local stream_imu = false
local taps_on = false
local stream_exp = false

local function draw_sprite(spr, x, y)
    sprite.set_palette(spr.num_colors, spr.palette_data)
    frame.display.bitmap(x, y, spr.width, spr.num_colors, 0, spr.pixel_data)
    frame.display.show()
end

function app_loop()
    frame.display.text('showcase ready', 1, 1)
    frame.display.show()
    print('Frame app is running')

    -- Taps are delivered by interrupt; forward them only while subscribed.
    frame.imu.tap_callback(function()
        if taps_on then
            pcall(tap.send_tap, 'single')
        end
    end)

    local tick = 0

    while true do
        local rc, err = pcall(function()
            tick = tick + 1
            local items = data.process_raw_items()

            for _, item in ipairs(items) do
                local flag, raw = item[1], item[2]

                if flag == SPRITE_MSG then
                    held = sprite.parse_sprite(raw)
                    draw_sprite(held, 1, 1)
                    print('sprite ' .. held.width .. 'x' .. held.height ..
                          ' ' .. held.num_colors .. 'col')

                elseif flag == COORDS_MSG then
                    local c = sprite_coords.parse_sprite_coords(raw)
                    if held ~= nil then
                        draw_sprite(held, c.x, c.y)
                    end

                elseif flag == TEXT_BLOCK_MSG then
                    local tsb = text_sprite_block.parse_text_sprite_block(raw)
                    if tsb ~= nil and tsb.first_sprite_index ~= nil then
                        print('text block ' .. tostring(tsb.sprite_count))
                    end

                elseif flag == IMU_CTRL_MSG then
                    stream_imu = (code.parse_code(raw).value == 1)
                    print('imu ' .. tostring(stream_imu))

                elseif flag == TAP_CTRL_MSG then
                    taps_on = (code.parse_code(raw).value == 1)
                    print('taps ' .. tostring(taps_on))

                elseif flag == AUTOEXP_SET_MSG then
                    camera.set_auto_exp_settings(camera.parse_auto_exp_settings(raw))
                    print('autoexp settings applied')

                elseif flag == MANUALEXP_SET_MSG then
                    camera.set_manual_exp_settings(camera.parse_manual_exp_settings(raw))
                    print('manual exp applied')

                elseif flag == EXP_CTRL_MSG then
                    stream_exp = (code.parse_code(raw).value == 1)
                    print('exp ' .. tostring(stream_exp))

                elseif flag == CAPTURE_MSG then
                    camera.capture_and_send(camera.parse_capture_settings(raw))
                end
            end

            -- ~10 Hz IMU updates while subscribed
            if stream_imu and tick % 5 == 0 then
                imu.send_imu_data()
            end

            -- Auto-exposure must be stepped about every 100ms to converge;
            -- report the result and the FPGA metering registers as it goes.
            if stream_exp and tick % 5 == 0 then
                local ok, res = pcall(camera.run_auto_exposure)
                if ok and res ~= nil then
                    pcall(camera.send_autoexp_result, res)
                end
                pcall(camera.send_metering_data)
            end

            frame.sleep(0.02)
        end)

        if rc == false then
            print('app_loop error: ' .. tostring(err))
            break
        end
    end
end

app_loop()
