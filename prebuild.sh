#!/bin/bash
# Prebuild Lambda dependencies without Docker.
# DevOps Agent is a new service whose botocore data is not yet in the standard boto3.
# This script bundles boto3 + the custom devops-agent service model into .bundled/ for CDK to pick up.
#
# Run this ONCE before `cdk deploy` (re-run when investigation_notifier.py changes).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$SCRIPT_DIR/lambda/investigation_notifier/.bundled"

echo "==> Cleaning previous bundle..."
rm -rf "$TARGET"
mkdir -p "$TARGET"

echo "==> Installing boto3==1.43.9 (with DevOps Agent stub support)..."
pip install --target "$TARGET" --quiet 'boto3==1.43.9' 'botocore==1.43.9'

echo "==> Injecting DevOps Agent service model..."
cp -r "$SCRIPT_DIR/lambda/investigation_notifier/botocore-ext/devops-agent" "$TARGET/botocore/data/devops-agent"

echo "==> Copying handler + shared utils..."
cp "$SCRIPT_DIR/lambda/investigation_notifier/investigation_notifier.py" "$TARGET/"
cp "$SCRIPT_DIR/lambda/investigation_notifier/dingtalk_utils.py" "$TARGET/"

echo "==> Done. Now run: cdk deploy"
