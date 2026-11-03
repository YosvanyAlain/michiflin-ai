#!/usr/bin/env bash
# Isolated, temporary and bounded local model for one GitHub test runner.
# Never restore identity, access bot state, persist weights or print the API key.
set -euo pipefail

WORK=/opt/michiflin-local-gguf-test
MODEL="$WORK/model.gguf"
USER_NAME=michiflin-gguf
UNIT=michiflin-local-gguf
RUNTIME=/run/michiflin-local-gguf
SOCKET="$RUNTIME/llama.sock"
KEY_FILE="${RUNNER_TEMP:?}/michiflin-local-gguf-key"
ENGINE_URL='https://github.com/ggml-org/llama.cpp/releases/download/b11960/llama-b11960-bin-ubuntu-x64.tar.gz'
ENGINE_SHA='31cc3a6bc638ac6c48dc4b1b74888f1eaadf3d14227c1e5f67daf9a49541d97a'
MODEL_URL='https://huggingface.co/mradermacher/Huihui-Qwen3-4B-Instruct-2507-abliterated-GGUF/resolve/c9e90669eeb205d5af35c28a3e9983fc9293c2ec/Huihui-Qwen3-4B-Instruct-2507-abliterated.Q4_K_M.gguf?download=true'
MODEL_SHA='b1324e2f1bd2e0610d591f54f93e38d90b187f69d1f215f4cace349eade678d7'

cleanup() {
  # The directory's marker binds deletion to files made by this job; never
  # touch the workspace, state, backups or any pre-existing model directory.
  if sudo test -f "$WORK/.test-run-${GITHUB_RUN_ID:-unknown}"; then
    if sudo systemctl is-active --quiet "$UNIT.service"; then
      if ! sudo systemctl stop "$UNIT.service"; then echo 'SAFE_GGUF_STOP_FAILED'; return 1; fi
    fi
    if sudo systemctl is-active --quiet "$UNIT.service"; then echo 'SAFE_GGUF_STILL_ACTIVE'; return 1; fi
    sudo rm -rf -- "$WORK"
    sudo userdel "$USER_NAME" >/dev/null 2>&1 || true
  fi
  rm -f -- "$KEY_FILE"
}

prepare() {
  [[ "${GITHUB_ACTIONS:-}" == 'true' && "$(uname -m)" == 'x86_64' ]] || { echo 'SAFE_GGUF_PREP_UNSUPPORTED'; return 1; }
  # Leave headroom for the bot, its browser, backups and downloads.
  local available_kib disk_kib memory_limit
  available_kib=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
  disk_kib=$(df -Pk /opt | awk 'NR==2 {print $4}')
  memory_limit=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo max)
  printf 'SAFE_GGUF_PREFLIGHT memory_kib=%s disk_kib=%s cgroup=%s\n' "$available_kib" "$disk_kib" "$memory_limit"
  [[ "$available_kib" -ge 8000000 && "$disk_kib" -ge 4500000 ]] || { echo 'SAFE_GGUF_PREP_LOW_RESOURCES'; return 1; }
  if [[ "$memory_limit" =~ ^[0-9]+$ ]] && (( memory_limit < 10000000000 )); then echo 'SAFE_GGUF_PREP_LOW_CGROUP'; return 1; fi
  command -v systemd-run >/dev/null && command -v curl >/dev/null || return 1
  if ! command -v setfacl >/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install --yes --no-install-recommends acl >/dev/null
  fi
  if id "$USER_NAME" >/dev/null 2>&1 || sudo test -e "$WORK" || \
     [[ "$(sudo systemctl show "$UNIT.service" --property=LoadState --value 2>/dev/null || true)" == 'loaded' ]]; then
    echo 'SAFE_GGUF_PREP_NAME_CONFLICT'; return 1
  fi
  sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
  if ! sudo install -d -o "$USER_NAME" -g "$USER_NAME" -m 0700 "$WORK"; then
    sudo userdel "$USER_NAME" >/dev/null 2>&1 || true
    echo 'SAFE_GGUF_PREP_DIRECTORY_FAILED'; return 1
  fi
  if ! sudo -u "$USER_NAME" touch "$WORK/.test-run-${GITHUB_RUN_ID:?}"; then
    sudo rmdir "$WORK" >/dev/null 2>&1 || true
    sudo userdel "$USER_NAME" >/dev/null 2>&1 || true
    echo 'SAFE_GGUF_PREP_MARKER_FAILED'; return 1
  fi
  # Download and unpack as a separate UID with an empty environment. This
  # preparation runs before any bot state is restored on the runner.
  sudo -u "$USER_NAME" env -i HOME="$WORK" PATH='/usr/bin:/bin' \
    WORK="$WORK" ENGINE_URL="$ENGINE_URL" ENGINE_SHA="$ENGINE_SHA" MODEL_URL="$MODEL_URL" MODEL_SHA="$MODEL_SHA" \
    bash -c '
      set -euo pipefail; umask 077; cd "$WORK"
      echo SAFE_GGUF_ENGINE_DOWNLOAD
      curl --fail --location --silent --show-error --retry 2 --retry-max-time 150 --connect-timeout 25 --max-time 150 "$ENGINE_URL" -o engine.tar.gz
      printf "%s  engine.tar.gz\n" "$ENGINE_SHA" | sha256sum --check --status
      tar -xzf engine.tar.gz
      test -x llama-b11960/llama-server
      rm -f engine.tar.gz
      echo SAFE_GGUF_MODEL_DOWNLOAD
      curl --fail --location --silent --show-error --retry 2 --retry-max-time 420 --connect-timeout 25 --max-time 420 "$MODEL_URL" -o model.partial
      test "$(wc -c < model.partial)" -eq 2497281312
      printf "%s  model.partial\n" "$MODEL_SHA" | sha256sum --check --status
      mv model.partial model.gguf
    '
  echo 'SAFE_GGUF_PREP_VERIFIED'
}

start() {
  sudo test -f "$WORK/.test-run-${GITHUB_RUN_ID:?}" && sudo test -f "$MODEL" || return 1
  umask 077
  python3 -c 'import secrets;print(secrets.token_hex(32))' > "$KEY_FILE"
  sudo install -o "$USER_NAME" -g "$USER_NAME" -m 0600 "$KEY_FILE" "$WORK/api-key"
  # The model sees neither /home (bot state) nor the network. Only a Unix
  # socket is shared with the runner; no prompts or HTTP bodies are logged.
  sudo systemd-run --quiet --unit="$UNIT" --property="User=$USER_NAME" \
    --property=RuntimeDirectory=michiflin-local-gguf --property=RuntimeDirectoryMode=0700 \
    --property=UMask=0077 --property=NoNewPrivileges=yes --property=ProtectHome=yes \
    --property=ProtectSystem=strict --property=PrivateTmp=yes --property=PrivateDevices=yes \
    --property=PrivateNetwork=yes --property=ProtectKernelTunables=yes \
    --property=ProtectKernelModules=yes --property=ProtectControlGroups=yes \
    --property=RestrictSUIDSGID=yes --property='RestrictAddressFamilies=AF_UNIX' \
    --property=MemoryMax=7G --property=CPUQuota=200% --property=TasksMax=64 --property=Nice=10 \
    --property=TimeoutStopSec=10 --property=StandardOutput=null --property=StandardError=null \
    "$WORK/llama-b11960/llama-server" --model "$MODEL" --host "$SOCKET" \
    --api-key-file "$WORK/api-key" --ctx-size 3072 --threads 2 --threads-batch 2 \
    --parallel 1 --poll 0 --poll-batch 0 --no-webui
  local socket_prepared=0
  for _ in $(seq 1 180); do
    if ! sudo systemctl is-active --quiet "$UNIT.service"; then echo 'SAFE_GGUF_SERVER_INACTIVE'; return 1; fi
    if [[ "$socket_prepared" == 0 ]] && sudo test -S "$SOCKET"; then
      sudo setfacl -m "u:$(id -u):rx" "$RUNTIME"
      sudo setfacl -m "u:$(id -u):rw" "$SOCKET"
      socket_prepared=1
    fi
    if [[ "$socket_prepared" == 1 ]] && python3 tools/local_gguf_health.py; then
      echo 'SAFE_GGUF_SERVER_READY_AUTHENTICATED'; return 0
    fi
    sleep 1
  done
  echo 'SAFE_GGUF_SERVER_READY_TIMEOUT'
  return 1
}

case "${1:-}" in
  prepare) prepare ;;
  start) start ;;
  stop) cleanup ;;
  *) echo 'Usage: local_gguf_session.sh prepare|start|stop' >&2; exit 2 ;;
esac
