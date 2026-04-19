# AWS deployment guide

This guide takes you from zero AWS infrastructure to a running **dry-run**
Polymarket momentum bot on ECS Fargate with a web dashboard. You will run
every command below — nothing in this repo talks to AWS directly.

> ⚠️ **Safety first.**
> - Keep `DRY_RUN=true` until you have watched the bot run, read its signals,
>   and understand the strategy.
> - Use a **dedicated trading wallet** with a small balance. Never use a
>   wallet that holds real funds you cannot afford to lose.
> - **Never** paste your private key into a chat, email, ticket, or public
>   git commit. The wallet secret is populated manually via the AWS CLI.
> - Use an IAM user (or SSO role) with MFA enabled. Do not use the AWS
>   account root.
> - Prediction markets may be restricted in your jurisdiction. You are
>   responsible for compliance with Polymarket's terms and local law.
> - This is educational software, not financial advice.

## 0. Prerequisites

Install on your workstation:

- **AWS CLI v2** — [docs](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
- **Docker** — any recent version
- **Node.js 18+** and `npm`
- **Python 3.10+** and `pip`
- **AWS CDK v2**: `npm install -g aws-cdk`

Verify:

```bash
aws --version
docker --version
node --version
cdk --version
python3 --version
```

## 1. Configure AWS credentials

Create an IAM user (or SSO role) with administrator access **to this account
only** and configure a named profile:

```bash
aws configure --profile polybot
# AWS Access Key ID:    <your key>
# AWS Secret Access Key: <your secret>
# Default region name:   us-east-1
# Default output format: json

export AWS_PROFILE=polybot
export AWS_REGION=us-east-1
aws sts get-caller-identity
```

## 2. Clone and install

```bash
git clone https://github.com/carldestefano/polymarket-momentum-bot.git
cd polymarket-momentum-bot

# Python deps for CDK
cd infra/cdk
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ../..
```

## 3. Bootstrap CDK (first time in this account/region only)

```bash
cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/$AWS_REGION \
  --profile $AWS_PROFILE
```

## 4. Initial deploy (creates ECR repo, placeholder secret, tables, etc.)

The first deploy will fail on the ECS task because there is no image in ECR
yet. That's expected — we'll push the image next.

```bash
cd infra/cdk
cdk deploy --require-approval never
cd ../..
```

Save the outputs (you'll see them in the terminal and in the CloudFormation
console). The important ones:

- `EcrRepoUri`
- `WalletSecretName`
- `ConfigTableName`
- `DashboardUrl`

## 5. Build and push the Docker image

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI=$(aws cloudformation describe-stacks \
  --stack-name PolymarketMomentumBot \
  --query "Stacks[0].Outputs[?OutputKey=='EcrRepoUri'].OutputValue" \
  --output text)

aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin $ECR_URI

docker build -t polymarket-momentum-bot:latest .
docker tag polymarket-momentum-bot:latest $ECR_URI:latest
docker push $ECR_URI:latest
```

## 6. Populate the wallet secret

The CDK created a placeholder secret. Replace its value with your real
credentials **from the AWS console** or CLI. Use a dedicated trading wallet.

**AWS Console:** Secrets Manager → `<WalletSecretName>` → *Retrieve secret value* → *Edit* → paste the JSON below.

**CLI:**

```bash
SECRET_NAME=$(aws cloudformation describe-stacks \
  --stack-name PolymarketMomentumBot \
  --query "Stacks[0].Outputs[?OutputKey=='WalletSecretName'].OutputValue" \
  --output text)

cat > /tmp/wallet.json <<'EOF'
{
  "PRIVATE_KEY": "0xYOUR_PRIVATE_KEY_HEX",
  "FUNDER_ADDRESS": "0xYOUR_FUNDER_ADDRESS",
  "SIGNATURE_TYPE": "0",
  "CHAIN_ID": "137",
  "CLOB_HOST": "https://clob.polymarket.com"
}
EOF

aws secretsmanager put-secret-value \
  --secret-id "$SECRET_NAME" \
  --secret-string file:///tmp/wallet.json

shred -u /tmp/wallet.json 2>/dev/null || rm -f /tmp/wallet.json
```

Notes:
- `SIGNATURE_TYPE`: `0` EOA, `1` POLY_PROXY, `2` GNOSIS_SAFE.
- You do **not** need to populate the secret for dry-run mode, because the
  dry-run code path never needs to sign anything. Leaving the placeholder in
  place is safe.

## 7. Seed the config table with DRY_RUN=true

```bash
CONFIG_TABLE=$(aws cloudformation describe-stacks \
  --stack-name PolymarketMomentumBot \
  --query "Stacks[0].Outputs[?OutputKey=='ConfigTableName'].OutputValue" \
  --output text)

aws dynamodb put-item \
  --table-name "$CONFIG_TABLE" \
  --item '{
    "bot_id":      {"S": "default"},
    "dry_run":     {"BOOL": true},
    "kill_switch": {"BOOL": false},
    "max_trade_size_usdc": {"N": "5"},
    "max_daily_spend_usdc": {"N": "25"}
  }'
```

## 8. Run the bot

The EventBridge rule runs the bot on a cadence (default: every 5 minutes).
To trigger a one-off run immediately:

```bash
CLUSTER=$(aws cloudformation describe-stacks \
  --stack-name PolymarketMomentumBot \
  --query "Stacks[0].Outputs[?OutputKey=='ClusterName'].OutputValue" \
  --output text)

TASK_DEF=$(aws cloudformation describe-stacks \
  --stack-name PolymarketMomentumBot \
  --query "Stacks[0].Outputs[?OutputKey=='TaskDefinitionArn'].OutputValue" \
  --output text)

# Grab any public subnet from the VPC created by the stack.
SUBNET=$(aws ec2 describe-subnets \
  --filters "Name=tag:aws-cdk:subnet-type,Values=Public" \
  --query "Subnets[0].SubnetId" --output text)

aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_DEF" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[\"$SUBNET\"],assignPublicIp=ENABLED}"
```

### Always-on service mode (optional)

The default is scheduled one-shot runs, which is cheaper and safer. If you
want an always-on service instead:

1. Disable the EventBridge rule:
   ```bash
   aws events disable-rule --name $(aws events list-rules \
     --query "Rules[?starts_with(Name, 'PolymarketMomentumBot-BotScheduleRule')].Name | [0]" \
     --output text)
   ```
2. Create an ECS service from the task definition. The container's default
   `CMD` is `--once --env-file ""`; override it for continuous mode:
   ```bash
   aws ecs register-task-definition ...  # override command = ["--env-file", ""]
   aws ecs create-service --cluster $CLUSTER --service-name momentum-bot \
     --task-definition <arn-of-override> --launch-type FARGATE --desired-count 1 \
     --network-configuration ...
   ```

## 9. View logs

```bash
LOG_GROUP=$(aws cloudformation describe-stacks \
  --stack-name PolymarketMomentumBot \
  --query "Stacks[0].Outputs[?OutputKey=='LogGroupName'].OutputValue" \
  --output text)

aws logs tail "$LOG_GROUP" --follow
```

## 10. Open the dashboard

```bash
aws cloudformation describe-stacks \
  --stack-name PolymarketMomentumBot \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardUrl'].OutputValue" \
  --output text
```

Open the URL. You should see heartbeat, recent signals, and recent orders.

### Change kill-switch / dry-run via the dashboard

The dashboard has buttons for both. They write to the `ConfigTable`; the bot
reads that table at the start of every scan, so changes take effect on the
next cadence tick.

## 11. Tear everything down

```bash
cd infra/cdk
cdk destroy
```

Tables and the wallet secret are retained on stack deletion by default
(so you cannot accidentally lose state). Delete them manually in the console
if you want a truly clean slate.

## GUI authentication

The default dashboard is **open to the public internet**. For a real
deployment, front the CloudFront distribution with one of:

- Cognito Hosted UI + Lambda@Edge (most flexible)
- AWS WAF IP allowlist (simplest, lowest-friction)
- A CloudFront signed-cookie gate

Each of these is a paragraph of code in the stack — intentionally left out of
the default to keep the first-time deploy simple. **Do not** expose live
trading controls without auth.

## Cost notes (qualitative)

- **ECS Fargate** scheduled one-shot every 5 min: tiny — pennies per day.
- **DynamoDB** pay-per-request with this traffic: free-tier eligible.
- **Lambda + API Gateway** dashboard traffic: well within free tier.
- **CloudFront + S3** static site: under a dollar a month at low traffic.
- **NAT gateway**: not used — the stack uses public subnets only.
- **Logs**: set to 30-day retention by default; drop to 7 days if needed.

The biggest cost surprises on AWS are (a) forgetting to tear down unused
resources and (b) leaving the bot in **live** mode with wider risk limits.
Keep `DRY_RUN=true` until you are certain.

## Troubleshooting

**ECS task stuck in PROVISIONING or STOPPED:**
check the task's *Stopped reason* in the console — usually an image pull
failure (step 5 not yet done) or a missing secret permission.

**`get_secret_value` AccessDenied:**
only the ECS task role has access. The GUI Lambda does not — by design.

**Dashboard shows empty tables:**
the bot hasn't completed a scan yet. Wait for the next EventBridge tick or
trigger a one-off run (step 8). Log lines go to CloudWatch Logs (step 9).

**`cdk bootstrap` fails with an account mismatch:**
double-check `AWS_PROFILE` and `aws sts get-caller-identity`.

## File map

- `Dockerfile` — bot container (non-root, `boto3` installed)
- `src/polymarket_momentum_bot/aws/` — optional AWS adapters
- `infra/cdk/` — CDK v2 (Python) app
  - `stacks/bot_stack.py` — one stack, all resources
  - `lambda/api/handler.py` — HTTP API Lambda
- `infra/gui/` — static dashboard (HTML/CSS/JS)
