# local-whisper-stt

Local push-to-talk dictation for Windows. Hold **Right Ctrl + Right Alt**, speak,
release, and the text is typed into whatever has focus. Nothing leaves the machine.

See [PLAN.md](PLAN.md) for the design and the reasoning behind every decision.

## Installing it

`INSTALL.md` is written for a coding agent. Clone this repo, open it with one,
and say *"install this by following INSTALL.md"* — it will detect your hardware,
pick models to match, and verify the result against the test suite. The install
genuinely varies by machine (GPU vs CPU, VRAM, model choice), which is why it is
a guide for an agent rather than a script.

Windows only. See **Requirements** below.

## Using it

Hold both keys and talk. An empty `~~` appears as soon as the hold threshold
passes -- that is the confirmation it is listening, and also confirmation the
caret is somewhere that accepts text. Provisional text fills in between the
tildes and is replaced by the final transcription as each phrase completes. A
red dot shows beside the notification area while recording.

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
| `vocabulary.md` | terms to bias towards, **in priority order** (a template; copy to `vocabulary.local.md`) |
| `filler.md` | words to strip from the final text |

`vocabulary.md` ships as a **short generic template**, and a `vocabulary.local.md`
beside it wins and is gitignored -- that is where your own project and client
names belong. Keep the list short on purpose: hotwords are decoder context, and
Whisper will sometimes emit them straight into its output when the audio is
ambiguous, so a term listed here can appear in your text even when you did not
say it. Only add words that are genuinely misrecognised often enough to be worth
that. The preview pass ignores the list entirely; only the final pass is biased.

The word lists are markdown bullet lists: lines starting with `-` are entries,
everything else is commentary. They are re-read at the start of **every**
dictation, so editing them takes effect on the next thing you say.
`config.json` needs `--reload`, and a hotkey change needs a restart.

A broken `config.json` never prevents startup: bad values fall back to defaults
and are logged.

The settings most worth touching:

| Setting | Default | Notes |
|---|---|---|
| `chunking.silence_gap_ms` | 1000 | The most consequential number here. Too low splits sentences mid-thought and leaves a stray capital and period at the seam; too high delays commits. |
| `hotkey.hold_threshold_ms` | 500 | Below this a press is discarded. |
| `runtime.idle_exit_minutes_*` | 15 | Idle time before the worker is killed, per power state. Not a limit on dictation length. |
| `runtime.max_dictation_minutes` | 60 | Only so a stuck key cannot record forever. |
| `output.marker_open` / `_close` | `~` | Avoid characters editors auto-pair -- that breaks the typing invariant. |
| `chunking.max_chunk_s` | 12 | Forced cut when you never pause. Lower commits more often; higher costs more per pass. |
| `preview.refresh_ms` | 700 | Must stay above the ~0.3 s a preview pass costs, or previews starve the finals. |
| `models.final_ac` | turbo | `faster-whisper-large-v3` is more accurate for ~0.5 s more per chunk. |

## How it is put together

Two processes, because a single one cannot have both a fast second dictation and
a small idle footprint.

- **Supervisor** (`lwstt/supervisor.py`) is always resident at ~25 MB and
  contains **no third-party code at all** -- stdlib and `ctypes` only. It owns
  the keyboard hook, audio capture (`winmm`), the typing state machine, the
  recording dot and the worker's lifecycle.
- **Worker** (`lwstt/worker/`) is spawned on first use and killed after 15
  minutes idle, returning all of its RAM and VRAM. It holds faster-whisper,
  the VAD, the models and logging.

`lwstt/core/` is pure logic -- no Windows, no ML -- and is where nearly all the
tests point.

## Tests

```
pytest              350 fast tests
pytest -m slow      32 more, needing the models and a GPU
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

- Windows, NVIDIA GPU with ~2 GB free VRAM on the default turbo-only setup
  (~5 GB if `final_ac` is switched to `large-v3`)
- Models in `%USERPROFILE%\ai-models\` (`faster-whisper-large-v3` and
  `faster-whisper-large-v3-turbo`)
- `ffmpeg` on `PATH` for Opus log compression; without it logs stay as WAV

## Logging

Every dictation is kept in `logs/YYYY-MM-DD/` as Opus audio plus a JSON
transcript with per-segment timestamps, for a possible future fine-tune.

This means **anything you dictate is stored, in audio and in text**, including
into a password field. `logging.enabled: false` turns it off.

## Staying offline

Nothing here needs the network: the models live in `%USERPROFILE%\ai-models\`
and every pass runs locally. `tools\firewall.ps1`, run from an elevated
PowerShell, makes that structural rather than a promise -- it blocks this venv's
`python.exe` and `pythonw.exe` in both directions, so no dependency can send
audio or transcripts anywhere, and faster-whisper cannot quietly re-download a
model.

```powershell
.\tools\firewall.ps1                     # block
.\tools\firewall.ps1 -Remove             # unblock
Disable-NetFirewallRule -Group "Local Whisper STT"   # temporarily, e.g. for pip
```

While the rules are on, `pip` cannot install into this venv -- it runs through
the same interpreter.

One trap worth knowing, because it fails silently. A firewall rule matches the
**image path of the running process**, and a uv-built venv's `python.exe` is a
trampoline that re-execs the base interpreter: the process Windows actually sees
is `C:\Program Files\Python313\pythonw.exe`, so rules naming the venv path look
correct in the firewall UI and block nothing. The script refuses to run against a
trampoline; `-ReplaceTrampolines` swaps in real interpreter copies, which is what
stdlib `venv --copies` produces. Verify with:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'app\.py' } |
  Select-Object ProcessId, ExecutablePath
```

The path shown must be the one the rules name.

## It does not work in apps running as administrator

If the window you are typing into was started with "Run as administrator" and
this app was not, **nothing happens at all** — no markers, no text, no beep, and
nothing in `logs/app.log`. It looks like the app has crashed. It has not.

That is Windows **UIPI** (User Interface Privilege Isolation). Every process has
an integrity level, and a process at a lower level may not send input to one at
a higher level. It is the rule that stops malware running as you from driving an
elevated window or clicking its own UAC prompt. Because the block applies to the
keyboard hook and not just to typing, the hotkey never fires in the first place,
which is why the failure is silent rather than partial.

Affected: elevated PowerShell or Terminal, Task Manager, Registry Editor, most
installers, and any editor someone launched as administrator. Everything running
normally is fine.

**This is deliberate and will not be fixed.** Running the dictation app elevated
would make it work, and it would also mean a permanent system-wide keyboard hook
running as administrator, plus a UAC prompt at every logon. That is a far worse
thing to have on a machine than one documented limitation. The legitimate
bypass — `uiAccess="true"` in the manifest, which is how screen readers do it —
needs the binary signed by a trusted CA *and* installed under `Program Files`,
which does not fit a cloned repo.

If you genuinely need it and accept the trade, the change is small and any
coding agent can make it for you. Please read the paragraph above first.

## Licence

Apache License 2.0 — see `LICENSE`. Use it commercially, fork it, ship it in a
product; the only conditions are the usual ones of keeping the notice and
stating what you changed. The licence also grants patent rights explicitly,
which is the practical difference from MIT.
