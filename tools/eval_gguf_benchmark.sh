#!/usr/bin/env bash
set -euo pipefail
WORK="${RUNNER_TEMP:?}/michiflin-gguf-evaluation"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT
printf 'SAFE_GGUF_HARDWARE cpu=%s memory_kib=%s available_disk_kib=%s\n' "$(nproc)" "$(awk '/MemTotal/{print $2}' /proc/meminfo)" "$(df -Pk "$WORK" | awk 'NR==2{print $4}')"
git clone --quiet --depth 1 --branch v0.8.0 https://github.com/ggml-org/llama.cpp.git "$WORK/llama.cpp"
test "$(git -C "$WORK/llama.cpp" rev-parse HEAD)" = "8642541b255bfb5dc39cd351a178be09013a55d6"
cmake -S "$WORK/llama.cpp" -B "$WORK/llama-build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_SERVER=ON > "$WORK/cmake.log"
cmake --build "$WORK/llama-build" --target llama-server -j 4 >> "$WORK/cmake.log" 2>&1 || { tail -n 60 "$WORK/cmake.log"; exit 1; }
SERVER="$WORK/llama-build/bin/llama-server"
test -x "$SERVER"
download_and_evaluate() {
  local name="$1" url="$2" expected="$3" actual
  echo "SAFE_GGUF_DOWNLOAD_BEGIN $name"
  curl --fail --location --silent --show-error --connect-timeout 30 --max-time 600 --retry 2 --retry-delay 3 "$url" -o "$WORK/model.gguf"
  actual="$(sha256sum "$WORK/model.gguf" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then echo "SAFE_GGUF_SHA_MISMATCH $name"; exit 1; fi
  echo "SAFE_GGUF_SHA_OK $name"
  python3 tools/eval_gguf_benchmark.py "$name" "$WORK/model.gguf" "$SERVER"
  rm -f "$WORK/model.gguf"
}
download_and_evaluate 'qwen3-1.7b-abliterated-q4km' 'https://huggingface.co/bartowski/mlabonne_Qwen3-1.7B-abliterated-GGUF/resolve/fb7311cea48018c704b343c2ccabaa0cbc13f4f3/mlabonne_Qwen3-1.7B-abliterated-Q4_K_M.gguf?download=true' 'bea197fd05bad4126cdfe3198b53e6458b731ba554d9bc33548d0259788a7ba8'
download_and_evaluate 'qwen3-4b-instruct-abliterated-q4km' 'https://huggingface.co/mradermacher/Huihui-Qwen3-4B-Instruct-2507-abliterated-GGUF/resolve/c9e90669eeb205d5af35c28a3e9983fc9293c2ec/Huihui-Qwen3-4B-Instruct-2507-abliterated.Q4_K_M.gguf?download=true' 'b1324e2f1bd2e0610d591f54f93e38d90b187f69d1f215f4cace349eade678d7'
echo 'SAFE_GGUF_EVAL_DONE'
