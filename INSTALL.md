# Installing this — instructions for a coding agent

**If you are a human:** you do not have to follow this yourself. Clone the repo,
open it with a coding agent (Claude Code, Cursor, or similar) and say *"install
this by following INSTALL.md"*. The agent will detect your hardware, pick the
right models, and verify the result. Skim the **What this does to your machine**
section first so you know what you are agreeing to.

**If you are an agent:** work top to bottom. Every phase ends with a check you
can run. Do not skip the checks — several failures in this project present as
something else entirely, and the checks are what tell the difference. Ask the
user before doing anything in **Decisions that are the user's, not yours**.

---

## What this does to your machine

- Installs a Python virtual environment (~250 MB, or ~2.2 GB with CUDA).
- Downloads speech models to `%USERPROFILE%\ai-models\` (1.5–3 GB).
- Runs a background process that holds a **system-wide keyboard hook** so it can
  see the push-to-talk keys. This is genuinely how the app works, and it is also
  what a keylogger does — expect antivirus to take an interest. All the source
  is in this repo.
- **Records every dictation to disk** as audio plus text, unencrypted, forever,
  by default. See the logging decision below.

## Decisions that are the user's, not yours

Ask before each. Do not assume.

1. **Dictation logging** — on by default, and it keeps audio *and* transcripts of
   everything said, including anything dictated into a password field. Confirm
   they want it, or set `logging.enabled: false` in `config.json`.
2. **Firewall rules** — `tools\firewall.ps1` blocks this venv's interpreter from
   the network in both directions. Recommended, needs an elevated prompt, and it
   stops `pip` from working in this venv until lifted.
3. **Start at logon** — a shortcut in `shell:startup`.

---

## Phase 1 — Check the platform

This is **Windows-only** and not portable. It uses `WH_KEYBOARD_LL` for the
hotkey, `SendInput` for typing, `winmm` for audio capture and Win32 for the
recording indicator. If the user is on macOS or Linux, stop and tell them: this
would need a new `lwstt/win/` layer, not a tweak.

Requires Python 3.11+ (developed on 3.13).

```powershell
python --version
```

## Phase 2 — Detect the hardware, then choose models

This decides everything downstream, so do it before installing anything.

```powershell
nvidia-smi --query-gpu=name,memory.total --format=csv
```

Pick from the table. `preview` runs constantly while the user speaks; `final_ac`
and `final_battery` produce the committed text.

| Hardware | preview / final | `device` | `compute_type` | Download |
|---|---|---|---|---|
| NVIDIA, ≥6 GB VRAM | `faster-whisper-large-v3-turbo` | `cuda` | `float16` | turbo (~1.6 GB) |
| NVIDIA, ≥10 GB VRAM, wants best accuracy | preview turbo, final `faster-whisper-large-v3` | `cuda` | `float16` | both (~4.5 GB) |
| NVIDIA, 4–6 GB VRAM | `faster-whisper-large-v3-turbo` | `cuda` | `int8_float16` | turbo (~1.6 GB) |
| No GPU / AMD / Intel | `faster-whisper-small` (or `-base`) | `cpu` | `int8` | small (~500 MB) |

Notes that matter:

- **CPU is much slower than real time for the large models.** Do not put
  `large-v3` on a CPU and call it installed — it will lag far behind speech and
  the app will feel broken. On CPU also raise `preview.refresh_ms` to about
  `1500` and consider `preview.enabled: false`.
- The default config uses turbo for all three slots. That was a deliberate
  choice on the author's machine: turbo is nearly as accurate as `large-v3` for
  this use and much faster, which matters more than the last fraction of a
  percent when text appears as you speak.
- AMD/Intel GPUs and NPUs are not supported by CTranslate2 here. Use CPU.

## Phase 3 — Create the virtual environment

**Use stdlib `venv` with `--copies`.** Not `uv`, unless you read the trampoline
warning in `CLAUDE.md` first and handle it.

```powershell
python -m venv --copies .venv
```

`--copies` puts real interpreters in `.venv\Scripts\` rather than launchers.
A uv-created venv uses a trampoline that re-execs the base interpreter, which
makes the firewall rules in Phase 6 match nothing while looking correct. Using
`--copies` avoids that class of problem entirely, and gives you `pip`.

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
```

If Phase 2 chose `cuda`, also:

```powershell
.\.venv\Scripts\python -m pip install -r requirements-cuda.txt
```

Skip that file on CPU — it is ~2 GB of CUDA runtime nothing will use.

**Check:**

```powershell
.\.venv\Scripts\python -c "import faster_whisper, ctranslate2, numpy; print('deps ok')"
```

## Phase 4 — Download the models

Models go in `%USERPROFILE%\ai-models\<name>\` as flat, self-contained
directories, deliberately outside the repo — they are multi-GB blobs that do not
belong in version control, and keeping them in one place means several projects
can share them.

Use these exact sources. Do not substitute a search result: there are many
re-uploads of Whisper conversions by unknown accounts, and the point of running
locally is not having to trust one.

| Model | HuggingFace repo |
|---|---|
| `faster-whisper-large-v3-turbo` | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` |
| `faster-whisper-large-v3` | `Systran/faster-whisper-large-v3` |
| `faster-whisper-small` | `Systran/faster-whisper-small` |

```powershell
.\.venv\Scripts\python -c @'
from huggingface_hub import snapshot_download
snapshot_download("mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                  local_dir=r"%USERPROFILE%\ai-models\faster-whisper-large-v3-turbo")
'@
```

Both are mechanical CTranslate2 conversions of OpenAI's MIT-licensed weights by
third parties. A user who would rather trust neither can convert from OpenAI's
own repo instead — the conversion is deterministic, so hashing the result also
verifies a downloaded copy is unmodified. `PLAN.md` has the command.

Each directory must end up with `model.bin`, `config.json`, `tokenizer.json`
and `vocabulary.json`. If the download used a HuggingFace cache and left links
rather than real files, resolve them into real files — a later cache cleanup
would otherwise break the install.

## Phase 5 — Configure

Edit `config.json` with the Phase 2 choices. `models.dir` defaults to
`~/ai-models`, which expands per-user; leave it alone unless the models are
somewhere else. Environment variables such as `%USERPROFILE%` work too.

```json
"models": {
  "dir": "~/ai-models",
  "preview": "faster-whisper-large-v3-turbo",
  "final_ac": "faster-whisper-large-v3-turbo",
  "final_battery": "faster-whisper-large-v3-turbo",
  "device": "cuda",
  "compute_type": "float16"
}
```

Then offer the user a personal vocabulary. Copy `vocabulary.md` to
`vocabulary.local.md` and edit that — the `.local.md` file wins and is
gitignored, so their project and client names never reach version control.
It is worth doing: it is the difference between "SQL Alchemy" and
"SQLAlchemy", and it is re-read at the start of every dictation, so edits
take effect on the next thing said, with no restart.

**Check — the whole config resolves and the models are really there:**

```powershell
.\.venv\Scripts\python -m pytest tests\test_architecture.py tests\test_config.py -q
```

## Phase 6 — Optional: cut it off from the network (ask first)

Nothing here needs the internet after Phase 4. From an **elevated** PowerShell:

```powershell
.\tools\firewall.ps1
```

Read `CLAUDE.md` before running this. The short version: a firewall rule matches
the image path of the *running* process, so if the venv was built by uv its
python.exe is a trampoline and the rules will silently match nothing. Phase 3's
`--copies` avoids that; the script refuses to run against a trampoline anyway.

**Check — a refusal, not a timeout:**

```powershell
.\.venv\Scripts\python -c "import socket;s=socket.socket();s.settimeout(5);s.connect(('1.1.1.1',443))"
```

Expect `PermissionError`. A timeout proves nothing — it can just mean the host
is unreachable.

Tell the user: while these rules are on, `pip` cannot install into this venv.
`Disable-NetFirewallRule -Group "Local Whisper STT"` lifts them temporarily.

## Phase 7 — Verify properly

```powershell
.\.venv\Scripts\python -m pytest tests\ -q            # 432, no GPU needed
.\.venv\Scripts\python -m pytest tests\ -q -m slow    # 32, loads the real models
```

The slow tests spawn real worker subprocesses and transcribe synthesised speech
with known ground truth, so they are a genuine end-to-end check of the install.
They should pass with the firewall on. If they fail on CPU with timeouts, the
model is too large for the hardware — go back to Phase 2.

Then start it and have the user try it for real:

```powershell
.\.venv\Scripts\pythonw app.py
```

Hold **Right Ctrl + Right Alt**, speak, release. Two `~~` markers appear when it
is listening, provisional text appears between them as you speak, and it is
replaced by the final text. Confirm with the user that text actually lands in
their editor — that is the only check that covers the whole path.

```powershell
.\.venv\Scripts\python app.py --status
```

## Phase 8 — Optional: start at logon (ask first)

Put a shortcut to `pythonw app.py` in `shell:startup`, with "Start in" set to the
repo directory. It costs about 23 MB resident while idle; the worker holding the
models is spawned on demand and evicted after 15 idle minutes.

---

## If something is wrong

`logs\app.log` is the supervisor's log. Read `CLAUDE.md` — it lists the failures
that present as something else, which is most of them. The ones that will cost
you the most time if you have not read it:

- A failing `pip` install is probably the firewall, not a broken index.
- A `uv` command can silently undo the firewall protection.
- Nothing that reads stdin may be spawned from the worker; that is the protocol
  pipe.
- Text appearing late is worth measuring by replaying a real recording from
  `logs\`, not by reading code.
