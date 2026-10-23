"""On-glass menu / app launcher (SID 0x03).  Port of ``lib/protocol/menu.dart`` and the
plugin-item half of ``evenhub_pages.dart:494-527``.

The push REPLACES the whole menu; the app's rule is 5..10 items, no duplicates
(menu.dart:26,69-77).  Built-in ids are LIVE; plugin items are DECODED (native-protocol.md
section 7).  There is no SID 0x03 "selected" message: see :func:`parse_selection_event`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import pb
from .pb import Writer, message_field, varint_field

SID_MENU = 0x03
CMD_APP_SEND_MENU_INFO, CMD_OS_RESPONSE_MENU_INFO = 0, 1

#: Built-in menu apps (LIVE ids, menu.dart:15-25); 1/4/5/6/7/8/11/48 are also the SIDs of those features.
BUILTIN = {1: "Dashboard", 4: "Notifications", 5: "Translate", 6: "Teleprompt", 7: "Even AI", 8: "Navigate",
           11: "Conversate", 48: "Terminal", 266: "Silent mode"}
DASHBOARD, NOTIFICATIONS, TRANSLATE, TELEPROMPT, EVEN_AI, NAVIGATE, CONVERSATE, TERMINAL, SILENT_MODE = 1, 4, 5, 6, 7, 8, 11, 48, 266
DEFAULT_MENU = (1, 4, 5, 6, 7)  # evenhub_pages.dart:227
MIN_ITEMS, MAX_ITEMS = 5, 10
ITEM_BUILTIN, ITEM_PLUGIN = 0, 1
PLUGIN_ICON = 1
NAME_MAX_CHARS = 31  # firmware itemName is a 32-byte nanopb string: longer fails the whole menu decode
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class PluginItem:
    name: str
    app_id: int  # the EvenHub widget id the glasses send back in 0xE0 cmd 17
    icon: int = PLUGIN_ICON


def plugin_name(name: str | None) -> str:
    """The name the official app shows: control chars stripped, <=31 code points, 'custom' if empty."""
    n = _CTRL.sub("", name or "").strip()
    return n[:NAME_MAX_CHARS] if n else "custom"


def builtin_item(app_id: int) -> bytes:
    """``{1: 0 itemType, 4: appId}`` (menu.dart:739-740)."""
    return varint_field(1, ITEM_BUILTIN) + varint_field(4, app_id)


def plugin_item(item: PluginItem) -> bytes:
    """``{1: 1, 2: iconNum, 3: name <=31 B, 4: appId}`` (menu.dart:755-762)."""
    w = Writer().varint(1, ITEM_PLUGIN).varint(2, item.icon)
    name = plugin_name(item.name).encode("utf-8")
    while len(name) > NAME_MAX_CHARS:
        name = name[:-1]
        while name and (name[-1] & 0xC0) == 0x80:
            name = name[:-1]
    if name:
        w.bytes(3, name)
    return w.varint(4, item.app_id).to_bytes()


def validate(builtin_ids, plugins=()) -> str | None:
    """The app-side rule; None when valid (menu.dart:779-787, evenhub_pages.dart:516-521)."""
    ids = list(builtin_ids)
    bad = [i for i in ids if i not in BUILTIN]
    if bad:
        return f"not a menu app: {bad}"
    all_ids = ids + [p.app_id for p in plugins]
    if len(set(all_ids)) != len(all_ids):
        return "duplicate menu items"
    if not MIN_ITEMS <= len(all_ids) <= MAX_ITEMS:
        return f"the menu needs {MIN_ITEMS} to {MAX_ITEMS} items (has {len(all_ids)})"
    return None


def menu_push(magic: int, builtin_ids=DEFAULT_MENU, plugins=(), strict: bool = False) -> bytes:
    """Whole-menu replacement ``{1: 0, 2: m, 3: {1: count, 2: Item...}}`` (LIVE for built-ins).
    Example default menu, magic 0x7b:
    ``0800107b1a200805120408002001120408002004120408002005120408002006120408002007``.
    With ``strict`` the 5..10 / no-duplicate rule raises; the plain builder does not enforce it
    (menu.dart:742-750), matching the Python original.  Reply: cmd 1 echoing the magic."""
    if strict:
        err = validate(builtin_ids, plugins)
        if err:
            raise ValueError(err)
    items = [builtin_item(i) for i in builtin_ids] + [plugin_item(p) for p in plugins]
    send = Writer().varint(1, len(items))
    for it in items:
        send.message(2, it)
    return varint_field(1, CMD_APP_SEND_MENU_INFO) + varint_field(2, magic) + message_field(3, send.to_bytes())


def parse_menu_reply(payload: bytes) -> tuple[int, int]:
    """(cmd, magic) of a SID 0x03 frame; the ack is cmd 1 with the pushed magic."""
    return pb.read_varint(payload, 1, -1), pb.read_varint(payload, 2, -1)


# ── selection events (native-protocol.md section 4) ─────────────────────────────────────

#: SID the glasses open when a built-in item is chosen, and the opening command (features.dart:610-629)
_OPEN_CMDS = {0x08: (1, "MENU_STARTUP"), 0x06: (162, "FILE_LIST_REQUEST"), 0x05: (164, "LANG_SWITCH_REQ"), 0x07: (1, "WAKE_UP")}


@dataclass(frozen=True)
class MenuEvent:
    kind: str  # 'foreground' (0x0D push), 'open' (native app opened its channel), 'plugin' (0xE0 cmd 17)
    sid: int  # SID of the frame
    app_id: int | None  # built-in app id / foreground SID, or plugin widget id
    detail: str = ""

    @property
    def app_name(self) -> str:
        return BUILTIN.get(self.app_id or -1, f"0x{(self.app_id or 0):02x}")


def parse_selection_event(sid: int, payload: bytes) -> MenuEvent | None:
    """Map what actually arrives after a menu choice onto a MenuEvent, or None:

    * ``0x0D`` app-sync push ``{3: {1 bg, 2 fg}}`` -> ``foreground`` with the foreground SID (LIVE);
    * a native app opening its own channel (Navigate 0x08 cmd 1, Teleprompt 0x06 cmd 162,
      Translate 0x05 cmd 164, Even AI 0x07 CTRL WAKE_UP) -> ``open``;
    * a plugin item -> ``0xE0`` cmd 17 ``{20: {1: widget_id}}`` -> ``plugin`` (answer with a page or cmd 18).
    """
    cmd = pb.read_varint(payload, 1, -1)
    if sid == 0x0D:
        st = pb.read_bytes(payload, 3)
        if st is None:
            return None
        fg = pb.read_varint(st, 2, 0)
        return MenuEvent("foreground", sid, fg, f"bg=0x{pb.read_varint(st, 1, 0):02x}")
    if sid == 0xE0 and cmd == 17:
        sub = pb.read_bytes(payload, 20)
        return MenuEvent("plugin", sid, pb.read_varint(sub, 1, 0) if sub is not None else None, "MENU_STARTUP")
    if sid in _OPEN_CMDS:
        want, name = _OPEN_CMDS[sid]
        if cmd == want:
            if sid == 0x07:
                ctrl = pb.read_bytes(payload, 3)
                if ctrl is None or pb.read_varint(ctrl, 1, 0) != 1:
                    return None
            return MenuEvent("open", sid, sid, name)
    return None
