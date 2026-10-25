"""x402 payment plumbing for the opt-in Tier-2 (paid) checks.

Adapted field-for-field from the fleet's proven client in
~/workspace/verdict-loop/verdict_loop/x402.py (itself adapted from
QuincyGB/token-verdict-tools), re-based from httpx to requests:

1. POST the endpoint unpaid -> 402 with a base64 JSON challenge in the
   `payment-required` header.
2. Pick the `exact` / `eip155:8453` accepts entry.
3. Build an EIP-3009 TransferWithAuthorization {from, to: payTo,
   value: amount, validAfter: "0", validBefore: now + maxTimeoutSeconds,
   nonce: 32 random bytes}.
4. Sign as EIP-712 typed data with domain {name/version from
   accepts.extra, chainId 8453, verifyingContract: asset}.
5. Retry with the payload base64-encoded in the `PAYMENT-SIGNATURE`
   header. The service's facilitator settles USDC on Base server-side.

Safety contract: with no signer configured this module NEVER pays —
callers get the parsed challenge and exact price instead. Private keys
are read from the environment only and are never logged, printed, or
included in any report.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

BASE_NETWORK = "eip155:8453"
USDC_DECIMALS = 6

TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


class ServiceError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"request failed: HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def parse_payment_required(response: requests.Response) -> dict:
    header = response.headers.get("payment-required")
    if header:
        try:
            decoded = json.loads(base64.b64decode(header).decode("utf-8"))
            if isinstance(decoded, dict) and decoded.get("accepts"):
                return decoded
        except (ValueError, UnicodeDecodeError):
            pass
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("accepts"):
        return body
    raise ValueError("402 response carried no parseable x402 payment challenge")


def select_requirement(
    challenge: dict, network: str = BASE_NETWORK, scheme: str = "exact"
) -> dict:
    for entry in challenge.get("accepts") or []:
        if entry.get("scheme") == scheme and entry.get("network") == network:
            return entry
    raise ValueError(
        f"challenge offers no {scheme} requirement on {network}"
    )


def build_authorization(
    from_address: str,
    requirement: dict,
    *,
    now: int | None = None,
    nonce: str | None = None,
) -> dict:
    now = int(time.time()) if now is None else now
    return {
        "from": from_address,
        "to": requirement["payTo"],
        "value": str(requirement["amount"]),
        "validAfter": "0",
        "validBefore": str(now + int(requirement.get("maxTimeoutSeconds", 300))),
        "nonce": nonce or ("0x" + secrets.token_hex(32)),
    }


def sign_authorization(account, requirement: dict, authorization: dict) -> str:
    extra = requirement.get("extra") or {}
    if not extra.get("name") or not extra.get("version"):
        raise ValueError(
            "payment requirement is missing EIP-712 domain parameters "
            f"(extra.name/extra.version) for asset {requirement.get('asset')}"
        )
    network = requirement.get("network", BASE_NETWORK)
    if not network.startswith("eip155:"):
        raise ValueError(f"unsupported network for EVM signing: {network}")
    domain = {
        "name": extra["name"],
        "version": extra["version"],
        "chainId": int(network.split(":", 1)[1]),
        "verifyingContract": requirement["asset"],
    }
    message = {
        "from": authorization["from"],
        "to": authorization["to"],
        "value": int(authorization["value"]),
        "validAfter": int(authorization["validAfter"]),
        "validBefore": int(authorization["validBefore"]),
        "nonce": bytes.fromhex(authorization["nonce"].removeprefix("0x")),
    }
    signed = account.sign_typed_data(
        domain_data=domain,
        message_types=TRANSFER_WITH_AUTHORIZATION_TYPES,
        message_data=message,
    )
    hex_sig = signed.signature.hex()
    return hex_sig if hex_sig.startswith("0x") else "0x" + hex_sig


def build_payment_payload(
    challenge: dict, requirement: dict, authorization: dict, signature: str
) -> dict:
    return {
        "x402Version": challenge.get("x402Version", 2),
        "resource": challenge.get("resource"),
        "accepted": requirement,
        "payload": {"authorization": authorization, "signature": signature},
    }


def encode_payment_header(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def load_account_from_env(env: dict | None = None):
    """Signer from the OPERATOR'S OWN environment.

    LISTSMITH_CI_PRIVATE_KEY or AGENT_PRIVATE_KEY: a 0x-prefixed key.
    LISTSMITH_CI_KEY_FILE or AGENT_KEY_FILE: path to a file holding it.
    Returns None when unset — Tier 2 then never pays. eth_account is
    imported lazily so Tier 1 works without a signer stack. The key
    value itself is never returned, logged, or printed.
    """
    from eth_account import Account

    env = os.environ if env is None else env
    key = (env.get("LISTSMITH_CI_PRIVATE_KEY") or env.get("AGENT_PRIVATE_KEY") or "").strip()
    if not key:
        key_file = (
            env.get("LISTSMITH_CI_KEY_FILE") or env.get("AGENT_KEY_FILE") or ""
        ).strip()
        if key_file:
            key = Path(key_file).read_text(encoding="utf-8").strip()
    if not key:
        return None
    if not key.startswith("0x"):
        key = "0x" + key
    return Account.from_key(key)


@dataclass
class PaidCall:
    url: str
    paid: bool
    price_units: int
    response: dict | None = None
    note: str = ""


def call_paid_endpoint(
    session: requests.Session,
    url: str,
    body: dict,
    *,
    account=None,
    timeout: float = 30.0,
) -> PaidCall:
    """POST `url`, settling the x402 challenge when a signer is present.

    No account -> paid=False with the exact price that WOULD be charged;
    the response body is never fabricated.
    """
    response = session.post(url, json=body, timeout=timeout)
    if response.status_code == 200:
        return PaidCall(url=url, paid=False, price_units=0,
                        response=_safe_json(response),
                        note="endpoint answered without payment")
    if response.status_code != 402:
        raise ServiceError(response.status_code, _safe_json(response))

    challenge = parse_payment_required(response)
    requirement = select_requirement(challenge)
    price_units = int(requirement["amount"])

    if account is None:
        return PaidCall(url=url, paid=False, price_units=price_units,
                        note="no signer configured — nothing was paid")

    authorization = build_authorization(account.address, requirement)
    signature = sign_authorization(account, requirement, authorization)
    payload = build_payment_payload(challenge, requirement, authorization, signature)
    paid = session.post(
        url, json=body, timeout=timeout,
        headers={"PAYMENT-SIGNATURE": encode_payment_header(payload)},
    )
    if paid.status_code == 200:
        return PaidCall(url=url, paid=True, price_units=price_units,
                        response=paid.json(), note="settled")
    if paid.status_code == 402:
        raise ServiceError(402, "paid retry still returned 402 (settlement rejected)")
    raise ServiceError(paid.status_code, _safe_json(paid))


def _safe_json(response: requests.Response):
    try:
        return response.json()
    except ValueError:
        return response.text[:500]
