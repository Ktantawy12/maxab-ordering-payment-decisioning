#!/usr/bin/env python
"""Prepares one fresh demo order for the live Loom trigger demonstration.

Not part of the pipeline or the 100k dataset -- generates a single,
clearly-labeled record (customer_id prefixed demo_) with a fresh order_id
and current timestamp each run, so re-running (rehearsal, then the actual
take) never collides with a prior run's write. Writes the record locally
and prints the exact commands to run live; does not upload anything itself
unless --upload is passed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

BUCKET = "maxab-decisioning-raw-orders-811546800963-eu-central-1"
DECISIONS_TABLE = "maxab-decisioning-Decisions"
DOWNSTREAM_ACTIONS_TABLE = "maxab-decisioning-DownstreamActions"
PROFILE = "maxab-deploy"
REGION = "eu-central-1"
AWS_CLI = "aws"  # assumes aws is on PATH in the demo shell; adjust if not


def build_demo_order() -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    order_id = f"demo-{uuid.uuid4().hex[:10]}"
    return {
        "order_id": order_id,
        "customer_id": "demo_customer_001",
        "source_updated_at": now,
        "created_at": now,
        "basket_value": 175.50,
        "currency": "EGP",
        "item_count": 4,
        "payment_method": "card",
        "delivery_governorate": "Cairo",
        "is_first_order": True,
        "device_id": "demo_device_001",
        "device_shared_count": 1,
        "ip_country": "EG",
        "order_hour": datetime.now(timezone.utc).hour,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload", action="store_true", help="Upload immediately instead of just preparing.")
    parser.add_argument("--output-dir", type=Path, default=Path("data/demo"))
    args = parser.parse_args()

    order = build_demo_order()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    local_path = args.output_dir / f"{order['order_id']}.ndjson"
    local_path.write_text(json.dumps(order) + "\n", encoding="utf-8")

    s3_key = f"batches/{order['order_id']}.ndjson"

    print(f"Demo order prepared: {order['order_id']}")
    print(f"Local file: {local_path}")
    print(json.dumps(order, indent=2))

    upload_cmd = [
        AWS_CLI, "s3", "cp", str(local_path), f"s3://{BUCKET}/{s3_key}",
        "--profile", PROFILE, "--region", REGION,
    ]
    print("\n--- Command to run live to trigger the pipeline ---")
    print(" ".join(upload_cmd))

    print("\n--- Commands to show the result afterward (wait ~10-20s) ---")
    print(
        f'{AWS_CLI} dynamodb get-item --table-name {DECISIONS_TABLE} '
        f'--key \'{{"order_id":{{"S":"{order["order_id"]}"}}}}\' '
        f'--profile {PROFILE} --region {REGION}'
    )
    print(
        f'{AWS_CLI} dynamodb get-item --table-name {DOWNSTREAM_ACTIONS_TABLE} '
        f'--key \'{{"order_id":{{"S":"{order["order_id"]}"}},"action_type":{{"S":"FULFILLMENT_RELEASE"}}}}\' '
        f'--profile {PROFILE} --region {REGION}'
    )

    print("\n--- Optional: tail both Lambda log groups live in separate terminals ---")
    print(
        f'{AWS_CLI} logs tail /aws/lambda/maxab-decisioning-decision-engine '
        f'--follow --profile {PROFILE} --region {REGION}'
    )
    print(
        f'{AWS_CLI} logs tail /aws/lambda/maxab-decisioning-downstream-processor '
        f'--follow --profile {PROFILE} --region {REGION}'
    )

    if args.upload:
        print("\n--upload passed: uploading now...")
        subprocess.run(upload_cmd, check=True)
        print("Uploaded.")


if __name__ == "__main__":
    main()
