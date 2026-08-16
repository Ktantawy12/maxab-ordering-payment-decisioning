"""Lambda #1: decision engine.

Triggered by EventBridge when a new NDJSON batch lands in RawOrdersBucket
under batches/ (plan section 6). Reads CustomerProfilesTable read-only (v3
correction -- this function never creates/updates a profile), scores each
record via decision_engine.scoring.decide, and writes one DecisionsTable
item per record through an individual, conditional PutItem -- not
BatchWriteItem, which cannot carry a per-item ConditionExpression. Writes
are issued concurrently via a small thread pool to keep a 2,500-record
batch well within the function's timeout.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from common.models import CustomerProfile, to_decimal, utcnow_iso
from decision_engine.scoring import decide

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DECISIONS_TABLE = os.environ["DECISIONS_TABLE"]
CUSTOMER_PROFILES_TABLE = os.environ["CUSTOMER_PROFILES_TABLE"]

MAX_WRITE_WORKERS = 16

_dynamodb = boto3.resource("dynamodb")
_s3 = boto3.client("s3")


def _get_profile(customer_id: str, cache: dict, table) -> Optional[CustomerProfile]:
    """Read-only lookup with a per-invocation cache -- safe because nothing
    else writes this table during the invocation (v3: sole writer is
    DownstreamProcessorFunction, in a different Lambda entirely)."""
    if customer_id in cache:
        return cache[customer_id]
    resp = table.get_item(Key={"customer_id": customer_id})
    item = resp.get("Item")
    profile = CustomerProfile.from_item(item) if item else None
    cache[customer_id] = profile
    return profile


def _signals_to_item(signals: dict) -> dict:
    out = {}
    for name, payload in signals.items():
        converted = {
            "score": to_decimal(round(payload["score"], 6)),
            "weight": to_decimal(payload["weight"]),
        }
        if name == "basket_value":
            converted["raw_value"] = to_decimal(payload["raw_value"])
        elif name == "tenure":
            raw = payload["raw_value"]
            converted["raw_value"] = {
                "account_age_days": to_decimal(raw["account_age_days"]),
                "approval_rate": to_decimal(round(raw["approval_rate"], 6)),
            }
        elif name == "fraud":
            converted["contributing_flags"] = payload["contributing_flags"]
        elif name == "payment_risk":
            converted["payment_method"] = payload["payment_method"]
        out[name] = converted
    return out


def _build_decision_item(order: dict, result: dict, lambda_request_id: str, source_s3_key: str) -> dict:
    return {
        "order_id": order["order_id"],
        "customer_id": order["customer_id"],
        "decision": result["decision"],
        "overall_score": to_decimal(round(result["overall_score"], 6)),
        "ruleset_version": result["ruleset_version"],
        "signals": _signals_to_item(result["signals"]),
        "override_applied": result["override_applied"],
        "source_updated_at": order["source_updated_at"],
        "decided_at": utcnow_iso(),
        "lambda_request_id": lambda_request_id,
        "source_s3_key": source_s3_key,
    }


def _write_decision(table, item: dict) -> str:
    """Individual conditional PutItem (plan section 6):
    attribute_not_exists(order_id) -> create; source_updated_at strictly
    increasing -> legitimate update overwrite; anything else (exact replay
    or out-of-order redelivery) -> ConditionalCheckFailedException, treated
    as a safe no-op skip rather than an error."""
    try:
        table.put_item(
            Item=item,
            ConditionExpression=(
                "attribute_not_exists(order_id) OR source_updated_at < :incoming"
            ),
            ExpressionAttributeValues={":incoming": item["source_updated_at"]},
        )
        return "written"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "skipped_duplicate_or_stale"
        raise


def _process_batch(bucket: str, key: str, lambda_request_id: str) -> dict:
    decisions_table = _dynamodb.Table(DECISIONS_TABLE)
    profiles_table = _dynamodb.Table(CUSTOMER_PROFILES_TABLE)

    obj = _s3.get_object(Bucket=bucket, Key=key)

    profile_cache: dict = {}
    items = []
    for raw_line in obj["Body"].iter_lines():
        if not raw_line:
            continue
        order = json.loads(raw_line)
        profile = _get_profile(order["customer_id"], profile_cache, profiles_table)
        result = decide(order, profile)
        items.append(_build_decision_item(order, result, lambda_request_id, key))

    counts = {"written": 0, "skipped_duplicate_or_stale": 0, "errors": 0}
    failed_order_ids = []
    with ThreadPoolExecutor(max_workers=MAX_WRITE_WORKERS) as pool:
        futures = {pool.submit(_write_decision, decisions_table, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                outcome = future.result()
                counts[outcome] += 1
            except Exception:
                counts["errors"] += 1
                failed_order_ids.append(item["order_id"])
                logger.exception("Failed to write decision for order_id=%s", item["order_id"])

    logger.info(json.dumps({
        "event": "batch_processed",
        "bucket": bucket,
        "key": key,
        "record_count": len(items),
        **counts,
    }))

    if counts["errors"]:
        # Surface a failure so the async invocation's DLQ destination
        # captures this batch for investigation, instead of silently
        # dropping the records that failed to write.
        raise RuntimeError(
            f"{counts['errors']} decision(s) failed to write for {key}: {failed_order_ids}"
        )

    return {"processed": len(items), **counts}


def lambda_handler(event, context):
    bucket = event["detail"]["bucket"]["name"]
    key = event["detail"]["object"]["key"]
    return _process_batch(bucket, key, context.aws_request_id)
