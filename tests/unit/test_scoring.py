import math

import pytest

from common.models import (
    BASKET_P50,
    BASKET_P90,
    BASKET_P99,
    CustomerProfile,
    DECISION_APPROVE,
    DECISION_DECLINE,
    DECISION_MANUAL_REVIEW,
    FRAUD_HARD_DECLINE,
    OVERRIDE_FRAUD_DECLINE,
    OVERRIDE_NEW_COD_LARGE_BASKET,
)
from decision_engine.scoring import (
    basket_score,
    clamp,
    decide,
    fraud_score,
    payment_score,
    tenure_score,
)


def base_order(**overrides) -> dict:
    order = {
        "order_id": "ord_1",
        "customer_id": "cust_1",
        "source_updated_at": "2026-01-10T12:00:00.000000Z",
        "created_at": "2026-01-10T12:00:00.000000Z",
        "basket_value": 200.0,
        "currency": "EGP",
        "item_count": 3,
        "payment_method": "card",
        "delivery_governorate": "Cairo",
        "is_first_order": True,
        "device_id": "dev_1",
        "device_shared_count": 1,
        "ip_country": "EG",
        "order_hour": 14,
    }
    order.update(overrides)
    return order


# --- clamp ---

def test_clamp_bounds():
    assert clamp(-1) == 0
    assert clamp(2) == 1
    assert clamp(0.5) == 0.5


# --- basket_score ---

def test_basket_score_first_order_bands():
    assert basket_score(BASKET_P50 - 1, None) == 0.1
    assert basket_score(BASKET_P50 + 1, None) == 0.3
    assert basket_score(BASKET_P90 + 1, None) == 0.6
    assert basket_score(BASKET_P99 + 1, None) == 0.9


def test_basket_score_returning_customer_uses_zscore():
    profile = CustomerProfile.new("cust_1", "2025-01-01T00:00:00.000000Z")
    profile.order_count = 5
    profile.avg_basket_value = 200.0
    profile.stddev_basket_value = 20.0

    # exactly at the customer's own average -> z=0 -> score 0.5
    assert math.isclose(basket_score(200.0, profile), 0.5)
    # 2 std devs above -> z=2 -> 0.5 + 0.15*2 = 0.8
    assert math.isclose(basket_score(240.0, profile), 0.8)
    # far below average clamps at 0, far above clamps at 1
    assert basket_score(0.0, profile) >= 0.0
    assert basket_score(100_000.0, profile) == 1.0


def test_basket_score_zero_stddev_does_not_divide_by_zero():
    profile = CustomerProfile.new("cust_1", "2025-01-01T00:00:00.000000Z")
    profile.order_count = 1
    profile.avg_basket_value = 200.0
    profile.stddev_basket_value = 0.0
    # should not raise, and any deviation should push score to the max
    assert basket_score(201.0, profile) == 1.0
    assert math.isclose(basket_score(200.0, profile), 0.5)


# --- tenure_score ---

def test_tenure_score_no_profile_is_neutral():
    score, raw = tenure_score("2026-01-10T00:00:00.000000Z", None)
    assert raw["account_age_days"] == 0
    assert raw["approval_rate"] == 0.5


def test_tenure_score_long_tenure_high_approval_is_low_risk():
    profile = CustomerProfile.new("cust_1", "2025-01-01T00:00:00.000000Z")
    profile.order_count = 20
    profile.approved_count = 20
    score, raw = tenure_score("2026-06-01T00:00:00.000000Z", profile)
    assert raw["approval_rate"] == 1.0
    assert score < 0.1


def test_tenure_score_new_profile_no_history_is_higher_risk_than_established():
    established = CustomerProfile.new("cust_1", "2025-01-01T00:00:00.000000Z")
    established.order_count = 20
    established.approved_count = 20
    established_score, _ = tenure_score("2026-06-01T00:00:00.000000Z", established)

    brand_new = CustomerProfile.new("cust_2", "2026-06-01T00:00:00.000000Z")
    new_score, _ = tenure_score("2026-06-01T00:00:00.000000Z", brand_new)

    assert new_score > established_score


# --- fraud_score ---

def test_fraud_score_no_flags():
    order = base_order()
    score, flags = fraud_score(order, None, is_first_order=True)
    assert score == 0.0
    assert flags == []


def test_fraud_score_velocity_flag():
    profile = CustomerProfile.new("cust_1", "2026-01-01T00:00:00.000000Z")
    profile.last_order_at = "2026-01-10T11:45:00.000000Z"  # 15 min before this order
    order = base_order(created_at="2026-01-10T12:00:00.000000Z")
    score, flags = fraud_score(order, profile, is_first_order=False)
    assert "velocity" in flags
    assert score >= 0.4


def test_fraud_score_velocity_flag_not_triggered_outside_window():
    profile = CustomerProfile.new("cust_1", "2026-01-01T00:00:00.000000Z")
    profile.last_order_at = "2026-01-10T10:00:00.000000Z"  # 2 hours before
    order = base_order(created_at="2026-01-10T12:00:00.000000Z")
    _, flags = fraud_score(order, profile, is_first_order=False)
    assert "velocity" not in flags


def test_fraud_score_odd_hour_requires_first_order():
    odd_hour_first = base_order(order_hour=2, is_first_order=True)
    _, flags_first = fraud_score(odd_hour_first, None, is_first_order=True)
    assert "odd_hour" in flags_first

    odd_hour_repeat = base_order(order_hour=2)
    _, flags_repeat = fraud_score(odd_hour_repeat, None, is_first_order=False)
    assert "odd_hour" not in flags_repeat


def test_fraud_score_address_payment_mismatch():
    order = base_order(ip_country="US", delivery_governorate="Cairo")
    _, flags = fraud_score(order, None, is_first_order=True)
    assert "address_payment_mismatch" in flags


def test_fraud_score_no_mismatch_when_governorate_missing():
    order = base_order(ip_country="US", delivery_governorate=None)
    _, flags = fraud_score(order, None, is_first_order=True)
    assert "address_payment_mismatch" not in flags


def test_fraud_score_device_reuse_flag():
    order = base_order(device_shared_count=5)
    _, flags = fraud_score(order, None, is_first_order=True)
    assert "device_reuse" in flags


def test_fraud_score_caps_at_one():
    profile = CustomerProfile.new("cust_1", "2026-01-01T00:00:00.000000Z")
    profile.last_order_at = "2026-01-10T11:45:00.000000Z"
    order = base_order(
        created_at="2026-01-10T12:00:00.000000Z",
        order_hour=2,
        ip_country="US",
        device_shared_count=5,
    )
    score, flags = fraud_score(order, profile, is_first_order=True)
    assert score == 1.0
    assert len(flags) == 4  # velocity + odd_hour + mismatch + device_reuse = 0.4+0.2+0.2+0.2=1.0 exactly


# --- payment_score ---

def test_payment_score_ranking():
    assert payment_score("card") < payment_score("wallet") < payment_score("cod")


def test_payment_score_unknown_method_raises():
    with pytest.raises(KeyError):
        payment_score("bitcoin")


# --- combine / decide: threshold bands ---

def test_decide_approve_band():
    order = base_order(basket_value=100.0, payment_method="card")
    result = decide(order, None)
    assert result["overall_score"] < 0.35
    assert result["decision"] == DECISION_APPROVE
    assert result["override_applied"] is None


def test_decide_manual_review_band():
    # Engineered to land in [0.35, 0.65) without tripping a hard override.
    profile = CustomerProfile.new("cust_1", "2025-06-01T00:00:00.000000Z")
    profile.order_count = 3
    profile.approved_count = 1
    profile.avg_basket_value = 200.0
    profile.stddev_basket_value = 20.0
    order = base_order(basket_value=260.0, payment_method="cod", is_first_order=False)
    result = decide(order, profile)
    assert 0.35 <= result["overall_score"] < 0.65
    assert result["decision"] == DECISION_MANUAL_REVIEW
    assert result["override_applied"] is None


def test_decide_decline_band_without_override():
    # Engineered to clear 0.65 through the weighted score alone (fraud_score
    # stays at 0.4, below the 0.8 hard-decline override threshold).
    profile = CustomerProfile.new("cust_1", "2026-01-09T00:00:00.000000Z")
    profile.order_count = 4
    profile.approved_count = 0
    profile.declined_count = 4
    profile.avg_basket_value = 200.0
    profile.stddev_basket_value = 10.0
    order = base_order(
        basket_value=260.0,
        payment_method="cod",
        is_first_order=False,
        ip_country="US",
        device_shared_count=5,
    )
    result = decide(order, profile)
    assert result["signals"]["fraud"]["score"] < FRAUD_HARD_DECLINE
    assert result["overall_score"] >= 0.65
    assert result["decision"] == DECISION_DECLINE
    assert result["override_applied"] is None


def test_decide_threshold_boundary_just_below_approve_max():
    # overall_score for a pure-card, average-basket, established customer
    # should land safely under 0.35.
    profile = CustomerProfile.new("cust_1", "2020-01-01T00:00:00.000000Z")
    profile.order_count = 50
    profile.approved_count = 50
    profile.avg_basket_value = 200.0
    profile.stddev_basket_value = 20.0
    order = base_order(basket_value=200.0, payment_method="card", is_first_order=False)
    result = decide(order, profile)
    assert result["decision"] == DECISION_APPROVE
    assert result["overall_score"] < 0.35


# --- hard overrides ---

def test_hard_override_fraud_forces_decline_even_with_low_other_signals():
    profile = CustomerProfile.new("cust_1", "2020-01-01T00:00:00.000000Z")
    profile.order_count = 50
    profile.approved_count = 50
    profile.avg_basket_value = 200.0
    profile.stddev_basket_value = 20.0
    profile.last_order_at = "2026-01-10T11:45:00.000000Z"
    order = base_order(
        created_at="2026-01-10T12:00:00.000000Z",
        basket_value=200.0,
        payment_method="card",
        order_hour=2,
        ip_country="US",
        device_shared_count=5,
        is_first_order=False,
    )
    result = decide(order, profile)
    assert result["signals"]["fraud"]["score"] >= 0.8
    assert result["decision"] == DECISION_DECLINE
    assert result["override_applied"] == OVERRIDE_FRAUD_DECLINE


def test_hard_override_new_customer_cod_large_basket_forces_manual_review():
    order = base_order(
        basket_value=BASKET_P99 + 1,
        payment_method="cod",
        is_first_order=True,
        order_hour=14,  # not odd-hour, keep fraud_score low so the override is what fires
        ip_country="EG",
        device_shared_count=1,
    )
    result = decide(order, None)
    assert result["signals"]["fraud"]["score"] < FRAUD_HARD_DECLINE
    assert result["decision"] == DECISION_MANUAL_REVIEW
    assert result["override_applied"] == OVERRIDE_NEW_COD_LARGE_BASKET


def test_decide_signals_payload_has_full_lineage_fields():
    order = base_order()
    result = decide(order, None)
    assert set(result.keys()) >= {
        "decision", "overall_score", "ruleset_version", "override_applied", "signals",
    }
    assert set(result["signals"].keys()) == {
        "basket_value", "tenure", "fraud", "payment_risk",
    }
    for signal in result["signals"].values():
        assert "score" in signal and "weight" in signal
