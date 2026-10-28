#!/usr/bin/env bash
# Runs ON the instance (as root) at first boot, from the unpacked code zip.  Inputs: REGION, SECRET_ID, CODE_URI.  Log: /var/log/cva-bootstrap.log
set -euo pipefail
exec > >(tee -a /var/log/cva-bootstrap.log) 2>&1
: "${REGION:?}" "${SECRET_ID:?}" "${CODE_URI:?}"
APP=/opt/cva/app
export DEBIAN_FRONTEND=noninteractive
echo "== packages"
apt-get update -y
apt-get install -y python3-venv python3-pip unzip curl libreoffice-core libreoffice-impress fonts-liberation fonts-dejavu-core
id cva >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/cva --shell /usr/sbin/nologin cva
mkdir -p /var/lib/cva/runs /etc/cva && chown -R cva:cva /var/lib/cva
echo "== python environments (A01 needs mcp 1.x; the AWS Transform server needs mcp 2.x, so each has its own)"
python3 -m venv /opt/cva/venv
/opt/cva/venv/bin/pip install -q --upgrade pip
/opt/cva/venv/bin/pip install -q -r "$APP/requirements.txt"
python3 -m venv /opt/cva/mcp-venv
/opt/cva/mcp-venv/bin/pip install -q --upgrade pip
/opt/cva/mcp-venv/bin/pip install -q "awslabs.aws-transform-mcp-server==0.2.3"
echo "== settings"
cat > /etc/cva/cva.env <<EOF
AWS_REGION=$REGION
AWS_DEFAULT_REGION=$REGION
CVA_AUTH_FILE=/etc/cva/auth.json
CVA_COOKIE_SECURE=always
CVA_RUNS_DIR=/var/lib/cva/runs
CVA_HOST=0.0.0.0
CVA_TRANSFORM_MCP_CMD=/opt/cva/mcp-venv/bin/awslabs.aws-transform-mcp-server
HOME=/var/lib/cva
PYTHONDONTWRITEBYTECODE=1
PATH=/opt/cva/venv/bin:/usr/local/bin:/usr/bin:/bin
EOF
chmod 644 /etc/cva/cva.env
cat > /opt/cva/update.sh <<EOF
#!/usr/bin/env bash
# Pull the latest code zip and sign-in secret, reinstall, restart.  Run as root (the deploy script calls it through SSM).
set -euo pipefail
export AWS_DEFAULT_REGION=$REGION
aws s3 cp "$CODE_URI" /tmp/cva.zip
unzip -q -o /tmp/cva.zip -d $APP
/opt/cva/venv/bin/pip install -q -r $APP/requirements.txt
aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --query SecretString --output text > /etc/cva/auth.json.new
install -o root -g cva -m 0640 /etc/cva/auth.json.new /etc/cva/auth.json && rm -f /etc/cva/auth.json.new
systemctl restart cva
EOF
chmod 755 /opt/cva/update.sh
cat > /etc/systemd/system/cva.service <<EOF
[Unit]
Description=A01 CVA Output Processing Agent
After=network-online.target
Wants=network-online.target
[Service]
User=cva
Group=cva
WorkingDirectory=$APP
EnvironmentFile=/etc/cva/cva.env
ExecStart=/opt/cva/venv/bin/python -m cva ui --port 8080
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
[Install]
WantedBy=multi-user.target
EOF
echo "== sign-in secret and start"
export AWS_DEFAULT_REGION=$REGION
aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --query SecretString --output text > /etc/cva/auth.json.new
install -o root -g cva -m 0640 /etc/cva/auth.json.new /etc/cva/auth.json && rm -f /etc/cva/auth.json.new
systemctl daemon-reload
systemctl enable --now cva
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/health >/dev/null; then echo "== A01 is up"; exit 0; fi
  sleep 2
done
echo "== A01 did not answer /health: see journalctl -u cva" >&2
exit 1
