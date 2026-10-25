"""Tier-2 (mocked — nothing is ever paid in tests) and CLI tests."""

import json

import pytest

from listsmith_ci import paid as paid_mod
from listsmith_ci.cli import build_parser, load_baseline, run_check, save_baseline
from listsmith_ci.fetch import FixtureFetcher
from listsmith_ci.x402pay import PaidCall

from .fixtures import URL, clean_challenge, clean_manifest_entry


# ---------------------------------------------------------------------------
# Paid extraction helpers
# ---------------------------------------------------------------------------

def test_extract_grade_score_shapes():
    assert paid_mod.extract_grade_score({"score": 88}) == 88
    assert paid_mod.extract_grade_score({"grade": {"overall": 72.5}}) == 72.5
    assert paid_mod.extract_grade_score({"nope": True}) is None
    assert paid_mod.extract_grade_score(None) is None


def test_extract_route_verdict_shapes():
    assert paid_mod.extract_route_verdict({"verdict": "TRUSTED"}) == "TRUSTED"
    assert paid_mod.extract_route_verdict({"result": {"verdict": "CAUTION"}}) == "CAUTION"
    assert paid_mod.extract_route_verdict({}) is None


# ---------------------------------------------------------------------------
# Tier-2 orchestration (mocked caller)
# ---------------------------------------------------------------------------

def make_caller(mapping):
    def caller(session, url, body, account=None, **kw):
        return mapping[url]
    return caller


def test_paid_grade_pass_and_fail():
    caller = make_caller({
        paid_mod.LISTSMITH_GRADE_URL: PaidCall(
            url=paid_mod.LISTSMITH_GRADE_URL, paid=True, price_units=100000,
            response={"score": 91}),
    })
    results = paid_mod.run_paid_checks(URL, account=object(), grade=True, caller=caller)
    assert results[0].rule == "paid-grade" and results[0].status == "pass"

    caller = make_caller({
        paid_mod.LISTSMITH_GRADE_URL: PaidCall(
            url=paid_mod.LISTSMITH_GRADE_URL, paid=True, price_units=100000,
            response={"score": 42}),
    })
    results = paid_mod.run_paid_checks(URL, account=object(), grade=True, caller=caller)
    assert results[0].status == "fail"
    assert "below the floor" in results[0].detail


def test_paid_route_verdict_must_be_trusted():
    for verdict, expected in (("TRUSTED", "pass"), ("CAUTION", "fail"),
                              ("AVOID", "fail")):
        caller = make_caller({
            paid_mod.ROUTEVEDICT_CHECK_URL: PaidCall(
                url=paid_mod.ROUTEVEDICT_CHECK_URL, paid=True,
                price_units=50000, response={"verdict": verdict}),
        })
        results = paid_mod.run_paid_checks(
            URL, account=object(), route_verdict=True, caller=caller)
        assert results[0].status == expected, verdict


def test_unpaid_tier2_never_passes():
    caller = make_caller({
        paid_mod.LISTSMITH_GRADE_URL: PaidCall(
            url=paid_mod.LISTSMITH_GRADE_URL, paid=False, price_units=100000,
            note="no signer configured — nothing was paid"),
    })
    results = paid_mod.run_paid_checks(URL, account=None, grade=True, caller=caller)
    assert results[0].status == "fail"


# ---------------------------------------------------------------------------
# CLI end-to-end on fixtures
# ---------------------------------------------------------------------------

def parse(argv):
    return build_parser().parse_args(argv)


def test_cli_clean_run_exit_zero_and_baseline_written(tmp_path):
    baseline = tmp_path / "baseline.json"
    fetcher = FixtureFetcher({URL: (clean_challenge(), clean_manifest_entry())})
    args = parse(["check", URL, "--baseline", str(baseline)])
    code, text = run_check(args, fetcher=fetcher, environ={})
    assert code == 0
    assert "All checks passed" in text
    saved = load_baseline(str(baseline))
    assert URL in saved and saved[URL]["sha256"]
    # Second run against the recorded baseline: drift passes too.
    code2, _ = run_check(args, fetcher=fetcher, environ={})
    assert code2 == 0


def test_cli_trap_exits_one(tmp_path):
    from .fixtures import trap_oversized_example
    challenge, entry = trap_oversized_example()
    fetcher = FixtureFetcher({URL: (challenge, entry)})
    args = parse(["check", URL, "--baseline", str(tmp_path / "b.json")])
    code, text = run_check(args, fetcher=fetcher, environ={})
    assert code == 1
    assert "sample-size" in text


def test_cli_paid_without_signer_is_usage_error(tmp_path):
    fetcher = FixtureFetcher({URL: (clean_challenge(), clean_manifest_entry())})
    args = parse(["check", URL, "--baseline", str(tmp_path / "b.json"),
                  "--paid-grade"])
    code, text = run_check(args, fetcher=fetcher, environ={})
    assert code == 2
    assert "no signer" in text


def test_cli_report_file_written(tmp_path):
    report_path = tmp_path / "report.md"
    fetcher = FixtureFetcher({URL: (clean_challenge(), clean_manifest_entry())})
    args = parse(["check", URL, "--baseline", str(tmp_path / "b.json"),
                  "--report", str(report_path)])
    code, _ = run_check(args, fetcher=fetcher, environ={})
    assert code == 0
    content = report_path.read_text()
    assert "listsmith-ci report" in content
    assert URL in content


def test_baseline_roundtrip(tmp_path):
    path = str(tmp_path / "b.json")
    save_baseline(path, {"u": {"sha256": "abc", "surface": {}}})
    assert load_baseline(path)["u"]["sha256"] == "abc"
    assert json.loads(open(path).read())["version"] == 1
