"""Tier-2 (paid, opt-in) deep checks.

Two fleet services grade an endpoint the way buyers and inspectors do:

  - ListSmith  POST /v1/grade  ($0.10) — an itemized listing grade.
  - RouteVerdict POST /v1/check ($0.05) — trust & liveness verdict.

Both are paid with the SELLER's wallet key (their CI secret), never
ours, and both are OFF by default. Results become named checks:
"paid-grade" (score must meet the configured floor) and
"paid-route-verdict" (verdict must be TRUSTED).
"""

from __future__ import annotations

import os

import requests

from .checks import CheckResult
from .x402pay import ServiceError, call_paid_endpoint

LISTSMITH_GRADE_URL = os.environ.get(
    "LISTSMITH_CI_GRADE_URL",
    "https://list-smith-production.up.railway.app/v1/grade",
)
ROUTEVEDICT_CHECK_URL = os.environ.get(
    "LISTSMITH_CI_ROUTEVEDICT_URL",
    "https://route-verdict-production.up.railway.app/v1/check",
)

RULE_GRADE = "paid-grade"
RULE_ROUTE_VERDICT = "paid-route-verdict"


def extract_grade_score(grade_response: dict | None) -> float | None:
    """Find the numeric score in a ListSmith grade response.

    Tolerates the shapes ListSmith has shipped: a top-level score, a
    nested grade object, or a 0-100 `overall`. Returns None when the
    response carries nothing score-like — which is itself a failure,
    never a pass.
    """
    if not isinstance(grade_response, dict):
        return None
    for key in ("score", "overall", "grade_score"):
        value = grade_response.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    for key in ("grade", "result", "summary"):
        nested = grade_response.get(key)
        if isinstance(nested, dict):
            found = extract_grade_score(nested)
            if found is not None:
                return found
    return None


def extract_route_verdict(check_response: dict | None) -> str | None:
    if not isinstance(check_response, dict):
        return None
    verdict = check_response.get("verdict")
    if isinstance(verdict, str):
        return verdict
    nested = check_response.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("verdict"), str):
        return nested["verdict"]
    return None


def run_paid_checks(
    target_url: str,
    *,
    account,
    session: requests.Session | None = None,
    grade: bool = False,
    route_verdict: bool = False,
    grade_floor: float = 80.0,
    grade_url: str = LISTSMITH_GRADE_URL,
    route_verdict_url: str = ROUTEVEDICT_CHECK_URL,
    caller=call_paid_endpoint,
) -> list[CheckResult]:
    """Run the enabled Tier-2 checks against one seller endpoint."""
    session = session or requests.Session()
    results: list[CheckResult] = []

    if grade:
        try:
            call = caller(session, grade_url, {"url": target_url}, account=account)
        except ServiceError as exc:
            results.append(CheckResult(
                RULE_GRADE, "fail",
                f"ListSmith grade call failed: HTTP {exc.status}",
            ))
        else:
            if not call.paid:
                results.append(CheckResult(
                    RULE_GRADE, "fail",
                    "ListSmith grade was not paid for "
                    f"({call.note}) — a paid check that didn't settle "
                    "cannot pass",
                ))
            else:
                score = extract_grade_score(call.response)
                if score is None:
                    results.append(CheckResult(
                        RULE_GRADE, "fail",
                        "ListSmith grade response carried no score",
                    ))
                elif score < grade_floor:
                    results.append(CheckResult(
                        RULE_GRADE, "fail",
                        f"ListSmith grade {score:g} is below the floor "
                        f"of {grade_floor:g}",
                    ))
                else:
                    results.append(CheckResult(
                        RULE_GRADE, "pass",
                        f"ListSmith grade {score:g} meets the floor "
                        f"of {grade_floor:g}",
                    ))

    if route_verdict:
        try:
            call = caller(
                session, route_verdict_url, {"url": target_url}, account=account
            )
        except ServiceError as exc:
            results.append(CheckResult(
                RULE_ROUTE_VERDICT, "fail",
                f"RouteVerdict check call failed: HTTP {exc.status}",
            ))
        else:
            if not call.paid:
                results.append(CheckResult(
                    RULE_ROUTE_VERDICT, "fail",
                    "RouteVerdict check was not paid for "
                    f"({call.note}) — a paid check that didn't settle "
                    "cannot pass",
                ))
            else:
                verdict = extract_route_verdict(call.response)
                if verdict is None:
                    results.append(CheckResult(
                        RULE_ROUTE_VERDICT, "fail",
                        "RouteVerdict response carried no verdict",
                    ))
                elif verdict.upper() != "TRUSTED":
                    results.append(CheckResult(
                        RULE_ROUTE_VERDICT, "fail",
                        f"RouteVerdict verdict is {verdict}, must be TRUSTED",
                    ))
                else:
                    results.append(CheckResult(
                        RULE_ROUTE_VERDICT, "pass",
                        "RouteVerdict verdict is TRUSTED",
                    ))
    return results
