# Working in this repo

Push-to-talk dictation for Windows. Hold Right Ctrl + Right Alt, speak, release.
Everything runs locally on the GPU.

`README.md` explains what it does and how it is configured. `PLAN.md` records
the design and every rejected alternative. This file is the short list of things
that will bite you, most of which look like ordinary breakage and are not.

## The app is firewalled off the network. `pip` and `uv` will fail.

Windows Firewall rules block this venv's `python.exe` and `pythonw.exe` in **both
directions** (`tools\firewall.ps1`, group `Local Whisper STT`). Nothing here
needs the internet, and the rules make that structural rather than a promise.

`pip` runs through the same blocked interpreter, so installs fail with a network
error that looks like a broken index, a proxy problem, or DNS. It is not. Lift
the rules for as long as you need them and put them back:

```powershell
Disable-NetFirewallRule -Group "Local Whisper STT"
# ... install ...
Enable-NetFirewallRule  -Group "Local Whisper STT"
```

Do not "fix" a failing install by deleting the rules, adding an allow rule, or
routing around them. Ask first — being offline is the point.

## Do not let uv rebuild the venv's interpreters

**This is the part that fails silently, so read it before running any uv command.**

A firewall rule matches the *image path of the running process*. uv builds a
venv whose `python.exe` is a **trampoline** that re-execs the base interpreter,
and under one of those the process Windows actually sees is the base install
(e.g. `C:\Program Files\Python313\pythonw.exe`), so rules naming the venv path
match nothing. They look completely correct in the firewall UI while blocking
not one packet. Check `.venv/pyvenv.cfg` for a `uv =` line to see how yours was
built.

`.venv\Scripts\python.exe` and `pythonw.exe` must therefore be **real
interpreter copies**, which is what stdlib `venv --copies` produces and what
`INSTALL.md` tells you to create. Where a venv was built by uv instead, the
trampolines are kept beside the copies as `.uv-trampoline`. That swap is
deliberate. Do not restore them, and do not recreate the venv with uv without
redoing it.

`uv sync` and `uv pip install` may rewrite those two files back to trampolines.
After any uv command that touches them, re-run:

```powershell
.\tools\firewall.ps1 -ReplaceTrampolines    # elevated
```

Blocking the base interpreter instead is not an option: it would cut off every
other Python on the machine.

Verify protection is real — never assume it from the rule list:

```powershell
# the running app must report the venv path, not C:\Program Files\...
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'app\.py' } |
  Select-Object ProcessId, ExecutablePath
```

```
# and a direct connect must be refused, not time out
.venv\Scripts\python.exe -c "import socket;s=socket.socket();s.settimeout(5);s.connect(('1.1.1.1',443))"
# expected: PermissionError
```

A timeout is not proof — it can just mean the host is unreachable.
`PermissionError` is the firewall.

## The worker's stdin is the protocol pipe

`lwstt/worker/` talks to the supervisor over stdin/stdout with length-prefixed
frames. Any child process it spawns inherits that stdin, and a child that reads
stdin will eat command frames.

This actually happened: `encode_opus` spawned ffmpeg without redirecting stdin.
ffmpeg reads stdin for its interactive keys, so it sat on the pipe until its
120 s timeout — delaying `DONE` past two minutes (leaving `~~` stranded on
screen), desyncing the stream into "unknown message type 0", and leaving
undeleted WAVs behind. One line, three unrelated-looking bugs.

**Every subprocess spawned from the worker passes `stdin=subprocess.DEVNULL`**,
plus the tool's own opt-out flag where it has one (`ffmpeg -nostdin`).
`tests/test_audio_log.py` enforces both.

## Nothing slow may sit between the keys coming up and the text landing

`END` must reply `DONE` before doing anything expensive. Writing the WAV and
encoding it costs roughly a fifth of a second per minute of speech, and the
markers stay on screen for every bit of it. Logging happens on a daemon thread
after the reply; the worker joins those threads on shutdown so a dictation
followed straight by an eviction still keeps its recording.

The same rule covers the supervisor. The empty `~~` pair is cleared when the
keys are released, not when `DONE` arrives, so no delay downstream can strand
it.

## The supervisor must stay light

The supervisor is always resident (~23 MB). It is **stdlib and ctypes only** —
no numpy, no faster-whisper, no ctranslate2, no onnxruntime, no torch, no
sounddevice. That constraint is the entire reason for the two-process split; an
import in the wrong module silently costs ~300 MB of resident RAM.
`tests/test_architecture.py` enforces it by importing each module in a
subprocess and checking `sys.modules`.

Models live in `%USERPROFILE%\ai-models\` and are loaded only by the worker,
which is spawned on demand and evicted after 15 idle minutes.

## Tests

```
.venv\Scripts\python -m pytest tests\ -q            # 432 fast, no GPU
.venv\Scripts\python -m pytest tests\ -q -m slow    # 32, real models on the GPU
```

Slow tests spawn real worker subprocesses and load both models from disk; they
pass with the network blocked, and it is worth keeping them that way. Dictation
logging is asynchronous, so a test that checks for a log file must wait for it
(`wait_for_log`) rather than assume `DONE` means it is written.

## Debugging a dictation

`logs/app.log` is the supervisor's log. `logs/YYYY-MM-DD/` holds every dictation
as Opus plus a JSON transcript with per-segment timestamps — which means real
recordings can be replayed through the real worker at real-time speed to measure
release-to-`DONE` directly. That is how the ffmpeg hang was found, after several
wrong theories from reading code alone. Prefer replaying a real recording over
reasoning about timing.

`cancelled pending output: user started typing` in `app.log` means the user typed
while the supervisor still believed a dictation was in flight. A cluster of those
means `DONE` is arriving late — look at what runs before it.
