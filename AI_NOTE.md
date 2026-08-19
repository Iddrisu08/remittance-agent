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

**One thing it got wrong that I caught.** It built the screen as a debug console — presets,
fault-injection controls, budget overrides, an idempotency-key field, a raw ledger table —
when the brief asked for a small screen where a person types a goal, watches the steps, and
sees the result. It had optimised for demonstrating its own implementation rather than for
the person using it. I had it cut the request-shaping controls back out to the API where they
belong, and made the steps appear as the loop reaches them instead of rendering all seven up
front.
