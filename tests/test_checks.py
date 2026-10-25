"""Tier-1 rule tests: every trap fails with its named rule; clean passes."""

import copy

import pytest

from listsmith_ci import checks
from listsmith_ci.checks import run_tier1

from .fixtures import (
    ALL_TRAPS,
    URL,
    clean_challenge,
    clean_manifest_entry,
)


def run(challenge, entry, baseline_entry=None, update=False):
    report, surface = run_tier1(
        URL, challenge, entry, baseline_entry, update_baseline=update
    )
    return report, surface


def by_rule(report):
    return {r.rule: r for r in report.results}


def test_clean_fixture_passes_every_rule():
    report, _ = run(clean_challenge(), clean_manifest_entry())
    assert report.passed, [(r.rule, r.detail) for r in report.results]
    rules = by_rule(report)
    for rule in checks.TIER1_RULES:
        assert rules[rule].status in ("pass", "notice"), rule
    # First sight of a surface records the baseline as a notice.
    assert rules["drift"].status == "notice"
    assert "baseline recorded" in rules["drift"].detail


@pytest.mark.parametrize("trap_name", list(ALL_TRAPS))
def test_each_trap_fails_with_its_named_rule(trap_name):
    builder, expected_rule = ALL_TRAPS[trap_name]
    challenge, entry = builder()
    report, _ = run(challenge, entry)
    rules = by_rule(report)
    assert not report.passed
    assert rules[expected_rule].status == "fail", (
        trap_name, rules[expected_rule].detail)
    # Each fixture carries exactly ONE trap: every other rule must pass.
    for rule, result in rules.items():
        if rule in (expected_rule, "drift"):
            continue
        assert result.status == "pass", (trap_name, rule, result.detail)


def test_sample_size_boundary():
    # The cap is a measured bracket: 1,453 kept / 1,721 dropped by
    # Settled's extractor. The enforced line is the largest proven-kept
    # size, 1,453 serialized bytes (checks.SAMPLE_CAP_BYTES).
    challenge, entry = clean_challenge(), clean_manifest_entry()
    out = challenge["extensions"]["bazaar"]["info"]["output"]
    out["example"] = {"verdict": "OK", "score": 91, "pad": ""}
    schema = copy.deepcopy(out["schema"])
    schema["properties"]["pad"] = {"type": "string"}
    out["schema"] = schema
    entry["outputSchema"] = schema
    base = checks.serialized_size(out["example"])
    out["example"]["pad"] = "x" * (checks.SAMPLE_CAP_BYTES - base)
    assert checks.serialized_size(out["example"]) == checks.SAMPLE_CAP_BYTES
    assert checks.check_sample_size(challenge).status == "pass"
    out["example"]["pad"] += "x"
    result = checks.check_sample_size(challenge)
    assert result.status == "fail"
    assert "nothing_to_compare" in result.detail


def test_description_boundary():
    challenge, entry = clean_challenge(), clean_manifest_entry()
    challenge["resource"]["description"] = "d" * 500
    assert checks.check_description(challenge, entry).status == "pass"
    challenge["resource"]["description"] = "d" * 501
    assert checks.check_description(challenge, entry).status == "fail"


def test_hollow_variants():
    for hollow in (None, {}, {"type": "object"}, {"type": "object", "example": {}}):
        challenge, entry = clean_challenge(), clean_manifest_entry()
        if hollow is None:
            del challenge["extensions"]["bazaar"]["info"]["output"]["schema"]
            entry["outputSchema"] = None
        else:
            challenge["extensions"]["bazaar"]["info"]["output"]["schema"] = hollow
            entry["outputSchema"] = hollow
        assert checks.check_typed_schema(challenge, entry).status == "fail", hollow


def test_missing_manifest_entry_fails_agreement():
    report, _ = run(clean_challenge(), None)
    assert by_rule(report)["manifest-agreement"].status == "fail"


def test_fetch_error_fails_closed():
    report, surface = run_tier1(
        URL, None, None, None, update_baseline=False,
        fetch_error="could not reach endpoint",
    )
    assert not report.passed
    assert report.results[0].rule == "fetch"
    assert surface == {}


# ---------------------------------------------------------------------------
# Drift baseline lifecycle
# ---------------------------------------------------------------------------

def test_drift_lifecycle():
    challenge, entry = clean_challenge(), clean_manifest_entry()
    # 1. First run records a baseline and passes.
    report1, surface1 = run(challenge, entry)
    assert report1.passed
    baseline_entry = {
        "sha256": checks.surface_hash(surface1),
        "surface": surface1,
    }
    # 2. Same surface passes drift.
    report2, _ = run(clean_challenge(), clean_manifest_entry(), baseline_entry)
    assert by_rule(report2)["drift"].status == "pass"
    assert report2.passed
    # 3. A changed surface fails drift with a readable field diff.
    changed_challenge = clean_challenge()
    changed_challenge["accepts"][0]["amount"] = "50000"
    changed_entry = clean_manifest_entry()
    changed_entry["accepts"][0]["amount"] = "50000"
    report3, _ = run(changed_challenge, changed_entry, baseline_entry)
    drift = by_rule(report3)["drift"]
    assert drift.status == "fail"
    assert "challenge.amount" in drift.detail
    assert "20000" in drift.detail and "50000" in drift.detail
    # 4. Explicit update re-records instead of failing.
    report4, surface4 = run(changed_challenge, changed_entry,
                            baseline_entry, update=True)
    assert by_rule(report4)["drift"].status == "notice"
    assert checks.surface_hash(surface4) != baseline_entry["sha256"]
