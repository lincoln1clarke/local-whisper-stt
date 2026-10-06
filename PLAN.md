# local-whisper-stt

A local push-to-talk dictation app for Windows. Hold a hotkey, speak, release, and the
text is typed into whatever has focus. Everything runs on this machine — no account, no
network, no outage.

Status: **built; awaiting real-world testing.** 333 tests pass (306 fast, 27
needing the models). Everything below has been implemented. What has *not*
happened is a human holding the keys and speaking -- see `README.md` to run it.

## Why

Three existing tools, three problems:

| Tool | Problem |
|---|---|
| `Win+H` (Windows Voice Typing) | Poor accuracy, no punctuation; toggle rather than push-to-talk |
| Claude Code `/voice` | Cloud-only — "streams your recorded audio to Anthropic's servers"; delays and occasional failures |
| claude.ai dictation | Allows one dictation per prompt box, then the button disappears |

A local tool fixes all three at once: it types into *any* focused input, unlimited times,
with no network dependency.

## Measured feasibility

On this machine (RTX 2000 Ada Laptop, 8 GB VRAM; display runs on the Intel iGPU so the
full 8 GB is free), `faster-whisper` `large-v3` in fp16:

| Utterance | Release → text |
|---|---|
| 3 s | 0.45 s |
| 6 s | 0.57 s |
| 15 s | 0.90 s |
| 30 s | 1.81 s |

Cold model load 5.7 s. 3,859 MiB resident, 4,005 MiB peak. Roughly **16.6x realtime**.

Sub-second on a normal sentence — faster than a network round trip.

## Architecture: two decoupled passes over chunked audio

Two processes (see *Resource lifecycle*): a permanently resident **supervisor** with no
third-party dependencies, holding the hotkey, audio capture and typing; and a **worker**,
spawned on demand and killed on idle, holding the models and both transcription passes.

Inside the worker, the central design decision — two independent pipelines over one shared
audio buffer:

- **Preview** — every ~400 ms, transcribe the *current phrase* with greedy decoding on
  `turbo`, type it into the target app wrapped in provisional markers. Disposable. Never
  feeds the final.
- **Final** — beam search, VAD-trimmed, `large-v3`. Runs **per chunk, rolling**, not once
  at the end. Replaces the provisional text for that chunk and commits it.

Normal streaming ASR is hard because the streamed text *is* the final text — once a word
is shown it can never be retracted, which forces commit-only-the-stable-prefix machinery
(LocalAgreement-2). Making the preview explicitly disposable removes that constraint and
all of that machinery with it.

The final run beats the preview even on the same audio, because it uses beam search
instead of greedy, gets clean VAD trimming, sees the complete phrase instead of a
truncated tail, has no chunk-boundary artifacts, and runs on the larger model.

### Why chunking is free: Whisper's 30 s windows are independent

Whisper's encoder consumes exactly 30 seconds of audio as one window. **Windows do not
see each other.** The only cross-window link is the decoder's text prompt — the
`condition_on_previous_text` flag — and this app sets it to `False` anyway (see *Trailing
"okay" / "so"* below).

So with those settings, audio from earlier in a dictation contributes *nothing* to how
later audio is transcribed. Re-transcribing the whole buffer is pure waste, not a quality
trade. This single fact is what the rest of the architecture rests on.

### Rolling finalization

Chunk boundaries are VAD silence gaps of at least `silence_gap_ms` (default 800 ms — see
*seam artifacts* below), with a forced cut at ~25 s if the speaker never pauses (staying
inside the 30 s window).

Once a chunk is closed by a silence gap, nothing later will improve it. So the
final-quality pass runs on it **immediately, while the user is still talking** — not at
key release. A silence gap is simultaneously the signal to close the chunk and the moment
the GPU has slack, since the preview has no new audio to chase.

At release, the only work left is the final pass on the last incomplete chunk — typically
5–15 s of audio.

| Dictation length | Whole-buffer final (rejected) | Rolling final (chosen) |
|---|---|---|
| 30 s | 1.8 s | ~0.5 s |
| 2 min | ~7 s | ~0.5 s |
| 10 min | **~36 s** | ~0.5 s |

**Release latency becomes constant regardless of dictation length.** Target is under 5 s;
this lands an order of magnitude below it.

Both models resident is ~4.7 GB of 8 GB, so there is no memory pressure — only compute
contention, which the silence gaps largely absorb. Rolling final passes are queued and run
one at a time; two chunks closing in quick succession must not run concurrently on the
same model instance.

### The VAD padding trap

Silero's `speech_pad_ms` defaults to **400**, widening every detected speech
region on both sides. That narrows every gap *between* regions by twice the
padding: a real 2.43 s pause measures as 1.63 s.

Chunk boundaries are decided from those gaps, so the padding silently doubles
the pause a speaker must leave before a chunk will close -- and a chunk that
never closes grows until the forced cut, dragging the preview pass with it.
Detection therefore runs with `speech_pad_ms=0`, and the cut is padded
explicitly by `CUT_PAD_S` (0.25 s) so the last word is not clipped.

### Measured cost of each pass

Transcription latency by audio length, on this machine:

| audio | turbo, greedy | large-v3, beam 5 | Silero VAD |
|---|---|---|---|
| 5 s | 0.28 s | 0.70 s | 0.007 s |
| 12 s | 0.33 s | 0.85 s | 0.017 s |
| 25 s | 0.43 s | **1.95 s** | 0.038 s |

Two things follow. Turbo is nearly flat with length while large-v3 scales
badly, which is what makes a bounded `max_chunk_s` matter. And a preview pass
costs ~0.3 s, so a 400 ms refresh saturates the GPU and starves the finals that
produce the real text -- the refresh interval must stay comfortably above the
pass cost. It is 700 ms.

`max_chunk_s` is 12 s rather than 25 s for the same reason: it bounds the worst
case for both passes.

### What chunking is *not* free of: seam artifacts

The independence argument above is about **accuracy** — no word is transcribed worse for
being in a shorter window. It does not extend to **punctuation and capitalisation**.

Whisper treats each window as a complete utterance, so every chunk tends to arrive
capitalised at the front and terminated with a period. Cut mid-sentence at a breath pause
and one spoken sentence becomes `I went to the store. And then I left.`

No post-hoc repair is attempted — deciding that a period "should not" be there is
rewriting, and rewriting is the thing this tool refuses to do. Chunks are joined with a
single space and otherwise left alone.

The mitigation is instead to **put the boundaries where sentences actually end**, by
raising `silence_gap_ms` until it clears normal mid-sentence pauses. 500 ms is likely too
low — ordinary hesitation pauses run 200–500 ms — so the default is **800 ms**, and this
is the first thing to tune against real speech. A longer gap costs nothing except slightly
later commits.

### The second, more important benefit

Because this app types in place (see *Output*), rolling finalization bounds the
**backspace blast radius**. Text behind the last silence gap is committed: markers
removed, never touched again. Only the current phrase is ever provisional, so the largest
rewrite is tens of characters.

Under a whole-buffer final, finishing a ten-minute dictation would mean rewriting ten
minutes of typed text in a single operation, where one desync — an autocorrect, a stray
keypress — shreds the document. Rolling commit makes that failure mode structurally
impossible. This matters more than the latency win.

## Models

`faster-whisper` accepts `large-v3` and `turbo` / `large-v3-turbo` as built-in shorthands.

- **Preview: `turbo`.** ~1.6 GB, 4 decoder layers vs `large-v3`'s 32.
- **Final: `turbo`**, on both AC and battery.

When both slots name the same weights the worker shares one model rather than
loading a second copy, halving VRAM and skipping a redundant load.

*Measured:* on 12 s chunks large-v3 costs 0.85 s against turbo's 0.33 s, and
end-to-end release latency differs by under 0.2 s. Switching `final_ac` back to
`faster-whisper-large-v3` costs little and buys accuracy; it is a one-line
change.

Settings expose `preview`, `final_ac` and `final_battery` separately. Power state is read
at key-down via `GetSystemPowerStatus`. Both finals are `large-v3` today, so this is
currently a no-op; the hook exists so battery behaviour can be tuned once measured.

*Note for later:* if `final_battery` is ever set to `turbo`, the turbo weights are already
resident as the preview model, so the switch costs zero reload. Setting the two finals to
differ in any other combination costs a ~5.7 s model load on each power transition.

### Model choice: staying on Whisper, deliberately

Whisper large-v3 is from Nov 2023 and no longer tops the HuggingFace Open ASR
Leaderboard. Evaluated and rejected:

- **Granite Speech 4.1 2B (5.33%), Canary-Qwen-2.5B (5.63%), Qwen3-ASR, Voxtral** — all
  decode through an actual LLM. This app explicitly rejects LLM rewriting (see below);
  an LLM *decoder* reintroduces exactly that risk one layer down, where it cannot be
  observed. These models normalise disfluencies as a side effect of decoding, which is
  the specific behaviour this tool exists to avoid. Wrong architecture, independent of
  WER.
- **Parakeet TDT 0.6B v3 (6.32%, far faster)** — genuinely attractive: as a
  frame-synchronous transducer it *structurally cannot* hallucinate text from silence,
  which would eliminate the trailing-"okay" problem by construction rather than by
  mitigation. Rejected for now anyway: the Windows path is third-party ONNX exports (no
  provenance improvement), hotword biasing is less mature, and latency is already a
  solved problem here.

Leaderboard WER is dominated by hard multi-speaker far-field sets (AMI, Earnings22). One
speaker on a close-talk mic puts every model in that table within noise of the others.
The lever that actually matters on this workload is vocabulary biasing, where Whisper's
`hotwords` is the most mature open implementation.

Decision: **stay on Whisper large-v3. Do not benchmark alternatives.** Revisit only if
trailing hallucination proves genuinely disruptive in daily use.

### Model provenance and location

The `faster-whisper` shorthands resolve to third-party CTranslate2 conversions:

- `large-v3` maps to `Systran/faster-whisper-large-v3` — SYSTRAN maintains
  `faster-whisper` and CTranslate2 itself, so this is the canonical publisher for the
  format.
- `turbo` maps to `mobiuslabsgmbh/faster-whisper-large-v3-turbo` — Mobius Labs GmbH, a
  real Berlin ML company, but an unrelated third party. There is no SYSTRAN turbo repo.

Both are mechanical conversions of OpenAI's MIT-licensed weights. To depend on neither,
convert from OpenAI's own repos:

```
ct2-transformers-converter --model openai/whisper-large-v3 \
  --output_dir %USERPROFILE%\ai-models\large-v3 \
  --copy_files tokenizer.json preprocessor_config.json --quantization float16
```

Conversion is deterministic, so hashing the output against a downloaded `model.bin` also
*verifies* that the third-party copy is unmodified.

**Storage:** `%USERPROFILE%\ai-models\`, not inside this repo — a multi-GB blob in a code
project is a `.gitignore` accident, and other projects transcribing video should not have
to reach into an app folder. Set a user-level `HF_HOME=%USERPROFILE%\ai-models\hf` so every
future HuggingFace download from any tool lands there instead of `~\.cache`.

Models are loaded **by absolute local path**, with `HF_HUB_OFFLINE=1` set, so the runtime
makes no network calls by construction.

## Configuration: files, no UI

No tray icon, no settings window. Three files in the app directory, edited directly.

```
config.json      settings
vocabulary.md    "- Hopf" ...   -> hotwords, in priority order
filler.md        "- um"   ...   -> regex-stripped from final text
```

Markdown parse rule: lines beginning `-` or `*` are entries; everything else is free-form
commentary, so the word lists document themselves. **Order in `vocabulary.md` is
priority** — the top N entries that fit the ~224-token prompt budget are used, which
answers prioritisation without needing a scoring scheme.

```jsonc
{
  "hotkey": {
    "keys": ["right_ctrl", "right_alt"],
    "hold_threshold_ms": 500
  },
  "audio": {
    "input_device": null,
    "open_on_keydown": true
  },
  "models": {
    "dir": "~/ai-models",
    "preview": "large-v3-turbo",
    "final_ac": "large-v3",
    "final_battery": "large-v3",
    "device": "cuda",
    "compute_type": "float16"
  },
  "chunking": {
    "silence_gap_ms": 800,
    "max_chunk_s": 25
  },
  "feedback": {
    "beep_on_arm": true,
    "beep_on_commit": false,
    "beep_on_error": true,
    "recording_dot": true
  },
  "preview": {
    "enabled": true,
    "refresh_ms": 400,
    "beam_size": 1
  },
  "final": {
    "beam_size": 5,
    "condition_on_previous_text": false,
    "vad_filter": true,
    "language": "en"
  },
  "vocabulary": { "file": "vocabulary.md", "mode": "hotwords", "max_tokens": 224 },
  "filler":     { "file": "filler.md", "enabled": true },
  "runtime": {
    "preload_on_start": false,
    "max_dictation_minutes": 60,
    "idle_unload_minutes_ac": 15,
    "idle_unload_minutes_battery": 15,
    "idle_exit_minutes_ac": 15,
    "idle_exit_minutes_battery": 15
  },
  "output": {
    "marker_open": "~",
    "marker_close": "~",
    "normalize_newlines_to_space": true
  },
  "logging": {
    "enabled": true,
    "dir": "logs",
    "format": "opus",
    "encoder": "ffmpeg",
    "bitrate_kbps": 24
  }
}
```

## Control surface

There is no UI, so the ways to observe and control a running instance have to be
deliberate rather than incidental.

**Single instance**, enforced with a named mutex. A second launch does not start a rival
hook; it becomes a client and sends a command to the running one over a named pipe:

| Command | Effect |
|---|---|
| `--quit` | Clean shutdown: unhook, kill worker, release mutex |
| `--reload` | Re-read `config.json` and both word lists |
| `--status` | Print worker state, model residency, idle timer |
| `--list-devices` | Enumerate `waveIn` devices with indices, for `audio.input_device` |

**Reload timing.** `vocabulary.md` and `filler.md` are read fresh at the start of every
dictation — they are tiny, and editing a word list should take effect on the next thing
you say, not on the next restart. `config.json` is re-read on `--reload`; hotkey changes
still need a restart because the hook is installed once.

**Errors.** With no tray icon and no console, a failure is otherwise silent. Everything
goes to `logs/app.log`, and a *fatal* startup error (no CUDA, model path missing,
malformed config) additionally raises a `MessageBox` via `user32` — the one case where
silence would leave the tool apparently working but dead.

**Malformed config never prevents startup.** Unparseable or invalid keys fall back to
defaults, loudly logged. Being unable to dictate because of a trailing comma is a worse
failure than running with a stale setting.

**Autostart:** a shortcut in the Startup folder invoking `pythonw.exe` (not `python.exe`,
which would leave a console window on screen). No Task Scheduler — that is only needed for
elevation, which is explicitly not wanted, and its default settings stop tasks on battery.

## Feedback: you must be able to tell it is listening

Push-to-talk with no UI and a possible 6–10 s cold start means pressing the keys, talking,
and seeing nothing. Indistinguishable from broken.

A short `winsound.Beep` on arm — stdlib, no dependency, played on a thread so it never
blocks the hook — closes that gap completely. Defaults: beep on arm, beep on error or
abort, silent on commit (the text appearing is its own confirmation).

### Recording indicator

**No persistent tray icon.** Nothing in the notification area while idle.

While recording, a small red dot appears beside the notification area, next to the
microphone-in-use glyph Windows already shows. Glanceable confirmation without a beep, and
without occupying tray space the other 99% of the time.

Implemented as a layered, click-through, topmost window — `user32` and `gdi32` via
`ctypes`, so the supervisor's zero-dependency rule holds. Position comes from the
`Shell_TrayWnd` / `TrayNotifyWnd` rect.

`Shell_NotifyIcon` was the obvious alternative and is rejected: Windows 11 hides new tray
icons in the overflow flyout by default, so an icon that only exists during recording
would frequently not be visible at all — which defeats the entire purpose.

Two implementation notes. The taskbar is itself topmost, so `HWND_TOPMOST` must be
re-asserted on a short timer while the dot is visible or it can fall behind; this only runs
during a dictation, so the cost is nil. And if the tray rect cannot be resolved — unusual
taskbar placement, multi-monitor oddities — fall back to the bottom-right corner just
above the taskbar rather than failing.

The supervisor already runs a message loop for `WH_KEYBOARD_LL`, so this adds a window,
not an architecture. Drawing must stay trivial: anything slow here stalls the hook.

## Hotkey: Right Ctrl + Right Alt, held

Push-to-talk, not toggle. Both keys held together is unambiguous — no application binds
that pair alone — while each key on its own keeps working normally.

Rejected: `Ctrl+Alt+Space` (three keys), `Alt+Space` (the Windows system menu shortcut,
and PowerToys Run's default), `Win+H` (reserved), bare `Shift+Space` (types in too many
applications).

### Deferred forwarding

A low-level keyboard hook (`WH_KEYBOARD_LL`) is required: it is the only mechanism that
can *suppress* a key, and suppression is what keeps the held modifiers from leaking into
the target app. (`RegisterHotKey` cannot express a modifier-only combo and gives no
key-up event, so it cannot do push-to-talk at all.)

The hook cannot know at Right-Ctrl-down whether Right Alt is coming, and forwarding it
eagerly would leave the target app believing Ctrl is held — turning dictated "hello" into
Ctrl+H, Ctrl+E, Ctrl+L. So: **swallow the first key and hold it pending.**

| Then | Action |
|---|---|
| The other combo key arrives — at any delay | Armed. Neither key was ever forwarded. |
| Any other key arrives first (e.g. `RCtrl+C`) | Flush the pending down-event, then pass the other key through. Normal shortcut. |
| Released with nothing else pressed | Inject a down+up pair so a genuine solo tap still registers. |

There is **no time limit and no window to press inside of.** A modifier held alone with
nothing else pressed has no effect in any application, so deferring it is invisible.

### While armed, all keys are suppressed

Once the combo is armed the hook swallows **every** key, not just the two being held. The
typing state machine's whole correctness rests on "N characters typed equals N characters
present," and a stray keypress during dictation breaks that invariant just as surely as an
auto-pairing editor would. You are already holding two keys with one hand; suppressing the
rest costs nothing real.

This also buys an abort: **Escape while armed discards the dictation**, backspaces out any
provisional text, and beeps. Without suppression there would be no free key to spend on
that.

### Timing

Recording starts at key-down. Release before `hold_threshold_ms` (500 ms) discards the
audio as a stray tap. The threshold gates *keeping* the recording, never *starting* it,
so no speech is lost to debouncing.

**Nothing is typed before the threshold passes.** The preview refresh (~400 ms) can
otherwise produce text for a press that is about to be discarded, leaving characters
behind from a dictation that never happened.

The microphone is **opened on key-down and closed on release** — not held open. An
always-open stream would buy ~50 ms of pre-roll against clipping the first phoneme, but a
standing open microphone is a liability that a key-triggered one is not, and the natural
human gap between pressing keys and starting to speak absorbs device startup anyway. If
first-word clipping shows up in practice, the fix is to start speaking a beat later, not
to change this.

## Output: type in place, mark what is provisional

Text is typed directly into the focused application via `SendInput` with
`KEYEVENTF_UNICODE`.

This injects a UTF-16 code unit directly rather than simulating a key — **no virtual key,
no scan code, no keyboard layout, no Shift, no Caps Lock.** Capitals, punctuation,
em-dashes and accented characters all go through identically, with no shift state to
manage. The whole string is batched into one `INPUT` array and issued in a single call,
so a sentence lands essentially instantly.

Clipboard paste was rejected: it is flaky in terminals (frequently needs a second
attempt), has a restore race because paste is asynchronous in some apps, and fights
clipboard managers.

Two hard rules:

- **Backspace must be a real `VK_BACK` keystroke**, not a Unicode event.
- **Never send Enter.** A newline injected into a terminal executes the line. Newlines are
  normalised to spaces; a transcript has no legitimate reason to contain one.

No focus or text-field detection. It is plain characters with no modifiers — with nothing
focused they go nowhere and nothing happens. A detection gate would only produce false
negatives that silently eat dictations already spoken.

**But the focused *window* is pinned.** `GetForegroundWindow` is captured when the combo
arms and rechecked before every `SendInput` batch. If it has changed — a click elsewhere,
an alert stealing focus — typing stops and the dictation aborts. This is not about
detecting a text field; it is about never backspacing into a document that is not the one
the earlier characters went into. That failure mode is silent and destructive, and the
check is two API calls.

### Provisional markers

Provisional text is wrapped in `~` ... `~`. On finalisation the markers and the
provisional text are backspaced away and the committed text typed in their place.

The markers are a **visual affordance only** — the program already knows exactly what it
typed, character for character, and needs no delimiters to compute the backspace count.

**Only the diverging tail is retyped.** Diff the old provisional text against the new,
find the longest common prefix, and backspace from there. Whisper previews usually revise
only the last word or two, so a typical update is 3–8 backspaces rather than 60. Far less
flicker, and far less exposure to desync.

**Previews can be limited to the start of a dictation** (`preview.window_s`, off by
default). Every preview is typed, backspaced and typed again as the commit, so each
sentence costs roughly three times its length in keystrokes. A target that redraws
slowly — a terminal running a TUI is the case that prompted this — cannot keep up, and
the backlog grows for as long as the dictation runs. Past the window nothing provisional
is typed: each chunk lands once, and the only characters ever deleted are the markers
after it. The opening seconds keep their preview because that is the only evidence the
thing is working. A preview still on screen when the window closes is left for its
commit to replace rather than removed, which would be one more delete. The worker stops
running preview passes at the same point, which also returns that GPU time to the finals.

With no preview to watch, the markers have to say more, so past the window they have
two states. `~~` means it is recording and nothing is waiting. `~|~` means speech is
buffered whose text has not been typed yet. The worker reports that state as it changes
(`PENDING`), and every `COMMIT` carries it too, so the text and the marker that follows
it go out as one edit instead of a marker typed and then corrected. `~|~` behaves like
provisional text rather than like the empty pair: it survives the keys coming up, because
the text it promises is still on its way, and comes down on `DONE` — which also covers
the case where the buffered audio turns out to hold nothing worth typing.

*Why `~`:* the whole scheme rests on the invariant that N characters typed equals N
characters present, and the only mechanism that breaks it is **auto-pairing** — an editor
inserting a character that was never typed. VS Code's default auto-close set is `{}`,
`[]`, `()`, `""`, `''` and backtick, plus `<` in HTML/JSX/XML where `<>` is a fragment and
gets a `</>` added. Nothing auto-pairs `~`, in any editor or file type.

Secondary reasons: markdown strikethrough needs *two* tildes, so a single one is inert in
Obsidian; `>` at line start would be a blockquote; and one character backspaces more
cheaply than two. If it ever does cause trouble, a non-ASCII marker (`›`, `¦`) has zero
syntactic meaning in any language, markup, or shell. Configurable either way.

*Known limit:* rich editors with live autocorrect (Word especially, Google Docs somewhat)
silently capitalise and curl quotes *after* typing, which changes the character count out
from under the invariant. Plain-text targets — terminals, VS Code, Obsidian, browser
inputs — are exact. Accepted. One accidental protection: terminal readline will not let
backspace eat the prompt, so overrun is harmless there.

### Elevation

Runs **non-elevated**. Windows UIPI blocks a Medium-integrity process from sending input
to a High-integrity (elevated) window, so dictation will not work in an admin PowerShell,
Task Manager, or a UAC-elevated installer. Those are rare enough to accept.

If it ever becomes annoying, the fix is a Task Scheduler entry with "run with highest
privileges" launched from a shortcut — a launch-configuration change, not an architecture
change.

## Resource lifecycle

The program is idle almost all of the time. It should cost almost nothing while idle.

Approximate footprint if everything is imported and loaded eagerly:

| | |
|---|---|
| Bare Python + `ctypes` hook | ~25 MB RAM |
| plus `numpy`, `sounddevice` | ~75 MB RAM |
| plus `faster-whisper`, `ctranslate2`, `onnxruntime` imported | ~300–400 MB RAM |
| Model resident | **4.7 GB VRAM** |

Holding 4.7 GB of an 8 GB card permanently, for a program that is idle 99% of the time,
is the part that actually bites.

Python cannot un-import. Once `faster_whisper` has been imported its ~300 MB of RSS stays
for the process lifetime; freeing the model returns the VRAM but not the RAM. A
single-process design therefore cannot have both a fast second dictation and a small idle
footprint.

**So the app is two processes.** This is a requirement, not an optimisation: 300 MB
resident 24/7 for a program in use a few minutes a day is not an acceptable trade.

### Supervisor — always resident, zero third-party dependencies

Holds the keyboard hook, the audio capture, the typing state machine, and the worker's
lifecycle. **Standard library and `ctypes` only** — no `numpy`, no `sounddevice`, no ML
imports, ever. Target ~25 MB RSS.

Audio capture is `ctypes` against **`winmm.dll`** (`waveInOpen` / `waveInAddBuffer` /
`waveInStart`), not `sounddevice`. PortAudio would drag `numpy` in and take the supervisor
to ~75 MB. The MME API is roughly 150 lines of `ctypes` and its higher buffer latency
(~30–50 ms) is irrelevant for dictation. Capture must live here, not in the worker,
because recording has to start at key-down — several seconds before a cold worker is
ready.

The consequence is worth stating plainly: **the permanently resident process contains no
third-party code at all.** That is simultaneously the smallest possible idle footprint and
the smallest possible attack surface for the component that sees every keystroke.

### Worker — spawned on demand, killed on idle

Holds `faster-whisper`, CTranslate2, ONNX Runtime, the Silero VAD, and both models. Owns
chunk-boundary detection, both transcription passes, log writing, and the detached
`ffmpeg` encode. Killed outright when idle, returning **all** of its RAM and VRAM.

**"Idle" means no dictation has happened for that long — it is not a limit on how long a
single dictation may run.** A dictation can continue indefinitely; rolling finalization
means a 45-minute one costs no more at release than a 30-second one. The only cap is
`runtime.max_dictation_minutes` (default 60), which exists purely so a stuck key cannot
record forever, and hitting it commits everything transcribed so far rather than
discarding it.

Eviction timing is settable per power state, mirroring the model settings:

| Knob | Default | Effect |
|---|---|---|
| `runtime.idle_unload_minutes_ac` | 15 | On mains: drop models from VRAM, keep the process |
| `runtime.idle_unload_minutes_battery` | 15 | On battery: same |
| `runtime.idle_exit_minutes_ac` | 15 | On mains: kill the worker entirely |
| `runtime.idle_exit_minutes_battery` | 15 | On battery: same |

All four are 15 to start, which means a plain single-stage kill on both power states. Two
axes are available once real use says which way to tune:

- **Unload vs exit.** Splitting them (e.g. 15 / 60) keeps the process alive after the VRAM
  is freed, trading ~300 MB of resident RAM during a working session for a much faster
  second dictation. Worth it only if the reboot proves annoying.
- **Mains vs battery.** Battery almost certainly wants shorter timers than mains; that is
  the whole reason the axis exists. Left equal until measured rather than guessed.

Power state is read by the idle timer itself, not cached from the last dictation, so
unplugging mid-session takes effect immediately.

### IPC

`subprocess.Popen` with length-prefixed messages over stdin/stdout; stderr for logs. No
sockets, no third-party IPC.

- Supervisor to worker: raw 16 kHz mono PCM (32 KB/s — trivial for a pipe), plus
  end-of-dictation.
- Worker to supervisor: `PREVIEW <text>`, `COMMIT <chunk_id> <text>`.

The supervisor owns the typing state machine and diffs incoming text against what it last
typed, because it is the only process that knows character-for-character what is on
screen. The worker only ever sends text.

### The cost, stated honestly

First press after an eviction: interpreter start plus ML imports (~2–4 s) plus cold model
load (5.7 s) — so roughly **6–10 s before provisional text appears**. No audio is lost;
the supervisor is recording from key-down and buffers into the pipe once the worker is up
(10 s of audio is 320 KB). The final transcript is unaffected. You simply see nothing for
the first several seconds of the first dictation after a gap.

`runtime.preload_on_start` exists for the opposite trade.

**Crash handling:** if the worker dies mid-dictation the supervisor must backspace away
whatever provisional text it has typed, rather than leaving `~half a sentence~` in the
document. The supervisor knows the exact count, so this is reliable.

*A note on the battery argument:* resident RAM that is never touched costs close to
nothing beyond DRAM refresh, so the RAM-to-battery link is weak. Holding 4.7 GB of VRAM is
the real drain — it keeps the GPU memory controller out of its lowest power state. The
eviction policy above addresses the one that actually matters, and the process split
addresses the RAM on principle.

*Not doing:* a C or Rust supervisor would idle at ~3 MB instead of ~25 MB. Not worth the
complexity jump for 20 MB.

## Security

The threat model is narrow but real: this program is a global keyboard hook with
microphone access and a several-GB dependency tree.

**Be honest about the hook.** `WH_KEYBOARD_LL` receives *every* keystroke system-wide —
there is no way to subscribe to only two keys. The process genuinely does see everything
typed; it simply discards it. That is a code-discipline property, not an architectural
one, so the callback must be a handful of auditable lines: compare the virtual key
against exactly two values, return immediately otherwise, never buffer, never log.

*(The alternative — polling `GetAsyncKeyState` on just those two keys — reads nothing
else, but cannot suppress keys, which this design requires. Rejected.)*

Hardening, in descending order of value:

1. **Block outbound network at the firewall.** The app has zero legitimate network need
   once the model is local. One Windows Firewall outbound-block rule scoped to the venv's
   `python.exe` covers every dependency at once. Note this is what actually does the
   security work: the supervisor/worker split exists for resource reasons (see *Resource
   lifecycle*) and hands over the isolation as a side effect, but a subprocess boundary on
   its own stops no exfiltration.
2. **Pin dependencies with hashes.** `pip install --require-hashes` against a locked
   requirements file. This is the real defence against supply-chain compromise: a
   republished version cannot silently change what gets installed.
3. **`HF_HUB_OFFLINE=1` plus absolute local model paths.** No network calls by
   construction, independent of the firewall rule.
4. **Do not install `torch`.** `faster-whisper` does not need it — the bundled Silero VAD
   runs on `onnxruntime`. Skipping it removes an enormous amount of surface.
5. Non-elevated (above).

### Dependency budget

Split by process, which is what keeps the resident half clean.

**Supervisor — zero third-party packages.**

| Need | Dependency |
|---|---|
| Keyboard hook, `SendInput`, power state | `ctypes` -> `user32.dll`, `kernel32.dll` |
| Audio capture | `ctypes` -> `winmm.dll` |
| Config, IPC, everything else | stdlib |

No `keyboard`, no `pynput`, no `sounddevice`, no `numpy`.

**Worker — where all third-party code lives.**

| Need | Dependency |
|---|---|
| ASR + VAD | `faster-whisper` -> `ctranslate2`, `tokenizers`, `onnxruntime`, `huggingface-hub`, `av` |
| Arrays | `numpy` |
| Opus encode | `ffmpeg.exe` (detached subprocess, not a package) |

Two notes: feeding `faster-whisper` a float32 array directly means `av` is never invoked
at runtime, and the Silero VAD arrives bundled with `faster-whisper`, so VAD costs no
additional dependency.

**Opus encoding is a detached `ffmpeg` subprocess, not a library.** `ffmpeg` is a
standalone binary, so it imports nothing, holds no RAM in the resident process, and lives
only for the ~100 ms it takes to encode. It runs *after* the text is typed, so it is off
the hot path entirely. If `ffmpeg` is absent, keep the WAV and carry on.

A nightly batch job was considered and rejected: this is a laptop, and it is asleep at
night. Per-dictation encoding needs no scheduler at all.

## Tailoring: word lists, not an LLM

- **`vocabulary.md`** — terms said often (project names, jargon, proper nouns), as a
  bullet list in priority order. Fed to Whisper as `hotwords` / `initial_prompt`. This is
  the mechanism that fixed "Hopf" (which YouTube captions rendered as "hub") in the
  a lecture-transcript corpus. Capped at ~224 tokens; the top entries that fit are used.
- **`filler.md`** — filler words ("um", "uh") stripped from the final text by
  deterministic regex post-processing, **not** by the model. Whisper's `suppress_tokens`
  is token-level and too blunt. Deterministic removal is reviewable and predictable.

**Numbers and units are left exactly as Whisper renders them.** It is inconsistent about
"twenty five" versus "25", and normalising would be deterministic rather than semantic —
so it would not breach the verbatim rule the way LLM rewriting does. It is still a
transformation nobody asked for, and the filler list already covers the one class of
edit that is genuinely wanted. Decided, not deferred.

### Explicitly not doing: LLM rewriting

Whisper Flow passes output through an LLM to clean it up. Tempting — it would turn
"let's meet Thursday, wait, no, Friday" into "let's meet Friday" — but rejected, for a
reason that matters more than that example:

> Sometimes there is a lot of reasoning about *why* you want to do something, and then you
> reverse it. You don't want to lose the reasoning. An LLM will compress it heavily, which
> is not good.

Once an LLM is allowed to rewrite, it can rewrite things that shouldn't be touched, and
the source is gone. A silent wrong "correction" is worse than a visible transcription
error, because you never know which words were changed. Dictation should be verbatim.

Accepting that "Thursday, wait, no, Friday" stays verbatim is the correct trade. Tuning
the speech-to-text settings directly gets most of the benefit with none of the risk.

**This is also why the LLM-decoder ASR models were rejected above.** The same principle
applies one layer down, where it would be invisible.

## Hallucination on silence

### Trailing "okay" / "so"

Claude Code's dictation frequently appends a stray "okay" or "so" — hallucination on
trailing silence and breath. Whisper has the same failure mode (it produced repetition
loops in a lecture-transcript corpus), so going local does not fix it for free. Mitigations:

1. VAD-trim trailing silence before the audio reaches the model — biggest win.
2. `condition_on_previous_text=False` — free here, since chunks are independent by design.
3. Drop a final segment that is a single known filler with weak log-probability.

### Silence must never reach the model

The more dangerous case is a recording that is *entirely* silence — keys held by accident,
a muted or dead microphone, a pocket press that clears the 500 ms threshold. Fed pure
silence, Whisper does not return nothing; it very reliably emits training-set residue like
`Thank you.` or `Thanks for watching!`. Typed straight into whatever had focus.

**Rule: if VAD finds no speech in a chunk, that chunk produces no text and is never sent
to the model.** If a whole dictation contains no speech, nothing is typed and the error
beep sounds. This is a guard, not a heuristic — it runs before transcription, so there is
no output to second-guess.

## Abort conditions

Any of these stops a dictation, backspaces away provisional text, and beeps:

| Trigger | Why |
|---|---|
| `Escape` while armed | Deliberate cancel |
| Foreground window changed | Never backspace into a different document |
| Worker crash or pipe EOF | No further text is coming |
| Audio device lost mid-recording | Unplugged headset; the rest of the audio is gone |
| `WM_POWERBROADCAST` suspend | Hook and audio device are about to be torn down |

Provisional text is always removed on abort. Committed text is left alone — it is already
final, and silently deleting text the user watched settle would be worse than leaving a
partial dictation.

A UAC prompt appearing mid-dictation switches to the secure desktop; the foreground-window
check catches this as an ordinary focus change.

## Logging

Every dictation is kept, for possible fine-tuning on this specific voice later.

```
logs/2026-08-28/143022-a1b2c3.opus     audio
logs/2026-08-28/143022-a1b2c3.json     final transcript + segment timestamps + settings
```

The JSON keeps per-segment `start`/`end` so audio and text stay aligned — a requirement
for any future training use.

Audio is captured to a temporary WAV and converted immediately after the dictation is
typed, by a detached `ffmpeg` subprocess that deletes the WAV on success (see
*Dependency budget*).

Target: **Opus, ~24 kbps VBR, 16 kHz mono** (Whisper resamples to 16 kHz anyway). At
2 h/day that is roughly 20 MB/day, ~8 GB/year; 16 kbps roughly halves it. Lossless FLAC
would be ~21 GB/year — too much.
*Caveat:* lossy compression bakes in codec artifacts. Opus at this bitrate is widely used
for ASR training and should be fine, but it is a real trade against storage.

**Retention is unlimited by design.** ~8 GB/year is affordable on a modern disk, and the
whole point of keeping logs is a future fine-tune, which wants the oldest recordings as
much as the newest. Nothing prunes them; revisit if the directory ever becomes a problem.

*The implication, stated once:* every dictation is kept forever, in audio and in text.
Anything spoken into a password field, a private message, or a credential prompt lands in
`logs/` in plain form. `logging.enabled: false` is the switch; there is deliberately no
per-dictation prompt, because a prompt on every use would be worse than the exposure.
`logs/` and any model directory are gitignored.

**Intermediate preview transcripts are discarded.** Keeping every streamed hypothesis
would bloat the logs enormously, and to interpret one you would also need the exact audio
offsets it was produced from. It would show how much the first pass drifts, but that
doesn't feed back into anything actionable. Not worth the complexity or the volume.

## First-run setup

One-time, and none of it is code. Worth writing down because the plan otherwise assumes a
machine that is already in this state.

Models live in `%USERPROFILE%\ai-models\` as flat, self-contained directories loaded by
absolute path — no cache indirection, no snapshot hashes:

```
%USERPROFILE%\ai-models\faster-whisper-large-v3\        final pass
%USERPROFILE%\ai-models\faster-whisper-large-v3-turbo\  preview pass
```

1. **Done.** `large-v3` copied out of the HuggingFace cache (2.9 GB, no re-download).
2. **Done.** `large-v3-turbo` fetched from `mobiuslabsgmbh`.
3. **Done.** venv created; `faster-whisper` + `numpy` installed, no `torch`.
4. **Done.** `ffmpeg` 7.1.1 already on `PATH`.
5. Optionally set a user-level `HF_HOME=%USERPROFILE%\ai-models\hf` so future
   HuggingFace downloads from any tool land there rather than `~\.cache`.
6. The original cache copy at
   `%USERPROFILE%\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3`
   is now redundant and can be deleted to reclaim 2.9 GB.
7. Add the Windows Firewall outbound-block rule for the venv's `python.exe`.
8. Add the Startup-folder shortcut to `pythonw.exe app.py`.

## Build order

1. **Supervisor: hook + typing.** Hotkey with deferred forwarding, `SendInput` Unicode
   typing, marker wrap/unwrap, diff-based retyping. No ASR — feed it canned strings. All
   the Windows-integration risk lives here, so it goes first and gets proven alone.
2. **Supervisor: audio capture.** `winmm.dll` via `ctypes`, writing a WAV. Still no ASR.
   Confirms the resident half is complete and dependency-free before anything heavy
   exists.
3. **Worker + IPC.** Spawn, pipe protocol, lifecycle and idle kill, crash cleanup. Wire it
   to a single whole-buffer `large-v3` pass. First end-to-end dictation. Confirm the
   latency feels right in real use.
4. **Chunking + rolling finalization.** VAD-gap chunk boundaries, per-chunk final pass
   during silence, progressive commit. This is what makes long dictations work.
5. **Preview loop.** Turbo on the current chunk at ~400 ms. Pure addition; cannot break
   the core path. The payoff of keeping the two passes decoupled.
6. **Word lists.** Vocabulary hotwords + deterministic filler stripping.
7. **Logging** plus the detached `ffmpeg` encode.
8. **Hardening.** Firewall rule, hashed requirements lock, offline environment variables.

Steps 3–5 are the substance and are individually small. The long tail is Windows
integration: autostart, mic device selection, and keeping the held keys from leaking into
the target app.

## Open questions

- Turbo's preview latency is unmeasured, but bounded: the preview only ever sees the
  current chunk (at most ~25 s), never the whole buffer. Not worth benchmarking before
  building.
- Whether a 15-minute single-stage kill is the right default, on either power state, or
  whether the 6–10 s worker reboot is annoying enough to warrant splitting the unload
  timers from the exit timers. Needs real use to answer.
- Whether `winmm.dll` capture via `ctypes` is as straightforward as expected. It is the one
  place the supervisor's zero-dependency rule costs real effort; if it turns out badly,
  the fallback is `sounddevice` at ~75 MB idle rather than ~25 MB.
- GPU contention between the ~400 ms preview loop and a rolling final pass, when a chunk
  closes mid-speech rather than during a long silence.
- Battery behaviour is unmeasured. The GPU now works continuously while the keys are held
  rather than in one burst.
- **`silence_gap_ms` = 800 is a guess.** It is the single most consequential unmeasured
  number in the plan: too low and sentences get cut mid-thought, taking a spurious capital
  and period at the seam; too high and commits lag. Tune it first, against real speech.
- Whether `hotwords` or `initial_prompt` is the better vocabulary mechanism. They occupy
  the same ~224-token budget and cannot both be used; `mode` in the config selects one.
