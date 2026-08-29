# Vocabulary

Terms you say often that Whisper gets wrong: project names, jargon, proper
nouns. Fed to the model as hotwords **in the order listed** — the budget is
about 224 tokens and entries are taken from the top until it is full, so put
what matters most first.

Anything that is not a `-` bullet is commentary and is ignored, so notes like
this one are free.

**Deliberately empty.** Hotwords are decoder context, and Whisper will sometimes
emit them straight into its output when the audio is ambiguous — so a term in
here can appear in your text even when you did not say it. Only add words that
are genuinely misrecognised often enough to be worth that trade. The preview
pass ignores this list entirely (`vocabulary.apply_to_preview`); only the final
pass is biased.

Add entries below, one per line:

- Claude
- Git
- SQLAlchemy