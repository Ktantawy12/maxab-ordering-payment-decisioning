"""Lambda #2: downstream processor.

Triggered by DecisionsTable's DynamoDB Stream, INSERT-only (event source
mapping FilterCriteria in the SAM template). Sole owner of
CustomerProfilesTable (v3 correction): reads the customer's current
profile, safely initializes one if it doesn't exist yet, writes the
updated aggregate back, and writes a DownstreamActionsTable action derived
from the decision. See plan sections 6 and 8.

Uses `source_updated_at` off the decision item as the order's timestamp for
profile aggregation -- DecisionsTable's schema (plan section 5) doesn't
carry a separate `created_at`, and the generator keeps source_updated_at
equal to the order's created_at by construction, so this is the intended
field, not a substitution.
"""
from __future__ import annotations

import json
import logging
import os

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from common.models import (
    CustomerProfile,
    DECISION_TO_ACTION,
    to_decimal,
    update_running_stats,
    utcnow_iso,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CUSTOMER_PROFILES_TABLE = os.environ["CUSTOMER_PROFILES_TABLE"]
DOWNSTREAM_ACTIONS_TABLE = os.environ["DOWNSTREAM_ACTIONS_TABLE"]

_dynamodb = boto3.resource("dynamodb")
_deserializer = TypeDeserializer()

DECISION_COUNTER_FIELD = {
    "APPROVE": "approved_count",
    "MANUAL_REVIEW": "manual_review_count",
    "DECLINE": "declined_count",
}


def _deserialize_image(image: dict) -> dict:
    return {k: _deserializer.deserialize(v) for k, v in image.items()}


def _get_or_init_profile(customer_id: str, order_timestamp: str, table) -> CustomerProfile:
    """READ step (plan section 8). Returns the existing profile, or a fresh
    in-memory default if this is the customer's first order to finish
    downstream processing -- there is no separate "create" path elsewhere."""
    resp = table.get_item(Key={"customer_id": customer_id})
    item = resp.get("Item")
    if item:
        return CustomerProfile.from_item(item)
    return CustomerProfile.new(customer_id, first_seen_at=order_timestamp)


def _apply_order_to_profile(
    profile: CustomerProfile, decision: str, basket_value: float, order_timestamp: str
) -> CustomerProfile:
    new_avg, new_stddev = update_running_stats(
        profile.avg_basket_value, profile.stddev_basket_value, profile.order_count, basket_value
    )
    profile.avg_basket_value = new_avg
    profile.stddev_basket_value = new_stddev
    profile.cumulative_gmv += basket_value
    profile.order_count += 1
    counter_field = DECISION_COUNTER_FIELD[decision]
    setattr(profile, counter_field, getattr(profile, counter_field) + 1)
    profile.last_order_at = order_timestamp
    profile.last_decision = decision
    return profile


def _write_profile(table, profile: CustomerProfile) -> None:
    # Unconditional: this function is the table's only writer (v3
    # correction), so no other Lambda can race this put.
    table.put_item(Item=profile.to_item())


def _write_downstream_action(table, decision_item: dict, profile_after: CustomerProfile) -> str:
    action_type, status = DECISION_TO_ACTION[decision_item["decision"]]
    reason_codes = decision_item.get("signals", {}).get("fraud", {}).get("contributing_flags", [])
    item = {
        "order_id": decision_item["order_id"],
        "action_type": action_type,
        "status": status,
        "customer_id": decision_item["customer_id"],
        "decision": decision_item["decision"],
        "source_decision_request_id": decision_item.get("lambda_request_id", ""),
        "created_at": utcnow_iso(),
        "details": {
            "reason_codes": reason_codes,
            # Only correct if the profile read+update above already ran --
            # the demonstrable proof of the read-before-write requirement.
            "customer_order_count_after": to_decimal(profile_after.order_count),
        },
    }
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(order_id)")
        return "written"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Guards against stream-record redelivery of the same INSERT.
            return "skipped_duplicate"
        raise


def _process_record(record: dict, profiles_table, actions_table) -> None:
    decision_item = _deserialize_image(record["dynamodb"]["NewImage"])

    order_timestamp = decision_item["source_updated_at"]
    customer_id = decision_item["customer_id"]
    basket_value = float(decision_item["signals"]["basket_value"]["raw_value"])
    decision = decision_item["decision"]

    profile = _get_or_init_profile(customer_id, order_timestamp, profiles_table)
    profile = _apply_order_to_profile(profile, decision, basket_value, order_timestamp)
    _write_profile(profiles_table, profile)

    outcome = _write_downstream_action(actions_table, decision_item, profile)
    logger.info(json.dumps({
        "event": "downstream_action_processed",
        "order_id": decision_item["order_id"],
        "decision": decision,
        "outcome": outcome,
    }))


def lambda_handler(event, context):
    profiles_table = _dynamodb.Table(CUSTOMER_PROFILES_TABLE)
    actions_table = _dynamodb.Table(DOWNSTREAM_ACTIONS_TABLE)

    batch_item_failures = []
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue  # defensive; the event source mapping already filters to INSERT
        try:
            _process_record(record, profiles_table, actions_table)
        except Exception:
            logger.exception("Failed to process stream record eventID=%s", record.get("eventID"))
            batch_item_failures.append({"itemIdentifier": record["dynamodb"]["SequenceNumber"]})

    return {"batchItemFailures": batch_item_failures}
