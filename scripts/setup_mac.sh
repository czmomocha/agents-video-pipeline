#!/usr/bin/env bash
# Mac (Apple Silicon) 一次性环境准备
set -e

echo "==> 1) Brew dependencies"
which ffmpeg     >/dev/null || brew install ffmpeg
which uv         >/dev/null || brew install uv
which ollama     >/dev/null || brew install ollama
brew upgrade ollama || true

echo "==> 2) Verify Ollama version (≥ 0.20.3 required for Gemma 4 tool calling)"
ollama --version

echo "==> 3) Pull Gemma 4 E4B (M1 32GB does NOT need 26B)"
ollama pull gemma4:e4b

echo "==> 4) Python venv & deps"
cd "$(dirname "$0")/.."
uv venv --python 3.11
uv sync

cat <<'EOF'

==> 5) MANUAL STEPS (still required):

  a) ComfyUI 端：
     - 安装 ComfyUI-Manager
     - 通过 Manager 安装节点：ComfyUI-GGUF, ComfyUI-LTXVideo
     - 下载 Sulphur 2 GGUF (Q4_K_M / Q5_K_M) 到 ComfyUI/models/checkpoints/
     - 启动：
         python main.py --listen 127.0.0.1 --port 8188 \
                        --use-split-cross-attention --force-fp16

  b) 复制 workflow：
     cp "<ComfyUI>/custom_nodes/.../ltx23_t2v distilled.json" \
        ./workflows/sulphur2_t2v.json

  c) 在 ComfyUI Web UI 加载 workflows/sulphur2_t2v.json，
     打开 "Show node IDs"，把 5 个节点 ID 抄到 config/node_mapping.yaml
     （详见 workflows/_placeholders.md）

  d) 下载 sulphur_prompt_enhancer_model-q8_0.gguf 到 ./models/  （可选）

  e) 自检：
     python -m src.cli env

  f) 跑通第一镜头：
     python -m src.cli shot --prompt "a foggy mountain at dawn, cinematic"

EOF
