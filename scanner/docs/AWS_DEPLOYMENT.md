# AWS Deployment Guide — Polymarket BTC Scanner (Stage 1)

This guide walks through deploying the scanner-only stack to your own
AWS account using AWS CDK v2 (Python). The stack is Stage 1: **no
wallet, no private keys, no order placement, no trading**.

## 0. What you are about to deploy

- 1 Scanner Lambda (Python 3.12) scheduled every 5 minutes.
- 1 API Lambda (Python 3.12) behind an HTTP API Gateway with a
  Cognito JWT authorizer.
- 3 DynamoDB tables (pay-per-request): config, scans, opportunities.
- 1 S3 bucket + 1 CloudFront distribution serving the static dashboard.
- 1 Cognito User Pool + Hosted UI domain for dashboard login.
- 2 CloudWatch log groups (1 month retention).

Everything is in a single CDK stack called **`PolymarketScannerStack`**.

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

Recognised keys: `market_limit`, `top_n`, `short_horizon_only`,
`log_level`.

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

- **No wallet. No private keys. No order placement. No trading.**
- The API Lambda has no Secrets Manager permissions.
- All API routes require a valid Cognito JWT.
- CORS is locked to the CloudFront dashboard origin.
- IAM roles are scoped per-Lambda with least-privilege DynamoDB grants.
