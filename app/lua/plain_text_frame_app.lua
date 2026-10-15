-- Device-side Frame app: receive TxPlainText messages and render them.
--
-- Paired with ../plain_text.py. The message code below MUST match the code the
-- host passes to send_message().
--
-- Uploaded as frame_app.lua; the std libs arrive minified, so the require()
-- names carry the .min suffix (upload_stdlua_libs(minified=True) is the default).

local data = require('data.min')
local plain_text = require('plain_text.min')

-- must match TEXT_MSG in plain_text.py
local TEXT_MSG = 0x0a

-- Glyphs are 48px tall and the per-glyph fit check (y + 48 <= 400) fails
-- SILENTLY, so anything below this just disappears with no error.
local MAX_Y = 352

-- Accumulated lines. The FPGA clears the write buffer on every show(), so a
-- frame has to be redrawn in full -- drawing only the newest arrival would
-- wipe every previous line off the display.
local lines = {}

local function redraw()
    for _, ln in ipairs(lines) do
        frame.display.text(ln.string, ln.x, ln.y,
                           { color = ln.color, spacing = ln.spacing })
    end
    frame.display.show()
end

function app_loop()
    lines = { { string = 'Waiting for text...', x = 1, y = 1,
                color = 'WHITE', spacing = 4 } }
    redraw()

    -- REQUIRED: start_frame_app(await_print=True) blocks until the device
    -- prints something. Without a startup print the host times out with
    -- "device didn't respond" even though the app is running fine.
    print('Frame app is running')

    while true do
        local rc, err = pcall(function()
            -- Ordered queue of {flag, raw_bytes} in arrival order. The caller
            -- dispatches and parses -- data.app_data/data.parsers no longer exist.
            local items = data.process_raw_items()

            for _, item in ipairs(items) do
                local flag, raw = item[1], item[2]

                if flag == TEXT_MSG then
                    local msg = plain_text.parse_plain_text(raw)

                    if msg.y > MAX_Y then msg.y = MAX_Y end

                    -- first real line replaces the placeholder
                    if #lines == 1 and lines[1].string == 'Waiting for text...' then
                        lines = {}
                    end

                    lines[#lines + 1] = msg
                    redraw()
                    print('rendered: ' .. msg.string)
                end
            end

            frame.sleep(0.05)
        end)

        if rc == false then
            print('app_loop error: ' .. tostring(err))
            break
        end
    end
end

app_loop()
