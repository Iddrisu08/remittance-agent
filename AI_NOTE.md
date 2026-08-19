# AI note

**What I used it for.** Implementation, mostly — turning decisions I'd already made into
FastAPI/SQLModel code, writing the tests, and drafting the docs. I directed the design and
reviewed the output rather than accepting a finished thing.

**What I accepted as it was.** The mechanical parts: SQLModel/SQLAlchemy wiring, the SQLite
pragmas, response serialisation, most of the test scaffolding, and the mocked FX and receipt
fixtures. Where the code was a faithful translation of a decision I'd already made, I let it
stand.

**Where I overrode it, and why.** The important one was failure handling. AI put it to me as a
binary — bill for the completed work and land the run in `failed`, or refund the whole run
atomically — and framed recovery as a separate either/or: retry from scratch, or resume. I
rejected both framings. If completed steps cache their outputs, a rerun reuses them and the
customer isn't billed a second time for work already done: it saves them money and saves us
the recompute. That one decision collapsed the failure question and the recovery question
into a single mechanism, and it's why a resumed run costs exactly zero for cached steps and
lands on the same total as a run that never failed.

The related call was pricing: **charge for work that produced a usable artifact, don't charge
for work that produced nothing** — dynamic, rather than a binary charge-or-refund keyed on
failure. Also mine, against simpler defaults it offered: per-step costs that vary by tool
instead of a flat rate; an append-only ledger instead of a mutable counter, so there's an
audit trail; a credit budget the customer sets instead of a fixed constant; and SQLite rather
than in-memory state, so idempotency survives a restart.

**One thing it got wrong that I caught.** The brief says the screen should let a person watch
the steps appear. The AI drew all seven up front as a greyed-out checklist that filled itself
in — which looks like progress but isn't, since the whole plan was on screen before the agent
had done anything. I had it draw each step only once the loop actually reaches it, so the list
grows as the run executes. That's what's in the recording: a few seconds in you see three
steps, not seven.

I did also have it strip the screen back to just the goal, the steps and the result at one
point. I reverted that. For this audience the fuller screen is the right call — the ledger
rows, the idempotency keys and the budget meters are the evidence for the three things the
brief says it cares about, and burying them behind curl would have made the run loop harder to
judge, not easier.
