#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: ./deploy.sh [--profile <aws-profile>] [--region <aws-region>] [--remote-account-id <account-id>]"
  echo ""
  echo "Options:"
  echo "  --profile            AWS CLI profile name (optional; uses default credentials if omitted)"
  echo "  --region             AWS region to deploy into (optional; uses profile/env default if omitted)"
  echo "  --remote-account-id  AWS account ID allowed to assume the AvatarIntegration-CFMTipsMCP role"
  echo "                       (optional; skips cross-account role creation if omitted)"
  exit 1
}

PROFILE=""
REGION=""
REMOTE_ACCOUNT_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="${2:?--profile requires a value}"
      shift 2
      ;;
    --region)
      REGION="${2:?--region requires a value}"
      shift 2
      ;;
    --remote-account-id)
      REMOTE_ACCOUNT_ID="${2:?--remote-account-id requires a value}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown option: $1"
      usage
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$SCRIPT_DIR/infra"

# Build CDK CLI flags
CDK_FLAGS=()
if [ -n "$PROFILE" ]; then
  CDK_FLAGS+=(--profile "$PROFILE")
  echo "==> Using AWS profile: $PROFILE"
else
  echo "==> Using default AWS credentials"
fi

if [ -n "$REGION" ]; then
  export CDK_DEFAULT_REGION="$REGION"
  export AWS_DEFAULT_REGION="$REGION"
  echo "==> Target region: $REGION"
fi

CONTEXT_FLAGS=()
if [ -n "$REMOTE_ACCOUNT_ID" ]; then
  CONTEXT_FLAGS+=(--context "remote_account_id=$REMOTE_ACCOUNT_ID")
  echo "==> Remote account ID: $REMOTE_ACCOUNT_ID (will create AvatarIntegration-CFMTipsMCP role)"
fi

# 1. Create/activate Python virtualenv for CDK dependencies
if [ ! -d "$INFRA_DIR/.venv" ]; then
  echo "==> Creating virtualenv at infra/.venv"
  python3 -m venv "$INFRA_DIR/.venv"
fi
source "$INFRA_DIR/.venv/bin/activate"

# 2. Install CDK Python dependencies
echo "==> Installing CDK dependencies"
pip install -q -r "$INFRA_DIR/requirements.txt"

# 3. Bootstrap CDK (idempotent — safe to run repeatedly)
echo "==> Bootstrapping CDK"
rm -rf "$INFRA_DIR/cdk.out"
npx cdk bootstrap "${CDK_FLAGS[@]}" "${CONTEXT_FLAGS[@]}" \
  --app "python3 $INFRA_DIR/app.py" \
  --context "@aws-cdk/core:bootstrapQualifier=hnb659fds" \
  --output "$INFRA_DIR/cdk.out" \
  --path-metadata false \
  --version-reporting false 2>&1 || {
  echo "ERROR: CDK bootstrap failed. Check your AWS credentials and permissions."
  exit 1
}

# 4. Deploy the stack
echo "==> Deploying CfmTipsMcpStack"
npx cdk deploy "${CDK_FLAGS[@]}" "${CONTEXT_FLAGS[@]}" \
  --app "python3 $INFRA_DIR/app.py" \
  --require-approval never \
  --outputs-file "$SCRIPT_DIR/cdk-outputs.json" \
  2>&1

# 5. Print outputs
echo ""
echo "==> Deployment complete!"
echo ""

if [ -f "$SCRIPT_DIR/cdk-outputs.json" ]; then
  API_ENDPOINT=$(python3 -c "import json; d=json.load(open('$SCRIPT_DIR/cdk-outputs.json')); print(d.get('CfmTipsMcpStack',{}).get('ApiEndpoint','(not found)'))")
  LAMBDA_ARN=$(python3 -c "import json; d=json.load(open('$SCRIPT_DIR/cdk-outputs.json')); print(d.get('CfmTipsMcpStack',{}).get('LambdaFunctionArn','(not found)'))")

  echo "API Gateway Endpoint: $API_ENDPOINT"
  echo "Lambda Function ARN:  $LAMBDA_ARN"

  # Print cross-account outputs if present
  AVATAR_ROLE_ARN=$(python3 -c "import json; d=json.load(open('$SCRIPT_DIR/cdk-outputs.json')); print(d.get('CfmTipsMcpStack',{}).get('AvatarIntegrationRoleArn',''))")
  AVATAR_EXTERNAL_ID=$(python3 -c "import json; d=json.load(open('$SCRIPT_DIR/cdk-outputs.json')); print(d.get('CfmTipsMcpStack',{}).get('AvatarIntegrationExternalId',''))")

  if [ -n "$AVATAR_ROLE_ARN" ]; then
    echo ""
    echo "Cross-Account Integration:"
    echo "  Role ARN:    $AVATAR_ROLE_ARN"
    echo "  External ID: $AVATAR_EXTERNAL_ID"
    echo ""
    echo "  The remote account should use these values to configure assume-role."
  fi
else
  echo "WARNING: cdk-outputs.json not found. Check deployment logs above."
  exit 1
fi
