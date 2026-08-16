"""Decision scoring: 4 independent signals combined into a 3-tier decision.

Pure functions, no AWS calls -- see design plan section 7 for the exact
formula/thresholds and section 6 for how this is wired into the handler.

Note on "is_first_order": scoring treats a customer as first-order when no
CustomerProfiles item exists for them (order_count == 0), not from the raw
record's self-reported `is_first_order` field. Trusting a client-supplied
flag for a signal that feeds fraud/override logic would be circular; profile
absence is the only source of truth the pipeline itself controls. See
scripts/seed_data.py's module docstring for the same clarification from the
generator side.
"""
from __future__ import annotations

from typing import Optional

from common.models import (
    APPROVE_MAX,
    BASKET_P50,
    BASKET_P90,
    BASKET_P99,
    CustomerProfile,
    DECISION_APPROVE,
    DECISION_DECLINE,
    DECISION_MANUAL_REVIEW,
    DEVICE_REUSE_THRESHOLD,
    FRAUD_FLAG_ADDRESS_MISMATCH,
    FRAUD_FLAG_DEVICE_REUSE,
    FRAUD_FLAG_ODD_HOUR,
    FRAUD_FLAG_VELOCITY,
    FRAUD_HARD_DECLINE,
    MANUAL_REVIEW_MAX,
    ODD_HOUR_MAX,
    OVERRIDE_FRAUD_DECLINE,
    OVERRIDE_NEW_COD_LARGE_BASKET,
    PAYMENT_RISK,
    RULESET_VERSION,
    VELOCITY_WINDOW_MINUTES,
    WEIGHT_BASKET,
    WEIGHT_FRAUD,
    WEIGHT_PAYMENT,
    WEIGHT_TENURE,
    parse_iso,
)


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def basket_score(basket_value: float, profile: Optional[CustomerProfile]) -> float:
    if profile is None or profile.order_count == 0:
        if basket_value < BASKET_P50:
            return 0.1
        if basket_value < BASKET_P90:
            return 0.3
        if basket_value < BASKET_P99:
            return 0.6
        return 0.9
    z = (basket_value - profile.avg_basket_value) / max(profile.stddev_basket_value, 1e-6)
    return clamp(0.5 + 0.15 * z)


def tenure_score(order_created_at: str, profile: Optional[CustomerProfile]) -> tuple[float, dict]:
    if profile is None or profile.order_count == 0:
        account_age_days = 0
        approval_rate = 0.5  # neutral prior, no history yet
    else:
        account_age_days = (parse_iso(order_created_at) - parse_iso(profile.first_seen_at)).days
        approval_rate = profile.approved_count / max(profile.order_count, 1)
    score = clamp(1 - (0.5 * min(account_age_days, 180) / 180 + 0.5 * approval_rate))
    return score, {"account_age_days": account_age_days, "approval_rate": approval_rate}


def fraud_score(
    order: dict,
    profile: Optional[CustomerProfile],
    is_first_order: bool,
) -> tuple[float, list[str]]:
    flags: list[str] = []
    raw_score = 0.0

    if profile is not None and profile.last_order_at:
        delta_minutes = abs(
            (parse_iso(order["created_at"]) - parse_iso(profile.last_order_at)).total_seconds()
        ) / 60
        if delta_minutes < VELOCITY_WINDOW_MINUTES:
            raw_score += FRAUD_FLAG_VELOCITY
            flags.append("velocity")

    if order["order_hour"] <= ODD_HOUR_MAX and is_first_order:
        raw_score += FRAUD_FLAG_ODD_HOUR
        flags.append("odd_hour")

    if order.get("delivery_governorate") is not None and order["ip_country"] != "EG":
        raw_score += FRAUD_FLAG_ADDRESS_MISMATCH
        flags.append("address_payment_mismatch")

    if order["device_shared_count"] >= DEVICE_REUSE_THRESHOLD:
        raw_score += FRAUD_FLAG_DEVICE_REUSE
        flags.append("device_reuse")

    return min(1.0, raw_score), flags


def payment_score(payment_method: str) -> float:
    return PAYMENT_RISK[payment_method]


def decide(order: dict, profile: Optional[CustomerProfile]) -> dict:
    """order is a raw order record dict (see common.models.OrderRecord).
    profile is the customer's CustomerProfilesTable item, or None if the
    decision engine's read-only lookup found nothing (plan section 6)."""
    is_first_order = profile is None or profile.order_count == 0

    b_score = basket_score(order["basket_value"], profile)
    t_score, t_raw = tenure_score(order["created_at"], profile)
    f_score, f_flags = fraud_score(order, profile, is_first_order)
    p_score = payment_score(order["payment_method"])

    overall_score = (
        WEIGHT_BASKET * b_score
        + WEIGHT_TENURE * t_score
        + WEIGHT_FRAUD * f_score
        + WEIGHT_PAYMENT * p_score
    )

    override_applied = None
    if f_score >= FRAUD_HARD_DECLINE:
        decision = DECISION_DECLINE
        override_applied = OVERRIDE_FRAUD_DECLINE
    elif (
        is_first_order
        and order["payment_method"] == "cod"
        and order["basket_value"] > BASKET_P99
    ):
        decision = DECISION_MANUAL_REVIEW
        override_applied = OVERRIDE_NEW_COD_LARGE_BASKET
    elif overall_score < APPROVE_MAX:
        decision = DECISION_APPROVE
    elif overall_score < MANUAL_REVIEW_MAX:
        decision = DECISION_MANUAL_REVIEW
    else:
        decision = DECISION_DECLINE

    return {
        "decision": decision,
        "overall_score": overall_score,
        "ruleset_version": RULESET_VERSION,
        "override_applied": override_applied,
        "signals": {
            "basket_value": {
                "score": b_score,
                "weight": WEIGHT_BASKET,
                "raw_value": order["basket_value"],
            },
            "tenure": {
                "score": t_score,
                "weight": WEIGHT_TENURE,
                "raw_value": t_raw,
            },
            "fraud": {
                "score": f_score,
                "weight": WEIGHT_FRAUD,
                "contributing_flags": f_flags,
            },
            "payment_risk": {
                "score": p_score,
                "weight": WEIGHT_PAYMENT,
                "payment_method": order["payment_method"],
            },
        },
    }
