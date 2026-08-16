#!/usr/bin/env bash
# Tears down the maxab-decisioning stack and its supporting bootstrap
# resources. This exact sequence was validated by hand during development --
# see README.md "Teardown" section for why each step exists (in particular,
# the delete-marker cleanup: RawOrdersBucket has VersioningConfiguration
# Status=Suspended, which -- contrary to what "suspended" suggests -- still
# makes S3 create delete markers on object delete. CloudFormation refuses to
# delete a bucket with any delete markers left in it, so `aws s3 rm
# --recursive` alone is not enough).
set -euo pipefail

REGION="${REGION:-eu-central-1}"
PROFILE="${PROFILE:-maxab-deploy}"
STACK_NAME="${STACK_NAME:-maxab-decisioning}"
ACCOUNT_ID="${ACCOUNT_ID:-811546800963}"
RAW_ORDERS_BUCKET="maxab-decisioning-raw-orders-${ACCOUNT_ID}-${REGION}"
ARTIFACTS_BUCKET="maxab-decisioning-sam-artifacts-${ACCOUNT_ID}-${REGION}"
AWS="aws"

echo "=== 1. Emptying ${RAW_ORDERS_BUCKET} (current objects) ==="
"$AWS" s3 rm "s3://${RAW_ORDERS_BUCKET}" --recursive --profile "$PROFILE" --region "$REGION" || true

echo "=== 2. Cleaning up delete markers left by Suspended versioning ==="
"$AWS" s3api list-object-versions --bucket "$RAW_ORDERS_BUCKET" \
  --profile "$PROFILE" --region "$REGION" \
  --query 'DeleteMarkers[].{Key:Key,VersionId:VersionId}' --output json \
  | python3 -c "
import json, sys, subprocess
markers = json.load(sys.stdin) or []
for m in markers:
    subprocess.run([
        'aws', 's3api', 'delete-object', '--bucket', '$RAW_ORDERS_BUCKET',
        '--key', m['Key'], '--version-id', m['VersionId'],
        '--profile', '$PROFILE', '--region', '$REGION',
    ], check=True)
    print(f\"deleted marker: {m['Key']} ({m['VersionId']})\")
"

echo "=== 3. Deleting the CloudFormation stack (all 11 app resources) ==="
"$AWS" cloudformation delete-stack --stack-name "$STACK_NAME" --profile "$PROFILE" --region "$REGION"
"$AWS" cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --profile "$PROFILE" --region "$REGION"
echo "Stack deleted."

echo "=== 4. Emptying and deleting the SAM artifacts bucket ==="
"$AWS" s3 rm "s3://${ARTIFACTS_BUCKET}" --recursive --profile "$PROFILE" --region "$REGION" || true
"$AWS" s3api delete-bucket --bucket "$ARTIFACTS_BUCKET" --region "$REGION" --profile "$PROFILE"
echo "Artifacts bucket deleted."

echo "=== 5. Verifying no app resources remain (requires broader list permissions -- use an admin/root profile, not $PROFILE) ==="
echo "Run manually with an admin profile if step 5 permissions aren't available to $PROFILE:"
echo "  aws s3api list-buckets --query \"Buckets[?starts_with(Name,'maxab-decisioning')].Name\""
echo "  aws dynamodb list-tables --region $REGION --query \"TableNames[?starts_with(@,'maxab-decisioning')]\""
echo "  aws lambda list-functions --region $REGION --query \"Functions[?starts_with(FunctionName,'maxab-decisioning')].FunctionName\""

echo "=== 6. IAM cleanup (requires an admin/root profile -- $PROFILE's own policy explicitly denies these actions on itself) ==="
cat <<'EOF'
  aws iam detach-user-policy --user-name maxab-decisioning-deploy \
    --policy-arn arn:aws:iam::ACCOUNT_ID:policy/maxab-decisioning-deploy-policy
  aws iam list-access-keys --user-name maxab-decisioning-deploy   # get the AccessKeyId
  aws iam delete-access-key --user-name maxab-decisioning-deploy --access-key-id <id>
  aws iam delete-user --user-name maxab-decisioning-deploy
  aws iam list-policy-versions --policy-arn arn:aws:iam::ACCOUNT_ID:policy/maxab-decisioning-deploy-policy
  # delete every non-default version, then:
  aws iam delete-policy --policy-arn arn:aws:iam::ACCOUNT_ID:policy/maxab-decisioning-deploy-policy
EOF

echo "=== Teardown of app resources complete. Steps 5-6 need an admin/root profile (documented above, not executed by this script). ==="
