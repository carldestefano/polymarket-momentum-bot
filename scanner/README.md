# Polymarket BTC Scanner — Stage 1 + Stage 2 paper trading + Stage 3 market making

**Scanner + paper trading + market-making SIMULATION only. No wallet.
No private keys. No order placement. No live trading.**

A beginner-friendly, read-only scanner that pulls active Polymarket
prediction markets, filters them to BTC-related markets, computes a set
of actionable metrics (best bid/ask, mid, spread, last, volume,
liquidity, time-to-resolution, threshold, fair-value placeholder,
edge, freshness), ranks the opportunities, and surfaces them through a
Cognito-protected static dashboard backed by a JWT-protected HTTP API.

Stage 2 adds a **paper trading simulation** that consumes Stage 1's
ranked opportunities and opens simulated BUY YES positions against
risk-bounded limits.

Stage 3 adds a **market-making simulation**: fair-value-based bid/ask
quote recommendations, maker-only quote lifecycle, conservative
book-cross fills, per-market inventory tracking with running
average-cost P&L, and inventory skew to push against one-sided
inventory. It never places real orders. See
[docs/AWS_DEPLOYMENT.md](docs/AWS_DEPLOYMENT.md#market-making-stage-3)
for how to enable it.

Fills, marks, and P&L for both stages are computed in software — nothing
touches Polymarket or any wallet.

> **Not financial advice.** Prediction markets can and do lose money.
> This project intentionally cannot place orders — it exists only to
> help you look at the BTC market landscape on Polymarket.

## What you get

- **Scanner Lambda** (Python 3.12) running on a 5-minute EventBridge
  schedule. Uses only the Python standard library (`urllib`) so there
  is no build step and no external dependencies.
- **Public Polymarket Gamma API** for the market catalog (no auth).
- **BTC classifier** that rejects markets that mention other crypto
  assets even if the word "bitcoin" appears somewhere in the question.
- **DynamoDB tables** (pay-per-request) for config, scan summaries, the
  latest ranked opportunities, Stage 2 `PaperPositionsTable` and
  `PaperTradesTable`, plus Stage 3 `MmQuotesTable`, `MmFillsTable`,
  `MmInventoryTable`.
- **HTTP API Lambda** behind Amazon API Gateway HTTP API with a
  Cognito JWT authorizer. Endpoints: `/status`, `/config`,
  `/opportunities`, `/scans`, `/scan` (manual trigger), Stage 2
  `/paper/status`, `/paper/positions`, `/paper/trades`, `/paper/reset`,
  plus Stage 3 `/mm/status`, `/mm/quotes`, `/mm/fills`,
  `/mm/inventory`, `/mm/reset`.
- **Static dashboard** (vanilla HTML + CSS + JS) deployed to S3 +
  CloudFront, logging in via the Cognito Hosted UI using the
  Authorization Code + PKCE flow.
- **CloudWatch log groups** for the scanner and API Lambdas.

## Architecture

```
                  EventBridge (rate(5 minutes))
                               |
                               v
             +-----------------------------------+
 Polymarket  |     Scanner Lambda (Python)       |
 Gamma API   |     - classify BTC markets        |
 --------->  |     - compute metrics             |
             |     - rank opportunities          |
             +--------+----------+---------------+
                      |          |
                      v          v
              +-------+---+  +---+-----------+
              | Scans DDB |  | Opps DDB      |
              +-----------+  +---------------+
                      ^          ^
                      |          |
              +-------+----------+---------+
              |      API Lambda (Python)   |
              |      JWT-protected HTTP    |
              +---------^------------------+
                        |
                        | fetch + bearer token
                        |
              +---------+-----------+
              |   CloudFront + S3   |
              |   static dashboard  |
              +---------^-----------+
                        |
              +---------+-----------+
              |  Cognito User Pool  |
              |  (Hosted UI, PKCE)  |
              +---------------------+
```

## Repo layout

```
scanner/
├── src/polymarket_scanner/     # scanner library (no AWS deps)
│   ├── classify.py             # BTC classifier + short-horizon detection
│   ├── parse.py                # threshold extraction + field parsing
│   ├── metrics.py              # mid/spread/fair-value/edge/freshness
│   ├── rank.py                 # opportunity scoring
│   ├── polymarket.py           # Gamma + Coinbase HTTP clients (urllib)
│   ├── paper.py                # Stage 2 paper-trading simulation engine
│   └── scan.py                 # run_scan() orchestration
├── tests/                      # pytest unit tests (offline)
├── infra/
│   ├── cdk/
│   │   ├── app.py              # CDK entrypoint (stages pkg into Lambda dir)
│   │   ├── cdk.json
│   │   ├── requirements.txt
│   │   ├── stacks/scanner_stack.py
│   │   └── lambda/
│   │       ├── scanner/handler.py
│   │       └── api/handler.py
│   └── gui/                    # static dashboard (index.html, app.js, style.css)
└── docs/
    └── AWS_DEPLOYMENT.md
```

## Local development

```
cd scanner
python -m pytest tests/
```

Tests are fully offline and do not hit the Polymarket API.

## Deploy

See [docs/AWS_DEPLOYMENT.md](docs/AWS_DEPLOYMENT.md) for step-by-step
instructions. The short version is:

```
cd scanner/infra/cdk
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap    # first time in this account/region
cdk deploy
```

Then create a Cognito dashboard user (see the deployment doc) and open
the `DashboardUrl` printed in the `cdk deploy` outputs.

## Paper trading (Stage 2)

**Disabled by default.** After the stack is deployed, paper trading
stays off until you set `paper_trading_enabled=true` in `ConfigTable`.
Enable it with the AWS CLI:

```
aws dynamodb update-item \
  --table-name "$(aws cloudformation describe-stacks \
      --stack-name PolymarketScannerStack \
      --query "Stacks[0].Outputs[?OutputKey=='ConfigTableName'].OutputValue" \
      --output text)" \
  --key '{"scanner_id":{"S":"default"}}' \
  --update-expression "SET paper_trading_enabled = :t" \
  --expression-attribute-values '{":t":{"BOOL":true}}'
```

Default risk controls (from `scanner/src/polymarket_scanner/paper.py`):

| key                                 | default | meaning |
|-------------------------------------|---------|---------|
| `max_paper_trade_usdc`              | 100     | max notional per new open |
| `max_paper_position_usdc_per_market`| 250     | max combined notional per market |
| `max_total_paper_exposure_usdc`     | 2000    | portfolio-wide open exposure cap |
| `min_edge_to_trade`                 | 0.05    | required fair_value − mid |
| `min_liquidity_usdc`                | 500     | min liquidity to consider |
| `max_spread`                        | 0.10    | max ask − bid |
| `max_resolution_days`               | 30      | skip far-dated markets |
| `cooldown_seconds`                  | 900     | minimum gap between re-opens |
| `allow_short_horizon_only`          | false   | restrict to ≤ 7-day markets |
| `close_on_edge_flip`                | true    | exit when fair_value < mid |
| `slippage_buffer`                   | 0.01    | added to ask on simulated fills |

How simulated fills work:

- **BUY YES only.** The Polymarket Gamma feed does not reliably expose a
  NO-side ask, so the scanner only simulates BUY YES to avoid fake
  precision on the NO side.
- Entry price = best ask + `slippage_buffer`, capped at 0.99. Never
  midpoint.
- Mark price for open positions = current best bid (conservative exit).
- Exit price on close = current best bid; if unavailable, falls back to
  last mark. Exits are labelled with a `close_reason` such as
  `edge_flipped`, `resolution_reached`, `near_resolution`, `no_bid`,
  `market_closed`, or `market_gone`.
- A `cooldown_seconds` window on each market prevents the scanner from
  repeatedly opening and closing the same market every 5 minutes.

**Why this is not live trading and not financial advice:** Stage 2 only
writes rows to DynamoDB and never talks to any order book, smart
contract, or wallet. The `PaperPositionsTable` and `PaperTradesTable`
are inert records. Prediction markets are risky, the fair-value
placeholder is a toy log-normal model, and a paper-trading simulation
does not replicate real fill dynamics, latency, or resolution outcomes.

### Reset the paper portfolio

From the dashboard, click **Reset paper portfolio** and confirm. Or via
the CLI (logged-in user's bearer token required):

```
curl -X POST "$API_URL/paper/reset" \
  -H "authorization: Bearer $ID_TOKEN" \
  -H "content-type: application/json" \
  -d '{}'
```

This deletes every row in `PaperPositionsTable`, `PaperTradesTable`,
and the `paper#latest` summary row in `OpportunitiesTable` for the
configured `scanner_id`.
