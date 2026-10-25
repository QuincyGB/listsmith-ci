"""Fetching the public discovery surfaces (unpaid only).

An unpaid POST returns the 402 challenge — that IS the discovery
document for Tier 1. We send the route's own published example input
when the manifest advertises one (mirroring how inspectors probe), and
an empty JSON object otherwise. Nothing here ever pays.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import urlsplit

import requests


class FetchError(Exception):
    pass


def parse_challenge(response: requests.Response) -> dict:
    """Parse the x402 challenge from a 402 response (header or body)."""
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
    raise FetchError("402 response carried no parseable x402 payment challenge")


def fetch_manifest(origin: str, session: requests.Session, timeout: float) -> dict | None:
    """GET <origin>/.well-known/x402; None when the service has none."""
    try:
        resp = session.get(f"{origin}/.well-known/x402", timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def manifest_entry_for(manifest: dict | None, url: str) -> dict | None:
    """The manifest resource entry whose URL matches the endpoint."""
    if not isinstance(manifest, dict):
        return None
    for entry in manifest.get("resources") or []:
        if not isinstance(entry, dict):
            continue
        target = entry.get("resource") or entry.get("url")
        if target == url:
            return entry
    return None


def example_input(manifest_entry: dict | None) -> dict:
    """The route's published example input body, when it advertises one."""
    if isinstance(manifest_entry, dict):
        schema = manifest_entry.get("inputSchema") or {}
        example = schema.get("example")
        if isinstance(example, dict):
            return example
    return {}


def fetch_endpoint(
    url: str, session: requests.Session, timeout: float = 20.0
) -> tuple[dict | None, dict | None, str | None]:
    """Fetch (challenge, manifest_entry, error) for one endpoint URL.

    The challenge is obtained with an unpaid request. A 200 answer
    means the route does not charge (or answered free) — there is no
    payment surface to check, which is reported as an error for this
    tool's purposes rather than silently passed.
    """
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    manifest = fetch_manifest(origin, session, timeout)
    entry = manifest_entry_for(manifest, url)
    try:
        resp = session.post(url, json=example_input(entry), timeout=timeout)
    except requests.RequestException as exc:
        return None, entry, f"could not reach endpoint: {exc}"
    if resp.status_code == 200:
        return None, entry, (
            "endpoint answered 200 to an unpaid request — no 402 "
            "challenge to check (route is free or misconfigured)"
        )
    if resp.status_code != 402:
        return None, entry, f"unpaid probe returned HTTP {resp.status_code}, expected 402"
    try:
        challenge = parse_challenge(resp)
    except FetchError as exc:
        return None, entry, str(exc)
    return challenge, entry, None


class FixtureFetcher:
    """Offline fetcher for tests: maps URL -> (challenge, manifest_entry)."""

    def __init__(self, fixtures: dict[str, tuple[dict | None, dict | None]]):
        self.fixtures = fixtures

    def __call__(self, url: str):
        if url not in self.fixtures:
            return None, None, f"no fixture for {url}"
        challenge, entry = self.fixtures[url]
        return challenge, entry, None
