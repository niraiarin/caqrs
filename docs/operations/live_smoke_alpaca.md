# Runbook: live smoke against Alpaca paper-mode

Operator-facing walk-through for running `scripts/live_smoke_alpaca.py`
against the real Alpaca paper-trading API. This is the operational
evidence that PR #92-#106's pieces actually work end-to-end. The
script is locked to AAPL and capped at 1 share so a single smoke
costs ~$200 of paper capital.

References:

- Script: `scripts/live_smoke_alpaca.py`
- ADR-0008 (live-broker safety perimeter)
- ADR-0009 (venue selection: Alpaca-first)

## Prerequisites

1. **Alpaca paper account**: free signup at
   <https://alpaca.markets>. Generate paper API key + secret in the
   dashboard. **Do not** use the live API key for this smoke — the
   script's default URLs and safety guards target paper.
2. **dotenvx** installed (per project `~/.claude/CLAUDE.md`).
3. **caqrs[live-broker]** extras installed:

   ```bash
   uv sync --extra live-broker
   ```

## First-time `.env` setup

Create `.env` at the project root (gitignored). Required keys:

```dotenv
LIVE_BROKER_API_KEY=<paper key from Alpaca dashboard>
LIVE_BROKER_API_SECRET=<paper secret>
LIVE_BROKER_BASE_URL=https://paper-api.alpaca.markets
LIVE_BROKER_WSS_URL=wss://paper-api.alpaca.markets/stream
# Only required when running --live-submit (see Step 2):
LIVE_BROKER_ENABLE_LIVE_ORDERS=<some opaque token you generate>
```

Encrypt with dotenvx so the file is safe to commit if you ever
change your mind:

```bash
dotenvx encrypt
```

The `LIVE_BROKER_ENABLE_LIVE_ORDERS` value can be any
non-whitespace string — its purpose is the byte-equality check
against `--confirm-token` per NFR-LIVE-BROKER-1's two-step approval.
Treat it as a secret you copy/paste into the CLI invocation, not
as a credential.

## Step 1: dry-run handshake

This connects to the websocket, runs the auth + subscribe protocol,
verifies your credentials are accepted, and exits — no order
submission.

```bash
dotenvx run -- uv run python scripts/live_smoke_alpaca.py
```

Expected output (~3 seconds, exit 0):

```
[plan] base_url=https://paper-api.alpaca.markets
[plan] wss_url=wss://paper-api.alpaca.markets/stream
[plan] journal=var/alpaca_journal.sqlite
[plan] mode=dry-run (auth+subscribe only)
[plan] ticker=AAPL side=buy max_shares=1
[ws] connected; auth+subscribe handshake complete
[dry-run] no order submitted; cleaning up.
```

If `[ws] connected` doesn't appear within ~3 seconds, the websocket
auth probably failed. Check:

- The credentials are paper (not live).
- `LIVE_BROKER_WSS_URL` ends with `/stream` (not `/v2/stream` or
  another path).
- Nothing trailing-whitespace in the env values (the auth payload
  is exact-equal on both sides).

## Step 2: live-submit (1-share AAPL test)

Once Step 1 passes, you can submit a real paper order:

```bash
dotenvx run -- uv run python scripts/live_smoke_alpaca.py \
    --live-submit --confirm-token "$LIVE_BROKER_ENABLE_LIVE_ORDERS"
```

The `--confirm-token` value MUST byte-match
`$LIVE_BROKER_ENABLE_LIVE_ORDERS`. Using the shell expansion (as
above) is the easiest way to avoid whitespace mismatches.

Expected output (~5-60 seconds depending on market hours, exit 0):

```
[plan] base_url=https://paper-api.alpaca.markets
[plan] wss_url=wss://paper-api.alpaca.markets/stream
[plan] journal=var/alpaca_journal.sqlite
[plan] mode=live-submit
[plan] ticker=AAPL side=buy max_shares=1
[ws] connected; auth+subscribe handshake complete
[preflight] paper would submit qty=1 (cap: 1 share(s))
[ok] live orders enabled (NFR-LIVE-BROKER-1 two-step approval passed)
[submit] cycle_id=smoke-XXXXXXXX
[submit] decision_run_id=smoke-decision-YYYYYYYY
[submit] status=submitted
[submit] fill: AAPL buy 1 @ ~200
[await] waiting up to 60s for terminal event...
[outcome] FILLED ({'cycle_id': 'smoke-XXXXXXXX', ...})
```

Market-hours caveat: Alpaca's paper market mirrors real US market
hours. Outside of 09:30-16:00 ET, `--await` will likely time out
(no fills until the open). The order still queues — a follow-up
inspection of the journal (Step 3) will confirm.

## Step 3: inspect the journal

The script writes to `var/alpaca_journal.sqlite` by default
(project-relative). Inspect with `sqlite3`:

```bash
sqlite3 var/alpaca_journal.sqlite '.schema'
sqlite3 var/alpaca_journal.sqlite 'SELECT client_order_id, cycle_id, symbol, side, qty, submitted_at FROM submissions;'
sqlite3 var/alpaca_journal.sqlite 'SELECT client_order_id, fill_id, qty, fill_price_usd, is_partial FROM fills;'
sqlite3 var/alpaca_journal.sqlite 'SELECT client_order_id, reason FROM cancellations;'
```

Cross-check: the `cycle_id` from the journal matches the
`[submit] cycle_id=` line in the script output; the `fill_id` (if
present) is Alpaca's `execution_id`. This proves PR #101 (journal
wire-in), #102 (execution_id dedup), and #103 (cancel dedup) are
working end-to-end.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `[ws] FATAL: Alpaca rejected websocket auth` | Wrong credentials, or live key used against paper URL | Re-check `.env`; regenerate paper key in Alpaca dashboard |
| Script hangs on `[ws]` | DNS / firewall blocking `paper-api.alpaca.markets` | Test from host: `curl -sI https://paper-api.alpaca.markets/v2/account` |
| `enable_live_orders_after_human_approval rejected` | `--confirm-token` does not byte-match env var | Use `--confirm-token "$LIVE_BROKER_ENABLE_LIVE_ORDERS"`; check `[hint]` line if length mismatch |
| `[outcome] TIMEOUT` | Market closed, or Alpaca latency | Run during market hours; check Alpaca status page |
| `paper pre-flight resolves qty > max_shares` | Capital × weight / price exceeds 1 share | Should not happen on AAPL with current constants; check `--max-shares` |
| `LIVE_BROKER_BASE_URL ... resolves to the production live endpoint` | Env var accidentally set to live URL | Verify `.env` — paper URL contains `paper-` prefix |

## After paper smoke passes

A successful end-to-end run is the prerequisite to enabling live
(non-paper) trading. **Live enablement is a separate ADR-level
decision** — do not flip `LIVE_BROKER_BASE_URL` to the production
endpoint without:

1. Reviewing the daily-loss cap (`live_broker_daily_loss_cap_usd`,
   currently hardcoded at $100 in the smoke).
2. Choosing capital allocation strategy + initial deposit.
3. Adding a `--i-know-this-is-live` invocation as a deliberate,
   tedious gesture.
4. Drafting an incident playbook (kill-switch path, broker-side
   cancel-all, journal-replay recovery).

The smoke is a smoke; live trading is its own commitment.

## Journal hygiene

`var/alpaca_journal.sqlite` accumulates rows over time. For a
multi-day smoke campaign, periodically:

- Rotate the file: `mv var/alpaca_journal.sqlite var/alpaca_journal.$(date +%F).sqlite`
- Inspect attribution coverage: every `cycle_id` in the event log
  should resolve via `journal.attribution()`.
- Back up before destructive ops — the journal is the source of
  truth for restart-survival attribution per ADR-0008.

## Known limitations (as of 2026-05)

- `--ticker` is locked to AAPL per Codex PR #106 round 1 major.
  Expanding requires adding a curated allow-list with documented
  price expectations.
- The script's `--max-wait-seconds 60` timeout fires unconditionally;
  there's no retry or re-poll. For longer-lived smokes, run the
  websocket consumer as a separate background process (the script
  is single-shot by design).
- Live websocket reconnect (per PR #105's exponential backoff) only
  fires for transport errors; programming-error exceptions still
  propagate out and stop the smoke. This is intentional safety.
