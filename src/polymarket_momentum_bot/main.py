"""Run loop: discover → evaluate → risk-check → trade.

Start in dry-run mode (the default) until you are confident.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import BotConfig
from .logging_setup import setup_logging
from .market_data import MarketDataClient, extract_token_ids
from .risk import RiskManager
from .strategy import Signal, evaluate
from .trader import Trader

log = logging.getLogger("polymarket_momentum_bot")


def _market_metrics(market: Dict[str, Any]) -> Dict[str, float]:
    def _f(key: str) -> float:
        try:
            return float(market.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "volume24hr": _f("volume24hr"),
        "liquidity": _f("liquidity") or _f("liquidityNum"),
    }


def run_once(
    config: BotConfig,
    md: MarketDataClient,
    risk: RiskManager,
    trader: Trader,
    state_writer: Optional[Any] = None,
    metrics: Optional[Any] = None,
) -> None:
    """Execute one full scan across the top markets."""
    markets = md.list_active_markets(
        max_markets=config.max_markets,
        allowed_categories=config.category_allowlist or None,
    )
    log.info("Fetched %d active markets", len(markets))
    if state_writer is not None:
        state_writer.heartbeat(
            status="scanning",
            extra={
                "markets_fetched": len(markets),
                "dry_run": config.dry_run,
                "kill_switch": config.kill_switch,
            },
        )
    if metrics is not None:
        metrics.put("MarketsFetched", len(markets))

    for market in markets:
        question = market.get("question") or market.get("slug") or "?"
        token_ids = extract_token_ids(market)
        if not token_ids:
            log.debug("Skip (no CLOB token ids): %s", question)
            continue

        m_metrics = _market_metrics(market)
        spread = md.spread(token_ids[0])
        tradeable, reason = risk.market_is_tradeable(
            volume_24h=m_metrics["volume24hr"],
            liquidity=m_metrics["liquidity"],
            spread=spread,
        )
        if not tradeable:
            log.debug("Skip market %s: %s", question, reason)
            continue

        # Use the first (usually YES) outcome token for the signal.
        token_id = token_ids[0]
        try:
            history = md.price_history(
                token_id=token_id,
                interval=config.price_interval,
                fidelity=config.price_fidelity,
            )
        except Exception as exc:
            log.warning("price_history failed for %s: %s", token_id, exc)
            if state_writer is not None:
                state_writer.record_error("price_history", f"{token_id}: {exc}")
            continue

        result = evaluate(
            history,
            window=config.ma_window,
            currently_long=risk.book.is_long(token_id),
        )
        log.info(
            "Market %.60s | last=%s MA%s=%s signal=%s (%s)",
            question,
            f"{result.last_price:.4f}" if result.last_price is not None else "-",
            config.ma_window,
            f"{result.moving_average:.4f}" if result.moving_average is not None else "-",
            result.signal.value,
            result.reason,
        )
        if state_writer is not None:
            state_writer.record_signal(
                token_id=token_id,
                question=str(question),
                signal=result.signal.value,
                last_price=result.last_price,
                moving_average=result.moving_average,
                reason=result.reason,
            )

        if result.signal is Signal.HOLD or result.last_price is None:
            continue

        side = result.signal.value
        price = float(result.last_price)
        if side == "BUY":
            size = max(0.0, config.max_trade_size_usdc / max(price, 1e-6))
        else:
            pos = risk.book.positions.get(token_id)
            size = pos.size if pos else 0.0
        if size <= 0:
            log.info("Skip %s %s: computed size <= 0", side, token_id)
            continue

        ok, reason = risk.check_order(token_id, side, size, price)
        if not ok:
            log.info("Risk blocked %s %s: %s", side, token_id, reason)
            continue

        order = trader.place_limit_order(token_id, side, size, price)
        if state_writer is not None:
            state_writer.record_order(
                token_id=token_id,
                side=side,
                size=size,
                price=price,
                dry_run=order.dry_run,
                ok=order.ok,
                response=order.response,
                error=order.error,
            )
        if order.ok:
            risk.book.record_fill(token_id, side, size, price)
            if state_writer is not None:
                pos = risk.book.positions.get(token_id)
                state_writer.record_position(
                    token_id=token_id,
                    size=pos.size if pos else 0.0,
                    avg_price=pos.avg_price if pos else 0.0,
                )
            if metrics is not None:
                metrics.put("OrdersPlaced", 1)
            log.info(
                "Order ok | dry_run=%s %s %s size=%.4f @ %.4f resp=%s",
                order.dry_run,
                side,
                token_id,
                size,
                price,
                order.response,
            )
        else:
            if metrics is not None:
                metrics.put("OrdersFailed", 1)
            log.error("Order failed | %s %s: %s", side, token_id, order.error)


def _bootstrap_aws(config: BotConfig) -> Tuple[BotConfig, Optional[Any], Optional[Any]]:
    """Apply AWS adapters if configured. Returns (config, state_writer, metrics)."""
    if os.getenv("AWS_SECRET_ID"):
        try:
            from .aws.secrets import hydrate_env_from_secret

            hydrate_env_from_secret()
            config = BotConfig.from_env(env_file=None)
        except Exception as exc:  # pragma: no cover - network path
            log.error("Secrets Manager hydration failed: %s", exc)

    if os.getenv("CONFIG_TABLE"):
        try:
            from .aws.dynamo_config import fetch_config_overrides

            overrides = fetch_config_overrides()
            overrides.apply_to(config)
            log.info("Applied %d config overrides from DynamoDB", len(overrides.values))
        except Exception as exc:  # pragma: no cover - network path
            log.error("Config overlay load failed: %s", exc)

    state_writer = None
    metrics = None
    try:
        from .aws.state import MetricsPublisher, StateWriter

        writer = StateWriter()
        if writer.enabled:
            state_writer = writer
        publisher = MetricsPublisher()
        if publisher.enabled:
            metrics = publisher
    except Exception as exc:  # pragma: no cover
        log.debug("AWS state/metrics not initialised: %s", exc)

    return config, state_writer, metrics


def run_forever(
    config: BotConfig,
    state_writer: Optional[Any] = None,
    metrics: Optional[Any] = None,
) -> None:
    md = MarketDataClient(clob_host=config.clob_host)
    risk = RiskManager(config)
    trader = Trader(config)
    if not config.dry_run:
        trader.connect()
    log.info(
        "Starting bot | dry_run=%s kill_switch=%s MA=%d interval=%s",
        config.dry_run,
        config.kill_switch,
        config.ma_window,
        config.price_interval,
    )
    if state_writer is not None:
        state_writer.heartbeat(status="starting")
    while True:
        try:
            run_once(config, md, risk, trader, state_writer=state_writer, metrics=metrics)
        except KeyboardInterrupt:
            log.info("Interrupted by user — exiting.")
            if state_writer is not None:
                state_writer.heartbeat(status="stopped")
            return
        except Exception as exc:
            log.exception("run_once crashed; will retry after poll interval")
            if state_writer is not None:
                state_writer.record_error("run_once", str(exc))
        time.sleep(max(1, config.poll_interval_sec))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Polymarket CLOB momentum bot (educational)."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit (useful for cron/testing).",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file. Defaults to .env in the current directory.",
    )
    args = parser.parse_args(argv)

    config = BotConfig.from_env(env_file=args.env_file)
    setup_logging(level=config.log_level)

    log.warning(
        "DISCLAIMER: educational software; not financial advice. "
        "Running live trades involves real money and real risk."
    )

    config, state_writer, metrics = _bootstrap_aws(config)

    if args.once:
        md = MarketDataClient(clob_host=config.clob_host)
        risk = RiskManager(config)
        trader = Trader(config)
        if not config.dry_run:
            trader.connect()
        run_once(config, md, risk, trader, state_writer=state_writer, metrics=metrics)
        if state_writer is not None:
            state_writer.heartbeat(status="idle")
        return 0

    run_forever(config, state_writer=state_writer, metrics=metrics)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
