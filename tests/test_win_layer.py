"""Tests for the Windows layer pieces that can be checked without side effects.

Key-name resolution and SendInput event construction are pure enough to assert
directly; the event structures are inspected rather than dispatched.
"""

from __future__ import annotations

import pytest

from lwstt.win import keys


class TestKeyResolution:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("right_ctrl", keys.VK_RCONTROL),
            ("RIGHT_CTRL", keys.VK_RCONTROL),
            ("right-ctrl", keys.VK_RCONTROL),
            ("  right ctrl  ", keys.VK_RCONTROL),
            ("rctrl", keys.VK_RCONTROL),
            ("right_alt", keys.VK_RMENU),
            ("caps_lock", keys.VK_CAPITAL),
            ("escape", keys.VK_ESCAPE),
            ("esc", keys.VK_ESCAPE),
        ],
    )
    def test_known_names(self, name, expected):
        assert keys.resolve(name) == expected

    def test_single_letters_and_digits(self):
        assert keys.resolve("a") == ord("A")
        assert keys.resolve("Z") == ord("Z")
        assert keys.resolve("5") == ord("5")

    def test_unknown_name_raises(self):
        with pytest.raises(keys.UnknownKeyError):
            keys.resolve("wibble")

    def test_empty_name_raises(self):
        with pytest.raises(keys.UnknownKeyError):
            keys.resolve("")

    def test_resolve_combo(self):
        assert keys.resolve_combo(["right_ctrl", "right_alt"]) == {
            keys.VK_RCONTROL,
            keys.VK_RMENU,
        }

    def test_duplicate_combo_keys_rejected(self):
        """A combo of one key twice would arm on a single press."""
        with pytest.raises(keys.UnknownKeyError):
            keys.resolve_combo(["right_ctrl", "rctrl"])

    def test_left_and_right_are_distinct(self):
        assert keys.resolve("left_ctrl") != keys.resolve("right_ctrl")
        assert keys.resolve("left_alt") != keys.resolve("right_alt")

    def test_name_of_round_trips(self):
        for name in ("right_ctrl", "right_alt", "escape", "caps_lock"):
            assert keys.name_of(keys.resolve(name)) == name

    def test_name_of_unknown_is_readable(self):
        assert keys.name_of(0x99) == "vk_0x99"

    def test_right_hand_modifiers_are_extended(self):
        """Without KEYEVENTF_EXTENDEDKEY the target sees the left-hand key."""
        assert keys.is_extended(keys.VK_RCONTROL)
        assert keys.is_extended(keys.VK_RMENU)
        assert not keys.is_extended(keys.VK_LCONTROL)
        assert not keys.is_extended(ord("A"))


class TestSendInputEvents:
    """Event construction only -- nothing is dispatched to the system."""

    def test_ascii_produces_one_pair_per_character(self):
        from lwstt.win import sendinput

        events = sendinput.unicode_events("abc")
        assert len(events) == 6

    def test_events_are_marked_as_ours(self):
        """The hook must be able to recognise and ignore what we inject."""
        from lwstt.win import sendinput

        for event in sendinput.unicode_events("hi") + sendinput.vk_events(keys.VK_BACK):
            assert event.ki.dwExtraInfo == sendinput.INJECT_SIGNATURE

    def test_unicode_events_carry_no_virtual_key(self):
        """KEYEVENTF_UNICODE means no layout, no Shift, no Caps Lock."""
        from lwstt.win import sendinput

        for event in sendinput.unicode_events("A!é"):
            assert event.ki.wVk == 0
            assert event.ki.dwFlags & sendinput.KEYEVENTF_UNICODE

    def test_capital_letters_need_no_shift_event(self):
        from lwstt.win import sendinput

        assert len(sendinput.unicode_events("A")) == len(sendinput.unicode_events("a"))

    def test_scan_codes_are_the_utf16_code_units(self):
        from lwstt.win import sendinput

        events = sendinput.unicode_events("é")
        assert events[0].ki.wScan == ord("é")

    def test_astral_characters_become_surrogate_pairs(self):
        from lwstt.win import sendinput

        events = sendinput.unicode_events("🙂")
        assert len(events) == 4, "one down/up pair per UTF-16 code unit"
        assert 0xD800 <= events[0].ki.wScan <= 0xDBFF
        assert 0xDC00 <= events[2].ki.wScan <= 0xDFFF

    def test_empty_text_produces_nothing(self):
        from lwstt.win import sendinput

        assert sendinput.unicode_events("") == []

    def test_backspace_uses_a_real_virtual_key(self):
        from lwstt.win import sendinput

        events = sendinput.vk_events(keys.VK_BACK, 3)
        assert len(events) == 6
        assert all(e.ki.wVk == keys.VK_BACK for e in events)
        assert all(not e.ki.dwFlags & sendinput.KEYEVENTF_UNICODE for e in events)

    def test_down_and_up_alternate(self):
        from lwstt.win import sendinput

        events = sendinput.vk_events(keys.VK_BACK, 2)
        ups = [bool(e.ki.dwFlags & sendinput.KEYEVENTF_KEYUP) for e in events]
        assert ups == [False, True, False, True]

    def test_zero_backspaces_produces_nothing(self):
        from lwstt.win import sendinput

        assert sendinput.vk_events(keys.VK_BACK, 0) == []
        assert sendinput.send([]) == 0

    def test_extended_flag_is_set_for_right_hand_modifiers(self):
        from lwstt.win import sendinput

        assert sendinput.vk_down(keys.VK_RCONTROL).ki.dwFlags & sendinput.KEYEVENTF_EXTENDEDKEY
        assert not (
            sendinput.vk_down(keys.VK_LCONTROL).ki.dwFlags & sendinput.KEYEVENTF_EXTENDEDKEY
        )


class TestAudioDevices:
    def test_enumeration_works(self):
        from lwstt.win.audio import list_devices

        for device in list_devices():
            assert device.index >= 0
            assert isinstance(device.name, str)

    def test_recorder_reports_zero_before_starting(self):
        from lwstt.win.audio import Recorder

        recorder = Recorder()
        assert recorder.seconds_recorded == 0.0
        assert recorder.read_available() == b""

    def test_buffer_sizing_matches_the_sample_rate(self):
        from lwstt.win.audio import Recorder

        recorder = Recorder(sample_rate=16000, buffer_ms=100)
        assert recorder.bytes_per_buffer == 3200  # 16000 * 2 bytes * 0.1 s


class TestIndicatorPlacement:
    def test_dot_position_is_on_screen(self):
        from lwstt.win.indicator import dot_position

        x, y = dot_position()
        assert x > 0 and y > 0

    def test_dot_position_falls_back_without_a_tray(self, monkeypatch):
        import lwstt.win.indicator as module

        monkeypatch.setattr(module, "tray_rect", lambda: None)
        x, y = module.dot_position()
        assert x > 0 and y > 0
