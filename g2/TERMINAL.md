# Terminal Mode on the G2, without Even's app

Our replacement for Even's "Terminal Mode": Claude Code (or any console
program) shows up on the glasses. You can read what it said, approve or deny
its permission prompts, and answer it by voice. Nothing leaves the laptop.

```
Claude Code --hooks--> g2/terminal_hook.py --HTTP--> g2/terminal.py (hub, 127.0.0.1:8767)
~/.claude/projects/*/*.jsonl ----tail----------------------^   ^      |
terminal.py run -- claude  (ConPTY + pyte) ---/ws/agent--------/      | /ws (4 Hz)
                                                                      v
                                       g2/apps/terminal.py "Terminal"  +  http://127.0.0.1:8767/
```

## 1. One-time setup: hooks

```
.venv\Scripts\python.exe g2\terminal.py install-hooks
```

This merges eight entries (SessionStart, UserPromptSubmit, PreToolUse,
PostToolUse, PermissionRequest, Notification, Stop, SessionEnd) into
`%USERPROFILE%\.claude\settings.json`. It writes a timestamped backup first
(only when something changes), keeps your own hooks, can be run again safely,
and prints what it changed. `--settings <path>` targets another file, for
example a project's `.claude/settings.json`. To remove them:
`python g2\terminal.py uninstall-hooks`.

- PermissionRequest runs synchronously (timeout 60 s). The hook waits up to
  `G2_TERMINAL_APPROVAL_TIMEOUT` seconds (default 30) for the glasses. If
  nobody answers, the hub is down, or no glasses/page client is connected, it
  prints nothing and Claude Code shows its normal prompt. With no client
  connected it returns at once, so a laptop-only session is never delayed.
- The other events are `"async": true`, so they never slow a tool call.

Without hooks the hub still works from the transcripts alone (status, last
reply, last tool), but there are no approvals.

## 2. Start the hub

```
.venv\Scripts\python.exe g2\terminal.py serve
```

Open http://127.0.0.1:8767/ for the laptop page: sessions, screens,
allow/deny, and typing. `python g2\terminal.py sessions` prints the list.
Options: `--port`, `--limit` (newest N transcripts, default 8), `--projects`.

## 3. A session the glasses can type into

```
.venv\Scripts\python.exe g2\terminal.py run -- claude
.venv\Scripts\python.exe g2\terminal.py run --cols 100 --rows 30 -- powershell
```

`run` starts the command in a ConPTY sized for the glasses (48 x 9 by
default), renders it with pyte, mirrors it in your console, and forwards your
keystrokes. It attaches to a running hub, or starts its own hub if none is
running. Typed input comes from the glasses (voice), from the page, or from
`POST /sessions/<id>/input {"text": "...", "enter": true}` or `{"key": "y|n|enter|escape|ctrl-c|tab|up|down"}`.
"Waiting" for a console means the last line looks like a prompt (`>`, `$`,
`?`, `(y/n)`, ...) and there has been no output for 1.5 s.

Claude Code sessions you started normally are read-only on the glasses: you
can approve and deny, but typed replies need a console. The exception is a
`run -- claude` running in the same folder: replies to that transcript go to
its console. The hub guesses that link because hooks do not identify the
console.

## 4. On the glasses: the Terminal app (menu, second entry)

| where | gesture | does |
|---|---|---|
| list | swipe up / down | move the selection |
| list | tap | open the session |
| session | swipe up / down | scroll back / forward (the console's history, or Claude's last reply) |
| session | tap | voice reply. With an approval waiting, "yes / ok / go ahead" allows, "no" denies, and anything else denies with your words as the reason Claude sees. Otherwise the words are typed and Enter is pressed; "enter", "escape" and "control c" send those keys. |
| session | hold (long press) | approve the waiting permission. On a waiting console it presses Enter. Otherwise it goes back to the list. |
| session | swipe up while an approval waits | deny |
| anywhere | double tap | leave the app (the launcher) |

Status glyphs: `!` waiting for you, `*` busy, `-` idle, `x` ended. `ask`
marks a pending approval and `pty` marks a managed console. If the hub is not
running, the app says so and reconnects by itself.

## 5. Privacy

- The hub binds `127.0.0.1` only, and the hook script talks only to it.
- Transcripts are read in place and never copied. Nothing is written to the
  repo. `install-hooks` touches only the settings file and its backup.
- Speech-to-text is the launcher's local Whisper. No Even app, no cloud, no
  account.
- Anything that can reach localhost on this laptop can see the session list
  and approve. Do not forward the port.

## 6. Hook contract we rely on

Verified against code.claude.com/docs/en/hooks and /hooks-guide on 2026-10-23:

- Every hook receives JSON on stdin with `session_id`, `transcript_path`,
  `cwd`, `permission_mode` and `hook_event_name`. Tool events add
  `tool_name` and `tool_input`. PermissionRequest has no `tool_use_id` and
  may carry `permission_suggestions`. Stop adds `last_assistant_message`.
  Notification adds `message`, `title` and `notification_type`
  (`permission_prompt`, `idle_prompt`, ...). UserPromptSubmit adds `prompt`.
  SessionStart adds `source`.
- The PermissionRequest decision on stdout (exit 0):
  `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}`.
  Deny takes an optional `"message"` (shown to Claude) and `"interrupt": true`.
  Empty stdout means no decision, and the normal permission flow runs. A
  hook's allow does not override deny rules in settings.
- Settings: `"hooks": {"<Event>": [{"matcher": "...", "hooks": [{"type": "command", "command": "...", "timeout": s, "async": bool}]}]}`.
  The matcher is optional. The default command timeout is 600 s. Settings
  edits are normally picked up by a running Claude Code (check with `/hooks`).

## 7. Tests

```
python -m unittest g2.test_terminal g2.test_launcher
python g2/dev.py preview terminal --gestures tap --no-png
```
