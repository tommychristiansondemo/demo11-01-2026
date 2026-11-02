#!/usr/bin/env bash
# Idempotent build + deploy of the Enterprise Solution Review Assistant.
#   ./scripts/deploy.sh                 # deploy everything
#   ALARM_EMAIL=me@example.com ./scripts/deploy.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
REGION="${REGION:-us-west-2}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT"

[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip -q install uv; }
.venv/bin/uv pip install --quiet --python .venv/bin/python -r tests/requirements-dev.lock

echo "==> Unit + contract tests"
.venv/bin/python -m pytest -q

echo "==> Discovering invocable Bedrock models in $REGION"
MODEL_IDS="$(.venv/bin/python scripts/select_model.py "$REGION")"
echo "    using: $MODEL_IDS"

echo "==> Building bundles"
./scripts/build.sh
mkdir -p docs/download && cp projectknowledge.md docs/download/projectknowledge.md

echo "==> Bootstrapping (idempotent; qualifier archrev)"
(cd infra && npm ci --silent && npx cdk bootstrap --qualifier archrev --toolkit-stack-name ArchRevCDKToolkit \
  --tags Application=EnterpriseSolutionReviewAssistant --tags Environment=demo \
  "aws://$ACCOUNT/$REGION" "aws://$ACCOUNT/us-east-1" >/dev/null)

echo "==> Deploying stacks"
(cd infra && npx cdk deploy --all --require-approval never --outputs-file ../build/outputs.json \
  -c region="$REGION" -c modelIds="$MODEL_IDS" -c alarmEmail="${ALARM_EMAIL:-}")

echo "==> Deployed. Outputs in build/outputs.json"
.venv/bin/python -c "import json;o=json.load(open('build/outputs.json'))['ArchRevApp'];print('Site:',o['SiteUrl']);print('API :',o['ApiUrl'])"
