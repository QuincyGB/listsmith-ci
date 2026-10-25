"""Tier-1 (free, local) listing checks for x402 endpoints.

Every rule here is a trap that has silently cost a real seller calls.
The checks read only PUBLIC discovery surfaces — the unpaid 402
challenge and the /.well-known/x402 manifest — so they run in seconds
with no wallet and no payment.

Rules (stable names; CI output and baselines key on them):

  sample-size            — Settled's discovery extractor silently drops
                           a listing's output sample when the serialized
                           example is too large, and its receipts then
                           read "nothing_to_compare" instead of
                           "as_advertised". Measured on our own fleet
                           2026-10-25: a 1,453-byte example (RouteVerdict
                           /v1/check) was KEPT and a 1,721-byte example
                           (Token Verdict /v1/verdict, pre-trim) was
                           DROPPED, so the true cap sits somewhere in
                           (1453, 1721] bytes. We enforce the largest
                           PROVEN-kept size: an example over 1,453
                           serialized bytes is past every size ever
                           observed to survive, and fails. That is a
                           measured bracket, not gospel — if Settled
                           changes its extractor, this constant is the
                           one place to revisit.
  description-length     — the CDP facilitator rejects payments whose
                           listing description exceeds 500 characters.
  typed-schema           — 87.8% of catalog listings ship a hollow
                           {"type", "example"} output with no typed
                           schema (ListSmith's Oct 2026 catalog read).
                           A typed schema (properties / required / enum /
                           items / combinators) is what separates a real
                           contract from a shrug.
  example-matches-schema — the published output example must validate
                           against the route's own published output
                           schema (a local jsonschema check). Settled
                           compares delivered responses against the
                           sample; a sample that violates its own
                           schema fails that comparison by construction.
  manifest-agreement     — the /.well-known/x402 manifest entry for the
                           route must mirror the live 402 challenge:
                           amount, asset, payTo, network. A manifest
                           that disagrees with the challenge is drift
                           between the two surfaces buyers read.
  drift                  — the discovery surface (challenge terms +
                           bazaar info + manifest entry) is hashed and
                           compared against a baseline file recorded in
                           the seller's repo. First sight records the
                           baseline and passes with a notice; later
                           changes fail with a readable field diff.
                           Baselines are only (re)written on an explicit
                           update request — never silently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Settled's sample extractor cap, measured 2026-10-25 (see module docs):
# largest proven-kept sample. True cap is somewhere in (1453, 1721].
SAMPLE_CAP_BYTES = 1453
# CDP facilitator rejects listing descriptions longer than this.
DESCRIPTION_CAP_CHARS = 500

RULE_SAMPLE_SIZE = "sample-size"
RULE_DESCRIPTION = "description-length"
RULE_SCHEMA = "typed-schema"
RULE_EXAMPLE = "example-matches-schema"
RULE_MANIFEST = "manifest-agreement"
RULE_DRIFT = "drift"
RULE_FETCH = "fetch"

TIER1_RULES = [
    RULE_SAMPLE_SIZE,
    RULE_DESCRIPTION,
    RULE_SCHEMA,
    RULE_EXAMPLE,
    RULE_MANIFEST,
    RULE_DRIFT,
]


@dataclass
class CheckResult:
    rule: str
    status: str  # "pass" | "fail" | "notice"
    detail: str


@dataclass
class EndpointReport:
    url: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.status != "fail" for r in self.results)

    def add(self, rule: str, status: str, detail: str) -> None:
        self.results.append(CheckResult(rule=rule, status=status, detail=detail))


# ---------------------------------------------------------------------------
# Surface extraction
# ---------------------------------------------------------------------------

def bazaar_info(challenge: dict | None) -> dict:
    """extensions.bazaar.info from a parsed 402 challenge ({} if absent)."""
    if not isinstance(challenge, dict):
        return {}
    ext = (challenge.get("extensions") or {}).get("bazaar") or {}
    return ext.get("info") or {}


def challenge_description(challenge: dict | None) -> str | None:
    """Listing description as carried in the 402's resource block."""
    if not isinstance(challenge, dict):
        return None
    resource = challenge.get("resource")
    if isinstance(resource, dict):
        return resource.get("description")
    return None


def output_example(challenge: dict | None) -> Any:
    info = bazaar_info(challenge)
    output = info.get("output") or {}
    return output.get("example")


def output_schema(challenge: dict | None, manifest_entry: dict | None) -> Any:
    """The route's published output schema.

    Prefer the Bazaar extension's output.schema (the surface Settled and
    CDP read); fall back to the manifest entry's outputSchema.
    """
    info = bazaar_info(challenge)
    output = info.get("output") or {}
    schema = output.get("schema")
    if isinstance(schema, dict) and schema:
        return schema
    if isinstance(manifest_entry, dict):
        schema = manifest_entry.get("outputSchema")
        if isinstance(schema, dict) and schema:
            return schema
    return None


def select_accepts(accepts: list | None) -> dict | None:
    """The exact/eip155:8453 accepts entry, else the first entry."""
    entries = [a for a in (accepts or []) if isinstance(a, dict)]
    if not entries:
        return None
    for entry in entries:
        if entry.get("scheme") == "exact" and str(
            entry.get("network", "")
        ).startswith("eip155:"):
            return entry
    return entries[0]


def challenge_accepts(challenge: dict | None) -> dict | None:
    if not isinstance(challenge, dict):
        return None
    return select_accepts(challenge.get("accepts"))


def serialized_size(value: Any) -> int:
    """Byte size of the example's JSON serialization.

    Measured with json.dumps default separators — the same basis on
    which the fleet's kept/dropped bracket (1,453 kept / 1,721 dropped)
    was established, so the cap and the evidence share a ruler.
    """
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def is_typed_schema(schema: Any) -> bool:
    """A schema is 'typed' when it constrains shape beyond a bare type.

    Hollow: {"type": "object"} (or no schema at all). Typed: declares
    properties / required / enum / items / $ref / a combinator, or a
    non-object scalar type (a bare {"type": "string"} IS a contract
    for a scalar output; {"type": "object"} alone is not).
    """
    if not isinstance(schema, dict) or not schema:
        return False
    for key in ("properties", "required", "enum", "items", "$ref",
                "allOf", "anyOf", "oneOf", "pattern", "format"):
        if key in schema:
            return True
    type_ = schema.get("type")
    if isinstance(type_, str) and type_ not in ("object",):
        return True
    if isinstance(type_, list) and any(t != "object" for t in type_):
        return True
    return False


# ---------------------------------------------------------------------------
# Tier-1 rules
# ---------------------------------------------------------------------------

def check_sample_size(challenge: dict | None) -> CheckResult:
    example = output_example(challenge)
    if example is None:
        return CheckResult(
            RULE_SAMPLE_SIZE, "fail",
            "no output example published in the Bazaar extension — "
            "inspectors have no sample to compare deliveries against",
        )
    size = serialized_size(example)
    if size > SAMPLE_CAP_BYTES:
        return CheckResult(
            RULE_SAMPLE_SIZE, "fail",
            f"output example serializes to {size} bytes (> {SAMPLE_CAP_BYTES}); "
            "Settled's extractor drops oversized samples and receipts read "
            "nothing_to_compare — trim prose, keep the sample real",
        )
    return CheckResult(
        RULE_SAMPLE_SIZE, "pass",
        f"output example serializes to {size} bytes (<= {SAMPLE_CAP_BYTES})",
    )


def check_description(
    challenge: dict | None, manifest_entry: dict | None
) -> CheckResult:
    problems = []
    seen = []
    desc = challenge_description(challenge)
    if desc is not None:
        seen.append(("402 challenge", len(desc)))
        if len(desc) > DESCRIPTION_CAP_CHARS:
            problems.append(
                f"402 challenge description is {len(desc)} chars "
                f"(> {DESCRIPTION_CAP_CHARS})"
            )
    if isinstance(manifest_entry, dict) and manifest_entry.get("description"):
        mdesc = manifest_entry["description"]
        seen.append(("manifest", len(mdesc)))
        if len(mdesc) > DESCRIPTION_CAP_CHARS:
            problems.append(
                f"manifest description is {len(mdesc)} chars "
                f"(> {DESCRIPTION_CAP_CHARS})"
            )
    if not seen:
        return CheckResult(
            RULE_DESCRIPTION, "fail",
            "no description found on the challenge or the manifest entry",
        )
    if problems:
        return CheckResult(
            RULE_DESCRIPTION, "fail",
            "; ".join(problems)
            + " — CDP rejects payments over the 500-char cap",
        )
    detail = ", ".join(f"{where}: {n} chars" for where, n in seen)
    return CheckResult(RULE_DESCRIPTION, "pass", f"descriptions within cap ({detail})")


def check_typed_schema(
    challenge: dict | None, manifest_entry: dict | None
) -> CheckResult:
    info = bazaar_info(challenge)
    output = info.get("output") or {}
    schema = output_schema(challenge, manifest_entry)
    if schema is None:
        return CheckResult(
            RULE_SCHEMA, "fail",
            "no output schema published — output is a hollow "
            '{"type", "example"} with nothing for buyers to code against',
        )
    if not is_typed_schema(schema):
        return CheckResult(
            RULE_SCHEMA, "fail",
            "output schema is hollow (a bare type with no properties, "
            "required fields, enum, or items) — publish a typed schema",
        )
    keys = sorted(
        k for k in ("properties", "required", "enum", "items", "$ref")
        if k in schema
    )
    return CheckResult(
        RULE_SCHEMA, "pass",
        f"typed output schema present ({', '.join(keys) or schema.get('type')})",
    )


def check_example_matches_schema(
    challenge: dict | None, manifest_entry: dict | None
) -> CheckResult:
    import jsonschema

    example = output_example(challenge)
    schema = output_schema(challenge, manifest_entry)
    if example is None:
        return CheckResult(
            RULE_EXAMPLE, "fail", "no output example to validate"
        )
    if schema is None:
        return CheckResult(
            RULE_EXAMPLE, "fail",
            "no output schema to validate the example against",
        )
    try:
        jsonschema.validate(instance=example, schema=schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return CheckResult(
            RULE_EXAMPLE, "fail",
            f"output example violates its own schema at {path}: {exc.message}",
        )
    except jsonschema.SchemaError as exc:
        return CheckResult(
            RULE_EXAMPLE, "fail",
            f"published output schema is itself invalid: {exc.message}",
        )
    return CheckResult(
        RULE_EXAMPLE, "pass",
        "output example validates against the published output schema",
    )


# Fields a drift or a lying manifest would change on the payment surface.
_ACCEPTS_FIELDS = ("amount", "asset", "payTo", "network")


def check_manifest_agreement(
    challenge: dict | None, manifest_entry: dict | None
) -> CheckResult:
    live = challenge_accepts(challenge)
    if live is None:
        return CheckResult(
            RULE_MANIFEST, "fail", "live 402 challenge carries no accepts entry"
        )
    if not isinstance(manifest_entry, dict):
        return CheckResult(
            RULE_MANIFEST, "fail",
            "route is missing from the /.well-known/x402 manifest — "
            "buyers reading the manifest cannot find it",
        )
    listed = select_accepts(manifest_entry.get("accepts"))
    if listed is None:
        return CheckResult(
            RULE_MANIFEST, "fail",
            "manifest entry carries no accepts — nothing to mirror the "
            "live challenge",
        )
    diffs = []
    for f in _ACCEPTS_FIELDS:
        if str(listed.get(f)) != str(live.get(f)):
            diffs.append(f"{f}: manifest {listed.get(f)!r} != live {live.get(f)!r}")
    if diffs:
        return CheckResult(
            RULE_MANIFEST, "fail",
            "manifest disagrees with the live 402 challenge — "
            + "; ".join(diffs),
        )
    return CheckResult(
        RULE_MANIFEST, "pass",
        "manifest accepts mirror the live challenge "
        f"(amount {live.get('amount')}, network {live.get('network')})",
    )


# ---------------------------------------------------------------------------
# Drift (rule f)
# ---------------------------------------------------------------------------

def discovery_surface(
    url: str,
    challenge: dict | None,
    manifest_entry: dict | None,
) -> dict:
    """The canonical, hashable view of everything buyers can read."""
    live = challenge_accepts(challenge) or {}
    info = bazaar_info(challenge)
    entry = manifest_entry or {}
    listed = select_accepts(entry.get("accepts")) or {}
    return {
        "url": url,
        "challenge": {f: live.get(f) for f in _ACCEPTS_FIELDS},
        "description": challenge_description(challenge),
        "bazaar_info": info,
        "manifest": {
            "accepts": {f: listed.get(f) for f in _ACCEPTS_FIELDS},
            "description": entry.get("description"),
            "inputSchema": entry.get("inputSchema"),
            "outputSchema": entry.get("outputSchema"),
        },
    }


def surface_hash(surface: dict) -> str:
    canonical = json.dumps(
        surface, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        if not value:
            out[prefix] = value
        for key in sorted(value):
            out.update(_flatten(value[key], f"{prefix}.{key}" if prefix else key))
    elif isinstance(value, list):
        out[prefix] = json.dumps(value, sort_keys=True, ensure_ascii=False)
    else:
        out[prefix] = value
    return out


def diff_surfaces(old: dict, new: dict) -> list[str]:
    """Readable one-line-per-field diff between two discovery surfaces."""
    flat_old, flat_new = _flatten(old), _flatten(new)
    lines = []
    for key in sorted(set(flat_old) | set(flat_new)):
        if key not in flat_old:
            lines.append(f"{key}: added ({_short(flat_new[key])})")
        elif key not in flat_new:
            lines.append(f"{key}: removed (was {_short(flat_old[key])})")
        elif flat_old[key] != flat_new[key]:
            lines.append(
                f"{key}: {_short(flat_old[key])} -> {_short(flat_new[key])}"
            )
    return lines


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check_drift(
    url: str,
    challenge: dict | None,
    manifest_entry: dict | None,
    baseline_entry: dict | None,
    *,
    update_baseline: bool,
) -> tuple[CheckResult, dict]:
    """Returns (result, surface) — caller persists surface when updating."""
    surface = discovery_surface(url, challenge, manifest_entry)
    digest = surface_hash(surface)
    if update_baseline:
        return (
            CheckResult(RULE_DRIFT, "notice",
                        f"baseline updated (sha256 {digest[:12]}…)"),
            surface,
        )
    if baseline_entry is None:
        return (
            CheckResult(
                RULE_DRIFT, "notice",
                f"baseline recorded (sha256 {digest[:12]}…) — future runs "
                "will flag changes against it",
            ),
            surface,
        )
    if baseline_entry.get("sha256") == digest:
        return (
            CheckResult(RULE_DRIFT, "pass",
                        f"discovery surface unchanged (sha256 {digest[:12]}…)"),
            surface,
        )
    lines = diff_surfaces(baseline_entry.get("surface") or {}, surface)
    detail = "discovery surface drifted from baseline: " + "; ".join(lines[:10])
    if len(lines) > 10:
        detail += f"; … and {len(lines) - 10} more fields"
    detail += " (if this change is intended, re-run with update-baseline)"
    return CheckResult(RULE_DRIFT, "fail", detail), surface


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_tier1(
    url: str,
    challenge: dict | None,
    manifest_entry: dict | None,
    baseline_entry: dict | None,
    *,
    update_baseline: bool,
    fetch_error: str | None = None,
) -> tuple[EndpointReport, dict]:
    """Run all Tier-1 rules for one endpoint. Returns (report, surface)."""
    report = EndpointReport(url=url)
    if fetch_error:
        report.add(RULE_FETCH, "fail", fetch_error)
        return report, {}
    report.results.append(check_sample_size(challenge))
    report.results.append(check_description(challenge, manifest_entry))
    report.results.append(check_typed_schema(challenge, manifest_entry))
    report.results.append(check_example_matches_schema(challenge, manifest_entry))
    report.results.append(check_manifest_agreement(challenge, manifest_entry))
    drift_result, surface = check_drift(
        url, challenge, manifest_entry, baseline_entry,
        update_baseline=update_baseline,
    )
    report.results.append(drift_result)
    return report, surface
