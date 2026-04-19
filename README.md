# Polymarket Momentum Bot

A small, beginner-friendly Python project that trades Polymarket CLOB markets
using a simple **20-period moving-average momentum** strategy. It is
intentionally compact so you can read the whole thing top to bottom.

> ⚠️ **Disclaimer — educational software only.** This is not financial
> advice. Prediction markets can and do lose money. You are responsible for
> reviewing the code, understanding the risks, complying with your local laws
> (Polymarket is not available in some jurisdictions, including the US), and
> for any losses. Start in dry-run mode. Do not share your private key.

## What it does

1. **Discovers active markets** via Polymarket's public Gamma API — no wallet
   required for market discovery.
2. For each eligible market, **pulls CLOB price history** from
   `GET /prices-history` (points shaped `{t, p}`).
3. Computes a **20-period simple moving average**; emits:
   - `BUY` when last price crosses **above** the MA and no position is open.
   - `SELL` when last price crosses **below** the MA and a position is open.
4. **Applies risk limits** (size, daily spend, open positions, spread,
   liquidity, cooldown, kill switch) before anything is sent.
5. **Places GTC limit orders** via the official
   [`py-clob-client-v2`](https://github.com/Polymarket/py-clob-client-v2) SDK
   — or logs a simulated fill if `DRY_RUN=true` (the default).

## Project layout

```
.
├── src/polymarket_momentum_bot/
│   ├── config.py          # env-driven settings
│   ├── market_data.py     # public Gamma + CLOB HTTP helpers
│   ├── strategy.py        # 20-period MA momentum
│   ├── risk.py            # limits + position book
│   ├── trader.py          # py-clob-client-v2 wrapper with dry-run
│   ├── logging_setup.py   # console + rotating file logs
│   └── main.py            # run loop (--once for a single scan)
├── tests/                 # pytest unit tests (offline)
├── requirements.txt
├── pyproject.toml
├── .env.example
└── README.md
```

## Quick start

```bash
# 1. Clone + set up a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install runtime deps (the CLOB SDK is listed but optional for dry-run)
pip install -r requirements.txt
# If py-clob-client-v2 fails to resolve from PyPI, install from GitHub:
#   pip install git+https://github.com/Polymarket/py-clob-client-v2.git

# 3. Copy the example env and edit it
cp .env.example .env
$EDITOR .env

# 4. Run a single dry-run scan to make sure things work
python -m polymarket_momentum_bot.main --once

# 5. Run forever (still dry-run unless you changed DRY_RUN=false)
python -m polymarket_momentum_bot.main
```

## Wallet, funder, and signature types

Polymarket's CLOB uses **two-layer auth**:

- **L1 (EIP-712)** — your private key signs a typed message; the SDK uses
  that signature to *derive* your L2 API credentials (API key + secret +
  passphrase).
- **L2 (HMAC)** — those derived credentials authenticate REST calls to place,
  cancel, and query orders. Every order payload is also signed by your
  private key.

Set the following in `.env`:

| Var               | Meaning                                                                   |
|-------------------|---------------------------------------------------------------------------|
| `PRIVATE_KEY`     | Hex private key (0x-prefixed) of the signer EOA.                          |
| `SIGNATURE_TYPE`  | `0` EOA, `1` POLY_PROXY, `2` GNOSIS_SAFE.                                 |
| `FUNDER_ADDRESS`  | Wallet that holds the USDC. For EOA this is the signer address itself. For `POLY_PROXY` or `GNOSIS_SAFE`, this **must** be the proxy/Safe address. |
| `CHAIN_ID`        | `137` (Polygon mainnet).                                                  |
| `CLOB_HOST`       | `https://clob.polymarket.com`.                                            |

### Approvals / allowances

Before your first live trade you must grant the CLOB exchange contracts the
usual USDC + CTF approvals. The SDK (and the Polymarket web UI) handle this
during onboarding. See the Polymarket docs:
<https://docs.polymarket.com/>.

## Risk limits (defaults shown)

| Var                    | Default | What it controls                                  |
|------------------------|--------:|---------------------------------------------------|
| `DRY_RUN`              | `true`  | If true, no live orders are placed.               |
| `KILL_SWITCH`          | `false` | Master off-switch — blocks every order.           |
| `MAX_TRADE_SIZE_USDC`  | `5`     | Max USDC notional per single order.               |
| `MAX_DAILY_SPEND_USDC` | `25`    | Max USDC spent per UTC day.                       |
| `MAX_OPEN_POSITIONS`   | `3`     | Max concurrent positions.                         |
| `MIN_VOLUME_USDC`      | `5000`  | Skip markets below this 24h volume.               |
| `MIN_LIQUIDITY_USDC`   | `500`   | Skip markets below this on-book liquidity.        |
| `MAX_SPREAD`           | `0.05`  | Skip markets with wider bid/ask spread.           |
| `TRADE_COOLDOWN_SEC`   | `300`   | Minimum seconds between trades on same market.    |
| `CATEGORY_ALLOWLIST`   | *(empty)* | Comma-separated category/tag allowlist.         |
| `MARKET_ALLOWLIST`     | *(empty)* | Comma-separated CLOB token id allowlist.        |

## Running the tests

The tests are **offline** — they do not need network or credentials, and
they do not require `py-clob-client-v2` to be installed.

```bash
pip install pytest
pytest -q
```

## SDK notes

- The package name is `py-clob-client-v2`; depending on the release the
  importable module is either `py_clob_client` or `py_clob_client_v2`. The
  trader in `src/polymarket_momentum_bot/trader.py` tries both.
- The public API is still evolving. If a method name in the SDK changes,
  update the `create_or_derive_api_creds` / `create_and_post_order` calls in
  `trader.py` — comments mark the spots.
- For **market discovery / price history** nothing is signed — those are
  plain HTTP calls in `market_data.py`.

## Safety & legal

- This is **educational**. It ships tiny default sizes on purpose. Do not
  raise the limits until you have watched the bot in dry-run for a while and
  you understand every trade it logs.
- Polymarket is geo-restricted. Using it from a restricted region may
  violate the ToS or local law. **You** are responsible for compliance.
- Never commit your `.env` or private key to git. `.gitignore` already blocks
  `.env`, but double-check.
- Nothing in this repo is financial, legal, or tax advice.

## License

MIT. See `pyproject.toml`.
