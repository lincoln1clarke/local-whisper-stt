# Speech fixtures

Generated with Windows SAPI so the tests have a known ground truth without
anyone speaking into a microphone. 16 kHz, 16-bit, mono -- the format the
recorder produces and Whisper expects.

| File | Spoken text |
|---|---|
| `simple.wav` | The quick brown fox jumps over the lazy dog. |
| `two_part.wav` | First sentence here. *(1.5 s break)* Second sentence follows. |
| `numbers.wav` | I need twenty five widgets by Friday afternoon. |

Regenerate with `tests/fixtures/generate.ps1`.
