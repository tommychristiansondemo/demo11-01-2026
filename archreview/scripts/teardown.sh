#!/usr/bin/env bash
# Remove EVERYTHING this project created (the pre-existing awsteach.com hosted zone is kept).
#   ./scripts/teardown.sh                 # stacks + service-created log groups + CDK bootstrap (qualifier archrev)
#   KEEP_BOOTSTRAP=1 ./scripts/teardown.sh  # keep the ArchRevCDKToolkit stacks for faster redeploys
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/infra"
REGION="${REGION:-us-west-2}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT"

API_ID="$(aws cloudformation describe-stacks --region "$REGION" --stack-name ArchRevApp \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text 2>/dev/null | sed -E 's#https://([^.]+)\..*#\1#')"

echo "==> Destroying CDK stacks (ArchRevApp, ArchRevEdge)"
[ -d node_modules ] || npm ci --silent
npx cdk destroy --all --force -c region="$REGION"

echo "==> Deleting service-created log groups"
for r in "$REGION" us-east-1; do
  for prefix in /aws/bedrock-agentcore/runtimes/archrev_ /aws/lambda/ArchRevApp- /aws/lambda/ArchRevEdge-; do
    for lg in $(aws logs describe-log-groups --region "$r" --log-group-name-prefix "$prefix" --query 'logGroups[].logGroupName' --output text); do
      echo "    delete $r $lg"; aws logs delete-log-group --region "$r" --log-group-name "$lg"
    done
  done
done
if [ -n "${API_ID:-}" ] && [ "$API_ID" != "None" ]; then
  aws logs delete-log-group --region "$REGION" --log-group-name "API-Gateway-Execution-Logs_${API_ID}/v1" 2>/dev/null \
    && echo "    delete API-Gateway-Execution-Logs_${API_ID}/v1"
fi

if [ "${KEEP_BOOTSTRAP:-0}" != "1" ]; then
  echo "==> Removing CDK bootstrap (qualifier archrev) in $REGION and us-east-1"
  for r in "$REGION" us-east-1; do
    b="cdk-archrev-assets-$ACCOUNT-$r"
    if aws s3api head-bucket --bucket "$b" 2>/dev/null; then
      "$ROOT/.venv/bin/python" - "$b" "$r" <<'EOF'
import sys, boto3
b = boto3.resource("s3", region_name=sys.argv[2]).Bucket(sys.argv[1])
b.object_versions.delete(); b.objects.delete()
EOF
    fi
    aws ecr delete-repository --region "$r" --repository-name "cdk-archrev-container-assets-$ACCOUNT-$r" --force >/dev/null 2>&1
    aws cloudformation delete-stack --region "$r" --stack-name ArchRevCDKToolkit
    aws cloudformation wait stack-delete-complete --region "$r" --stack-name ArchRevCDKToolkit && echo "    $r toolkit removed"
  done
fi

echo "==> Verifying nothing tagged Application=EnterpriseSolutionReviewAssistant remains"
for r in "$REGION" us-east-1; do
  aws resourcegroupstaggingapi get-resources --region "$r" --tag-filters Key=Application,Values=EnterpriseSolutionReviewAssistant \
    --query 'ResourceTagMappingList[].ResourceARN' --output text
done
echo "==> Teardown complete (hosted zone awsteach.com retained)."
