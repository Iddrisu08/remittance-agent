# Remittance Agent — a slice of an agent's run loop

A goal in plain language ("Send $500 to my mom in Accra at the best rate") is executed as a
sequence of steps against mocked tools. The agent tracks progress, charges credits per step,
and returns the run's status and output.

**Stack:** Python · FastAPI · SQLModel + SQLite · vanilla HTML/JS.

```bash
./run.sh      # → http://127.0.0.1:8000
./test.sh     # 41 tests
```

`run.sh` creates the virtualenv, installs dependencies and starts the server. The database
schema is created on startup — there is no migration step.

> ### 👉 Start with [WALKTHROUGH.md](WALKTHROUGH.md)
> It runs the three required scenarios against the endpoint, shows the real output of each,
> and explains the mechanism — how the loop is bounded and how a retry is recognised.

---

## The three things the brief asks about

### A run must not loop or run away

Two limits, both checked **before** a step executes, both snapshotted onto the run row so a
config change can't retroactively rewrite an old verdict.

Hitting a limit is **its own end state, not a failure**:

```
completed | failed | max_steps_exceeded | max_credits_exceeded
```

A failure means something went wrong; a bound means the run did exactly what it was told.
The UI colours them differently — red for a fault, amber for a deliberate stop. Resuming a
bounded run is refused: retrying under the same limit would make the limit advisory.

→ `app/runner.py`, the two guards at the top of the step loop

### A step can fail partway

The run ends `failed`. Steps that finished **keep their charges and their cached outputs**;
the failed step is **charged nothing** and stores no partial artifact.

The call: *charge for work that produced a usable artifact.* Completed steps left outputs a
later attempt can reuse, so the caller got value. The failed step produced nothing.

Recovery is `POST /runs/{id}/resume` — cached steps replay at zero credits and only the
failed step onward re-executes. A failure that costs 2.750 resumes to a total of **5.400 —
exactly what a clean run costs.** Recovering never costs more than not failing.

→ `app/runner.py` (`execute_run`, `prepare_resume`)

### Cost has to be exact

Credits are integer **milli-credits** (1 credit = 1000 mc). No float touches the arithmetic,
and none touches the display either — `5400` renders as `"5.400"` by integer division.

`credits_used` **is not a column.** The only way to obtain a total is:

```sql
SELECT COALESCE(SUM(amount_mc), 0) FROM ledger WHERE run_id = ?
```

The ledger is append-only; a correction is a new negative row. Every entry carries a
deterministic key (`step:5:execute_transfer:attempt:1:charge`) under a `UNIQUE` constraint,
so a charge retried after a crash collides and is discarded — while a *legitimate* retry
still charges, because a resume increments the attempt number.

Money is the same discipline: USD cents, GHS pesewas, FX in basis points, all `int`.
Conversion is one exact multiply and one floor division, rounding down, with the remainder
returned rather than swallowed.

→ `app/db.py` (`credits_spent`, `charge`) · `app/pricing.py` · `app/tools.py`

---

## Idempotency

`POST /runs` accepts an `Idempotency-Key` header. Without one, the key is a SHA-256 of the
canonicalised request body — so an identical resubmission is recognised either way.

| Case | Response |
|---|---|
| New request | `201` |
| Duplicate of a finished run | `200` + `Idempotency-Replayed: true` |
| Duplicate of an **in-flight** run | `202` + `Retry-After` — never a second execution |
| Same key, **different** body | `422 idempotency_key_reuse` |

Enforcement is a `UNIQUE` constraint. The handler attempts the INSERT and interprets the
violation rather than checking first — a check-then-insert passes every single-threaded test
and double-charges the moment two requests arrive together, which is exactly when
idempotency is supposed to help. Six concurrent identical requests produce one `201`, five
`202`s, one run, one set of charges.

→ `app/api.py` (`create_run`)

---

## The pipeline

| # | Step | Cost | |
|---|---|---|---|
| 0 | `plan` | 0.250 | parse the goal into a structured intent |
| 1 | `validate_recipient` | 0.400 | KYC lookup at the payout partner |
| 2 | `fetch_fx_rates` | 1.200 | fan-out to 3 liquidity providers |
| 3 | `select_best_rate` | 0.100 | pick the highest **net** payout |
| 4 | `lock_quote` | 0.800 | reserve the rate for a TTL |
| 5 | `execute_transfer` | 2.500 | move the money |
| 6 | `write_receipt` | 0.150 | persist the final artifact |

A clean run costs **5.400** credits against a default budget of 6.000.

Costs vary 25× and the expensive steps sit late, which is what makes the failure and resume
paths worth looking at. The providers are tuned so the **best headline rate is not the best
deal** — one quotes the top rate and loses on its fee — so `select_best_rate` maximises net
payout, not `rate_bps`.

---

## API

```
POST /runs                 create + start (idempotent)
GET  /runs/{id}            status, steps, ledger, derived total, receipt
POST /runs/{id}/resume     replay cached steps free, re-execute from the failure
GET  /meta                 bounds + rate card
GET  /                     the screen
```

Request body: `goal` (required) · `fail_at_step` · `max_steps` · `max_credits_mc`.
Bounds may be lowered per request but never raised past the ceiling in `config.py`.

---

## The screen

Type a goal and watch the steps appear one at a time as the loop reaches them, then the
ledger and the receipt.

Everything a run does is on screen: the HTTP status of each request, the credit and step
meters against their limits, per-step cost and duration, click any step to see its raw
artifact, and the ledger with its idempotency keys. **Send duplicate request** replays the
last request so the idempotent response is visible without curl.

Presets across the top set up each scenario — happy path, failure and resume, both limits,
and the goals that fail on their own. `/?run=<id>` opens an existing run, which is useful
for showing a run created from the command line.

---

## Layout

```
app/
  models.py    Run, Step, LedgerEntry, status enums
  config.py    settings + ceilings
  pricing.py   pipeline, rate card, charging policy, ledger keys
  db.py        engine, SQLite pragmas, credits_spent() and charge()
  tools.py     the 7 mocked tools + integer money math
  runner.py    the bounded loop, failure handling, resume
  api.py       HTTP, idempotency, background execution
  static/      the screen (one file, no build step)
tests/         41 tests
```

`credits_spent` (the only way to read a total) and `charge` (the only way to spend) sit side
by side in `db.py` on purpose. Tools never touch the database — they read prior artifacts and
return a dict, which is what makes a replayed step indistinguishable from a fresh one.

---

## Notes

**On scope.** The brief says keep it small, so the *surface* is small: five endpoints, one
file per concern, no dependency beyond FastAPI and SQLModel, no build step. The depth went
into the three things the brief says it cares about — bounds, exact cost, and partial
failure — because that is where a run loop is actually hard.

**What I'd change with more time**

- The loop runs in FastAPI's `BackgroundTasks`. That keeps the run command to one process,
  but a restart mid-run leaves a run `running` until its lease expires. A real deployment
  puts the loop behind a queue and a worker.
- The plan is fixed, not agent-chosen. All 7 steps are materialised before `plan` executes so
  the screen can show progress. A real agent would choose its own steps, which changes what
  the step limit is protecting against.
- No retry-with-backoff inside a step. Failures are terminal-then-resumable, which is simpler
  to reason about but needs a client round-trip for a transient blip.
- The body-hash fallback has no TTL, so re-submitting an identical goal replays forever. Real
  systems expire the window or require the client to supply its own key.
- Refunds are modelled but unused — `LedgerEntry` supports negative amounts; nothing issues
  one yet.

**On the brief.** It says "6 steps" and then lists 7. I implemented all seven. With
`MAX_STEPS=8` that leaves exactly enough headroom for one failure and recovery (a resumed run
uses 8 attempts), which makes the step limit reachable in a demo instead of dead code.
