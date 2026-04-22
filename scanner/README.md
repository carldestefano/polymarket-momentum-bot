# Polymarket BTC Scanner — Stage 1

**Scanner-only. No wallet. No private keys. No order placement. No trading.**

A beginner-friendly, read-only scanner that pulls active Polymarket
prediction markets, filters them to BTC-related markets, computes a set
of actionable metrics (best bid/ask, mid, spread, last, volume,
liquidity, time-to-resolution, threshold, fair-value placeholder,
edge, freshness), ranks the opportunities, and surfaces them through a
Cognito-protected static dashboard backed by a JWT-protected HTTP API.

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
- **DynamoDB tables** (pay-per-request) for config, scan summaries, and
  the latest ranked opportunities.
- **HTTP API Lambda** behind Amazon API Gateway HTTP API with a
  Cognito JWT authorizer. Endpoints: `/status`, `/config`,
  `/opportunities`, `/scans`, `/scan` (manual trigger).
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
