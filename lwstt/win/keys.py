"""Virtual-key constants and config-name resolution.

Pure data and lookup, no ctypes, so the name mapping is unit-testable.
"""

from __future__ import annotations

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20

VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

VK_LWIN = 0x5B
VK_RWIN = 0x5C

# Keys Windows reports as "extended"; they need KEYEVENTF_EXTENDEDKEY when
# injected or the target sees the left-hand key instead.
EXTENDED_KEYS = frozenset({VK_RCONTROL, VK_RMENU, VK_LWIN, VK_RWIN})

_NAMES: dict[str, int] = {
    "right_ctrl": VK_RCONTROL,
    "right_control": VK_RCONTROL,
    "rctrl": VK_RCONTROL,
    "left_ctrl": VK_LCONTROL,
    "lctrl": VK_LCONTROL,
    "right_alt": VK_RMENU,
    "ralt": VK_RMENU,
    "left_alt": VK_LMENU,
    "lalt": VK_LMENU,
    "right_shift": VK_RSHIFT,
    "left_shift": VK_LSHIFT,
    "caps_lock": VK_CAPITAL,
    "capslock": VK_CAPITAL,
    "escape": VK_ESCAPE,
    "esc": VK_ESCAPE,
    "space": VK_SPACE,
    "tab": VK_TAB,
    "enter": VK_RETURN,
}

_VK_TO_NAME = {
    VK_RCONTROL: "right_ctrl",
    VK_LCONTROL: "left_ctrl",
    VK_RMENU: "right_alt",
    VK_LMENU: "left_alt",
    VK_RSHIFT: "right_shift",
    VK_LSHIFT: "left_shift",
    VK_CAPITAL: "caps_lock",
    VK_ESCAPE: "escape",
    VK_SPACE: "space",
}


class UnknownKeyError(ValueError):
    pass


def resolve(name: str) -> int:
    """Map a config key name to a virtual-key code."""
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    if key in _NAMES:
        return _NAMES[key]
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        return ord(key.upper())
    raise UnknownKeyError(f"unknown key name: {name!r}")


def resolve_combo(names: list[str]) -> set[int]:
    """Resolve a list of key names, rejecting duplicates."""
    vks = [resolve(n) for n in names]
    if len(set(vks)) != len(vks):
        raise UnknownKeyError(f"combo contains the same key twice: {names!r}")
    return set(vks)


def name_of(vk: int) -> str:
    return _VK_TO_NAME.get(vk, f"vk_{vk:#04x}")


def is_extended(vk: int) -> bool:
    return vk in EXTENDED_KEYS
