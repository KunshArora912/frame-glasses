-- main.lua: what the glasses show the moment they wake, and a launcher.
--
-- Runs on every boot (luaport.c: frame.stay_awake(true); require('main')).
-- Battery and charging state on screen, then a tap-driven menu of the apps
-- in this project. The apps themselves live on the laptop; picking one here
-- prints "launch:<id>" over Bluetooth and frame_host.py starts it. Every host
-- app sends a break on connect, which ends this script, and stop_frame_app()
-- resets the VM afterwards, which brings this screen back.
--
-- Install:  python install_main.py          Remove:  python install_main.py --remove
--
-- Gestures (grouped within half a second, like shell/gestures.py):
--   status   1 tap  menu                 3 taps  (nothing; these are wake taps)
--   menu     1 tap  move down            2 taps  open      3 taps  back
--   starting 3 taps back
--
-- Panel facts: every frame is redrawn in full (show() clears the buffer),
-- glyphs are 48 px tall, nothing may be drawn below y = 352.

-- Register the tap handler before anything else: the firmware runs the tap
-- hook in whatever Lua is executing, and a tap landing before this line would
-- otherwise find no callback (fixed in firmware fix4, but keep the window tiny).
local taps, last_tap = 0, 0
frame.imu.tap_callback(function()
    taps = taps + 1
    last_tap = frame.time.utc()
end)

local APPS = {
    { id = 'assistant', label = 'Assistant' },
    { id = 'shell',     label = 'Shell' },
    { id = 'todo',      label = 'Todo' },
    { id = 'navigate',  label = 'Navigate' },
    { id = 'showcase',  label = 'Showcase' },
}

local GRACE_S      = 1.5   -- ignore the wake-up taps that arrive during boot
local TAP_GROUP_S  = 0.5
local REFRESH_S    = 1.0
local BLANK_S      = 20    -- on battery: blank the panel after this long idle
local SLEEP_S      = 300   -- on battery: sleep after this long idle
local JITTER_S     = 15    -- on charger: shift the picture this often (OLED wear)

local now = frame.time.utc

local function charging()
    -- fix-build firmware exposes the PMIC charger-input monitor; >0.5 V means
    -- a charger is really delivering, ~0.1 V means no contact. nil = unknown.
    if frame.amux == nil then return nil end
    local ok, v = pcall(frame.amux, 1)
    if ok then return v > 0.5 end
    return nil
end

local function battery()
    local ok, v = pcall(frame.battery_level)
    if ok then return math.floor(v + 0.5) end
    return nil
end

local function connected()
    local ok, v = pcall(frame.bluetooth.is_connected)
    return ok and v
end

-- 1-bit bitmap bar: value 1 maps to palette index 1 + offset.
-- WHITE=1 GREY=2 RED=3 ORANGE=7 GREEN=10
local BAR_W, BAR_H = 320, 14
local function bar(x, y, pct, color_index)
    local fill = math.floor(BAR_W * pct / 100 / 8 + 0.5)    -- in bytes
    local row = string.rep('\255', fill) .. string.rep('\0', BAR_W / 8 - fill)
    if fill > 0 then
        frame.display.bitmap(x, y, BAR_W, 2, color_index - 1, string.rep(row, BAR_H))
    end
    -- rail under the fill
    local rail = string.rep('\255', BAR_W / 8)
    frame.display.bitmap(x, y + BAR_H + 2, BAR_W, 2, 2 - 1, string.rep(rail, 2))
end

local function draw_status(pct, chg, dx, dy)
    local x, y = 40 + dx, 40 + dy
    local level = pct and (pct .. '%') or '--%'
    local state, color
    if chg == true then
        if pct and pct >= 99 then state, color = 'Charged', 'GREEN'
        else state, color = 'Charging', 'GREEN' end
    elseif chg == false then
        state, color = 'On battery', 'WHITE'
    else
        state, color = '', 'WHITE'
    end
    frame.display.text(level, x, y)
    frame.display.text(state, x + 200, y, { color = color })
    local bar_color = 10                        -- GREEN
    if pct and pct < 20 then bar_color = 3     -- RED
    elseif pct and pct < 50 then bar_color = 7 -- ORANGE
    end
    bar(x, y + 64, pct or 0, bar_color)
    local foot = connected() and 'laptop linked' or 'tap for apps'
    frame.display.text(foot, x, 300, { color = 'GREY' })
    frame.display.show()
end

local function draw_menu(selected)
    local y = 6
    for i, app in ipairs(APPS) do
        if y > 352 then break end
        if i == selected then
            frame.display.text('> ' .. app.label, 14, y, { color = 'WHITE' })
        else
            frame.display.text('  ' .. app.label, 14, y, { color = 'GREY' })
        end
        y = y + 52
    end
    frame.display.text('1 down  2 open  3 back', 14, 300, { color = 'GREY' })
    frame.display.show()
end

local function draw_starting(app)
    frame.display.text('Starting ' .. app.label, 40, 100)
    if connected() then
        frame.display.text('laptop is loading it', 40, 170, { color = 'GREY' })
    else
        frame.display.text('needs the laptop', 40, 170, { color = 'ORANGE' })
        frame.display.text('run frame_host.py', 40, 230, { color = 'GREY' })
    end
    frame.display.text('3 taps  back', 40, 300, { color = 'GREY' })
    frame.display.show()
end

local function blank()
    frame.display.show()
end

local t0 = now()
local mode = 'status'
local selected = 1
local last_activity = t0
local last_draw = -1
local last_jitter = t0
local jitter = 0
local blanked = false
local launch_app = nil
local last_status = -100
local STATUS_S = 10    -- when a host is connected, report battery/charging this often

print('main.lua: wake screen + launcher')

while true do
    local t = now()

    -- resolve a tap group
    local gesture = 0
    if taps > 0 and t - last_tap > TAP_GROUP_S then
        gesture = taps
        taps = 0
        if t - t0 < GRACE_S + TAP_GROUP_S then gesture = 0 end
    end

    if gesture > 0 then
        last_activity = t
        blanked = false
        last_draw = -1
        if mode == 'status' then
            if gesture == 1 or gesture == 2 then mode = 'menu'; selected = 1 end
        elseif mode == 'menu' then
            if gesture == 1 then
                selected = selected % #APPS + 1
            elseif gesture == 2 then
                launch_app = APPS[selected]
                mode = 'starting'
                print('launch:' .. launch_app.id)
            elseif gesture >= 3 then
                mode = 'status'
            end
        elseif mode == 'starting' then
            if gesture >= 3 then mode = 'status' end
        end
    end

    local chg = charging()
    local idle = t - last_activity

    -- status:<pct>:<1 charging|0 battery|->:<firmware>  for the phone page / laptop
    if connected() and t - last_status >= STATUS_S then
        local pct = battery()
        print('status:' .. (pct or '') .. ':' .. (chg == true and '1' or chg == false and '0' or '-')
              .. ':' .. tostring(frame.FIRMWARE_VERSION))
        last_status = t
    end

    -- on battery: blank after a while, sleep after longer. Any tap wakes the
    -- screen; three taps wake the glasses from sleep.
    if chg ~= true and mode == 'status' then
        if idle > SLEEP_S then
            blank()
            frame.stay_awake(false)
            frame.sleep()
        elseif idle > BLANK_S and not blanked then
            blank()
            blanked = true
        end
    else
        blanked = false
    end

    -- on charger the picture is shifted a little now and then
    if chg == true and t - last_jitter > JITTER_S then
        jitter = (jitter + 1) % 4
        last_jitter = t
        last_draw = -1
    end

    if not blanked and (last_draw < 0 or t - last_draw >= REFRESH_S) then
        if mode == 'status' then
            draw_status(battery(), chg, jitter * 8, jitter * 6)
        elseif mode == 'menu' then
            draw_menu(selected)
        elseif mode == 'starting' then
            draw_starting(launch_app)
        end
        last_draw = t
    end

    frame.sleep(0.1)
end
