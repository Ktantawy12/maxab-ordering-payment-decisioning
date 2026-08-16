"""Moto-mocked integration tests for the downstream processor Lambda (plan
section 11). No live AWS calls, no cost."""
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ["CUSTOMER_PROFILES_TABLE"] = "TestCustomerProfiles"
os.environ["DOWNSTREAM_ACTIONS_TABLE"] = "TestDownstreamActions"

from decimal import Decimal  # noqa: E402

import boto3  # noqa: E402
from boto3.dynamodb.types import TypeSerializer  # noqa: E402
from moto import mock_aws  # noqa: E402

from downstream_processor import handler as downstream_handler  # noqa: E402

CUSTOMER_PROFILES_TABLE = os.environ["CUSTOMER_PROFILES_TABLE"]
DOWNSTREAM_ACTIONS_TABLE = os.environ["DOWNSTREAM_ACTIONS_TABLE"]

_serializer = TypeSerializer()


def _create_infra():
    ddb = boto3.resource("dynamodb")
    ddb.create_table(
        TableName=CUSTOMER_PROFILES_TABLE,
        AttributeDefinitions=[{"AttributeName": "customer_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "customer_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName=DOWNSTREAM_ACTIONS_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "order_id", "AttributeType": "S"},
            {"AttributeName": "action_type", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "order_id", "KeyType": "HASH"},
            {"AttributeName": "action_type", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb


def _decision_item(order_id, customer_id, decision, basket_value=Decimal("200.0"),
                    fraud_flags=None, request_id="req-1",
                    source_updated_at="2026-01-10T12:00:00.000000Z"):
    return {
        "order_id": order_id,
        "customer_id": customer_id,
        "decision": decision,
        "overall_score": Decimal("0.5"),
        "ruleset_version": "v1",
        "signals": {
            "basket_value": {
                "score": Decimal("0.5"), "weight": Decimal("0.2"), "raw_value": basket_value,
            },
            "tenure": {
                "score": Decimal("0.5"), "weight": Decimal("0.25"),
                "raw_value": {"account_age_days": Decimal("0"), "approval_rate": Decimal("0.5")},
            },
            "fraud": {
                "score": Decimal("0.2"), "weight": Decimal("0.35"),
                "contributing_flags": fraud_flags or [],
            },
            "payment_risk": {
                "score": Decimal("0.1"), "weight": Decimal("0.2"), "payment_method": "card",
            },
        },
        "override_applied": None,
        "source_updated_at": source_updated_at,
        "decided_at": "2026-01-10T12:00:01.000000Z",
        "lambda_request_id": request_id,
        "source_s3_key": "batches/batch-000.ndjson",
    }


def _stream_event(decision_items: list, event_names=None) -> dict:
    records = []
    for i, item in enumerate(decision_items):
        event_name = (event_names or [])[i] if event_names else "INSERT"
        records.append({
            "eventID": f"evt-{i}",
            "eventName": event_name,
            "dynamodb": {
                "SequenceNumber": str(1000 + i),
                "NewImage": {k: _serializer.serialize(v) for k, v in item.items()},
            },
        })
    return {"Records": records}


@mock_aws
def test_new_profile_is_safely_initialized():
    ddb = _create_infra()
    item = _decision_item("ord_a", "cust_new", "APPROVE")
    result = downstream_handler.lambda_handler(_stream_event([item]), None)
    assert result["batchItemFailures"] == []

    profiles = ddb.Table(CUSTOMER_PROFILES_TABLE)
    profile = profiles.get_item(Key={"customer_id": "cust_new"})["Item"]
    assert profile["order_count"] == 1
    assert profile["approved_count"] == 1
    assert profile["last_decision"] == "APPROVE"
    assert profile["first_seen_at"] == item["source_updated_at"]


@mock_aws
def test_existing_profile_is_read_and_updated():
    ddb = _create_infra()
    profiles = ddb.Table(CUSTOMER_PROFILES_TABLE)
    profiles.put_item(Item={
        "customer_id": "cust_1",
        "first_seen_at": "2025-01-01T00:00:00.000000Z",
        "order_count": 2,
        "approved_count": 1,
        "manual_review_count": 1,
        "declined_count": 0,
        "cumulative_gmv": Decimal("400"),
        "avg_basket_value": Decimal("200"),
        "stddev_basket_value": Decimal("0"),
        "last_order_at": "2025-06-01T00:00:00.000000Z",
        "last_decision": "MANUAL_REVIEW",
    })

    item = _decision_item("ord_b", "cust_1", "APPROVE", basket_value=Decimal("300"),
                           source_updated_at="2026-01-10T12:00:00.000000Z")
    downstream_handler.lambda_handler(_stream_event([item]), None)

    profile = profiles.get_item(Key={"customer_id": "cust_1"})["Item"]
    assert profile["order_count"] == 3
    assert profile["approved_count"] == 2
    assert profile["last_decision"] == "APPROVE"
    assert profile["last_order_at"] == "2026-01-10T12:00:00.000000Z"
    assert float(profile["cumulative_gmv"]) == 700.0


@mock_aws
def test_approve_maps_to_fulfillment_release_completed():
    ddb = _create_infra()
    item = _decision_item("ord_a", "cust_1", "APPROVE")
    downstream_handler.lambda_handler(_stream_event([item]), None)

    actions = ddb.Table(DOWNSTREAM_ACTIONS_TABLE)
    action = actions.get_item(Key={"order_id": "ord_a", "action_type": "FULFILLMENT_RELEASE"})["Item"]
    assert action["status"] == "COMPLETED"
    assert action["decision"] == "APPROVE"


@mock_aws
def test_manual_review_maps_to_review_queue_pending():
    ddb = _create_infra()
    item = _decision_item("ord_a", "cust_1", "MANUAL_REVIEW")
    downstream_handler.lambda_handler(_stream_event([item]), None)

    actions = ddb.Table(DOWNSTREAM_ACTIONS_TABLE)
    action = actions.get_item(Key={"order_id": "ord_a", "action_type": "MANUAL_REVIEW_QUEUE"})["Item"]
    assert action["status"] == "PENDING"


@mock_aws
def test_decline_maps_to_blocklist_log_completed():
    ddb = _create_infra()
    item = _decision_item("ord_a", "cust_1", "DECLINE")
    downstream_handler.lambda_handler(_stream_event([item]), None)

    actions = ddb.Table(DOWNSTREAM_ACTIONS_TABLE)
    action = actions.get_item(Key={"order_id": "ord_a", "action_type": "BLOCKLIST_LOG"})["Item"]
    assert action["status"] == "COMPLETED"


@mock_aws
def test_downstream_action_records_profile_count_after_proving_read_before_write():
    ddb = _create_infra()
    ddb.Table(CUSTOMER_PROFILES_TABLE).put_item(Item={
        "customer_id": "cust_1",
        "first_seen_at": "2025-01-01T00:00:00.000000Z",
        "order_count": 4,
        "approved_count": 4,
        "manual_review_count": 0,
        "declined_count": 0,
        "cumulative_gmv": Decimal("800"),
        "avg_basket_value": Decimal("200"),
        "stddev_basket_value": Decimal("0"),
        "last_order_at": "2025-06-01T00:00:00.000000Z",
        "last_decision": "APPROVE",
    })
    item = _decision_item("ord_a", "cust_1", "APPROVE")
    downstream_handler.lambda_handler(_stream_event([item]), None)

    actions = ddb.Table(DOWNSTREAM_ACTIONS_TABLE)
    action = actions.get_item(Key={"order_id": "ord_a", "action_type": "FULFILLMENT_RELEASE"})["Item"]
    # 4 prior orders + this one = 5, only correct if the profile update ran first.
    assert action["details"]["customer_order_count_after"] == 5


@mock_aws
def test_redelivered_stream_record_does_not_double_write_action():
    ddb = _create_infra()
    item = _decision_item("ord_a", "cust_1", "APPROVE")
    downstream_handler.lambda_handler(_stream_event([item]), None)
    result = downstream_handler.lambda_handler(_stream_event([item]), None)

    assert result["batchItemFailures"] == []  # redelivery is a handled no-op, not an error
    profile = ddb.Table(CUSTOMER_PROFILES_TABLE).get_item(Key={"customer_id": "cust_1"})["Item"]
    # Profile aggregation is not idempotent by design (each stream record
    # represents one real order); only the *action* write is deduplicated.
    assert profile["order_count"] == 2


@mock_aws
def test_non_insert_events_are_skipped():
    _create_infra()
    item = _decision_item("ord_a", "cust_1", "APPROVE")
    result = downstream_handler.lambda_handler(
        _stream_event([item], event_names=["MODIFY"]), None
    )
    assert result["batchItemFailures"] == []


@mock_aws
def test_poison_record_reported_without_blocking_other_records_in_batch():
    ddb = _create_infra()
    good_item = _decision_item("ord_good", "cust_1", "APPROVE")
    bad_event = {
        "eventID": "evt-bad",
        "eventName": "INSERT",
        "dynamodb": {"SequenceNumber": "999", "NewImage": {"order_id": {"S": "ord_bad"}}},
    }
    event = _stream_event([good_item])
    event["Records"].append(bad_event)

    result = downstream_handler.lambda_handler(event, None)
    assert result["batchItemFailures"] == [{"itemIdentifier": "999"}]

    actions = ddb.Table(DOWNSTREAM_ACTIONS_TABLE)
    assert "Item" in actions.get_item(Key={"order_id": "ord_good", "action_type": "FULFILLMENT_RELEASE"})
