# AWS Deployment Guide — Polymarket BTC Scanner (Stage 1 + Stage 2)

This guide walks through deploying the scanner + paper-trading stack to
your own AWS account using AWS CDK v2 (Python). The stack is **Stage 1
scanner + Stage 2 paper trading only**: no wallet, no private keys, no
order placement, no live trading.

## 0. What you are about to deploy

- 1 Scanner Lambda (Python 3.12) scheduled every 5 minutes. It runs
  the scan and then the paper-trading tick.
- 1 API Lambda (Python 3.12) behind an HTTP API Gateway with a
  Cognito JWT authorizer.
- 5 DynamoDB tables (pay-per-request): `ConfigTable`, `ScansTable`,
  `OpportunitiesTable`, `PaperPositionsTable`, `PaperTradesTable`.
- 1 S3 bucket + 1 CloudFront distribution serving the static dashboard.
- 1 Cognito User Pool + Hosted UI domain for dashboard login.
- 2 CloudWatch log groups (1 month retention).

Everything is in a single CDK stack called **`PolymarketScannerStack`**.
Stage 2 is additive: updating an existing stack with this version will
**add** the two new paper tables without destroying existing scan
history. All tables use `RemovalPolicy.RETAIN` to protect against
accidental data loss.

Estimated AWS cost at idle: pennies per day (Lambda + DDB pay-per-request
+ CloudFront free tier). Scale depends on scan frequency and dashboard
traffic.

## 1. Prerequisites

Install locally on your machine:

- Python 3.10+ (`python3 --version`)
- Node.js 18+ (`node --version`) — required by the CDK CLI
- AWS CLI v2 (`aws --version`)
- AWS CDK v2 CLI: `npm install -g aws-cdk` then `cdk --version`

You also need an AWS account with permissions to create the resources
listed above. Configure your shell with an AWS profile / region:

```
aws configure --profile scanner
export AWS_PROFILE=scanner
export AWS_REGION=us-east-1   # or your preferred region
export CDK_DEFAULT_REGION=$AWS_REGION
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
```

Verify:

```
aws sts get-caller-identity
```

## 2. Clone the repo and enter the scanner dir

```
git clone https://github.com/carldestefano/polymarket-momentum-bot.git
cd polymarket-momentum-bot/scanner
```

Or if you already have the repo:

```
cd polymarket-momentum-bot
git pull
cd scanner
```

## 3. Create a Python venv for the CDK app

```
cd infra/cdk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Bootstrap CDK (first time only, per account/region)

```
cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
```

## 5. Synthesize (optional dry run)

```
cdk synth
```

This should complete with no errors and print the CloudFormation
template to stdout. No resources are created yet.

## 6. Deploy

```
cdk deploy
```

CDK will print a diff and ask you to confirm IAM and security-group
changes. Type `y`. Deployment takes ~5–10 minutes (CloudFront is the
slow part).

When it finishes you will see outputs like:

```
PolymarketScannerStack.DashboardUrl      = https://dXXXXXXXX.cloudfront.net
PolymarketScannerStack.ApiUrl            = https://XXXXXXXX.execute-api.us-east-1.amazonaws.com
PolymarketScannerStack.UserPoolId        = us-east-1_XXXXXXXXX
PolymarketScannerStack.UserPoolClientId  = 1a2b3c4d5e6f7g8h9i0j
PolymarketScannerStack.CognitoDomain     = https://polyscan-123456789012-us-east-1.auth.us-east-1.amazoncognito.com
PolymarketScannerStack.HostedUiLoginUrl  = https://.../login?client_id=...
PolymarketScannerStack.ConfigTableName   = ...
PolymarketScannerStack.ScansTableName    = ...
PolymarketScannerStack.OpportunitiesTableName = ...
PolymarketScannerStack.ScannerFunctionName    = ...
PolymarketScannerStack.ScheduleRuleName       = ...
```

Save these — you'll use `DashboardUrl`, `UserPoolId`, and
`ScannerFunctionName` below.

## 7. Create a Cognito dashboard user

The user pool is configured with `self_sign_up_enabled=False`, so you
create users from the AWS CLI. Replace `USER_POOL_ID` and `EMAIL`.

```
export POOL=$(aws cloudformation describe-stacks \
  --stack-name PolymarketScannerStack \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
  --output text)

aws cognito-idp admin-create-user \
  --user-pool-id "$POOL" \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --message-action SUPPRESS
```

Set a permanent password (must satisfy the policy: min 12 chars, upper,
lower, digit, symbol):

```
aws cognito-idp admin-set-user-password \
  --user-pool-id "$POOL" \
  --username you@example.com \
  --password 'ReplaceMe!12345' \
  --permanent
```

## 8. Open the dashboard

Open the `DashboardUrl` output in your browser, click **Log in with
Cognito**, and sign in with the user you just created. You land back
on the dashboard with a bearer token in `sessionStorage`, and the
scanner tables populate on the next scheduled scan.

## 9. Trigger a scan manually

Two ways.

**From the dashboard:** click **Trigger scan now** on the main page.

**From the CLI:**

```
export FN=$(aws cloudformation describe-stacks \
  --stack-name PolymarketScannerStack \
  --query "Stacks[0].Outputs[?OutputKey=='ScannerFunctionName'].OutputValue" \
  --output text)

aws lambda invoke --function-name "$FN" --payload '{}' /tmp/scan.json
cat /tmp/scan.json
```

## 10. View logs

```
export SCANNER_LG="/aws/lambda/$FN"
aws logs tail "$SCANNER_LG" --since 1h --follow
```

The API Lambda log group is similarly named after the API function.

## 11. Adjust scanner config (optional)

The scanner reads overrides from DynamoDB on each run. The API Lambda
exposes these via `POST /config`, but you can also set them directly:

```
export CFG=$(aws cloudformation describe-stacks \
  --stack-name PolymarketScannerStack \
  --query "Stacks[0].Outputs[?OutputKey=='ConfigTableName'].OutputValue" \
  --output text)

aws dynamodb put-item --table-name "$CFG" --item '{
  "scanner_id": {"S": "default"},
  "market_limit": {"N": "300"},
  "top_n": {"N": "25"},
  "short_horizon_only": {"BOOL": true}
}'
```

Recognised scanner keys: `market_limit`, `top_n`, `short_horizon_only`,
`log_level`.

Paper trading keys (see `Paper trading (Stage 2)` below):
`paper_trading_enabled`, `max_paper_trade_usdc`,
`max_paper_position_usdc_per_market`, `max_total_paper_exposure_usdc`,
`min_edge_to_trade`, `min_liquidity_usdc`, `max_spread`,
`max_resolution_days`, `cooldown_seconds`, `allow_short_horizon_only`,
`close_on_edge_flip`, `slippage_buffer`.

## Paper trading (Stage 2)

Paper trading is **disabled by default**. When disabled, the scanner
still runs a paper tick on each scan so any pre-existing open positions
are marked to market, but no new positions are opened. To enable:

```
export CFG=$(aws cloudformation describe-stacks \
  --stack-name PolymarketScannerStack \
  --query "Stacks[0].Outputs[?OutputKey=='ConfigTableName'].OutputValue" \
  --output text)

aws dynamodb update-item --table-name "$CFG" \
  --key '{"scanner_id":{"S":"default"}}' \
  --update-expression "SET paper_trading_enabled = :t" \
  --expression-attribute-values '{":t":{"BOOL":true}}'
```

Tighten the risk controls on the same row (example: smaller caps, only
take higher-edge trades, short-horizon markets only):

```
aws dynamodb update-item --table-name "$CFG" \
  --key '{"scanner_id":{"S":"default"}}' \
  --update-expression "SET \
      max_paper_trade_usdc = :t, \
      max_paper_position_usdc_per_market = :m, \
      max_total_paper_exposure_usdc = :e, \
      min_edge_to_trade = :edge, \
      min_liquidity_usdc = :liq, \
      max_spread = :sp, \
      max_resolution_days = :d, \
      cooldown_seconds = :cd, \
      allow_short_horizon_only = :sh, \
      close_on_edge_flip = :cf, \
      slippage_buffer = :sl" \
  --expression-attribute-values '{
      ":t":  {"N":"50"},
      ":m":  {"N":"100"},
      ":e":  {"N":"500"},
      ":edge":{"N":"0.08"},
      ":liq":{"N":"1000"},
      ":sp": {"N":"0.08"},
      ":d":  {"N":"14"},
      ":cd": {"N":"1800"},
      ":sh": {"BOOL":true},
      ":cf": {"BOOL":true},
      ":sl": {"N":"0.01"}
  }'
```

You can also POST `/config` via the dashboard's authenticated API with
the same keys — both paths write to `ConfigTable`.

### How paper fills are simulated

- **BUY YES only.** Polymarket's Gamma feed does not reliably expose a
  NO-side order book to the scanner, so simulating BUY NO would rely on
  invented data. Only BUY YES is simulated to keep paper P&L honest.
- Entry price = current best ask + `slippage_buffer`, capped at 0.99 —
  never the midpoint.
- Mark price for an open position = current best bid (conservative).
- Exit price on close = current best bid. If a fresh bid is unavailable
  (market disappeared from the feed, no bids) the position exits at its
  last mark. Closes are labelled with a `close_reason`:
  `edge_flipped`, `resolution_reached`, `near_resolution` (<1h),
  `no_bid`, `market_closed`, or `market_gone`.
- A `cooldown_seconds` window per market blocks rapid re-opens after a
  close or prior fill.
- Risk gates (edge, spread, liquidity, resolution horizon, and the
  per-trade / per-market / total-exposure caps) are enforced before
  every open.

### Why this is not live trading and not financial advice

The paper-trading engine only writes DynamoDB rows. It never contacts
Polymarket, an exchange, a smart contract, or any wallet. It has no
wallet credentials and the IAM role has no Secrets Manager permissions.
Real trading involves fee schedules, latency, partial fills, and
resolution uncertainty that this simulation deliberately does not
model. Treat paper P&L as a signal-quality diagnostic, not a forecast
of live returns.

### Reset the paper trading state

Destructive operation — wipes every simulated position and fill for the
configured `scanner_id`. Either click **Reset paper portfolio** on the
dashboard (with browser confirmation), or call the authenticated API
endpoint:

```
# Grab a token by logging in through the dashboard once and copying
# pbs_id_token out of sessionStorage, or use another Cognito OAuth
# client flow; then:
curl -X POST "$API_URL/paper/reset" \
  -H "authorization: Bearer $ID_TOKEN" \
  -H "content-type: application/json" \
  -d '{}'
```

If you prefer to wipe straight from DynamoDB (bypassing the API), scan
the two tables and delete the rows with matching `scanner_id`:

```
export POS=$(aws cloudformation describe-stacks \
  --stack-name PolymarketScannerStack \
  --query "Stacks[0].Outputs[?OutputKey=='PaperPositionsTableName'].OutputValue" \
  --output text)
export TRD=$(aws cloudformation describe-stacks \
  --stack-name PolymarketScannerStack \
  --query "Stacks[0].Outputs[?OutputKey=='PaperTradesTableName'].OutputValue" \
  --output text)
# (Use aws dynamodb scan + delete-item in a loop, or recreate tables.)
```

## 12. Change the schedule

Edit `infra/cdk/app.py` or pass context:

```
cdk deploy -c schedule='rate(15 minutes)'
```

## 13. Destroy the stack

```
cdk destroy
```

DynamoDB tables and the Cognito User Pool have `RETAIN` removal
policies by default (so you don't lose history/users). If you want them
deleted too, first delete them manually from the AWS console or with
`aws dynamodb delete-table` / `aws cognito-idp delete-user-pool`.

## Troubleshooting

- **`cdk deploy` fails with "requires bootstrap"** → run step 4.
- **Dashboard shows "Cognito is not configured"** → the static
  `config.js` was not replaced at deploy time. Re-run `cdk deploy`.
- **401 from the API** → your token expired. Log out and log back in.
- **No opportunities appear** → either the scheduled scan has not run
  yet (first scan happens within 5 minutes) or Polymarket returned
  zero active BTC markets at scan time. Trigger a manual scan (step 9)
  and watch the scanner log group.

## Security notes

- **No wallet. No private keys. No order placement. No live trading.**
- The API Lambda has no Secrets Manager permissions.
- All API routes require a valid Cognito JWT, including every `/paper/*`
  route and the destructive `POST /paper/reset`.
- CORS is locked to the CloudFront dashboard origin.
- IAM roles are scoped per-Lambda with least-privilege DynamoDB grants
  (scanner + API both read/write `PaperPositionsTable` and
  `PaperTradesTable`; neither has any trading permission).
