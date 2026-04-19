"""Order placement — thin wrapper around ``py-clob-client-v2``.

The goal of this module is to insulate the rest of the bot from SDK version
churn. We lazily import the client so the bot (and its tests) can run without
the SDK installed. In dry-run mode we never touch the network for trading —
all orders are logged and returned as simulated fills.

Polymarket CLOB auth (summary):
- **L1**: sign an EIP-712 message with your private key. This *derives* your
  L2 API credentials (api key + secret + passphrase). The SDK does this for
  you in ``create_or_derive_api_creds`` / ``create_api_key``.
- **L2**: the derived HMAC credentials authenticate REST calls to place,
  cancel, and query orders.
- Order payloads are additionally signed by the private key and posted to the
  CLOB.
- ``signature_type`` selects how orders are signed:
    * ``0`` EOA         — signer == funder (your own wallet).
    * ``1`` POLY_PROXY  — signer is an EOA, funder is the Polymarket proxy.
    * ``2`` GNOSIS_SAFE — funder is a Safe contract.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import BotConfig

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    ok: bool
    dry_run: bool
    token_id: str
    side: str
    size: float
    price: float
    response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class Trader:
    """Uniform order-placement surface with a dry-run fallback.

    The live path requires ``py-clob-client-v2``. It is imported lazily so
    tests can exercise the dry-run code path without the SDK installed.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._client: Optional[Any] = None  # py_clob_client_v2.client.ClobClient
        self._client_error: Optional[str] = None

    # ------------------------------------------------------------- lifecycle

    def connect(self) -> None:
        """Initialise the live CLOB client. Safe to call multiple times."""
        if self.config.dry_run:
            log.info("DRY_RUN is true — skipping live CLOB client init.")
            return
        if self._client is not None:
            return

        self.config.require_live_credentials()

        try:
            # NOTE: The official package is published as ``py-clob-client-v2``
            # and installs an importable module named ``py_clob_client``. We
            # try both names to be robust across versions.
            try:
                from py_clob_client.client import ClobClient  # type: ignore
                from py_clob_client.clob_types import ApiCreds  # type: ignore
            except ImportError:  # pragma: no cover
                from py_clob_client_v2.client import ClobClient  # type: ignore
                from py_clob_client_v2.clob_types import ApiCreds  # type: ignore
        except ImportError as exc:
            self._client_error = (
                "py-clob-client-v2 is not installed. Install with: "
                "pip install py-clob-client-v2  (or) "
                "pip install git+https://github.com/Polymarket/py-clob-client-v2.git"
            )
            raise RuntimeError(self._client_error) from exc

        client = ClobClient(
            host=self.config.clob_host,
            key=self.config.private_key,
            chain_id=self.config.chain_id,
            signature_type=self.config.signature_type,
            funder=self.config.funder_address,
        )

        # Derive or create L2 API credentials via L1 EIP-712 signature.
        # ``create_or_derive_api_creds`` is the current method name; older
        # builds use ``create_api_key``. Try both.
        creds = None
        for attr in ("create_or_derive_api_creds", "create_api_key"):
            method = getattr(client, attr, None)
            if callable(method):
                try:
                    creds = method()
                    break
                except Exception as exc:  # pragma: no cover - network path
                    log.warning("%s failed: %s", attr, exc)
        if creds is None:
            raise RuntimeError(
                "Could not derive CLOB API credentials. Check PRIVATE_KEY, "
                "FUNDER_ADDRESS, SIGNATURE_TYPE, and SDK version."
            )

        # Attach credentials so subsequent REST calls are authenticated.
        set_creds = getattr(client, "set_api_creds", None)
        if callable(set_creds):
            set_creds(creds)

        self._client = client
        log.info(
            "Connected to CLOB host=%s chain=%s signature_type=%s funder=%s",
            self.config.clob_host,
            self.config.chain_id,
            self.config.signature_type,
            self.config.funder_address,
        )

    # ------------------------------------------------------------- ordering

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
    ) -> OrderResult:
        """Place a GTC limit order.

        In dry-run mode, returns a simulated fill without any network call.
        """
        side_u = side.upper()
        if side_u not in {"BUY", "SELL"}:
            return OrderResult(
                ok=False,
                dry_run=self.config.dry_run,
                token_id=token_id,
                side=side_u,
                size=size,
                price=price,
                error=f"invalid side: {side}",
            )

        if self.config.dry_run:
            log.info(
                "DRY_RUN order | %s %s size=%.4f @ %.4f on %s",
                side_u,
                token_id,
                size,
                price,
                self.config.clob_host,
            )
            return OrderResult(
                ok=True,
                dry_run=True,
                token_id=token_id,
                side=side_u,
                size=size,
                price=price,
                response={"simulated": True, "ts": int(time.time())},
            )

        # Live path ------------------------------------------------
        try:
            self.connect()
            assert self._client is not None
            # Try a few method names / payload shapes to be robust across
            # SDK versions. Users can pin the exact version they tested.
            from importlib import import_module

            try:
                types_mod = import_module("py_clob_client.clob_types")
            except ImportError:  # pragma: no cover
                types_mod = import_module("py_clob_client_v2.clob_types")

            order_args_cls = getattr(types_mod, "OrderArgs", None)
            if order_args_cls is None:
                raise RuntimeError("OrderArgs not found in clob_types")
            order_args = order_args_cls(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=side_u,
            )

            create_and_post = getattr(self._client, "create_and_post_order", None)
            if callable(create_and_post):
                response = create_and_post(order_args)
            else:  # pragma: no cover - compatibility branch
                signed = self._client.create_order(order_args)
                response = self._client.post_order(signed)

            return OrderResult(
                ok=True,
                dry_run=False,
                token_id=token_id,
                side=side_u,
                size=size,
                price=price,
                response=response if isinstance(response, dict) else {"raw": str(response)},
            )
        except Exception as exc:  # pragma: no cover - network path
            log.exception("order failed: %s", exc)
            return OrderResult(
                ok=False,
                dry_run=False,
                token_id=token_id,
                side=side_u,
                size=size,
                price=price,
                error=str(exc),
            )
