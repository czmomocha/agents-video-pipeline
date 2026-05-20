#!/usr/bin/env bash
# Mac (Apple Silicon) 一次性环境准备
set -e

echo "==> 1) Brew dependencies"
which ffmpeg     >/dev/null || brew install ffmpeg
which uv         >/dev/null || brew install uv
which ollama     >/dev/null || brew install ollama
brew upgrade ollama || true

# M2-D-2: TTS + ASR
which whisper-cli >/dev/null 2>&1 || brew install whisper-cpp || true


echo "==> 2) Verify Ollama version (≥ 0.20.3 required for Gemma 4 tool calling)"
ollama --version

echo "==> 3) Pull Gemma 4 E4B (M1 32GB does NOT need 26B)"
ollama pull gemma4:e4b

echo "==> 4) Python venv & deps"
cd "$(dirname "$0")/.."
uv venv --python 3.11
uv sync

echo "==> 4.1) Install Piper TTS (via pip)"
source .venv/bin/activate
pip install piper-tts || echo "⚠️  piper-tts installation failed, please install manually: pip install piper-tts"

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
     cp "<ComfyUI>/custom_nodes/.../ltx23_i2v.json" \
        ./workflows/sulphur2_i2v.json

  c) 在 ComfyUI Web UI 加载两个 workflow，
     打开 "Show node IDs"，把节点 ID 抄到 config/node_mapping.yaml
     （详见 workflows/_placeholders.md）

  d) 下载 sulphur_prompt_enhancer_model-q8_0.gguf 到 ./models/  （可选）

  e) M2-D-2 资源：
     - Piper 中文模型（推荐）：
         mkdir -p ./models/piper
         # 从 https://github.com/rhasspy/piper/blob/master/VOICES.md 选一个 zh_CN 模型
         # 例如 zh_CN-huayan-medium
         curl -L -o ./models/piper/zh_CN-huayan-medium.onnx \
              "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx"
         curl -L -o ./models/piper/zh_CN-huayan-medium.onnx.json \
              "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json"
     - Whisper.cpp 模型（推荐 base 或 medium）：
         mkdir -p ./models/whisper
         # 例如 ggml-base.bin（约 140MB）
         curl -L -o ./models/whisper/ggml-base.bin \
              "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"

  f) 自检：
     python -m src.cli env

  g) 跑通端到端：
     # 先无 I2V 调试 T2V 链：
     python -m src.cli render -t "中国茶文化的一天" --no-i2v --tts piper

     # 全链路（含配音+字幕）：
     python -m src.cli render -t "雨夜便利店里的相遇"

EOF
