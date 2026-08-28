"""Tests for deferred forwarding.

The failure this guards against is specific and severe: if Right Ctrl reaches
the application before we start typing, every dictated character arrives as a
control chord. "hello" becomes Ctrl+H, Ctrl+E, Ctrl+L.
"""

from __future__ import annotations

import pytest

from lwstt.core.hotkey_state import VK_ESCAPE, Decision, HotkeyMachine, Signal, State

RCTRL, RALT = 0xA3, 0xA5
KEY_C, KEY_A = 0x43, 0x41


@pytest.fixture
def m():
    return HotkeyMachine({RCTRL, RALT})


def press(machine, vk):
    return machine.on_key(vk, True)


def release(machine, vk):
    return machine.on_key(vk, False)


class TestConstruction:
    def test_requires_exactly_two_keys(self):
        with pytest.raises(ValueError):
            HotkeyMachine({RCTRL})
        with pytest.raises(ValueError):
            HotkeyMachine({RCTRL, RALT, KEY_C})

    def test_starts_idle(self, m):
        assert m.state is State.IDLE
        assert not m.armed


class TestArming:
    def test_first_key_is_swallowed_not_forwarded(self, m):
        d = press(m, RCTRL)
        assert d.swallow
        assert d.inject == []
        assert m.state is State.PENDING

    def test_second_key_arms(self, m):
        press(m, RCTRL)
        d = press(m, RALT)
        assert d.swallow
        assert d.signal is Signal.ARM
        assert m.armed

    def test_either_order_arms(self, m):
        press(m, RALT)
        d = press(m, RCTRL)
        assert d.signal is Signal.ARM

    def test_neither_key_ever_reaches_the_app(self, m):
        """The whole point: no modifier leaks while dictating."""
        decisions = [press(m, RCTRL), press(m, RALT)]
        assert all(d.swallow for d in decisions)
        assert all(d.inject == [] for d in decisions)

    def test_arming_after_a_long_hold(self, m):
        """There is no time limit -- holding one key for ages then pressing the
        other must still arm. This was an explicit design requirement."""
        press(m, RCTRL)
        for _ in range(10_000):  # stands in for elapsed time; no timer exists
            pass
        assert press(m, RALT).signal is Signal.ARM


class TestSoloTap:
    def test_solo_press_and_release_is_replayed(self, m):
        press(m, RCTRL)
        d = release(m, RCTRL)
        assert d.swallow
        assert d.inject == [(RCTRL, True), (RCTRL, False)]
        assert m.state is State.IDLE

    def test_solo_alt_tap_is_replayed(self, m):
        """A solo Alt tap focuses the menu bar; some users rely on it."""
        press(m, RALT)
        assert release(m, RALT).inject == [(RALT, True), (RALT, False)]

    def test_solo_tap_does_not_arm(self, m):
        press(m, RCTRL)
        assert release(m, RCTRL).signal is Signal.NONE


class TestPassthroughShortcuts:
    def test_rctrl_plus_c_flushes_the_modifier(self, m):
        press(m, RCTRL)
        d = press(m, KEY_C)
        assert not d.swallow, "the C must reach the application"
        assert d.inject == [(RCTRL, True)], "Ctrl must be flushed first, in order"
        assert m.state is State.PASSTHROUGH

    def test_subsequent_keys_pass_through(self, m):
        press(m, RCTRL)
        press(m, KEY_C)
        assert not press(m, KEY_A).swallow
        assert not release(m, KEY_A).swallow

    def test_releasing_the_modifier_returns_to_idle(self, m):
        press(m, RCTRL)
        press(m, KEY_C)
        d = release(m, RCTRL)
        assert not d.swallow, "the app needs the key-up to balance the flushed down"
        assert m.state is State.IDLE

    def test_passthrough_never_arms(self, m):
        press(m, RCTRL)
        press(m, KEY_C)
        assert press(m, RALT).signal is Signal.NONE

    def test_can_arm_normally_after_a_shortcut(self, m):
        press(m, RCTRL)
        press(m, KEY_C)
        release(m, KEY_C)
        release(m, RCTRL)
        assert m.state is State.IDLE
        press(m, RCTRL)
        assert press(m, RALT).signal is Signal.ARM


class TestArmedSuppression:
    def test_all_keys_are_suppressed_while_armed(self, m):
        press(m, RCTRL)
        press(m, RALT)
        for vk in (KEY_A, KEY_C, 0x20, 0x0D, 0x09):
            assert press(m, vk).swallow, f"vk {vk:#x} leaked into the document"
            assert release(m, vk).swallow

    def test_typing_while_armed_emits_no_signal(self, m):
        press(m, RCTRL)
        press(m, RALT)
        assert press(m, KEY_A).signal is Signal.NONE

    def test_releasing_either_key_disarms(self, m):
        for release_first in (RCTRL, RALT):
            machine = HotkeyMachine({RCTRL, RALT})
            press(machine, RCTRL)
            press(machine, RALT)
            d = release(machine, release_first)
            assert d.signal is Signal.DISARM
            assert d.swallow
            assert machine.state is State.IDLE

    def test_escape_aborts(self, m):
        press(m, RCTRL)
        press(m, RALT)
        d = press(m, VK_ESCAPE)
        assert d.signal is Signal.ABORT
        assert d.swallow
        assert m.state is State.IDLE

    def test_escape_when_not_armed_passes_through(self, m):
        assert not press(m, VK_ESCAPE).swallow

    def test_no_second_disarm_after_abort(self, m):
        press(m, RCTRL)
        press(m, RALT)
        press(m, VK_ESCAPE)
        assert release(m, RCTRL).signal is Signal.NONE


class TestRobustness:
    def test_unmatched_key_up_is_harmless(self, m):
        assert not release(m, KEY_A).swallow
        assert m.state is State.IDLE

    def test_key_up_for_the_other_combo_key_while_pending(self, m):
        press(m, RCTRL)
        d = release(m, RALT)  # never went down
        assert m.state is State.PENDING
        assert d.signal is Signal.NONE

    def test_repeated_downs_do_not_double_arm(self, m):
        """Windows auto-repeat sends many key-downs while a key is held."""
        press(m, RCTRL)
        press(m, RALT)
        for _ in range(5):
            assert press(m, RALT).signal is Signal.NONE
        assert m.armed

    def test_full_cycle_is_repeatable(self, m):
        for _ in range(3):
            assert press(m, RCTRL).swallow
            assert press(m, RALT).signal is Signal.ARM
            assert release(m, RCTRL).signal is Signal.DISARM
            release(m, RALT)
            assert m.state is State.IDLE

    def test_arm_disarm_leaves_no_keys_believed_held(self, m):
        press(m, RCTRL)
        press(m, RALT)
        release(m, RCTRL)
        release(m, RALT)
        assert m.held == set()


class TestDecisionShape:
    def test_default_decision_is_inert(self):
        d = Decision(swallow=False)
        assert d.signal is Signal.NONE
        assert d.inject == []

    def test_inject_lists_are_not_shared_between_decisions(self):
        a, b = Decision(swallow=False), Decision(swallow=False)
        a.inject.append((1, True))
        assert b.inject == []
