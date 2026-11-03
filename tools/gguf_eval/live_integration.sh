#!/usr/bin/env bash
set -euo pipefail
WORK="${RUNNER_TEMP:?}/michiflin-gguf-integration"
mkdir -p "$WORK"
trap 'rm -rf "$WORK" "$RUNNER_TEMP/llama-evaluation.log"' EXIT
git clone --quiet --depth 1 --branch v0.8.0 https://github.com/ggml-org/llama.cpp.git "$WORK/src"
test "$(git -C "$WORK/src" rev-parse HEAD)" = 8642541b255bfb5dc39cd351a178be09013a55d6
cmake -S "$WORK/src" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_SERVER=ON > "$WORK/compile.log"
cmake --build "$WORK/build" --target llama-server -j 4 >> "$WORK/compile.log" 2>&1 || { tail -n 60 "$WORK/compile.log"; exit 1; }
curl --fail --location --silent --show-error --connect-timeout 30 --max-time 600 --retry 2 --retry-delay 3 'https://huggingface.co/mradermacher/Huihui-Qwen3-4B-Instruct-2507-abliterated-GGUF/resolve/c9e90669eeb205d5af35c28a3e9983fc9293c2ec/Huihui-Qwen3-4B-Instruct-2507-abliterated.Q4_K_M.gguf?download=true' -o "$WORK/model.gguf"
echo 'b1324e2f1bd2e0610d591f54f93e38d90b187f69d1f215f4cace349eade678d7  '"$WORK/model.gguf" | sha256sum --check --status
echo 'SAFE_INTEGRATION_MODEL_SHA_OK'
python tools/gguf_eval/live_integration.py "$WORK/build/bin/llama-server" "$WORK/model.gguf"
