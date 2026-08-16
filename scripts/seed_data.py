#!/usr/bin/env python
"""Synthetic Ordering & Payment dataset generator (design plan section 9).

Generates ~100k realistic order records with Pareto-shaped customer skew, a
small share of structural edge cases, and fraud-shaped anomalies, then
writes them as batched NDJSON files under an output directory (default
data/batches/), plus a local-only ground-truth file used to sanity-check the
deployed pipeline's decision distribution later. The ground-truth file is
never uploaded to S3 and is not visible to the decision engine.

Two implementation clarifications made while coding, called out here since
they resolve details the design plan left implicit:

- Decisioning (plan section 7) treats "is this the customer's first order"
  as the absence of a CustomerProfiles item, not a self-reported field --
  trusting a client-supplied flag for a fraud signal would be circular. The
  `is_first_order` field on each raw record is a best-effort label set here
  for realism/debugging only and does not drive scoring.
- Plan section 9's illustrative fraud combo "velocity + odd-hour" is not
  jointly satisfiable under section 7's exact formulas: odd_hour_flag
  requires is_first_order=True, while velocity_flag requires a *second*
  order from the same customer (is_first_order=False by then). Fraud rows
  here instead use two combos that ARE jointly satisfiable: (a) first-order
  rows get odd_hour_flag + address_payment_mismatch_flag, (b) repeat-order
  rows get velocity_flag + device_reuse_flag.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from common.models import (  # noqa: E402
    BASKET_P90,
    BASKET_P50,
    BASKET_P99,
    DEVICE_REUSE_THRESHOLD,
    ODD_HOUR_MAX,
    PAYMENT_METHODS,
)

DEFAULT_TOTAL = 100_000
DEFAULT_BATCH_SIZE = 2_500
DEFAULT_SEED = 42
N_CUSTOMERS = 20_000
N_SHARED_DEVICES = 60
SHARED_DEVICE_REUSE_RANGE = (3, 8)

GOVERNORATES = [
    "Cairo", "Giza", "Alexandria", "Qalyubia", "Sharqia", "Dakahlia",
    "Gharbia", "Monufia", "Beheira", "Fayoum", "Ismailia", "Suez",
]
FOREIGN_COUNTRIES = ["SA", "AE", "US", "GB", "unknown"]
PAYMENT_WEIGHTS = {"cod": 0.5, "wallet": 0.3, "card": 0.2}

EDGE_CASE_SHARE = 0.015  # ~1.5% each, combinable
FRAUD_SHARE = 0.03

DATASET_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
DATASET_SPAN_DAYS = 60  # records/day (~1,667) must stay below the batch
                        # width (2,500, see main()) so any duplicate-edge-case
                        # pair (>=1 batch apart) always lands on different
                        # days -- guarantees the "later index -> strictly
                        # later timestamp" property the update edge case
                        # relies on, even with per-order hour now randomized.

# Grocery-delivery day-part shape: quiet overnight, ramps through the
# morning, a lunch peak, and the highest peak at dinner (18-21). Index 0
# = hour 0. Previously order_hour was `created_at.hour` off a purely
# linear timeline, which cycled uniformly through all 24 hours -- not
# realistic for an actual ordering pattern.
HOUR_WEIGHTS = [
    0.4, 0.3, 0.2, 0.2, 0.3, 0.5,   # 0-5: overnight
    1.5, 2.5, 3.0,                  # 6-8: breakfast ramp-up
    3.0, 3.5, 4.0,                  # 9-11: mid-morning
    5.5, 6.0, 4.5,                  # 12-14: lunch peak
    3.5, 3.5, 4.0,                  # 15-17: afternoon
    5.5, 6.5, 6.0, 4.5,             # 18-21: dinner peak (highest)
    2.5, 1.0,                       # 22-23: winding down
]


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _basket_value(rng: random.Random) -> float:
    # Lognormal calibrated so the median/90th-pct roughly match the scoring
    # breakpoints in common.models, keeping generator and scoring consistent.
    mu = math.log(BASKET_P50)
    sigma = (math.log(BASKET_P90) - mu) / 1.2816
    return round(rng.lognormvariate(mu, sigma), 2)


def _build_customer_pool() -> list[str]:
    return [f"cust_{i:06d}" for i in range(N_CUSTOMERS)]


def _build_customer_weights(n: int) -> list[float]:
    # Zipf-like power-law weights -> Pareto-shaped order-count skew. Exponent
    # 0.8 (not the originally-tried 1.3, which converges too aggressively --
    # ~1/zeta(1.3) of ALL orders landed on the single top-ranked customer,
    # and everything past roughly rank 5,500 was statistically unreachable
    # in a 100k-record draw). 0.8 keeps the full customer pool active while
    # still giving the top 20% of customers roughly 2/3 of all orders.
    return [1.0 / (rank ** 0.8) for rank in range(1, n + 1)]


def _build_device_pool(customers: list[str], rng: random.Random) -> dict[str, tuple[str, int]]:
    """Returns {customer_id: (device_id, device_shared_count)} for the
    subset of customers deliberately sharing a small pool of devices."""
    shared_ids = [f"dev_shared_{i:03d}" for i in range(N_SHARED_DEVICES)]
    pool = rng.sample(customers, k=min(len(customers), N_SHARED_DEVICES * 6))
    mapping: dict[str, tuple[str, int]] = {}
    idx = 0
    for device_id in shared_ids:
        reuse_n = rng.randint(*SHARED_DEVICE_REUSE_RANGE)
        assigned = pool[idx: idx + reuse_n]
        idx += reuse_n
        for c in assigned:
            mapping[c] = (device_id, len(assigned))
    return mapping


def generate_dataset(total: int = DEFAULT_TOTAL, seed: int = DEFAULT_SEED):
    """Returns (records, meta): parallel lists aligned by index.

    Each record is a JSON-serializable dict matching the raw order schema
    (plan section 5). meta[i] carries generator-internal ground truth
    (is_fraud_shaped, edge_cases) that is never written to the NDJSON output.
    """
    rng = random.Random(seed)

    customers = _build_customer_pool()
    weights = _build_customer_weights(len(customers))
    customer_device_map = _build_device_pool(customers, rng)

    # Bulk-draw the two weighted fields once (precomputed cum_weights) rather
    # than per-row -- per-row random.choices(weights=...) rebuilds the
    # cumulative distribution every call, which is O(n_customers) per row
    # and made the 100k run unacceptably slow.
    customer_cum_weights = list(itertools.accumulate(weights))
    customer_ids = rng.choices(customers, cum_weights=customer_cum_weights, k=total)
    payment_choices = list(PAYMENT_WEIGHTS.keys())
    payment_cum_weights = list(itertools.accumulate(PAYMENT_WEIGHTS.values()))
    payment_methods = rng.choices(payment_choices, cum_weights=payment_cum_weights, k=total)
    hour_cum_weights = list(itertools.accumulate(HOUR_WEIGHTS))
    order_hours = rng.choices(range(24), cum_weights=hour_cum_weights, k=total)

    records: list[dict] = []
    meta: list[dict] = []
    occurrences: dict[str, list[int]] = {}

    for i in range(total):
        customer_id = customer_ids[i]
        day_offset = int((i / total) * DATASET_SPAN_DAYS)
        created_at = (
            DATASET_START
            + timedelta(days=day_offset, hours=order_hours[i])
            + timedelta(minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
        )

        is_first_order = customer_id not in occurrences
        device_id, shared_count = customer_device_map.get(
            customer_id, (f"dev_{customer_id}", 1)
        )

        record = {
            "order_id": f"ord_{rng.getrandbits(48):012x}",
            "customer_id": customer_id,
            "source_updated_at": _iso(created_at),
            "created_at": _iso(created_at),
            "basket_value": _basket_value(rng),
            "currency": "EGP",
            "item_count": rng.randint(1, 12),
            "payment_method": payment_methods[i],
            "delivery_governorate": rng.choice(GOVERNORATES),
            "is_first_order": is_first_order,
            "device_id": device_id,
            "device_shared_count": shared_count,
            "ip_country": "EG",
            "order_hour": created_at.hour,
        }
        occurrences.setdefault(customer_id, []).append(i)
        records.append(record)
        meta.append({"is_fraud_shaped": False, "edge_cases": []})

    # Fraud shaping must run before edge-case duplication: it relies on
    # `occurrences` (customer_id -> row indices) computed above, which the
    # duplicate-order_id edge case (below) can invalidate by overwriting a
    # row's customer_id. Running fraud first, then excluding every row it
    # touched from being picked as a duplicate *target*, keeps both passes
    # internally consistent -- a row that already got its created_at/device
    # fields forced into a fraud shape never gets its customer_id/order_id
    # silently swapped out from under it afterward.
    fraud_touched = _apply_fraud_shapes(records, meta, rng, occurrences)
    _apply_edge_cases(records, meta, rng, total, exclude=fraud_touched)

    return records, meta


def _apply_edge_cases(records, meta, rng, total, exclude=frozenset()):
    n_each = max(1, int(total * EDGE_CASE_SHARE))

    # zero_basket_value and extreme_basket_value both mutate basket_value
    # with contradictory intent -- unlike the other edge cases (which touch
    # independent fields and are fine to combine), these two must be drawn
    # from disjoint index sets or one silently overwrites the other while
    # both tags remain in the ground truth.
    basket_mutation_pool = [i for i in range(total) if i not in exclude]
    rng.shuffle(basket_mutation_pool)
    zero_basket_indices = basket_mutation_pool[:n_each]
    extreme_basket_indices = basket_mutation_pool[n_each:2 * n_each]

    for i in zero_basket_indices:
        records[i]["basket_value"] = 0.0
        meta[i]["edge_cases"].append("zero_basket_value")

    for i in extreme_basket_indices:
        records[i]["basket_value"] = round(BASKET_P99 * rng.uniform(8, 12), 2)
        meta[i]["edge_cases"].append("extreme_basket_value")

    for i in rng.sample(range(total), k=n_each):
        records[i]["delivery_governorate"] = None
        meta[i]["edge_cases"].append("null_governorate")

    # Duplicate order_id "update" case: pick a slot roughly a batch-width
    # ahead of an existing order and re-emit it with a changed field and a
    # later source_updated_at, exercising the conditional-PutItem overwrite
    # path (plan section 6) end-to-end when the pipeline runs.
    #
    # A row picked as the duplicate's *source* (j) must never later be
    # picked as a *target* (i) for a different duplicate -- overwriting it
    # would silently change the order_id/customer_id that an earlier
    # duplicate already copied by value, orphaning that link. `exclude`
    # additionally protects rows fraud-shaping already touched.
    batch_width = max(1, total // 40)
    candidates = list(range(batch_width, total))
    rng.shuffle(candidates)
    sources_used: set[int] = set()
    targets_used: set[int] = set()
    dup_count = 0
    for i in candidates:
        if dup_count >= n_each:
            break
        if i in exclude or i in sources_used or i in targets_used:
            continue
        earlier_max = i - batch_width
        if earlier_max <= 0:
            continue
        j = rng.randint(0, earlier_max - 1)
        if j in exclude or j in sources_used or j in targets_used:
            continue
        original = records[j]
        records[i]["order_id"] = original["order_id"]
        records[i]["customer_id"] = original["customer_id"]
        records[i]["basket_value"] = round(original["basket_value"] * rng.uniform(1.1, 1.5), 2)
        records[i]["payment_method"] = rng.choice(PAYMENT_METHODS)
        # created_at/source_updated_at at index i is already later than j's
        # (dataset timestamps increase with index) -- that's what makes this
        # a legitimate "update" under the ConditionExpression.
        meta[i]["edge_cases"].append("duplicate_order_id_update")
        sources_used.add(j)
        targets_used.add(i)
        dup_count += 1


def _apply_fraud_shapes(records, meta, rng, occurrences) -> set[int]:
    """Mutates records/meta in place; returns the set of row indices it
    touched (as either the flagged row or the prior order it anchors to),
    so the edge-case pass can avoid overwriting them afterward."""
    total = len(records)
    n_target = max(1, int(total * FRAUD_SHARE))
    n_velocity = n_target // 2
    n_odd_hour_mismatch = n_target - n_velocity

    repeat_customers = [c for c, idxs in occurrences.items() if len(idxs) >= 2]
    rng.shuffle(repeat_customers)
    first_order_indices = [i for i, r in enumerate(records) if r["is_first_order"]]
    rng.shuffle(first_order_indices)

    touched: set[int] = set()

    # (a) repeat customers: velocity_flag + device_reuse_flag
    for customer_id in repeat_customers[:n_velocity]:
        j, i = occurrences[customer_id][0], occurrences[customer_id][1]
        base_dt = _parse_iso(records[j]["created_at"])
        new_dt = base_dt + timedelta(minutes=rng.randint(5, 55))
        records[i]["created_at"] = _iso(new_dt)
        records[i]["source_updated_at"] = _iso(new_dt)
        records[i]["order_hour"] = new_dt.hour
        records[i]["device_id"] = f"dev_shared_fraud_{customer_id}"
        records[i]["device_shared_count"] = rng.randint(
            DEVICE_REUSE_THRESHOLD, DEVICE_REUSE_THRESHOLD + 4
        )
        meta[i]["is_fraud_shaped"] = True
        meta[i]["edge_cases"].append("fraud_velocity_device_reuse")
        touched.add(j)
        touched.add(i)

    # (b) first-order rows: odd_hour_flag + address_payment_mismatch_flag
    # Excludes rows loop (a) already touched: a repeat customer's *first*
    # order is itself a valid "first order" row and so could otherwise also
    # be picked here, which would overwrite the created_at loop (a) already
    # used to compute its paired row's velocity delta -- silently breaking
    # that pair's <60-minute invariant after the fact.
    first_order_indices = [i for i in first_order_indices if i not in touched]
    for i in first_order_indices[:n_odd_hour_mismatch]:
        dt = _parse_iso(records[i]["created_at"]).replace(hour=rng.randint(0, ODD_HOUR_MAX))
        records[i]["created_at"] = _iso(dt)
        records[i]["source_updated_at"] = _iso(dt)
        records[i]["order_hour"] = dt.hour
        records[i]["ip_country"] = rng.choice(FOREIGN_COUNTRIES)
        meta[i]["is_fraud_shaped"] = True
        meta[i]["edge_cases"].append("fraud_odd_hour_mismatch")
        touched.add(i)

    return touched


def write_batches(records: list[dict], batch_size: int, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for idx, start in enumerate(range(0, len(records), batch_size)):
        chunk = records[start:start + batch_size]
        path = output_dir / f"batch-{idx:03d}.ndjson"
        with path.open("w", encoding="utf-8") as f:
            for r in chunk:
                f.write(json.dumps(r) + "\n")
        paths.append(path)
    return paths


def write_ground_truth(records: list[dict], meta: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"order_id": r["order_id"], "row_index": i, **m}
        for i, (r, m) in enumerate(zip(records, meta))
    ]
    with path.open("w", encoding="utf-8") as f:
        json.dump(rows, f)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--total", type=int, default=DEFAULT_TOTAL)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output-dir", type=Path, default=Path("data/batches"))
    parser.add_argument("--ground-truth-path", type=Path, default=Path("data/ground_truth.json"))
    args = parser.parse_args()

    records, meta = generate_dataset(total=args.total, seed=args.seed)
    paths = write_batches(records, args.batch_size, args.output_dir)
    write_ground_truth(records, meta, args.ground_truth_path)

    n_fraud = sum(1 for m in meta if m["is_fraud_shaped"])
    n_edge = sum(1 for m in meta if m["edge_cases"])
    print(f"Wrote {len(records)} records across {len(paths)} batch files to {args.output_dir}")
    print(f"Fraud-shaped rows: {n_fraud} ({n_fraud/len(records):.1%})")
    print(f"Edge-case rows: {n_edge} ({n_edge/len(records):.1%})")
    print(f"Ground truth written to {args.ground_truth_path}")


if __name__ == "__main__":
    main()
