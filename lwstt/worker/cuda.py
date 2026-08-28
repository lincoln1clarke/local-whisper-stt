"""CUDA runtime library discovery.

CTranslate2 delay-loads cublas64_12.dll and the cuDNN libraries *by name* at
first use, not at import. Neither os.add_dll_directory nor a plain sys.path
entry helps: the search happens inside the already-loaded native extension and
looks at PATH.

Verified on this machine -- without this, the model loads on CUDA and then fails
on the first encode with "Library cublas64_12.dll is not found or cannot be
loaded". Preloading each DLL by absolute path puts it in the process's module
table, so the later load-by-name resolves to it.

Must run before faster_whisper is imported.
"""

from __future__ import annotations

import ctypes
import os
import site
from pathlib import Path

_SUBDIRS = ("cublas/bin", "cudnn/bin", "cuda_nvrtc/bin")


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    for entry in site.getsitepackages():
        candidate = Path(entry) / "nvidia"
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def prepare() -> list[str]:
    """Make the bundled CUDA libraries loadable. Returns the directories used."""
    used: list[str] = []
    for root in _candidate_roots():
        for sub in _SUBDIRS:
            directory = root / sub
            if not directory.is_dir():
                continue
            used.append(str(directory))
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(str(directory))
            except OSError:
                pass
            for dll in sorted(directory.glob("*.dll")):
                try:
                    ctypes.WinDLL(str(dll))
                except OSError:
                    # Some DLLs only load once their dependencies are present;
                    # the ones that matter succeed, and a failure here is not
                    # fatal on its own.
                    pass
    return used
