-- Device-side navigation HUD.
--
-- The host does every piece of thinking -- routing, snapping, deciding what
-- the next turn is. This app only draws, which keeps it small enough to be
-- reliable and means the hard logic stays unit-testable off-device.
--
-- Two facts about the panel shape this file:
--
--   * the FPGA clears the write buffer on every show(), so a frame has to
--     be redrawn IN FULL every time -- including the arrow. The sprite is
--     therefore kept resident and re-blitted on each redraw, not drawn once.
--   * glyphs are 48 px tall and the fit check (y + 48 <= 400) fails
--     SILENTLY, so anything placed below y=352 just disappears.
--
-- The arrow is sent only when the icon actually changes (a turn or two per
-- minute), while the text updates about once a second. That split is why
-- the HUD costs almost nothing on the link.

local data = require('data.min')
local sprite = require('sprite.min')
local code = require('code.min')
local tap = require('tap.min')

-- host -> device
NAV_MSG = 0x50           -- packed HUD fields, drawn as one frame
SPRITE_MSG = 0x20        -- TxSprite: the turn arrow
CLEAR_MSG = 0x43         -- TxCode: wipe the screen
TAP_CTRL_MSG = 0x41      -- TxCode: 1 = forward taps

-- Glyphs are 48 px tall and the fit check fails silently, so the lowest
-- text row below sits at exactly y=352, the last legal position.
local ARROW_X, ARROW_Y = 1, 36
local TEXT_X = 150            -- clear of the 128 px arrow
local FOOT_X = 14

local arrow = nil             -- resident sprite, re-blitted every redraw
local hud = nil
local taps_on = false

-- Field layout, chosen so the two things you read at a glance -- how far,
-- and onto what -- sit at eye level beside the arrow.
local function redraw()
    if arrow ~= nil then
        frame.display.bitmap(ARROW_X, ARROW_Y, arrow.width,
                             arrow.num_colors, 0, arrow.pixel_data)
    end

    if hud ~= nil then
        local accent = 'WHITE'
        if hud.off_route then
            accent = 'ORANGE'
        elseif hud.arrived then
            accent = 'GREEN'
        elseif hud.imminent then
            accent = 'YELLOW'
        end

        frame.display.text(hud.distance, TEXT_X, 44, { color = accent })
        frame.display.text(hud.street, TEXT_X, 100, { color = 'WHITE' })

        if hud.next_line ~= '' then
            frame.display.text(hud.next_line, FOOT_X, 196,
                               { color = 'CLOUDBLUE' })
        end
        if hud.remaining ~= '' then
            frame.display.text(hud.remaining, FOOT_X, 252, { color = 'GREY' })
        end
        if hud.status ~= '' then
            frame.display.text(hud.status, FOOT_X, 304, { color = accent })
        end
    end

    frame.display.show()
end

-- [flags][len][bytes] x 5 -- distance, street, next, remaining, status.
-- Length-prefixed rather than delimited so a street name can contain
-- anything at all without needing to be escaped.
local function parse_nav(raw)
    local flags = string.byte(raw, 1) or 0
    local fields = {}
    local pos = 2
    for i = 1, 5 do
        local n = string.byte(raw, pos) or 0
        fields[i] = (n > 0) and string.sub(raw, pos + 1, pos + n) or ''
        pos = pos + 1 + n
    end
    return {
        off_route = (flags & 0x01) ~= 0,
        arrived   = (flags & 0x02) ~= 0,
        stale     = (flags & 0x04) ~= 0,
        imminent  = (flags & 0x08) ~= 0,
        distance = fields[1], street = fields[2],
        next_line = fields[3], remaining = fields[4], status = fields[5],
    }
end

function app_loop()
    frame.display.text('navigation ready', 14, 150)
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
            local items = data.process_raw_items()

            for _, item in ipairs(items) do
                local flag, raw = item[1], item[2]

                if flag == NAV_MSG then
                    hud = parse_nav(raw)
                    redraw()

                elseif flag == SPRITE_MSG then
                    -- The host always follows an arrow with a NAV_MSG, so
                    -- leave the repaint to that one: redrawing here would
                    -- show the new arrow beside the OLD text for a frame.
                    local spr = sprite.parse_sprite(raw)
                    sprite.set_palette(spr.num_colors, spr.palette_data)
                    arrow = spr

                elseif flag == CLEAR_MSG then
                    arrow, hud = nil, nil
                    frame.display.text(' ', 1, 1)
                    frame.display.show()

                elseif flag == TAP_CTRL_MSG then
                    taps_on = (code.parse_code(raw).value == 1)
                end
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
