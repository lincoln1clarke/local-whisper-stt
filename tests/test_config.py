"""Tests for configuration loading.

The governing rule: a malformed config must never prevent startup. There is no
UI, so a config error that kills the process leaves a tool that looks installed
and simply does nothing.
"""

from __future__ import annotations

import json

import pytest

from lwstt.core.config import Config, config_from_dict, load_config


def cfg(data):
    return config_from_dict(data)


class TestDefaults:
    def test_defaults_match_the_plan(self):
        c = Config()
        assert c.hotkey.keys == ["right_ctrl", "right_alt"]
        assert c.hotkey.hold_threshold_ms == 500
        assert c.chunking.silence_gap_ms == 1000
        assert c.output.marker_open == "~"
        assert c.final.condition_on_previous_text is False
        assert c.runtime.max_dictation_minutes == 60
        assert c.audio.sample_rate == 16000

    def test_empty_dict_yields_defaults_without_warnings(self):
        c, warnings = cfg({})
        assert warnings == []
        assert c.hotkey.hold_threshold_ms == 500


class TestOverrides:
    def test_partial_override_leaves_siblings_alone(self):
        c, warnings = cfg({"hotkey": {"hold_threshold_ms": 250}})
        assert warnings == []
        assert c.hotkey.hold_threshold_ms == 250
        assert c.hotkey.keys == ["right_ctrl", "right_alt"]

    def test_multiple_sections(self):
        c, _ = cfg({"chunking": {"silence_gap_ms": 600}, "output": {"marker_open": "<"}})
        assert c.chunking.silence_gap_ms == 600
        assert c.output.marker_open == "<"

    def test_nullable_field_accepts_null_and_int(self):
        assert cfg({"audio": {"input_device": None}})[0].audio.input_device is None
        assert cfg({"audio": {"input_device": 3}})[0].audio.input_device == 3

    def test_float_field(self):
        c, _ = cfg({"chunking": {"max_chunk_s": 20.5}})
        assert c.chunking.max_chunk_s == 20.5


class TestRecovery:
    def test_unknown_section_is_ignored_with_a_warning(self):
        c, warnings = cfg({"nonsense": {"x": 1}})
        assert any("nonsense" in w for w in warnings)
        assert c.hotkey.hold_threshold_ms == 500

    def test_unknown_key_is_ignored_with_a_warning(self):
        _, warnings = cfg({"hotkey": {"typo_here": 1}})
        assert any("typo_here" in w for w in warnings)

    def test_wrong_type_falls_back(self):
        c, warnings = cfg({"hotkey": {"hold_threshold_ms": "soon"}})
        assert c.hotkey.hold_threshold_ms == 500
        assert any("hold_threshold_ms" in w for w in warnings)

    def test_bool_is_not_accepted_as_a_number(self):
        c, warnings = cfg({"hotkey": {"hold_threshold_ms": True}})
        assert c.hotkey.hold_threshold_ms == 500
        assert warnings

    def test_number_is_not_accepted_as_a_bool(self):
        c, warnings = cfg({"preview": {"enabled": 1}})
        assert c.preview.enabled is True
        assert warnings

    def test_non_integer_float_for_an_int_field_falls_back(self):
        c, warnings = cfg({"preview": {"refresh_ms": 400.5}})
        assert c.preview.refresh_ms == 700  # the default
        assert warnings

    def test_whole_float_is_accepted_for_an_int_field(self):
        c, warnings = cfg({"preview": {"refresh_ms": 400.0}})
        assert c.preview.refresh_ms == 400  # accepted: a whole number
        assert warnings == []

    @pytest.mark.parametrize("bad", [0, -1, -500])
    def test_non_positive_values_fall_back(self, bad):
        c, warnings = cfg({"chunking": {"silence_gap_ms": bad}})
        assert c.chunking.silence_gap_ms == 1000
        assert any("greater than 0" in w for w in warnings)

    def test_section_of_the_wrong_type_is_skipped(self):
        c, warnings = cfg({"hotkey": "nope"})
        assert c.hotkey.hold_threshold_ms == 500
        assert any("hotkey" in w for w in warnings)

    def test_root_of_the_wrong_type_yields_defaults(self):
        c, warnings = cfg([1, 2, 3])
        assert c.hotkey.hold_threshold_ms == 500
        assert warnings

    def test_bad_list_field_falls_back(self):
        c, warnings = cfg({"hotkey": {"keys": [1, 2]}})
        assert c.hotkey.keys == ["right_ctrl", "right_alt"]
        assert warnings

    def test_several_problems_are_all_reported(self):
        _, warnings = cfg(
            {"hotkey": {"hold_threshold_ms": "x", "bogus": 1}, "unknown_section": {}}
        )
        assert len(warnings) >= 3


class TestFileLoading:
    def test_missing_file_yields_defaults(self, tmp_path):
        c, warnings = load_config(tmp_path / "absent.json")
        assert c.hotkey.hold_threshold_ms == 500
        assert any("not found" in w for w in warnings)

    def test_valid_file(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"hotkey": {"hold_threshold_ms": 300}}), encoding="utf-8")
        c, warnings = load_config(p)
        assert c.hotkey.hold_threshold_ms == 300
        assert warnings == []

    def test_trailing_comma_does_not_prevent_startup(self, tmp_path):
        """The exact scenario named in PLAN.md."""
        p = tmp_path / "config.json"
        p.write_text('{"hotkey": {"hold_threshold_ms": 300,},}', encoding="utf-8")
        c, warnings = load_config(p)
        assert c.hotkey.hold_threshold_ms == 500
        assert any("invalid JSON" in w for w in warnings)

    def test_empty_file(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("", encoding="utf-8")
        c, warnings = load_config(p)
        assert c.hotkey.hold_threshold_ms == 500
        assert warnings

    def test_utf8_content(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"output": {"marker_open": "›"}}), encoding="utf-8")
        assert load_config(p)[0].output.marker_open == "›"

    def test_load_never_raises(self, tmp_path):
        for content in ["", "{", "null", "[]", '"str"', "123", "{'single': 1}"]:
            p = tmp_path / "c.json"
            p.write_text(content, encoding="utf-8")
            c, _ = load_config(p)
            assert isinstance(c, Config)


class TestPowerStateAccessors:
    def test_final_model_switches_on_power(self):
        c, _ = cfg({"models": {"final_ac": "big", "final_battery": "small"}})
        assert c.final_model(on_battery=False) == "big"
        assert c.final_model(on_battery=True) == "small"

    def test_idle_timers_switch_on_power(self):
        c, _ = cfg(
            {"runtime": {"idle_exit_minutes_ac": 30, "idle_exit_minutes_battery": 5}}
        )
        assert c.idle_exit_minutes(on_battery=False) == 30
        assert c.idle_exit_minutes(on_battery=True) == 5

    def test_unload_timers_switch_on_power(self):
        c, _ = cfg(
            {"runtime": {"idle_unload_minutes_ac": 20, "idle_unload_minutes_battery": 2}}
        )
        assert c.idle_unload_minutes(on_battery=False) == 20
        assert c.idle_unload_minutes(on_battery=True) == 2

    def test_defaults_are_equal_on_both_power_states(self):
        c = Config()
        assert c.final_model(True) == c.final_model(False)
        assert c.idle_exit_minutes(True) == c.idle_exit_minutes(False)


class TestModelPath:
    def test_relative_name_resolves_against_the_model_dir(self):
        c, _ = cfg({"models": {"dir": "C:/models"}})
        assert c.model_path("faster-whisper-large-v3").replace("\\", "/") == (
            "C:/models/faster-whisper-large-v3"
        )

    def test_absolute_path_is_left_alone(self):
        c, _ = cfg({"models": {"dir": "C:/models"}})
        assert c.model_path("D:/elsewhere/model").replace("\\", "/") == "D:/elsewhere/model"
