#!/usr/bin/env bash
# Deploy the A01 demo: CloudFront -> one EC2 instance.  Run from your laptop with admin access to the account.
#   ./deploy/deploy.sh [deploy]      first deploy, or a stack update
#   ./deploy/deploy.sh update        push new code (and a changed password) to the running instance, no stack change
#   ./deploy/deploy.sh status        URL, instance state, health
#   ./deploy/deploy.sh logs          tail the instance's bootstrap log and the app's journal (through SSM)
#   ./deploy/deploy.sh destroy       delete everything this script created
# Optional: RESET_PASSWORD=1 makes a new password; CVA_PASSWORD=... sets your own (12+ characters).
set -euo pipefail

EXPECTED_ACCOUNT="604775478403"
export AWS_REGION="us-east-1" AWS_DEFAULT_REGION="us-east-1" AWS_PAGER=""
STACK="cva-a01-demo-02"; USERNAME="cva-output-agent"; SECRET="cva/a01/auth"; KEY="code/cva-latest.zip"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cmd="${1:-deploy}"

need() { command -v "$1" >/dev/null || { echo "missing tool: $1" >&2; exit 1; }; }
for t in aws python3 zip curl; do need "$t"; done

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)" || { echo "AWS login not working: run 'aws sso login --profile <admin profile>' and export AWS_PROFILE" >&2; exit 1; }
[ "$ACCOUNT" = "$EXPECTED_ACCOUNT" ] || { echo "wrong account: logged in to $ACCOUNT, expected $EXPECTED_ACCOUNT (export AWS_PROFILE=aivar-vision-admin)" >&2; exit 1; }
BUCKET="cva-a01-deploy-${ACCOUNT}-${AWS_REGION}"

stack_out() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
secret_json() { aws secretsmanager get-secret-value --secret-id "$SECRET" --query SecretString --output text 2>/dev/null || true; }

build_and_upload() {
  aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null || {
    echo "creating bucket $BUCKET"
    aws s3api create-bucket --bucket "$BUCKET" >/dev/null
    aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  }
  local z; z="$(mktemp -d)/cva.zip"
  ( cd "$HERE" && zip -qr "$z" . -x "runs/*" "tests/*" ".git/*" ".venv/*" "*/__pycache__/*" "__pycache__/*" ".pytest_cache/*" ".ruff_cache/*" "*.zip" )
  aws s3 cp "$z" "s3://$BUCKET/$KEY" --only-show-errors
  echo "code uploaded: s3://$BUCKET/$KEY"
}

ensure_secret() {                       # sets ORIGIN_SECRET; sets NEW_PASSWORD only when a password was created
  local cur; cur="$(secret_json)"; NEW_PASSWORD=""
  if [ -n "$cur" ] && [ "${RESET_PASSWORD:-}" != "1" ] && [ -z "${CVA_PASSWORD:-}" ]; then
    ORIGIN_SECRET="$(printf '%s' "$cur" | python3 -c 'import sys,json;print(json.load(sys.stdin)["origin_secret"])')"
    echo "sign-in secret exists: kept (RESET_PASSWORD=1 to change the password)"; return
  fi
  NEW_PASSWORD="${CVA_PASSWORD:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(15))')}"
  [ "${#NEW_PASSWORD}" -ge 12 ] || { echo "password must have 12+ characters" >&2; exit 1; }
  local f; f="$(mktemp)"; chmod 600 "$f"
  local old_origin=""; [ -n "$cur" ] && old_origin="$(printf '%s' "$cur" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("origin_secret",""))')"
  printf '%s\n' "$NEW_PASSWORD" | ( cd "$HERE" && PYTHONPATH="$HERE" OLD_ORIGIN="$old_origin" USERNAME="$USERNAME" python3 -c '
import json, os, secrets, sys
from cva.ui.auth import hash_password
pw = sys.stdin.readline().rstrip("\n")
print(json.dumps({"username": os.environ["USERNAME"], "password_hash": hash_password(pw), "session_key": secrets.token_hex(32),
                  "origin_secret": os.environ["OLD_ORIGIN"] or secrets.token_hex(32)}))' ) > "$f"
  ORIGIN_SECRET="$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["origin_secret"])' "$f")"
  if [ -n "$cur" ]; then aws secretsmanager put-secret-value --secret-id "$SECRET" --secret-string "file://$f" >/dev/null
  else aws secretsmanager create-secret --name "$SECRET" --description "A01 demo sign-in (hash only), session key, origin secret" --secret-string "file://$f" >/dev/null; fi
  rm -f "$f"; echo "sign-in secret written"
}

ssm_run() {                             # ssm_run "<shell commands>"  (runs on the instance as root, prints the output)
  local iid cid; iid="$(stack_out InstanceId)"
  local params; params="$(python3 -c 'import json,sys;print(json.dumps({"commands":[sys.argv[1]]}))' "$1")"       # JSON, so commas and quotes in the command are safe
  cid="$(aws ssm send-command --instance-ids "$iid" --document-name AWS-RunShellScript --parameters "$params" --query Command.CommandId --output text)"
  aws ssm wait command-executed --command-id "$cid" --instance-id "$iid" 2>/dev/null || true
  aws ssm get-command-invocation --command-id "$cid" --instance-id "$iid" --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
}

wait_health() {
  local url="$1"; echo "waiting for $url/health (CloudFront and the first boot take 10 to 15 minutes) ..."
  for _ in $(seq 1 90); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url/health" || true)" = "200" ]; then echo "UP: $url"; return 0; fi
    sleep 10
  done
  echo "not healthy after 15 minutes: run './deploy/deploy.sh logs'" >&2; return 1
}

case "$cmd" in
  deploy)
    ensure_secret
    build_and_upload
    PL="$(aws ec2 describe-managed-prefix-lists --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing --query 'PrefixLists[0].PrefixListId' --output text)"
    # VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query "Vpcs[0].VpcId" --output text)
    # SUBNET_ID=$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC_ID" Name=defaultForAz,Values=true --query "Subnets[0].SubnetId" --output text)
    VPC_ID=vpc-01e8c0036ae574090

    SUBNET_ID=subnet-0eb4d135a18d6b6ac
 
    echo "Using default VPC $VPC_ID subnet $SUBNET_ID"
    [ -n "$PL" ] && [ "$PL" != "None" ] || { echo "CloudFront prefix list not found" >&2; exit 1; }
    aws cloudformation deploy --stack-name "$STACK" --template-file "$HERE/deploy/cloudformation.yaml" --capabilities CAPABILITY_IAM \
      --parameter-overrides "CodeBucket=$BUCKET" "SecretName=$SECRET" "OriginSecret=$ORIGIN_SECRET" "CloudFrontPrefixListId=$PL" "VpcId=$VPC_ID" "SubnetId=$SUBNET_ID" ${EXTRA_POLICY_ARN:+"ExtraPolicyArn=$EXTRA_POLICY_ARN"}
    URL="$(stack_out Url)"; wait_health "$URL"
    echo; echo "URL:      $URL"; echo "Username: $USERNAME"
    [ -n "$NEW_PASSWORD" ] && echo "Password: $NEW_PASSWORD   (shown once: store it now)" || echo "Password: unchanged"
    echo "Next: sign in, open the AWS Transform tab and run the connection check."
    ;;
  update)
    ensure_secret; build_and_upload
    ssm_run "/opt/cva/update.sh && systemctl is-active cva"
    [ -z "$NEW_PASSWORD" ] || echo "New password: $NEW_PASSWORD   (shown once)"
    wait_health "$(stack_out Url)"
    ;;
  status)
    URL="$(stack_out Url)"; echo "URL: $URL"
    aws ec2 describe-instances --instance-ids "$(stack_out InstanceId)" --query 'Reservations[0].Instances[0].State.Name' --output text
    echo "health: $(curl -s --max-time 10 "$URL/health" || echo unreachable)"
    ;;
  logs)
    ssm_run "tail -n 60 /var/log/cva-bootstrap.log; echo ----; journalctl -u cva -n 60 --no-pager"
    ;;
  destroy)
    read -r -p "Delete stack $STACK, the code bucket and the sign-in secret in account $ACCOUNT? Runs stored on the instance are lost. Type yes: " a
    [ "$a" = "yes" ] || { echo "cancelled"; exit 1; }
    aws cloudformation delete-stack --stack-name "$STACK"; aws cloudformation wait stack-delete-complete --stack-name "$STACK"
    aws s3 rb "s3://$BUCKET" --force >/dev/null 2>&1 || true
    aws secretsmanager delete-secret --secret-id "$SECRET" --force-delete-without-recovery >/dev/null 2>&1 || true
    echo "deleted"
    ;;
  *) echo "usage: $0 [deploy|update|status|logs|destroy]" >&2; exit 2 ;;
esac
