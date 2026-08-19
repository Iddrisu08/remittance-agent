# Decisions

**How I bounded the loop.** Two limits — a step count and a credit cap — checked **before**
each step runs, never after, so spend can't exceed the cap even transiently. Both are copied
onto the run row at creation, so changing a limit later can't retroactively rewrite whether
an old run was legitimately bounded. Hitting a limit is its **own terminal state**
(`max_steps_exceeded` / `max_credits_exceeded`), not `failed` — a failure means something went
wrong; a bound means the run did exactly what it was told. The counter tracks *attempts*, not
successes, and increments before the tool runs — a tool that fails still consumed a turn, or
a failing run could spin inside its own bound forever.

**How I handled the mid-run failure.** The run ends `failed`. Completed steps keep their
charges and their cached outputs; the failed step is **charged nothing** and stores no partial
artifact. The rule is *charge for work that produced a usable artifact*. Recovery is
`POST /runs/{id}/resume`: cached steps replay at zero credits and only the failed step onward
re-executes, so a failure that charged 2.750 resumes to **5.400 — exactly what a clean run
costs**. There's no special "free replay" path; replays are free because the code never
reaches `charge()` for them. Replayed steps also don't consume the step budget, or a run that
failed late could never resume. The artifact is committed *before* the charge, so a crash
between the two under-charges for reusable work rather than billing for nothing.

**How I counted credits.** Integer milli-credits (1 credit = 1000 mc) end to end — no float in
the arithmetic and none in the display (`5400` → `"5.400"` by integer division). `credits_used`
**is not a column**; the only way to get a total is `SUM(amount_mc)` over an append-only
ledger, so nothing can drift from the rows, and the loop re-reads that sum every iteration
rather than carrying a running total. Each row has a deterministic key
(`step:5:execute_transfer:attempt:1:charge`) under a `UNIQUE` constraint, so a charge retried
after a crash is dropped — while a legitimate retry still charges, because a resume increments
the attempt number. Money is the same: cents, pesewas, and basis points, all `int`, converted
with one exact multiply and one floor division.

**The trade-off I'm least sure about — making the failed step free.** It's clearly right for a
provider timeout: the caller got nothing, so billing them makes retrying punitive. But a real
`execute_transfer` that fails partway has still consumed a provider call, and a failure
doesn't consume the credit budget the way a success does — so a client could burn expensive
calls indefinitely at no charge. Charging partial cost on failure is more defensible
commercially, but it's harder to explain and it muddies resume: you'd pay twice for one
transfer that succeeded once. I chose the rule I can state in a sentence and that makes resume
provably never cost more than not failing. If this were real, I'd have the failed step record
what it actually consumed and settle that separately from the step rate card.
