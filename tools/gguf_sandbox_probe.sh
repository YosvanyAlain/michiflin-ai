#!/usr/bin/env bash
set -euo pipefail
UNIT=michiflin-gguf-sandbox-test
SERVICE_USER=michiflin-gguf-probe
RUNTIME=/run/michiflin-gguf-probe
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
if ! command -v setfacl >/dev/null; then sudo apt-get update -qq && sudo apt-get install --yes --no-install-recommends acl >/dev/null; fi
sudo systemd-run --quiet --unit="$UNIT" --property="User=$SERVICE_USER" \
 --property=RuntimeDirectory=michiflin-gguf-probe --property=RuntimeDirectoryMode=0700 \
 --property=NoNewPrivileges=yes --property=ProtectHome=yes --property=ProtectSystem=strict \
 --property=PrivateTmp=yes --property=PrivateDevices=yes --property=PrivateNetwork=yes \
 --property=ProtectKernelTunables=yes --property=ProtectKernelModules=yes --property=ProtectControlGroups=yes \
 --property=RestrictSUIDSGID=yes --property='RestrictAddressFamilies=AF_UNIX' \
 --property=MemoryMax=128M --property=CPUQuota=100% --property=TasksMax=32 \
 --property=StandardOutput=null --property=StandardError=null \
 /usr/bin/python3 /opt/michiflin-isolation-probe/probe.py "$GITHUB_WORKSPACE/.synthetic-canary" "$RUNTIME/probe.sock"
for i in $(seq 1 30); do
 if sudo test -S "$RUNTIME/probe.sock"; then break; fi
 if ! sudo systemctl is-active --quiet "$UNIT"; then echo SAFE_SERVICE_INACTIVE; exit 1; fi
 sleep 1
done
sudo setfacl -m "u:$(id -u):rx" "$RUNTIME"
sudo setfacl -m "u:$(id -u):rw" "$RUNTIME/probe.sock"
curl -fsS --unix-socket "$RUNTIME/probe.sock" --max-time 5 http://localhost/ > "$RUNNER_TEMP/probe-result.json"
if sudo -u nobody curl -fsS --unix-socket "$RUNTIME/probe.sock" --max-time 2 http://localhost/ >/dev/null 2>&1; then
 echo SAFE_UNAUTHORIZED_USER_ALLOWED; exit 1
else
 echo SAFE_UNAUTHORIZED_USER_BLOCKED
fi
python3 - <<'PY'
import json,os
x=json.load(open(os.path.join(os.environ['RUNNER_TEMP'],'probe-result.json')))
print('SAFE_UNIX_SANDBOX_PROBE',json.dumps(x,sort_keys=True))
assert all(x.values())
PY
sudo systemctl show "$UNIT.service" -p MemoryMax -p CPUQuotaPerSecUSec -p TasksMax -p PrivateNetwork -p RestrictAddressFamilies -p NoNewPrivileges -p ProtectHome | sed 's/^/SAFE_UNIX_SANDBOX_LIMIT /'
