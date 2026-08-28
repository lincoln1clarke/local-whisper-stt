# Filler words

Removed from the final text by plain regex, not by the model — deterministic and
reviewable. Matching is case-insensitive and respects word boundaries, so "um"
never touches "umbrella". A comma immediately after a filler is removed with it.

Multi-word entries work and win over shorter overlapping ones.

- um
- uh
- erm
- uhh
- hmm
