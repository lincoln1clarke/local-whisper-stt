# local-whisper-stt

Local push-to-talk dictation for Windows. Hold **Right Ctrl + Right Alt**, speak,
release, and the text is typed into whatever has focus. Nothing leaves the machine.

See [PLAN.md](PLAN.md) for the design and the reasoning behind every decision.

## Using it

Hold both keys and talk. Provisional text appears wrapped in `~tildes~` and is
replaced by the final transcription as each phrase completes. A red dot appears
beside the notification area while recording, and there is a short beep when it
starts listening.

- **Release** either key to finish.
- **Escape** while holding aborts and removes the provisional text.
- A press shorter than 500 ms is discarded, so a stray tap does nothing.

Dictations of any length work the same way. Because each phrase is finalised as
you speak it, releasing after 45 minutes costs the same ~0.5 s as releasing
after 5 seconds.

## Running it

```
.venv\Scripts\pythonw app.py        start (no console window)
python app.py --status              worker state, power state
python app.py --reload              re-read config.json
python app.py --quit                stop
python app.py --list-devices        enumerate microphones
```

A second launch does not start a rival instance; it sends a command to the
running one over a named pipe. Errors go to `logs/app.log`, and a fatal startup
error also raises a message box, since there is no other UI to notice.

To start it at logon, put a shortcut to `pythonw app.py` in the Startup folder
(`shell:startup`).

## Configuring it

Three files, edited directly. No settings window.

| File | Purpose |
|---|---|
| `config.json` | everything |
| `vocabulary.md` | terms to bias towards, **in priority order** |
| `filler.md` | words to strip from the final text |

The word lists are markdown bullet lists: lines starting with `-` are entries,
everything else is commentary. They are re-read at the start of **every**
dictation, so editing them takes effect on the next thing you say.
`config.json` needs `--reload`, and a hotkey change needs a restart.

A broken `config.json` never prevents startup: bad values fall back to defaults
and are logged.

The settings most worth touching:

| Setting | Default | Notes |
|---|---|---|
| `chunking.silence_gap_ms` | 800 | The most consequential number here. Too low splits sentences mid-thought and leaves a stray capital and period at the seam; too high delays commits. |
| `hotkey.hold_threshold_ms` | 500 | Below this a press is discarded. |
| `runtime.idle_exit_minutes_*` | 15 | Idle time before the worker is killed, per power state. Not a limit on dictation length. |
| `runtime.max_dictation_minutes` | 60 | Only so a stuck key cannot record forever. |
| `output.marker_open` / `_close` | `~` | Avoid characters editors auto-pair -- that breaks the typing invariant. |

## How it is put together

Two processes, because a single one cannot have both a fast second dictation and
a small idle footprint.

- **Supervisor** (`lwstt/supervisor.py`) is always resident at ~25 MB and
  contains **no third-party code at all** -- stdlib and `ctypes` only. It owns
  the keyboard hook, audio capture (`winmm`), the typing state machine, the
  recording dot and the worker's lifecycle.
- **Worker** (`lwstt/worker/`) is spawned on first use and killed after 15
  minutes idle, returning all of its RAM and the ~4.7 GB of VRAM. It holds
  faster-whisper, the VAD, both models and logging.

`lwstt/core/` is pure logic -- no Windows, no ML -- and is where nearly all the
tests point.

## Tests

```
pytest              306 fast tests
pytest -m slow      27 more, needing the models and a GPU
```

The slow tests transcribe real speech synthesised by Windows SAPI
(`tests/fixtures/`), so transcription is checked against a known ground truth
without anyone speaking into a microphone.

Three classes of test are worth knowing about:

- **Property-based** coverage of the typing diff: every edit must produce its
  target string, and the shared prefix -- committed text -- must never be
  retyped. This is the code that drives real backspaces into real documents.
- **Architecture tests** spawn a subprocess and assert that importing the
  supervisor never pulls in numpy, faster-whisper or onnxruntime. That
  constraint is the entire reason for the two-process split, and an import added
  in the wrong place would silently cost 300 MB of resident RAM.
- **Worker protocol tests** drive the real subprocess over the real pipe.

## Requirements

- Windows, NVIDIA GPU with ~5 GB free VRAM
- Models in `%USERPROFILE%\ai-models\` (`faster-whisper-large-v3` and
  `faster-whisper-large-v3-turbo`)
- `ffmpeg` on `PATH` for Opus log compression; without it logs stay as WAV

## Logging

Every dictation is kept in `logs/YYYY-MM-DD/` as Opus audio plus a JSON
transcript with per-segment timestamps, for a possible future fine-tune.

This means **anything you dictate is stored, in audio and in text**, including
into a password field. `logging.enabled: false` turns it off.
