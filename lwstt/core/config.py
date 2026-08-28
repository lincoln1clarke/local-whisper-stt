"""Configuration loading.

Rule from PLAN.md: a malformed config must never prevent startup. Anything
unparseable or out of range falls back to a default and is reported as a warning
for the caller to log. Being unable to dictate because of a trailing comma is a
worse failure than running with a stale setting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


class ConfigWarning(str):
    """A recoverable problem found while loading config."""


@dataclass
class HotkeySection:
    keys: list[str] = field(default_factory=lambda: ["right_ctrl", "right_alt"])
    hold_threshold_ms: int = 500


@dataclass
class AudioSection:
    input_device: int | None = None
    sample_rate: int = 16000
    open_on_keydown: bool = True


@dataclass
class ModelsSection:
    dir: str = r"~/ai-models"
    preview: str = "faster-whisper-large-v3-turbo"
    final_ac: str = "faster-whisper-large-v3"
    final_battery: str = "faster-whisper-large-v3"
    device: str = "cuda"
    compute_type: str = "float16"


@dataclass
class ChunkingSection:
    silence_gap_ms: int = 800
    max_chunk_s: float = 25.0


@dataclass
class FeedbackSection:
    # The red dot is the primary signal; beeps on every dictation get old fast.
    beep_on_arm: bool = False
    beep_on_commit: bool = False
    beep_on_error: bool = True
    recording_dot: bool = True


@dataclass
class PreviewSection:
    enabled: bool = True
    refresh_ms: int = 400
    beam_size: int = 1


@dataclass
class FinalSection:
    beam_size: int = 5
    condition_on_previous_text: bool = False
    vad_filter: bool = True
    language: str = "en"


@dataclass
class VocabularySection:
    file: str = "vocabulary.md"
    mode: str = "hotwords"
    max_tokens: int = 224


@dataclass
class FillerSection:
    file: str = "filler.md"
    enabled: bool = True


@dataclass
class RuntimeSection:
    preload_on_start: bool = False
    max_dictation_minutes: int = 60
    idle_unload_minutes_ac: int = 15
    idle_unload_minutes_battery: int = 15
    idle_exit_minutes_ac: int = 15
    idle_exit_minutes_battery: int = 15


@dataclass
class OutputSection:
    marker_open: str = "~"
    marker_close: str = "~"
    normalize_newlines_to_space: bool = True


@dataclass
class LoggingSection:
    enabled: bool = True
    dir: str = "logs"
    format: str = "opus"
    encoder: str = "ffmpeg"
    bitrate_kbps: int = 24


@dataclass
class Config:
    hotkey: HotkeySection = field(default_factory=HotkeySection)
    audio: AudioSection = field(default_factory=AudioSection)
    models: ModelsSection = field(default_factory=ModelsSection)
    chunking: ChunkingSection = field(default_factory=ChunkingSection)
    feedback: FeedbackSection = field(default_factory=FeedbackSection)
    preview: PreviewSection = field(default_factory=PreviewSection)
    final: FinalSection = field(default_factory=FinalSection)
    vocabulary: VocabularySection = field(default_factory=VocabularySection)
    filler: FillerSection = field(default_factory=FillerSection)
    runtime: RuntimeSection = field(default_factory=RuntimeSection)
    output: OutputSection = field(default_factory=OutputSection)
    logging: LoggingSection = field(default_factory=LoggingSection)

    # -- power-state-aware accessors -------------------------------------

    def final_model(self, on_battery: bool) -> str:
        return self.models.final_battery if on_battery else self.models.final_ac

    def idle_unload_minutes(self, on_battery: bool) -> int:
        r = self.runtime
        return r.idle_unload_minutes_battery if on_battery else r.idle_unload_minutes_ac

    def idle_exit_minutes(self, on_battery: bool) -> int:
        r = self.runtime
        return r.idle_exit_minutes_battery if on_battery else r.idle_exit_minutes_ac

    def model_path(self, name: str) -> str:
        """Resolve a model name against models.dir.

        An absolute path is returned untouched so a config can point anywhere.
        """
        p = Path(name)
        if p.is_absolute():
            return str(p)
        return str(Path(self.models.dir) / name)


# Values that must be positive to make any sense.
_POSITIVE_INT_FIELDS = {
    ("hotkey", "hold_threshold_ms"),
    ("audio", "sample_rate"),
    ("chunking", "silence_gap_ms"),
    ("preview", "refresh_ms"),
    ("preview", "beam_size"),
    ("final", "beam_size"),
    ("vocabulary", "max_tokens"),
    ("runtime", "max_dictation_minutes"),
    ("runtime", "idle_unload_minutes_ac"),
    ("runtime", "idle_unload_minutes_battery"),
    ("runtime", "idle_exit_minutes_ac"),
    ("runtime", "idle_exit_minutes_battery"),
    ("logging", "bitrate_kbps"),
}


def _coerce(value: Any, default: Any, path: str, warnings: list[str]) -> Any:
    """Coerce ``value`` to the type of ``default``, or fall back to it."""
    # bool must be checked before int: bool is a subclass of int.
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        warnings.append(f"{path}: expected true/false, got {value!r}; using {default!r}")
        return default

    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            warnings.append(f"{path}: expected a number, got {value!r}; using {default!r}")
            return default
        if isinstance(value, float) and value != int(value):
            warnings.append(f"{path}: expected a whole number, got {value!r}; using {default!r}")
            return default
        return int(value)

    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            warnings.append(f"{path}: expected a number, got {value!r}; using {default!r}")
            return default
        return float(value)

    if isinstance(default, str):
        if not isinstance(value, str):
            warnings.append(f"{path}: expected a string, got {value!r}; using {default!r}")
            return default
        return value

    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            warnings.append(f"{path}: expected a list of strings, got {value!r}; using {default!r}")
            return default
        return list(value)

    # default is None (nullable field, e.g. audio.input_device)
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    warnings.append(f"{path}: expected a number or null, got {value!r}; using null")
    return None


def _apply_section(section: Any, data: Any, name: str, warnings: list[str]) -> None:
    if not isinstance(data, dict):
        warnings.append(f"{name}: expected an object, got {type(data).__name__}; section ignored")
        return
    known = {f.name for f in fields(section)}
    for key, value in data.items():
        if key not in known:
            warnings.append(f"{name}.{key}: unknown setting, ignored")
            continue
        default = getattr(section, key)
        path = f"{name}.{key}"
        coerced = _coerce(value, default, path, warnings)
        if (name, key) in _POSITIVE_INT_FIELDS and isinstance(coerced, (int, float)):
            if coerced <= 0:
                warnings.append(f"{path}: must be greater than 0, got {coerced}; using {default!r}")
                coerced = default
        setattr(section, key, coerced)


def config_from_dict(data: Any) -> tuple[Config, list[str]]:
    """Build a Config from a plain dict, never raising."""
    cfg = Config()
    warnings: list[str] = []
    if not isinstance(data, dict):
        warnings.append("config root: expected an object; using all defaults")
        return cfg, warnings

    known = {f.name for f in fields(cfg)}
    for key, value in data.items():
        if key not in known:
            warnings.append(f"{key}: unknown section, ignored")
            continue
        section = getattr(cfg, key)
        if is_dataclass(section):
            _apply_section(section, value, key, warnings)
    return cfg, warnings


def load_config(path: str | Path) -> tuple[Config, list[str]]:
    """Load config from disk. Missing or broken files yield defaults + warnings."""
    p = Path(path)
    if not p.exists():
        return Config(), [f"{p}: not found; using defaults"]
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        return Config(), [f"{p}: could not be read ({exc}); using defaults"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return Config(), [f"{p}: invalid JSON at line {exc.lineno} ({exc.msg}); using defaults"]
    return config_from_dict(data)
