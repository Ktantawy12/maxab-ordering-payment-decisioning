import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from common.models import OrderRecord
from seed_data import (
    EDGE_CASE_SHARE,
    FRAUD_SHARE,
    generate_dataset,
)

SMALL_N = 2_000
SEED = 42


def _generate():
    return generate_dataset(total=SMALL_N, seed=SEED)


def test_record_count_matches_total():
    records, meta = _generate()
    assert len(records) == SMALL_N
    assert len(meta) == SMALL_N


def test_deterministic_for_fixed_seed():
    records_a, _ = generate_dataset(total=SMALL_N, seed=SEED)
    records_b, _ = generate_dataset(total=SMALL_N, seed=SEED)
    assert records_a == records_b


def test_all_records_parse_as_order_records():
    records, _ = _generate()
    for r in records:
        # Raises if any field is missing/mistyped for the shared schema.
        OrderRecord.from_json_dict(r)


def test_fraud_share_within_tolerance():
    _, meta = _generate()
    n_fraud = sum(1 for m in meta if m["is_fraud_shaped"])
    rate = n_fraud / SMALL_N
    assert abs(rate - FRAUD_SHARE) < 0.02, f"fraud rate {rate:.3f} not close to target {FRAUD_SHARE}"


def test_duplicate_order_id_rate_within_tolerance():
    records, meta = _generate()
    order_ids = [r["order_id"] for r in records]
    n_unique = len(set(order_ids))
    dup_rate = (SMALL_N - n_unique) / SMALL_N
    assert abs(dup_rate - EDGE_CASE_SHARE) < 0.02

    tagged = sum(1 for m in meta if "duplicate_order_id_update" in m["edge_cases"])
    assert tagged > 0
    assert abs(tagged / SMALL_N - EDGE_CASE_SHARE) < 0.02


def test_duplicate_rows_have_later_source_updated_at_than_original():
    records, meta = _generate()
    by_order_id: dict[str, list[int]] = {}
    for idx, r in enumerate(records):
        by_order_id.setdefault(r["order_id"], []).append(idx)

    for idx, m in enumerate(meta):
        if "duplicate_order_id_update" not in m["edge_cases"]:
            continue
        order_id = records[idx]["order_id"]
        indices = by_order_id[order_id]
        assert len(indices) >= 2
        earlier = [j for j in indices if j < idx]
        assert earlier, "duplicate row should have an earlier occurrence"
        original_idx = min(earlier)
        assert records[idx]["source_updated_at"] > records[original_idx]["source_updated_at"]


def test_edge_case_fields_do_not_crash_downstream_parsing():
    records, meta = _generate()
    for r, m in zip(records, meta):
        if "zero_basket_value" in m["edge_cases"]:
            assert r["basket_value"] == 0.0
        if "null_governorate" in m["edge_cases"]:
            assert r["delivery_governorate"] is None
        # Must still be a well-formed record regardless of edge-case mutation.
        OrderRecord.from_json_dict(r)


def test_fraud_velocity_rows_are_within_60_minutes_of_prior_order():
    import datetime as dt

    records, meta = _generate()
    by_order_id_positions: dict[str, list[int]] = {}
    for idx, r in enumerate(records):
        by_order_id_positions.setdefault(r["customer_id"], []).append(idx)

    for idx, m in enumerate(meta):
        if "fraud_velocity_device_reuse" not in m["edge_cases"]:
            continue
        customer_id = records[idx]["customer_id"]
        positions = by_order_id_positions[customer_id]
        prior_positions = [p for p in positions if p < idx]
        assert prior_positions
        prior_idx = prior_positions[0]

        t_prior = dt.datetime.strptime(
            records[prior_idx]["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        t_this = dt.datetime.strptime(records[idx]["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        delta_minutes = (t_this - t_prior).total_seconds() / 60
        assert 0 < delta_minutes < 60
        assert records[idx]["device_shared_count"] >= 3


def test_fraud_odd_hour_rows_are_first_order_and_in_odd_hour_window():
    records, meta = _generate()
    for idx, m in enumerate(meta):
        if "fraud_odd_hour_mismatch" not in m["edge_cases"]:
            continue
        assert records[idx]["is_first_order"] is True
        assert 0 <= records[idx]["order_hour"] <= 5
        assert records[idx]["ip_country"] != "EG"
