"""Moto-mocked integration tests for the decision engine Lambda (plan
section 11). No live AWS calls, no cost."""
import json
import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ["DECISIONS_TABLE"] = "TestDecisions"
os.environ["CUSTOMER_PROFILES_TABLE"] = "TestCustomerProfiles"

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from decision_engine import handler as decision_engine_handler  # noqa: E402

BUCKET = "test-raw-orders-bucket"
DECISIONS_TABLE = os.environ["DECISIONS_TABLE"]
CUSTOMER_PROFILES_TABLE = os.environ["CUSTOMER_PROFILES_TABLE"]


class FakeContext:
    aws_request_id = "test-request-id"


def _eventbridge_event(bucket: str, key: str) -> dict:
    return {"detail": {"bucket": {"name": bucket}, "object": {"key": key}}}


def _create_infra():
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket=BUCKET)

    ddb = boto3.resource("dynamodb")
    ddb.create_table(
        TableName=DECISIONS_TABLE,
        AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName=CUSTOMER_PROFILES_TABLE,
        AttributeDefinitions=[{"AttributeName": "customer_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "customer_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return s3, ddb


def _order(order_id, customer_id="cust_1", basket_value=100.0, payment_method="card",
           source_updated_at="2026-01-10T12:00:00.000000Z", **overrides):
    order = {
        "order_id": order_id,
        "customer_id": customer_id,
        "source_updated_at": source_updated_at,
        "created_at": source_updated_at,
        "basket_value": basket_value,
        "currency": "EGP",
        "item_count": 2,
        "payment_method": payment_method,
        "delivery_governorate": "Cairo",
        "is_first_order": True,
        "device_id": "dev_1",
        "device_shared_count": 1,
        "ip_country": "EG",
        "order_hour": 12,
    }
    order.update(overrides)
    return order


def _put_batch(s3, key: str, orders: list) -> None:
    body = "\n".join(json.dumps(o) for o in orders)
    s3.put_object(Bucket=BUCKET, Key=key, Body=body.encode("utf-8"))


def _invoke(key: str):
    return decision_engine_handler.lambda_handler(_eventbridge_event(BUCKET, key), FakeContext())


@mock_aws
def test_writes_decisions_with_full_lineage():
    s3, ddb = _create_infra()
    orders = [_order("ord_a"), _order("ord_b", basket_value=5000.0, payment_method="cod")]
    _put_batch(s3, "batches/batch-000.ndjson", orders)

    result = _invoke("batches/batch-000.ndjson")
    assert result["processed"] == 2
    assert result["written"] == 2

    table = ddb.Table(DECISIONS_TABLE)
    item_a = table.get_item(Key={"order_id": "ord_a"})["Item"]
    assert item_a["decision"] in {"APPROVE", "MANUAL_REVIEW", "DECLINE"}
    assert set(item_a["signals"].keys()) == {"basket_value", "tenure", "fraud", "payment_risk"}
    assert item_a["ruleset_version"] == "v1"
    assert item_a["source_s3_key"] == "batches/batch-000.ndjson"
    assert item_a["lambda_request_id"] == "test-request-id"


@mock_aws
def test_exact_replay_is_a_noop():
    s3, ddb = _create_infra()
    _put_batch(s3, "batches/batch-000.ndjson", [_order("ord_a", basket_value=100.0)])
    _invoke("batches/batch-000.ndjson")

    table = ddb.Table(DECISIONS_TABLE)
    first_decided_at = table.get_item(Key={"order_id": "ord_a"})["Item"]["decided_at"]

    # Re-deliver the byte-identical batch (same source_updated_at).
    result = _invoke("batches/batch-000.ndjson")
    assert result["skipped_duplicate_or_stale"] == 1
    assert result["written"] == 0

    item = table.get_item(Key={"order_id": "ord_a"})["Item"]
    assert item["decided_at"] == first_decided_at  # untouched


@mock_aws
def test_newer_source_updated_at_overwrites_as_update():
    s3, ddb = _create_infra()
    _put_batch(s3, "batches/batch-000.ndjson", [
        _order("ord_a", basket_value=100.0, source_updated_at="2026-01-10T12:00:00.000000Z")
    ])
    _invoke("batches/batch-000.ndjson")

    _put_batch(s3, "batches/batch-005.ndjson", [
        _order("ord_a", basket_value=9000.0, payment_method="cod",
               source_updated_at="2026-01-10T13:00:00.000000Z")
    ])
    result = _invoke("batches/batch-005.ndjson")
    assert result["written"] == 1

    table = ddb.Table(DECISIONS_TABLE)
    item = table.get_item(Key={"order_id": "ord_a"})["Item"]
    assert item["source_updated_at"] == "2026-01-10T13:00:00.000000Z"
    assert float(item["signals"]["basket_value"]["raw_value"]) == 9000.0


@mock_aws
def test_out_of_order_delivery_is_rejected():
    s3, ddb = _create_infra()
    _put_batch(s3, "batches/batch-005.ndjson", [
        _order("ord_a", basket_value=9000.0, source_updated_at="2026-01-10T13:00:00.000000Z")
    ])
    _invoke("batches/batch-005.ndjson")

    # An older-dated redelivery arrives after the newer one is already stored.
    _put_batch(s3, "batches/batch-000.ndjson", [
        _order("ord_a", basket_value=100.0, source_updated_at="2026-01-10T12:00:00.000000Z")
    ])
    result = _invoke("batches/batch-000.ndjson")
    assert result["skipped_duplicate_or_stale"] == 1

    table = ddb.Table(DECISIONS_TABLE)
    item = table.get_item(Key={"order_id": "ord_a"})["Item"]
    assert item["source_updated_at"] == "2026-01-10T13:00:00.000000Z"  # unchanged


@mock_aws
def test_decision_engine_never_writes_customer_profiles():
    s3, ddb = _create_infra()
    _put_batch(s3, "batches/batch-000.ndjson", [_order("ord_a")])
    _invoke("batches/batch-000.ndjson")

    profiles_table = ddb.Table(CUSTOMER_PROFILES_TABLE)
    assert profiles_table.scan()["Items"] == []


@mock_aws
def test_reads_existing_profile_for_scoring():
    s3, ddb = _create_infra()
    profiles_table = ddb.Table(CUSTOMER_PROFILES_TABLE)
    profiles_table.put_item(Item={
        "customer_id": "cust_1",
        "first_seen_at": "2020-01-01T00:00:00.000000Z",
        "order_count": 50,
        "approved_count": 50,
        "manual_review_count": 0,
        "declined_count": 0,
        "cumulative_gmv": 10000,
        "avg_basket_value": 200,
        "stddev_basket_value": 20,
        "last_order_at": "2026-01-01T00:00:00.000000Z",
        "last_decision": "APPROVE",
    })
    _put_batch(s3, "batches/batch-000.ndjson", [
        _order("ord_a", customer_id="cust_1", basket_value=200.0, payment_method="card")
    ])
    _invoke("batches/batch-000.ndjson")

    table = ddb.Table(DECISIONS_TABLE)
    item = table.get_item(Key={"order_id": "ord_a"})["Item"]
    # An established, 100%-approval customer at their own average basket
    # value should score low risk on both signals -> APPROVE.
    assert item["decision"] == "APPROVE"
    assert float(item["signals"]["tenure"]["raw_value"]["approval_rate"]) == 1.0
