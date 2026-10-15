-- Device-side self-test helpers for Frame.
--
-- Uploaded as st.lua by selftest.py and required as 'st'. Each function
-- print()s one compact pipe-delimited line which the host parses.
--
-- NOTE: Lua 5.4 errors on string.format('%d', x) when x is a non-integer
-- float, and several frame.* APIs return floats -- so this file concatenates
-- or uses %.1f rather than %d throughout.

local M = {}

function M.sys()
    print(frame.HARDWARE_VERSION .. '|' .. frame.FIRMWARE_VERSION ..
          '|' .. frame.GIT_TAG .. '|' .. frame.battery_level())
end

function M.bt()
    print(frame.bluetooth.address() .. '|' .. frame.bluetooth.max_length())
end

function M.mem()
    collectgarbage('collect')
    print(collectgarbage('count'))
end

-- Display: brightness, custom palette entry, default + named colour, show
function M.disp()
    frame.display.set_brightness(0)
    frame.display.assign_color('RED', 255, 0, 0)
    frame.display.text('SELFTEST OK', 120, 100)
    frame.display.text('green text', 120, 160, { color = 'GREEN' })
    frame.display.text('red text', 120, 220, { color = 'RED' })
    frame.display.show()
    print('ok')
end

-- Draw a 2-colour bitmap: 8 px wide, 1 bit per pixel, alternating columns
function M.bitmap()
    local row = string.char(0xAA)
    frame.display.bitmap(100, 300, 8, 2, 1, row:rep(8))
    frame.display.show()
    print('ok')
end

-- Microphone: returns bytes_read|min_sample|max_sample
-- A flat min==max means silence or a dead mic; real audio has spread.
function M.mic(rate, depth, secs)
    -- The mic is HARDWARE state and survives a Lua VM reset, so a previous
    -- aborted run can leave it sampling and make start() raise
    -- "already started". Clear it first.
    pcall(frame.microphone.stop)

    frame.microphone.start { sample_rate = rate, bit_depth = depth }
    frame.sleep(secs)
    local d = frame.microphone.read(1024)
    frame.microphone.stop()

    if d == nil or #d == 0 then
        print('nil|0|0')
        return
    end

    -- Decode real SAMPLES, not bytes: at 16-bit the low byte alone spans
    -- 0..255 regardless of mic health, so a byte-wise min/max always
    -- "passes" and tells you nothing.
    --
    -- Decimate hard. string.unpack over a few thousand samples is slow
    -- enough to trip the device watchdog, which resets the Lua VM and
    -- leaves the microphone running. ~128 samples is plenty to see signal.
    local mn, mx = 32767, -32768
    local step = 2
    local width = 1
    if depth == 16 then width = 2 end
    step = width * math.max(1, math.floor(#d / width / 128))

    for i = 1, #d - width + 1, step do
        local s
        if depth == 16 then s = string.unpack('<h', d, i)
        else s = string.unpack('<b', d, i) end
        if s < mn then mn = s end
        if s > mx then mx = s end
    end
    print(#d .. '|' .. mn .. '|' .. mx)
end

-- IMU: orientation plus raw accelerometer and magnetometer
function M.imu()
    local d = frame.imu.direction()
    local r = frame.imu.raw()
    print(string.format('%.1f|%.1f|%.1f|%.0f,%.0f,%.0f|%.0f,%.0f,%.0f',
        d.roll, d.pitch, d.heading,
        r.accelerometer.x, r.accelerometer.y, r.accelerometer.z,
        r.compass.x, r.compass.y, r.compass.z))
end

-- Filesystem: mkdir, write, read back, listdir, cleanup
function M.fs()
    local payload = 'frame selftest payload 12345'
    frame.file.mkdir('/st')

    local f = frame.file.open('/st/a.txt', 'write')
    -- close() even if write() errors: an unclosed handle corrupts the file
    local wrote = pcall(f.write, f, payload)
    f:close()
    if not wrote then print('FAIL|0|0') return end

    local g = frame.file.open('/st/a.txt', 'read')
    local got = g:read()
    g:close()
    if got == nil then got = '' end

    local n = 0
    for _, _ in ipairs(frame.file.listdir('/st')) do n = n + 1 end

    frame.file.remove('/st/a.txt')
    frame.file.remove('/st')

    local ok = 'FAIL'
    if got == payload then ok = 'ok' end
    print(ok .. '|' .. #got .. '|' .. n)
end

-- Time: set UTC, read back, decompose
function M.tim()
    frame.time.utc(1760000000)
    local t = frame.time.date()
    print(frame.time.utc() .. '|' .. t.year .. '-' .. t.month .. '-' .. t.day)
end

-- FPGA: read the camera metering registers (proves the SPI link)
function M.fpga()
    local d = frame.fpga_read(0x25, 6)
    if d == nil then print('nil') return end
    print(#d .. '|' .. string.byte(d, 1))
end

-- Camera exposure telemetry: shutter|gain|scene_brightness|spot_brightness
-- Shutter/gain pinned at max with brightness ~0 means no light on the sensor.
function M.cam(steps)
    -- power_save is documented but unverifiable from any installed source;
    -- don't let its absence fail the whole camera check
    pcall(frame.camera.power_save, false)
    for _ = 1, steps do
        frame.camera.auto {}
        frame.sleep(0.1)
    end
    local a = frame.camera.auto {}
    print(string.format('%.0f|%.1f|%.5f|%.5f',
        a.shutter, a.analog_gain, a.brightness.scene, a.brightness.spot.average))
end

-- Camera manual control. NOTE: frame.camera.set_exposure() does NOT exist in
-- firmware v25.080.0838 despite the docs -- the real name is set_shutter().
function M.cam_manual()
    local ok = pcall(frame.camera.set_shutter, 16383)
    ok = pcall(frame.camera.set_gain, 248) and ok
    ok = pcall(frame.camera.set_white_balance, 512, 512, 512) and ok
    if not ok then print('setter-failed') return end
    frame.sleep(0.3)
    local d = frame.fpga_read(0x25, 6)
    if d == nil or #d < 3 then print('fpga-nil') return end
    print(string.byte(d, 1) .. ',' .. string.byte(d, 2) .. ',' .. string.byte(d, 3))
end

-- Compression: arm a real LZ4 round trip. The host then sends an lz4-FRAME
-- compressed payload as raw data; decompressed chunks come back via
-- process_function. NOTE: raw lz4 *block* format is rejected -- it must be
-- frame format (with the magic header), as produced by `lz4 -9 -B1024`.
function M.lz4_arm(block)
    if frame.compression == nil or frame.compression.decompress == nil then
        print('missing')
        return
    end
    LZ4_GOT = ''
    frame.compression.process_function(function(d) LZ4_GOT = LZ4_GOT .. d end)
    frame.bluetooth.receive_callback(function(d)
        local ok, err = pcall(frame.compression.decompress, d, block)
        if not ok then LZ4_GOT = 'ERR ' .. tostring(err) end
    end)
    print('armed')
end

function M.lz4_result()
    print(#LZ4_GOT .. '|' .. string.sub(LZ4_GOT, 1, 32))
end

function M.lz4_clear()
    frame.bluetooth.receive_callback(nil)
    frame.compression.process_function(nil)
    print('cleared')
end

-- Tap: arm a callback. Host waits, then calls M.tap_count().
TAPS = 0
function M.tap_arm()
    TAPS = 0
    frame.imu.tap_callback(function() TAPS = TAPS + 1 end)
    print('armed')
end

function M.tap_count()
    print(TAPS)
end

function M.tap_disarm()
    frame.imu.tap_callback(nil)
    print('disarmed')
end

return M
