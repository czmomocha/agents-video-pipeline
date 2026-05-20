# 本地视频生产线（Agents Video Pipeline）

> 基于 **Sulphur 2 (LTX-Video 2.3) + Gemma 4 + ComfyUI** 的全本地多智能体视频生产线。
> 硬件目标：**Mac M1 / 32GB 统一内存**。

## 文档

- 架构设计：[`docs/01-architecture-design.md`](docs/01-architecture-design.md)
- M1 实现清单：[`docs/02-m1-implementation-checklist.md`](docs/02-m1-implementation-checklist.md)

## M1 阶段目标

```bash
python -m src.cli shot --prompt "a foggy mountain at dawn, cinematic"
# → output/<date>/<task_id>/shots/01.mp4   （6s, 1080p, Sulphur 2 出片）
```

## 一次性环境准备（Mac）

```bash
# 1. 系统依赖
brew install ffmpeg uv
brew upgrade ollama   # 必须 ≥ 0.20.3（修复 Gemma 4 工具调用 bug）

# 2. 拉模型（仅 E4B；M1 32GB 不下 26B）
ollama pull gemma4:e4b

# 3. 项目依赖
uv venv
uv sync

# 4. ComfyUI 端
#    - 安装 ComfyUI-Manager / ComfyUI-GGUF / ComfyUI-LTXVideo 节点
#    - 下载 Sulphur 2 GGUF 整合包（Q4_K_M / Q5_K_M）到 ComfyUI/models/
#    - 复制 Sulphur 2 仓库 workflows/ltx23_t2v\ distilled.json
#      → 本项目 workflows/sulphur2_t2v.json
#    - 启动 ComfyUI（建议参数）：
#      python main.py --listen 127.0.0.1 --port 8188 \
#                     --use-split-cross-attention --force-fp16

# 5. Sulphur prompt enhancer（GGUF）
#    下载 sulphur_prompt_enhancer_model-q8_0.gguf 到 ./models/

# 6. 工作流节点 ID 映射（一次性人工配置）
#    打开 ComfyUI Web UI 加载 sulphur2_t2v.json，
#    把 5 个关键节点的 ID 抄到 config/node_mapping.yaml
#    见 workflows/_placeholders.md
```

## 自检

```bash
python -m src.cli env
```

## 目录

```
src/
  adapters/   # 外部能力适配（ComfyUI / Ollama / GGUF / FFmpeg）
  agents/     # 智能体（M1 只有 prompt_smith）
  orchestrator/  # 状态/工具定义（M2 起接入 LangGraph）
  pipeline/   # 生产线主循环（M3 起）
  utils/      # 互斥锁、日志、IO
  cli.py
  config.py
workflows/    # ComfyUI workflow JSON（不入 git）
models/       # GGUF 模型（不入 git）
output/       # 生成产物
docs/         # 设计文档
scripts/      # 一次性脚本
```

## 当前进度

- [x] 架构设计 v1.0
- [x] M1 实现清单
- [x] 项目骨架（v0.1.0-scaffold）
- [x] M2-A：DirectorAgent + 全量 schema
- [x] M2-B：LangGraph 最小图
- [x] M2-C：Scriptwriter + Storyboarder（v0.2.0-director）
- [x] M2-D-1：ShotProducer + Compositor，端到端无声成片（v0.3.0-render）
- [x] **M2-D-2：Voice + Subtitle，端到端有声+字幕成片**（本次提交）
- [ ] M3：批量生产线（队列 + 定时 + Web UI + 断点续跑）

## 命令速查

| 命令 | 用途 | 需要在线服务 |
|---|---|---|
| `python -m src.cli env` | 环境自检 | — |
| `python -m src.cli plan -t "..."` | 仅 Director | Ollama + Gemma 4 |
| `python -m src.cli script -p plan.json` | 仅 Scriptwriter | Ollama + Gemma 4 |
| `python -m src.cli storyboard -p plan.json -S script.json` | 仅 Storyboarder | Ollama + Gemma 4 |
| `python -m src.cli plan-and-prompts -t "..."` | 完整 LLM 规划链 | Ollama + Gemma 4 |
| `python -m src.cli render -t "..."` | **端到端：topic→final.mp4（含配音+字幕）** | Ollama + ComfyUI + FFmpeg + (TTS, ASR) |
| `python -m src.cli shot -p "..."` | 单镜头出片（M1 闭环） | Ollama + ComfyUI |

## render 命令选项

| Flag | 说明 |
|---|---|
| `--no-i2v` | 全部走 T2V，调试用 |
| `--tts auto` | 自动选 TTS 后端（默认 piper > edge > silent） |
| `--tts piper` | 强制 piper-tts（纯本地推荐） |
| `--tts edge` | edge-tts（云端，需联网，中文优秀） |
| `--tts gpt_sovits` | GPT-SoVITS（最佳中文，待 Mac 端就绪） |
| `--tts none` | 跳过配音 |
| `--no-subtitles` | 跳过字幕 |
| `--no-burn-subs` | 生成 .srt 但不烧录到视频 |
| `--use-enhancer` | 启用 Sulphur 自带的 GGUF prompt enhancer |
