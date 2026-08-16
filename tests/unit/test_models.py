import math

from common.models import (
    CustomerProfile,
    OrderRecord,
    update_running_stats,
)


def test_order_record_json_roundtrip():
    record = OrderRecord(
        order_id="ord_1",
        customer_id="cust_1",
        source_updated_at="2026-01-01T00:00:00.000000Z",
        created_at="2026-01-01T00:00:00.000000Z",
        basket_value=123.45,
        currency="EGP",
        item_count=3,
        payment_method="card",
        delivery_governorate="Cairo",
        is_first_order=True,
        device_id="dev_1",
        device_shared_count=1,
        ip_country="EG",
        order_hour=14,
    )
    roundtripped = OrderRecord.from_json_dict(record.to_json_dict())
    assert roundtripped == record


def test_customer_profile_item_roundtrip():
    profile = CustomerProfile.new("cust_1", "2026-01-01T00:00:00.000000Z")
    profile.order_count = 3
    profile.approved_count = 2
    profile.cumulative_gmv = 450.5
    profile.avg_basket_value = 150.16
    profile.stddev_basket_value = 12.3

    roundtripped = CustomerProfile.from_item(profile.to_item())
    assert roundtripped.customer_id == profile.customer_id
    assert roundtripped.order_count == 3
    assert math.isclose(roundtripped.avg_basket_value, 150.16)
    assert math.isclose(roundtripped.stddev_basket_value, 12.3)


def test_update_running_stats_matches_batch_computation():
    values = [100.0, 200.0, 150.0, 400.0, 50.0]
    avg, stddev, n = 0.0, 0.0, 0
    for v in values:
        avg, stddev = update_running_stats(avg, stddev, n, v)
        n += 1

    expected_avg = sum(values) / len(values)
    expected_variance = sum((v - expected_avg) ** 2 for v in values) / len(values)
    expected_stddev = math.sqrt(expected_variance)

    assert math.isclose(avg, expected_avg, rel_tol=1e-9)
    assert math.isclose(stddev, expected_stddev, rel_tol=1e-9)


def test_update_running_stats_first_value_has_zero_stddev():
    avg, stddev = update_running_stats(0.0, 0.0, 0, 42.0)
    assert avg == 42.0
    assert stddev == 0.0
