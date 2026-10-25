"""listsmith-ci command line.

    listsmith-ci check <url> [<url> ...]
        [--baseline PATH] [--update-baseline]
        [--report PATH] [--summary PATH]
        [--paid-grade] [--paid-route-verdict] [--grade-floor 80]

Exit codes: 0 = every check passed (notices allowed),
            1 = at least one check failed,
            2 = usage / configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from .checks import EndpointReport, run_tier1, surface_hash
from .fetch import fetch_endpoint
from .paid import run_paid_checks

DEFAULT_BASELINE = ".listsmith-ci-baseline.json"


# ---------------------------------------------------------------------------
# Baseline file
# ---------------------------------------------------------------------------

def load_baseline(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    return endpoints if isinstance(endpoints, dict) else {}


def save_baseline(path: str, endpoints: dict) -> None:
    payload = {"version": 1, "endpoints": endpoints}
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

_STATUS_ICON = {"pass": "✅", "fail": "❌", "notice": "ℹ️"}


def render_report(reports: list[EndpointReport], *, when: str) -> str:
    lines = [f"# listsmith-ci report — {when}", ""]
    total_fail = sum(
        1 for r in reports for c in r.results if c.status == "fail"
    )
    if total_fail:
        lines.append(f"**❌ {total_fail} failing check(s)** across "
                     f"{len(reports)} endpoint(s).")
    else:
        lines.append(f"**✅ All checks passed** across {len(reports)} endpoint(s).")
    lines.append("")
    for report in reports:
        verdict = "✅ PASS" if report.passed else "❌ FAIL"
        lines.append(f"## {verdict} — {report.url}")
        lines.append("")
        lines.append("| Check | Result | Detail |")
        lines.append("|---|---|---|")
        for c in report.results:
            detail = c.detail.replace("|", "\\|")
            lines.append(f"| `{c.rule}` | {_STATUS_ICON[c.status]} {c.status} | {detail} |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="listsmith-ci",
        description="Fail your build on x402 listing traps before "
                    "inspectors and buyers find them.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check one or more endpoint URLs")
    check.add_argument("urls", nargs="+", help="endpoint URLs to check")
    check.add_argument("--baseline", default=DEFAULT_BASELINE,
                       help=f"baseline file path (default {DEFAULT_BASELINE})")
    check.add_argument("--update-baseline", action="store_true",
                       help="rewrite the baseline with the current surfaces")
    check.add_argument("--report", default=None,
                       help="write the markdown report to this path")
    check.add_argument("--summary", default=None,
                       help="append the report to this path (e.g. "
                            "$GITHUB_STEP_SUMMARY)")
    check.add_argument("--paid-grade", action="store_true",
                       help="Tier 2: pay ListSmith /v1/grade ($0.10) per endpoint")
    check.add_argument("--paid-route-verdict", action="store_true",
                       help="Tier 2: pay RouteVerdict /v1/check ($0.05) per endpoint")
    check.add_argument("--grade-floor", type=float, default=80.0,
                       help="minimum ListSmith grade (default 80)")
    return parser


def run_check(args, *, fetcher=fetch_endpoint, paid_runner=run_paid_checks,
              environ=None) -> tuple[int, str]:
    environ = os.environ if environ is None else environ
    baseline = load_baseline(args.baseline)
    new_baseline = dict(baseline)
    session = requests.Session()
    reports: list[EndpointReport] = []

    paid_enabled = args.paid_grade or args.paid_route_verdict
    account = None
    if paid_enabled:
        from .x402pay import load_account_from_env

        account = load_account_from_env(environ)
        if account is None:
            return 2, (
                "Tier-2 checks are enabled but no signer is configured. "
                "Set the LISTSMITH_CI_PRIVATE_KEY secret (or "
                "LISTSMITH_CI_KEY_FILE) in the environment."
            )

    for url in args.urls:
        challenge, entry, error = fetcher(url, session, 20.0) \
            if fetcher is fetch_endpoint else fetcher(url)
        report, surface = run_tier1(
            url, challenge, entry, baseline.get(url),
            update_baseline=args.update_baseline, fetch_error=error,
        )
        if surface:
            if args.update_baseline or url not in baseline:
                new_baseline[url] = {
                    "sha256": surface_hash(surface),
                    "surface": surface,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
        if paid_enabled:
            report.results.extend(paid_runner(
                url, account=account, session=session,
                grade=args.paid_grade, route_verdict=args.paid_route_verdict,
                grade_floor=args.grade_floor,
            ))
        reports.append(report)

    if new_baseline != baseline:
        save_baseline(args.baseline, new_baseline)

    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = render_report(reports, when=when)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    failed = any(not r.passed for r in reports)
    return (1 if failed else 0), text


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        code, text = run_check(args)
        print(text)
        return code
    return 2


if __name__ == "__main__":
    sys.exit(main())
