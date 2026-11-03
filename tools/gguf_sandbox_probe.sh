#!/usr/bin/env bash
set -euo pipefail
UNIT=michiflin-gguf-sandbox-test
SERVICE_USER=michiflin-gguf-probe
cleanup() {
 sudo systemctl stop "$UNIT.service" >/dev/null 2>&1 || true
 sudo userdel "$SERVICE_USER" >/dev/null 2>&1 || true
 sudo rm -rf /opt/michiflin-isolation-probe
 rm -f "$GITHUB_WORKSPACE/.synthetic-canary"
}
trap cleanup EXIT
printf 'synthetic\n' > "$GITHUB_WORKSPACE/.synthetic-canary"
sudo mkdir -p /opt/michiflin-isolation-probe
printf 'public model' | sudo tee /opt/michiflin-isolation-probe/model.txt >/dev/null
sudo install -m 0644 tools/gguf_sandbox_probe.py /opt/michiflin-isolation-probe/probe.py
sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
# Check whether the runner itself can establish a direct connection first.
python3 - <<'PY'
import socket
try:
 with socket.create_connection(('1.1.1.1',443), timeout=5):print('SAFE_PROBE_HOST_OUTBOUND true')
except OSError:print('SAFE_PROBE_HOST_OUTBOUND false')
PY
sudo systemd-run --quiet --unit="$UNIT" --property="User=$SERVICE_USER" \
 --property=NoNewPrivileges=yes --property=ProtectHome=yes --property=ProtectSystem=strict \
 --property=PrivateTmp=yes --property=PrivateDevices=yes --property=ProtectKernelTunables=yes \
 --property=ProtectKernelModules=yes --property=ProtectControlGroups=yes \
 --property=RestrictSUIDSGID=yes --property='RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' \
 --property=IPAddressDeny=any --property=IPAddressAllow=localhost \
 --property=MemoryMax=128M --property=CPUQuota=100% --property=TasksMax=32 \
 --property=StandardOutput=null --property=StandardError=null \
 /usr/bin/python3 /opt/michiflin-isolation-probe/probe.py "$GITHUB_WORKSPACE/.synthetic-canary"
for i in $(seq 1 15); do
 if curl -fsS --noproxy '*' --max-time 3 http://127.0.0.1:18971/ > "$RUNNER_TEMP/probe-result.json"; then break; fi
 sleep 1
done
python3 - <<'PY'
import json,os
x=json.load(open(os.path.join(os.environ['RUNNER_TEMP'],'probe-result.json')))
print('SAFE_SANDBOX_PROBE',json.dumps(x,sort_keys=True))
assert all(x.values())
PY
sudo systemctl show "$UNIT.service" -p MemoryMax -p CPUQuotaPerSecUSec -p TasksMax -p NoNewPrivileges -p ProtectHome -p PrivateDevices -p IPAddressAllow -p IPAddressDeny | sed 's/^/SAFE_SANDBOX_LIMIT /'
