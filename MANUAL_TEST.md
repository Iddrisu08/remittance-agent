# Manual end-to-end test

A walkthrough you can run by hand against a running server. Every expected output below
was captured from an actual run — if yours differs, something is genuinely wrong (with two
documented exceptions, see [Things that look wrong but aren't](#things-that-look-wrong-but-arent)).

Takes about 10 minutes. Roughly 5 for the API, 5 for the UI.

---

## Setup

Start from a clean database so credit totals are reproducible:

```bash
cd remittance-agent
rm -rf data/            # optional: clears previous runs
./run.sh                # → http://127.0.0.1:8000
```

In a second terminal:

```bash
B=http://127.0.0.1:8000
```

Every command below uses `$B`. `jq` makes the output readable but is not required —
drop the `| jq ...` and read the raw JSON if you don't have it.

**Sanity check first:**

```bash
./test.sh
```
> **Expect:** `41 passed`. If this fails, stop — the manual walkthrough will fail too.

---

## Part A — the API

### 1. Rate card and bounds

```bash
curl -s $B/meta | jq -c '{full: .full_run_cost_display, defaults, ceilings}'
```
```json
{"full":"5.400","defaults":{"max_steps":8,"max_credits_mc":6000},"ceilings":{"max_steps":8,"max_credits_mc":6000}}
```
A clean run costs 5.400 credits against a 6.000 budget.

---

### 2. Happy path

```bash
curl -s -X POST $B/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual-happy' \
  -d '{"goal":"Send $500 to my mom in Accra at the best rate"}' \
  | jq -c '{status, steps: [.steps[].status] | unique, credits: .credits.used_display}'
```
```json
{"status":"queued","steps":["pending"],"credits":"0.000"}
```
> **Proves:** the response returns immediately with **all 7 steps already materialised** as
> `pending`. The plan is visible before any work happens.

Grab the id and poll (the run takes ~2.5s at the default 350ms simulated step latency):

```bash
RID=$(curl -s -X POST $B/runs -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual-happy' \
  -d '{"goal":"Send $500 to my mom in Accra at the best rate"}' | jq -r .id)

# poll a few times and watch steps flip pending → running → succeeded
curl -s $B/runs/$RID | jq -c '[.steps[] | "\(.name):\(.status)"]'

# then, once settled:
curl -s $B/runs/$RID | jq -c '{status, credits: .credits.used_display,
  ledger_rows: (.ledger|length), attempts: .bounds.steps_attempted,
  receipt: .receipt.received.display}'
```
```json
{"status":"completed","credits":"5.400","ledger_rows":7,"attempts":7,"receipt":"GHS 7,578.66"}
```

**The total is derived, not stored — verify it:**
```bash
curl -s $B/runs/$RID | jq '([.ledger[].amount_mc] | add) == .credits.used_mc'   # → true
curl -s $B/runs/$RID | jq '[.ledger[].amount_mc] | add'                        # → 5400
```
> **Proves:** the reported total is exactly the sum of the ledger rows. There is no
> `credits_used` column anywhere to disagree with them.

---

### 3. Idempotency — exact duplicate

```bash
curl -s -D- -o /tmp/dup.json -X POST $B/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual-happy' \
  -d '{"goal":"Send $500 to my mom in Accra at the best rate"}' \
  | grep -iE '^(HTTP|idempotency-replayed)'
jq -c '{credits: .credits.used_display, ledger_rows: (.ledger|length)}' /tmp/dup.json
```
```
HTTP/1.1 200 OK
idempotency-replayed: true
{"credits":"5.400","ledger_rows":7}
```
> **Proves:** `200`, not `201`. Same run returned, **credits unchanged at 5.400, still 7
> ledger rows.** Nothing re-executed, nothing re-charged.

---

### 4. Idempotency — same key, different body

```bash
curl -s -X POST $B/runs -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual-happy' \
  -d '{"goal":"Send $900 to my mom in Accra"}' | jq -c .detail
```
```json
{"code":"idempotency_key_reuse","message":"Idempotency-Key was already used with a different request body","run_id":"94691797..."}
```
> **Proves:** reusing a key for a different request is a **422**, not a silent replay of
> someone else's run. The conflicting `run_id` is returned so you can go look at it.

---

### 5. Idempotency — no header falls back to a body hash

```bash
A=$(curl -s -X POST $B/runs -H 'Content-Type: application/json' \
     -d '{"goal":"Send $250 to my sister in Kumasi"}')
echo "$A" | jq -c '{id: .id[0:8], source: .idempotency_source, key: .idempotency_key[0:20]}'

# resubmit the SAME request with the defaults spelled out explicitly
C=$(curl -s -X POST $B/runs -H 'Content-Type: application/json' \
     -d '{"goal":"Send $250 to my sister in Kumasi","max_steps":8,"max_credits_mc":6000}')
[ "$(echo "$A"|jq -r .id)" = "$(echo "$C"|jq -r .id)" ] && echo "SAME RUN ✓" || echo "DIFFERENT RUN ✗"
```
```
{"id":"334c98b0","source":"body_hash","key":"sha256:9a49c81323a84"}
SAME RUN ✓
```
> **Proves:** with no header, the body itself is the key — and **defaults are resolved
> before hashing**, so spelling out `max_steps: 8` is recognised as the same request rather
> than creating a second run and charging twice.

---

### 6. Idempotency under concurrency

The real test. Fire six identical requests in parallel:

```bash
for i in $(seq 1 6); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/runs \
    -H 'Content-Type: application/json' -H 'Idempotency-Key: manual-race' \
    -d '{"goal":"Send $310 to my brother in Takoradi"}' &
done | sort | uniq -c
wait
```
```
   1 201
   5 202
```
Then confirm only one run exists and it was charged once:
```bash
sleep 3
RACE=$(curl -s -X POST $B/runs -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual-race' \
  -d '{"goal":"Send $310 to my brother in Takoradi"}' | jq -r .id)
curl -s $B/runs/$RACE | jq -c '{status, credits: .credits.used_display, ledger_rows: (.ledger|length)}'
```
```json
{"status":"completed","credits":"5.400","ledger_rows":7}
```
> **Proves:** exactly one `201`; the other five saw an in-flight run and got `202`.
> One run, one set of charges. This is the case a "check if the key exists, then insert"
> implementation fails — it would create several runs and charge several times.

---

### 7. Partial failure

```bash
FID=$(curl -s -X POST $B/runs -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual-fail' \
  -d '{"goal":"Send $500 to my mom in Accra at the best rate","fail_at_step":"execute_transfer"}' \
  | jq -r .id)
sleep 3
curl -s $B/runs/$FID > /tmp/f.json
jq -c '{status, failed_step, error_code, credits: .credits.used_display, resumable,
        ledger_rows: (.ledger|length)}' /tmp/f.json
```
```json
{"status":"failed","failed_step":"execute_transfer","error_code":"injected_failure","credits":"2.750","resumable":true,"ledger_rows":5}
```

Now check the charging policy held:
```bash
jq '.steps[5].output == null'                                          /tmp/f.json  # true
jq '[.ledger[] | select(.step_name=="execute_transfer")] | length == 0' /tmp/f.json  # true
jq '[.steps[0:5][].output != null] | all'                               /tmp/f.json  # true
```
> **Proves, in order:** the failed step left **no partial artifact**; the failed step
> produced **no ledger row** (charged nothing); the five steps before it **cached their
> outputs** and kept their 2.750 in charges.

---

### 8. Resume

```bash
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST $B/runs/$FID/resume
sleep 3
curl -s $B/runs/$FID > /tmp/r.json
jq -c '{status, credits: .credits.used_display, attempts: .bounds.steps_attempted, resume_count}' /tmp/r.json
jq -c '[.steps[] | select(.replayed) | .name]' /tmp/r.json
jq -r '.ledger[] | select(.attempt==1) | "\(.entry_key)  \(.amount_display)"' /tmp/r.json
```
```
HTTP 202
{"status":"completed","credits":"5.400","attempts":8,"resume_count":1}
["plan","validate_recipient","fetch_fx_rates","select_best_rate","lock_quote"]
step:5:execute_transfer:attempt:1:charge  2.500
```
> **Proves, and this is the one to look at closely:**
> - Five steps **replayed at zero credits** — they appear in `.steps` as `replayed: true`
>   but added no ledger rows.
> - Total is **5.400 — identical to a clean run.** Recovering from a failure costs exactly
>   what never failing would have cost, never more.
> - `attempts: 8`, not 13 — **a replay is not an attempt**, so replays don't consume the
>   step budget. (If they did, a late failure could never resume.)
> - The retry's ledger key carries `attempt:1`, which is what makes it a legitimate new
>   charge rather than a suppressed duplicate of `attempt:0`.

---

### 9. Resume a completed run

```bash
curl -s -o /tmp/r2.json -w 'HTTP %{http_code}  ' -X POST $B/runs/$FID/resume
jq -r '"credits still \(.credits.used_display)"' /tmp/r2.json
```
```
HTTP 200  credits still 5.400
```
> **Proves:** idempotent no-op, not an error. The caller wants the run finished; it is.

---

### 10. Step bound

```bash
SID=$(curl -s -X POST $B/runs -H 'Content-Type: application/json' \
  -d '{"goal":"Send $500 to my mom in Accra","max_steps":3}' | jq -r .id)
sleep 2
curl -s $B/runs/$SID > /tmp/s.json
jq -c '{status, credits: .credits.used_display, attempts: .bounds.steps_attempted, resumable}' /tmp/s.json
jq -r .error_message /tmp/s.json
jq -c '[.steps[].status]' /tmp/s.json
curl -s -X POST $B/runs/$SID/resume -w '  HTTP %{http_code}\n' | jq -c .detail.code
```
```json
{"status":"max_steps_exceeded","credits":"1.850","attempts":3,"resumable":false}
"step budget exhausted: 3/3 attempts used before 'select_best_rate'"
["succeeded","succeeded","succeeded","skipped","skipped","skipped","skipped"]
"not_resumable"  HTTP 409
```
> **Proves:** the bound is **its own terminal state, not `failed`**. Unreached steps are
> `skipped` (so you can see exactly where it stopped), and the error message names the step
> it was *about* to run — the check happened **before** execution, not after.
> Resume is refused: resuming under the same bound would make the bound advisory.

---

### 11. Credit bound

```bash
CID=$(curl -s -X POST $B/runs -H 'Content-Type: application/json' \
  -d '{"goal":"Send $500 to my mom in Accra","max_credits_mc":2000}' | jq -r .id)
sleep 2
curl -s $B/runs/$CID > /tmp/c.json
jq -c '{status, credits: .credits.used_display, cap: .bounds.max_credits_display, resumable}' /tmp/c.json
jq -r .error_message /tmp/c.json
jq '.credits.used_mc <= .bounds.max_credits_mc' /tmp/c.json
```
```json
{"status":"max_credits_exceeded","credits":"1.950","cap":"2.000","resumable":false}
"credit budget exhausted: 'lock_quote' costs 800mc, 50mc of 2000mc remain"
true
```
> **Proves:** spend never exceeded the cap, not even transiently. Note it reached **1.950,
> not 1.850** — `select_best_rate` costs only 100mc and genuinely fit, so the loop ran it
> before refusing the 800mc `lock_quote`. The budget is spent as far as it honestly goes.

---

### 12. Bounds cannot be raised by the request

```bash
curl -s -X POST $B/runs -H 'Content-Type: application/json' \
  -d '{"goal":"Send $500 to my mom in Accra","max_credits_mc":1000000000}' \
  -w '\nHTTP %{http_code}\n' | jq -r '.detail[0].msg'
```
```
Value error, max_credits_mc may not exceed 6000
HTTP 422
```
> **Proves:** a request may **lower** its own bounds but never raise them past the operator
> ceiling. Otherwise the cap is a suggestion.

---

### 13. Failures that aren't injected

The failure machinery must work without `fail_at_step`:

```bash
for g in "Send \$500 to my mom in Lagos" "Send money to my mom in Accra" "Send \$500 to Accra"; do
  ID=$(curl -s -X POST $B/runs -H 'Content-Type: application/json' -d "{\"goal\":\"$g\"}" | jq -r .id)
  sleep 3
  curl -s $B/runs/$ID | jq -r "\"\(.status)  \(.error_code)  \(.credits.used_display) cr  ← $g\""
done
```
```
failed  unsupported_corridor   0.000 cr  ← Send $500 to my mom in Lagos
failed  no_amount              0.000 cr  ← Send money to my mom in Accra
failed  recipient_not_found    0.250 cr  ← Send $500 to Accra
```
> **Proves:** three real failure paths. The first two fail inside `plan` (step 0) and cost
> **nothing**. The third gets through `plan` — which produced a usable artifact and is
> charged 0.250 — then fails at `validate_recipient`, which is free. That 0.250 is the
> charging policy in miniature: **you pay for work that produced something.**

---

### 14. Money is integer-exact

Against the completed run from step 8:

```bash
jq -r '.steps[4].output | "debit \(.debit_usd_cents) = sendable \(.sendable_usd_cents) + fee \(.fee_usd_cents) → \(.debit_usd_cents == .sendable_usd_cents + .fee_usd_cents)"' /tmp/r.json
jq -r '.steps[4].output | "payout \(.payout_pesewas) == \(.sendable_usd_cents) × \(.rate_bps) ÷ 10000 → \(.payout_pesewas == ((.sendable_usd_cents * .rate_bps) / 10000 | floor))"' /tmp/r.json
jq -r '.steps[2].output.quotes[] | "  \(.provider)  rate \(.rate_display)  fee \(.fee_display)  payout \(.payout_pesewas)"' /tmp/r.json
jq -r '.steps[3].output | "chose \(.provider) — beat headline rate: \(.beat_headline_rate) by \(.runner_up_gap_pesewas) pesewas"' /tmp/r.json
```
```
debit 50000 = sendable 49801 + fee 199 → true
payout 758175 == 49801 × 152241 ÷ 10000 → true
  AkuaPay        rate 15.2241  fee $1.99   payout 758175
  VoltaRemit     rate 15.4784  fee $12.00  payout 755345
  GulfStream FX  rate 15.1262  fee $0.00   payout 756310
chose AkuaPay — beat headline rate: true by 1865 pesewas
```
> **Proves:** the transfer reconciles to the cent; the conversion is exactly
> `cents × bps ÷ 10000` floored; and `select_best_rate` **did real work** — VoltaRemit
> quotes the highest rate (15.4784) and loses on its $12 fee. An implementation that
> maximised `rate_bps` would pick the wrong provider here.

---

### 15. Persistence

```bash
# Ctrl-C the server, then ./run.sh again
curl -s $B/runs/$FID | jq -r '"\(.status)  \(.credits.used_display) cr  \(.ledger|length) ledger rows"'
```
```
completed  5.400 cr  7 ledger rows
```
> **Proves:** SQLite is a real file. Runs, steps and ledger all survive a restart.

---

## Part B — the UI

Open **http://127.0.0.1:8000**. The preset chips across the top set up each scenario.

| # | Do this | Look for |
|---|---------|----------|
| 1 | Load the page | Rate card top-right, 7 steps totalling **5.400 cr** |
| 2 | Click **Happy path** → **Run** | Green `201 Created` badge with the run id and key source |
| 3 | Watch during the run | Steps flip **grey → blue pulsing dot → green**, one at a time; credits meter climbs. This is the incremental polling — don't blink, it takes ~2.5s |
| 4 | Click any green step | Expands to the raw JSON artifact. Open `fetch_fx_rates` to see all three provider quotes |
| 5 | Scroll to the ledger | 7 rows, each with its full `entry_key`, a running total reaching **5.400**, and `0.600 left` |
| 6 | Scroll to the receipt | Green card: `$500.00` → `GHS 7,5xx.xx`, fee, rate, provider |
| 7 | Click **Send duplicate request** | Amber `200 OK — idempotent replay… credits unchanged at 5.400`. **Ledger does not grow.** |
| 8 | Click **Fail at transfer → resume** → **Run** | Red **FAILED** pill, red banner, **2.750 cr**, step 5 red, step 6 grey, **Resume** button appears |
| 9 | Click **Resume** | Steps 0–4 get an amber `replayed` tag and a green `0.000 cr`, with their real prices **struck through**. Step 5 shows `attempt 1`. Total lands on **5.400** |
| 10 | Check the ledger again | Now 7 rows — spot `step:5:execute_transfer:attempt:1:charge` sitting next to the `attempt:0` rows |
| 11 | Click **Credit bound (2000mc)** → **Run** | **Amber**, not red: `MAX CREDITS EXCEEDED — terminal, not a failure`. Credits bar turns amber at 1.950/2.000. **No Resume button** |
| 12 | Click **Step bound (3)** → **Run** | `MAX STEPS EXCEEDED`, steps 3–6 greyed as skipped |
| 13 | Click **Unsupported corridor** → **Run** | Red, fails on step 0, ledger empty, **0.000 cr** |
| 14 | Open DevTools → Network | `GET /runs/{id}` firing every 300ms while running, **stopping** the moment the run is terminal |

The colour split in rows 8 vs 11 is the point: **red = something went wrong, amber = the
loop stopped itself on purpose.**

---

## Things that look wrong but aren't

**The GHS amount changes between runs.** Provider rates are drawn from a PRNG seeded on the
run id, so each run sees a slightly different market — `GHS 7,578.66` vs `GHS 7,581.75`. The
same run is always reproducible; different runs are not identical. USD amounts, fees and
credit totals never vary.

**Concurrent duplicates return `202`, not `409`.** From the client's side that request
succeeded — they just arrived twice, usually via their own retry. `202` plus `Retry-After`
tells them to poll. A `409` would invite them to retry harder against a run already doing
the work.

**The credit bound stops at 1.950, not 1.850.** `select_best_rate` costs 100mc and fits.
The loop spends what the budget genuinely allows rather than bailing early.

**A resumed run shows `attempts: 8/8`.** Six attempts before the failure, two after.
Replays don't count. This is why `MAX_STEPS=8` against a 7-step pipeline is deliberate —
it leaves exactly enough headroom for one failure and recovery.

---

## Quick reference

| Scenario | Command flag | Terminal status | Credits |
|---|---|---|---|
| Happy path | — | `completed` | 5.400 |
| Injected failure | `"fail_at_step":"execute_transfer"` | `failed` | 2.750 |
| …then resumed | `POST /runs/{id}/resume` | `completed` | 5.400 |
| Step bound | `"max_steps":3` | `max_steps_exceeded` | 1.850 |
| Credit bound | `"max_credits_mc":2000` | `max_credits_exceeded` | 1.950 |
| Bad corridor | goal mentions Lagos | `failed` | 0.000 |
| No amount | goal has no `$` | `failed` | 0.000 |
| No recipient | `"Send $500 to Accra"` | `failed` | 0.250 |
