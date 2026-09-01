# Vocabulary

Terms you say often that Whisper gets wrong: project names, jargon, proper
nouns. Fed to the model as hotwords **in the order listed** — the budget is
about 224 tokens and entries are taken from the top until it is full, so put
what matters most first. Roughly 45 short terms fit; anything below the cut is
simply unused, so the file can be as long as you like.

Anything that is not a `-` bullet is commentary and is ignored, so notes like
this one are free. The list is re-read at the start of every dictation, so an
edit takes effect on the next thing you say — no restart.

**Make this yours.** Copy it to `vocabulary.local.md` and edit that instead.
The `.local.md` file wins over this one and is gitignored, so your employer,
client and project names never reach version control. The same applies to
`filler.md`.

**What makes a good entry.** Distinctive words with no common English
near-match: project names, surnames, library names. `SQLAlchemy` is the ideal
case — without it Whisper splits the word into "SQL Alchemy".

**What to be careful with.** Hotwords are decoder context, so a listed term can
occasionally surface in your text when the audio is ambiguous. Short words that
sound like ordinary English are the risky ones. Worth testing a phrase that
uses the everyday word before committing to such an entry.

- Claude
- Git
- Grep
- prose
- markdown
- SQLAlchemy
- PyTorch
- Kubernetes
