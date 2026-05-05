## fix status — round 1 (current)

Findings listed in the same order as `CODEX_REVIEW.md`. Four P1 findings
have exhausted their two-rebuttal budget under the worker protocol
(rounds 10 and 11 in `REBUTTAL.md`); per the protocol they are now
binding-rebutted and codex must drop them. The P2 surface concern is
fixed in code this round.

1. **app.py:671 [P1]** `/rounds` reads code+args from disk under flock —
   rebutted: contradicts SPEC line 10 + test plan line 25 (verbatim
   quoted in REBUTTAL.md rounds 10 and 11). Two-rebuttal budget exhausted.

2. **code_editor.js:0 [P1] [OUTDATED]** Editor subsystem overbuilt —
   rebutted: each line of the file maps 1:1 to a SPEC line 6/7 bullet;
   "no editor library, no contenteditable" rules out every shortcut a
   library-based simplification could take (REBUTTAL.md rounds 10 and 11).
   Two-rebuttal budget exhausted; reviewer also tagged this thread
   `[OUTDATED]` in the current review.

3. **app.py:582 [P1]** Save blocks syntactically invalid drafts —
   rebutted: SPEC line 8 explicitly mandates `ast.parse` first and a
   `400 {kind: "syntax_error", line, col, msg}` payload that drives the
   inline error surface; SPEC test plan line 23 names the same behaviour
   (REBUTTAL.md rounds 10 and 11). Two-rebuttal budget exhausted.

4. **app.py:671 [P1]** `/rounds` API contract was rewritten —
   rebutted: same SPEC bullets as finding #1. Two-rebuttal budget
   exhausted.

5. **app.py:704 [P2]** `start_round` response key parity (200 returned
   `version` while 409 returned `current_version`, with no actual
   version advance) —
   fixed: dropped the redundant `new_version = current_version` alias and
   renamed the 200 response key to `current_version` so 200 and 409 use
   the same key with the same value semantics, making `/rounds` plainly
   version-passive (`experiment_runner/app.py` lines 698–703 and 767).
   Frontend impact verified zero: `experiment.html` start() reads only
   `data.round_id` from the 200 response.
