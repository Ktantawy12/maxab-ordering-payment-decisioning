"""Shared schema and scoring constants for the decisioning pipeline.

Single source of truth for record shapes and rule constants, used by the
seed data generator, both Lambda handlers, and the test suite. See
design plan section 5 (item schemas) and section 7 (scoring formula).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import math
from decimal import Decimal
from typing import Optional

RULESET_VERSION = "v1"

# --- Basket value breakpoints (dataset-wide, EGP) ---
BASKET_P50 = 250.0
BASKET_P90 = 900.0
BASKET_P99 = 2500.0

# --- Signal weights (sum to 1.0) ---
WEIGHT_BASKET = 0.20
WEIGHT_TENURE = 0.25
WEIGHT_FRAUD = 0.35
WEIGHT_PAYMENT = 0.20

# --- Fraud rule flag contributions ---
FRAUD_FLAG_VELOCITY = 0.4
FRAUD_FLAG_ODD_HOUR = 0.2
FRAUD_FLAG_ADDRESS_MISMATCH = 0.2
FRAUD_FLAG_DEVICE_REUSE = 0.2
VELOCITY_WINDOW_MINUTES = 60
ODD_HOUR_MAX = 5  # order_hour in [0, ODD_HOUR_MAX] is "odd hour"
DEVICE_REUSE_THRESHOLD = 3

# --- Payment-method risk table (static, independent of the other signals) ---
PAYMENT_RISK = {"card": 0.1, "wallet": 0.2, "cod": 0.5}
PAYMENT_METHODS = tuple(PAYMENT_RISK.keys())

# --- Decision thresholds ---
APPROVE_MAX = 0.35
MANUAL_REVIEW_MAX = 0.65

# --- Hard override thresholds ---
FRAUD_HARD_DECLINE = 0.8

DECISION_APPROVE = "APPROVE"
DECISION_MANUAL_REVIEW = "MANUAL_REVIEW"
DECISION_DECLINE = "DECLINE"
DECISIONS = (DECISION_APPROVE, DECISION_MANUAL_REVIEW, DECISION_DECLINE)

OVERRIDE_FRAUD_DECLINE = "FRAUD_SCORE_HARD_DECLINE"
OVERRIDE_NEW_COD_LARGE_BASKET = "FIRST_ORDER_COD_LARGE_BASKET_MANUAL_REVIEW"

ACTION_FULFILLMENT_RELEASE = "FULFILLMENT_RELEASE"
ACTION_MANUAL_REVIEW_QUEUE = "MANUAL_REVIEW_QUEUE"
ACTION_BLOCKLIST_LOG = "BLOCKLIST_LOG"

STATUS_COMPLETED = "COMPLETED"
STATUS_PENDING = "PENDING"

# decision -> (action_type, status), per plan section 6/8
DECISION_TO_ACTION = {
    DECISION_APPROVE: (ACTION_FULFILLMENT_RELEASE, STATUS_COMPLETED),
    DECISION_MANUAL_REVIEW: (ACTION_MANUAL_REVIEW_QUEUE, STATUS_PENDING),
    DECISION_DECLINE: (ACTION_BLOCKLIST_LOG, STATUS_COMPLETED),
}


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)


def to_decimal(value) -> Decimal:
    """Convert floats to Decimal for DynamoDB, without float-repr artifacts."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(1) if value else Decimal(0)
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


@dataclasses.dataclass
class OrderRecord:
    """One line of a raw order NDJSON batch file (plan section 5)."""

    order_id: str
    customer_id: str
    source_updated_at: str
    created_at: str
    basket_value: float
    currency: str
    item_count: int
    payment_method: str
    delivery_governorate: Optional[str]
    is_first_order: bool
    device_id: str
    device_shared_count: int
    ip_country: str
    order_hour: int

    def to_json_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "OrderRecord":
        return cls(**{f.name: data[f.name] for f in dataclasses.fields(cls)})


@dataclasses.dataclass
class CustomerProfile:
    """CustomerProfilesTable item. Owned exclusively by the downstream
    processor (plan section 8) — the decision engine only ever reads it."""

    customer_id: str
    first_seen_at: str
    order_count: int = 0
    approved_count: int = 0
    manual_review_count: int = 0
    declined_count: int = 0
    cumulative_gmv: float = 0.0
    avg_basket_value: float = 0.0
    stddev_basket_value: float = 0.0
    last_order_at: str = ""
    last_decision: str = ""

    @classmethod
    def new(cls, customer_id: str, first_seen_at: str) -> "CustomerProfile":
        return cls(customer_id=customer_id, first_seen_at=first_seen_at)

    def to_item(self) -> dict:
        item = dataclasses.asdict(self)
        for key in (
            "order_count",
            "approved_count",
            "manual_review_count",
            "declined_count",
            "cumulative_gmv",
            "avg_basket_value",
            "stddev_basket_value",
        ):
            item[key] = to_decimal(item[key])
        return item

    @classmethod
    def from_item(cls, item: dict) -> "CustomerProfile":
        kwargs = {f.name: item[f.name] for f in dataclasses.fields(cls)}
        for key in (
            "order_count",
            "approved_count",
            "manual_review_count",
            "declined_count",
        ):
            kwargs[key] = int(kwargs[key])
        for key in ("cumulative_gmv", "avg_basket_value", "stddev_basket_value"):
            kwargs[key] = float(kwargs[key])
        return cls(**kwargs)


def update_running_stats(avg: float, stddev: float, n: int, new_value: float) -> tuple[float, float]:
    """Welford's online algorithm, reconstructing M2 from the stored stddev
    each call since only avg/stddev (not M2) are persisted on the profile
    item. n is the count *before* this new_value is included."""
    new_n = n + 1
    m2 = (stddev ** 2) * n
    delta = new_value - avg
    new_avg = avg + delta / new_n
    delta2 = new_value - new_avg
    new_m2 = m2 + delta * delta2
    new_stddev = math.sqrt(new_m2 / new_n) if new_n > 0 else 0.0
    return new_avg, new_stddev


@dataclasses.dataclass
class DecisionSignal:
    score: float
    weight: float


@dataclasses.dataclass
class DownstreamActionRecord:
    """DownstreamActionsTable item (plan section 5/8)."""

    order_id: str
    action_type: str
    status: str
    customer_id: str
    decision: str
    source_decision_request_id: str
    created_at: str
    reason_codes: list
    customer_order_count_after: int

    def to_item(self) -> dict:
        return {
            "order_id": self.order_id,
            "action_type": self.action_type,
            "status": self.status,
            "customer_id": self.customer_id,
            "decision": self.decision,
            "source_decision_request_id": self.source_decision_request_id,
            "created_at": self.created_at,
            "details": {
                "reason_codes": self.reason_codes,
                "customer_order_count_after": to_decimal(self.customer_order_count_after),
            },
        }
