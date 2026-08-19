# Walkthrough

Three scenarios run against the endpoint, then the mechanism behind them.

Every output below is copied from an actual run against a fresh database. To follow
along: `./run.sh`, then `B=http://127.0.0.1:8000` in another terminal.

---

## 1. A goal that completes

```bash
curl -X POST $B/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  -d '{"goal":"Send $500 to my mom in Accra at the best rate"}'
```

The response returns immediately — the run executes in the background, and the caller gets
an id to poll:

```
201 Created   Location: /runs/5acef5f3…
status: queued, all 7 steps: pending
```

Polling `GET /runs/{id}` shows the steps completing one at a time. When it settles:

```
final: completed   credits: 5.400   attempts: 7/8

receipt: $500.00 → GHS 7,577.52 via AkuaPay @ 1 USD = 15.2156 GHS
```

**The credits used, per step:**

| step | charge |
|---|---|
| plan | 0.250 |
| validate_recipient | 0.400 |
| fetch_fx_rates | 1.200 |
| select_best_rate | 0.100 |
| lock_quote | 0.800 |
| execute_transfer | 2.500 |
| write_receipt | 0.150 |
| **total** | **5.400** |

That total is not a stored field. `credits.used_mc` is computed on every read as
`SELECT SUM(amount_mc) FROM ledger WHERE run_id = ?`. There is no column anywhere that
could disagree with the rows.

---

## 2. The client retries, because the first response never arrived

The exact same request, sent again:

```bash
curl -X POST $B/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-1' \
  -d '{"goal":"Send $500 to my mom in Accra at the best rate"}'
```

```
HTTP/1.1 200 OK
idempotency-replayed: true
location: /runs/5acef5f30df14322962d26f7326a4b70

same run id: true   credits: 5.400   ledger rows: 7   attempts: 7
```

**`200`, not `201`.** Same run, same seven ledger rows, same 5.400. The step counter is
still 7 — nothing re-executed. The client gets the result it was owed, and is charged once.

### It also works without the client sending a key

A retry that omits the header is still recognised, because the request body is hashed and
used as the key:

```bash
curl -X POST $B/runs -H 'Content-Type: application/json' \
  -d '{"goal":"Send $250 to my sister in Kumasi"}'      # twice
```

```
first:  HTTP 201   id b073e22c   source body_hash
second: HTTP 200   id b073e22c   credits 5.400   ledger rows 7
```

Defaults are resolved *before* hashing, so `{"goal": "x"}` and
`{"goal": "x", "max_steps": 8}` hash to the same key rather than becoming two runs.

### Retries that arrive simultaneously

The harder case: six copies of one request in flight at once.

```bash
for i in $(seq 1 6); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/runs \
    -H 'Content-Type: application/json' -H 'Idempotency-Key: demo-race' \
    -d '{"goal":"Send $310 to my brother in Takoradi"}' &
done | sort | uniq -c
```

```
   1 201
   5 202
one run, charged once: 5.400 over 7 ledger rows
```

One request created the run; the other five found it already in flight and were told to
poll (`202` + `Retry-After: 1`) rather than starting a second execution.

### Reusing a key for a different request

```
422   idempotency_key_reuse
"Idempotency-Key was already used with a different request body"
run_id: 5acef5f30df14322962d26f7326a4b70
```

Returning the old run here would answer a question the caller did not ask, and hide a bug
in their code. The conflicting run id is included so they can go and look at it.

---

## 3. A tool step forced to fail partway

```bash
curl -X POST $B/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-fail' \
  -d '{"goal":"Send $500 to my mom in Accra at the best rate",
       "fail_at_step":"execute_transfer"}'
```

### What state the run ends in

```
end state: failed   failed at: execute_transfer   error: injected_failure
credits kept: 2.750 of 6.000 budget   resumable: true

  [0] plan                 succeeded   (artifact cached)
  [1] validate_recipient   succeeded   (artifact cached)
  [2] fetch_fx_rates       succeeded   (artifact cached)
  [3] select_best_rate     succeeded   (artifact cached)
  [4] lock_quote           succeeded   (artifact cached)
  [5] execute_transfer     failed
  [6] write_receipt        pending
```

`failed` is a terminal state, but a recoverable one. The run is not rolled back and not
left half-alive: the five steps that finished keep both their charges and their outputs.

### What happened to the credits already spent

```
step:0:plan:attempt:0:charge                 0.250
step:1:validate_recipient:attempt:0:charge   0.400
step:2:fetch_fx_rates:attempt:0:charge       1.200
step:3:select_best_rate:attempt:0:charge     0.100
step:4:lock_quote:attempt:0:charge           0.800
                                             ─────
                                             2.750
```

Five rows, not six. **The completed steps stay charged; the failed step is free.**

The call I made: *charge for work that produced a usable artifact.* Those five steps each
left an output that a later attempt can reuse, so the caller got value and pays for it.
`execute_transfer` produced nothing — its `output` is null, no partial artifact survives a
failure — so it costs nothing. Refunding the 2.750 would mean throwing away work that is
still good; charging for the failure would make retrying punitive.

### How the person recovers

```bash
curl -X POST $B/runs/{id}/resume     # → 202 Accepted
```

```
end state: completed   credits: 5.400   attempts: 8/8   resumed 1x
replayed free: [plan, validate_recipient, fetch_fx_rates, select_best_rate, lock_quote]

step:0:plan:attempt:0:charge                 0.250
step:1:validate_recipient:attempt:0:charge   0.400
step:2:fetch_fx_rates:attempt:0:charge       1.200
step:3:select_best_rate:attempt:0:charge     0.100
step:4:lock_quote:attempt:0:charge           0.800
step:5:execute_transfer:attempt:1:charge     2.500   ← the retry
step:6:write_receipt:attempt:0:charge        0.150
                                             ─────
                                             5.400

receipt: $500.00 → GHS 7,559.64 via AkuaPay
```

**5.400 — the same total as the run in scenario 1.** The five cached steps replayed at zero
credits; only `execute_transfer` and `write_receipt` actually ran. Recovering from a failure
costs exactly what never failing would have cost, and never more.

From the screen this is one button on the failure card: *Try again from here.*

---

## The bound

The brief's first requirement — a run must not execute unbounded steps and burn cost
forever. There are two limits, and hitting either is its own end state.

**Step limit** (`max_steps: 3`):
```
end state: max_steps_exceeded   attempts: 3/3   credits: 1.850
message: step budget exhausted: 3/3 attempts used before 'select_best_rate'
steps:   succeeded, succeeded, succeeded, skipped, skipped, skipped, skipped
resume → 409 not_resumable
```

**Credit limit** (`max_credits_mc: 2000`):
```
end state: max_credits_exceeded   credits: 1.950 of 2.000
message: credit budget exhausted: 'lock_quote' costs 800mc, 50mc of 2000mc remain
never exceeded the cap: true
```

Three things to notice:

- **This is not `failed`.** A failure means something went wrong; a bound means the run did
  exactly what it was told. They are different states, and the UI colours them differently
  — red for a fault, amber for a deliberate stop.
- **The message names the step it was *about* to run.** The check happens before the step
  executes, so cost is never incurred and then regretted. Spend never exceeds the cap, not
  even transiently.
- **The credit run stopped at 1.950, not 1.850.** `select_best_rate` costs 0.100 and genuinely
  fitted; `lock_quote`'s 0.800 did not. The loop spends what the budget honestly allows and
  stops at a real boundary.

Resuming a bounded run is refused with `409`. Retrying under the same limit would make the
limit advisory — raising it is a new decision, not a retry.

---

## The mechanism

### How the loop is bounded

Each run carries its own `max_steps` and `max_credits_mc`, copied onto the row at creation.
They are not read from config at check time: raising a limit next week must not retroactively
change whether last week's run was legitimately bounded.

Before every step, in this order:

```python
if run.steps_attempted >= run.max_steps:
    → max_steps_exceeded, remaining steps marked skipped, stop

spent = credits_spent(session, run.id)          # SUM over the ledger, every iteration
if spent + cost_of(step) > run.max_credits_mc:
    → max_credits_exceeded, remaining steps marked skipped, stop
```

Two details carry the weight:

**Spend is re-read from the ledger on every iteration**, not accumulated in a local variable.
A running total held in a variable is a second source of truth that can drift from the rows;
an indexed `SUM` per step cannot.

**`steps_attempted` counts attempts, not successes**, and is incremented *before* the tool
runs. A tool that fails still consumed a turn. Counting only successes would let a failing
run spin inside its own bound forever — the exact runaway the limit exists to prevent.

A replayed step does **not** increment it. If cached steps consumed the step budget, a run
that failed late could never be resumed — it would exhaust the limit re-walking work it had
already paid for. That is why the resumed run in scenario 3 reads 8/8 rather than 13/8.

### How the retry is recognised

The key is the client's `Idempotency-Key` header, or — when they don't send one — `sha256`
of the canonicalised request body. Either way it lands in a column with a **`UNIQUE`
constraint**:

```python
run = Run(id=uuid4().hex, idempotency_key=key, ...)
try:
    session.add(run); session.commit()          # 201, and start executing
except IntegrityError:
    session.rollback()
    return replay(existing_run)                 # 200 / 202
```

The handler attempts the insert and interprets the violation. It deliberately does **not**
`SELECT` first to see whether the key exists: a check-then-insert passes every
single-threaded test and double-charges the first time two requests arrive together — which
is precisely the situation idempotency exists to handle. The database is the only thing that
can arbitrate that race, so the database arbitrates it. That is what produces `1 201, 5 202`
under six concurrent retries instead of six runs.

The run row is committed **before** any step executes, so a duplicate arriving mid-flight
finds it and receives `202` + `Retry-After` — the current state and an instruction to poll —
rather than starting a second execution.

There is no stored response body. The reply is rebuilt from the run row on every call, so
`POST`, `GET` and `resume` all return the same shape. A frozen snapshot would hand a caller a
`failed` payload for a run that had since been resumed and completed.

### How double-charging is prevented inside a run

Idempotency at the HTTP edge is not enough — the loop itself can crash between persisting an
artifact and recording its charge. Every ledger row carries a deterministic key:

```
step:5:execute_transfer:attempt:1:charge
```

with `UNIQUE(run_id, entry_key)`. A charge retried after a crash collides and is discarded.
The same mechanism lets a *legitimate* retry through, because a resume increments the attempt
number — which is why `attempt:0` and `attempt:1` sit side by side in the scenario 3 ledger,
and why replaying `attempt:0` five times adds nothing.

The artifact is committed **before** the charge. If the process dies between them the run is
under-charged for work it can still reuse — the safe direction. Reversed, you would bill for
an artifact that isn't there.

### Why costs can't drift

Credits are integer milli-credits (1 credit = 1000 mc) from end to end. No float touches the
arithmetic, and none touches the display either — `5400` renders as `"5.400"` by integer
division and modulo, on the server and in the browser. Money is the same: USD cents, GHS
pesewas, and FX rates in basis points, all `int`, converted with one exact multiply and one
floor division:

```python
pesewas = usd_cents * rate_bps // 10_000
```

Rounding is always down, and `usd_to_ghs` returns the dropped remainder rather than
swallowing it, so a receipt reconciles to the last unit. Even parsing the goal avoids
floats — `"$0.10"` becomes `10` cents via `int(whole) * 100 + int(frac)`, because
`float("0.1") * 100` is `10.000000000000002`, and that is how cents go missing.
