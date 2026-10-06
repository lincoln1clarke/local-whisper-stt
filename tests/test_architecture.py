"""Tests that enforce the architectural constraints from PLAN.md.

These are the ones that catch a well-meaning refactor quietly undoing a
deliberate decision -- an import added in the wrong place is invisible in review
but costs 300 MB of permanently resident RAM.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Anything that would drag weight into the always-resident process.
FORBIDDEN_IN_SUPERVISOR = [
    "numpy",
    "faster_whisper",
    "ctranslate2",
    "onnxruntime",
    "torch",
    "sounddevice",
    "huggingface_hub",
    "tokenizers",
]


def run_probe(source: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
    )


class TestSupervisorStaysLight:
    def test_importing_the_supervisor_pulls_in_nothing_heavy(self):
        """The reason the two-process split exists at all."""
        probe = f"""
import sys
import lwstt.supervisor          # noqa: F401
import lwstt.worker_client       # noqa: F401
import lwstt.control             # noqa: F401
forbidden = {FORBIDDEN_IN_SUPERVISOR!r}
leaked = sorted(m for m in forbidden if m in sys.modules)
print("LEAKED:" + ",".join(leaked))
"""
        result = run_probe(probe)
        assert result.returncode == 0, result.stderr
        leaked = result.stdout.strip().removeprefix("LEAKED:")
        assert leaked == "", f"supervisor imported heavy modules: {leaked}"

    def test_core_imports_nothing_heavy_and_nothing_windows_specific(self):
        """core/ must stay pure so it is testable anywhere."""
        probe = f"""
import sys
from lwstt.core import chunker, config, diff, hotkey_state, protocol, textproc
from lwstt.core import typing_state, wordlists       # noqa: F401
forbidden = {FORBIDDEN_IN_SUPERVISOR!r} + ["ctypes"]
leaked = sorted(m for m in forbidden if m in sys.modules)
print("LEAKED:" + ",".join(leaked))
"""
        result = run_probe(probe)
        assert result.returncode == 0, result.stderr
        leaked = result.stdout.strip().removeprefix("LEAKED:")
        assert leaked == "", f"core imported: {leaked}"

    def test_app_entry_point_stays_light_until_it_starts(self):
        probe = f"""
import sys
sys.argv = ["app.py", "--help"]
import importlib.util
spec = importlib.util.spec_from_file_location("app_probe", "app.py")
module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(module)
except SystemExit:
    pass
forbidden = {FORBIDDEN_IN_SUPERVISOR!r}
leaked = sorted(m for m in forbidden if m in sys.modules)
print("LEAKED:" + ",".join(leaked))
"""
        result = run_probe(probe)
        assert result.returncode == 0, result.stderr
        leaked = result.stdout.strip().removeprefix("LEAKED:")
        assert leaked == "", f"app.py imported: {leaked}"


class TestSourceLevelConstraints:
    def _sources(self, package: str) -> list[Path]:
        return sorted((ROOT / "lwstt" / package).rglob("*.py"))

    def test_core_has_no_ctypes_or_ml_imports(self):
        offenders = []
        for path in self._sources("core"):
            text = path.read_text(encoding="utf-8")
            for banned in ("import ctypes", "import numpy", "faster_whisper"):
                if banned in text:
                    offenders.append(f"{path.name}: {banned}")
        assert offenders == [], offenders

    def test_supervisor_module_does_not_import_the_worker_package(self):
        text = (ROOT / "lwstt" / "supervisor.py").read_text(encoding="utf-8")
        assert "from .worker import" not in text
        assert "import lwstt.worker" not in text

    def test_worker_is_launched_as_a_subprocess_not_imported(self):
        text = (ROOT / "lwstt" / "worker_client.py").read_text(encoding="utf-8")
        assert '"-m", "lwstt.worker"' in text


class TestPackagedDefaults:
    def test_shipped_config_parses_without_warnings(self):
        from lwstt.core.config import load_config

        # use_local=False: these are claims about the file that ships, not
        # about whatever this machine lays over it.
        config, warnings = load_config(ROOT / "config.json", use_local=False)
        assert warnings == [], warnings
        assert config.hotkey.keys == ["right_ctrl", "right_alt"]
        assert config.chunking.silence_gap_ms == 1000
        assert config.output.marker_open == "~"

    def test_shipped_config_matches_the_dataclass_defaults(self):
        """Drift between config.json and the code defaults is a silent trap."""
        from lwstt.core.config import Config, load_config

        shipped, _ = load_config(ROOT / "config.json", use_local=False)
        defaults = Config()
        assert shipped.preview == defaults.preview
        assert shipped.runtime == defaults.runtime
        assert shipped.chunking == defaults.chunking
        assert shipped.output == defaults.output
        assert shipped.final == defaults.final

    def test_shipped_word_lists_parse(self):
        from lwstt.core.wordlists import load_list

        vocabulary = load_list(ROOT / "vocabulary.md")
        fillers = load_list(ROOT / "filler.md")
        assert "um" in fillers
        assert all(not t.startswith("#") for t in vocabulary + fillers)

    def test_vocabulary_holds_only_real_entries(self):
        """This file belongs to the user, so its contents are not asserted --
        only that nothing from the surrounding prose leaks in as a term.

        It ships with no entries: hotwords are decoder context and Whisper will
        sometimes emit them into its output, so a placeholder example becomes a
        word that appears in the user's text without being spoken.
        """
        from lwstt.core.wordlists import load_list

        for term in load_list(ROOT / "vocabulary.md"):
            assert term.strip() == term
            assert not term.startswith(("#", "*", "-"))
            assert len(term) < 60, f"prose leaked in as a term: {term!r}"

    def test_vocabulary_fits_the_configured_budget(self):
        """A term below the cut is unused, never an error -- but if the very
        first entry cannot fit, the list does nothing at all."""
        from lwstt.core.config import load_config
        from lwstt.core.wordlists import fit_to_budget, load_list

        config, _ = load_config(ROOT / "config.json")
        terms = load_list(ROOT / "vocabulary.md")
        if terms:
            assert fit_to_budget(terms, config.vocabulary.max_tokens)

    def test_configured_hotkey_resolves(self):
        from lwstt.core.config import load_config
        from lwstt.win import keys

        config, _ = load_config(ROOT / "config.json")
        combo = keys.resolve_combo(config.hotkey.keys)
        assert combo == {keys.VK_RCONTROL, keys.VK_RMENU}

    @pytest.mark.parametrize("name", ["preview", "final_ac", "final_battery"])
    def test_configured_models_exist_on_disk(self, name):
        from lwstt.core.config import expand_path, load_config

        config, _ = load_config(ROOT / "config.json")
        path = Path(config.model_path(getattr(config.models, name)))
        # expand_path, not Path: models.dir may be "~/ai-models", and an
        # unexpanded "~" is a relative path that never exists, so the guard
        # would skip the check on every machine instead of running it.
        if not expand_path(config.models.dir).is_dir():
            pytest.skip("model directory not present")
        assert path.is_dir(), f"{name} points at a missing directory: {path}"
        assert (path / "model.bin").is_file()
