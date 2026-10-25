"""Offline discovery-surface fixtures.

One CLEAN challenge+manifest pair, and one variant per trap. Builders
return deep copies so tests can mutate freely.
"""

from __future__ import annotations

import copy
import json

URL = "https://seller.example.com/v1/thing"

ACCEPTS = {
    "scheme": "exact",
    "network": "eip155:8453",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "amount": "20000",
    "payTo": "0x1111111111111111111111111111111111111111",
    "maxTimeoutSeconds": 300,
    "extra": {"name": "USD Coin", "version": "2"},
}

OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "score"],
    "properties": {
        "verdict": {"type": "string", "enum": ["OK", "BAD"]},
        "score": {"type": "number"},
    },
}

INPUT_SCHEMA = {
    "type": "object",
    "required": ["q"],
    "properties": {"q": {"type": "string"}},
}

DESCRIPTION = "A small, honest description of the thing this route does."


def clean_challenge() -> dict:
    return {
        "x402Version": 2,
        "resource": {"url": URL, "description": DESCRIPTION},
        "accepts": [copy.deepcopy(ACCEPTS)],
        "extensions": {
            "bazaar": {
                "info": {
                    "input": {"type": "http", "method": "POST",
                              "body": {"q": "x"}},
                    "output": {
                        "type": "json",
                        "example": {"verdict": "OK", "score": 91},
                        "schema": copy.deepcopy(OUTPUT_SCHEMA),
                    },
                },
                "schema": {"type": "object"},
            }
        },
    }


def clean_manifest_entry() -> dict:
    return {
        "resource": URL,
        "method": "POST",
        "description": DESCRIPTION,
        "accepts": [copy.deepcopy(ACCEPTS)],
        "inputSchema": copy.deepcopy(INPUT_SCHEMA),
        "outputSchema": copy.deepcopy(OUTPUT_SCHEMA),
    }


def trap_oversized_example() -> tuple[dict, dict]:
    challenge, entry = clean_challenge(), clean_manifest_entry()
    big = {"verdict": "OK", "score": 91, "narrative": "x" * 2000}
    # The oversized example must still validate against its schema, so
    # the schema grows the narrative field too — size is the ONLY trap.
    schema = copy.deepcopy(OUTPUT_SCHEMA)
    schema["properties"]["narrative"] = {"type": "string"}
    challenge["extensions"]["bazaar"]["info"]["output"]["example"] = big
    challenge["extensions"]["bazaar"]["info"]["output"]["schema"] = schema
    entry["outputSchema"] = copy.deepcopy(schema)
    return challenge, entry


def trap_long_description() -> tuple[dict, dict]:
    challenge, entry = clean_challenge(), clean_manifest_entry()
    challenge["resource"]["description"] = "d" * 501
    return challenge, entry


def trap_hollow_schema() -> tuple[dict, dict]:
    challenge, entry = clean_challenge(), clean_manifest_entry()
    # The 87.8% norm: output carries only {"type", "example"}.
    output = challenge["extensions"]["bazaar"]["info"]["output"]
    del output["schema"]
    entry["outputSchema"] = {"type": "object"}
    return challenge, entry


def trap_example_mismatch() -> tuple[dict, dict]:
    challenge, entry = clean_challenge(), clean_manifest_entry()
    # verdict not in the enum, score the wrong type.
    challenge["extensions"]["bazaar"]["info"]["output"]["example"] = {
        "verdict": "MAYBE", "score": "high",
    }
    return challenge, entry


def trap_manifest_mismatch() -> tuple[dict, dict]:
    challenge, entry = clean_challenge(), clean_manifest_entry()
    entry["accepts"][0]["amount"] = "99000"
    entry["accepts"][0]["payTo"] = "0x2222222222222222222222222222222222222222"
    return challenge, entry


ALL_TRAPS = {
    "oversized example": (trap_oversized_example, "sample-size"),
    "long description": (trap_long_description, "description-length"),
    "hollow schema": (trap_hollow_schema, "typed-schema"),
    "example/schema mismatch": (trap_example_mismatch, "example-matches-schema"),
    "manifest mismatch": (trap_manifest_mismatch, "manifest-agreement"),
}


def fixture_bytes() -> int:
    """Sanity helper: serialized size of the clean example."""
    example = clean_challenge()["extensions"]["bazaar"]["info"]["output"]["example"]
    return len(json.dumps(example, separators=(",", ":")).encode())
