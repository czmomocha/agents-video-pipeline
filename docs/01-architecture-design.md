# 本地化全自动视频生产线 —— 技术架构设计文档

> 版本：**v1.0**（基于 Sulphur 2 / LTX-Video 2.3 / Gemma 4 真实能力定稿）
> 日期：2026-05-14
> 项目目标：**Agent 编排驱动的本地视频生产线，源源不断生成视频片段并自动拼接成片**
> 运行环境：Mac（Apple Silicon）+ ComfyUI + Sulphur 2 + Ollama/LM Studio + Gemma 4

---

## 0. TL;DR

构建一个**纯本地、自动化、可批量生产**的视频流水线：

```
        ┌─────────── Idea Pool ───────────┐
        │ 主题队列 (人工/RSS/LLM 自生成)   │
        └─────────────┬───────────────────┘
                      │
  ┌───────────────────▼─────────────────────────────────────┐
  │        Multi-Agent Orchestrator (LangGraph)             │
  │                                                          │
  │  ┌─Director───┐  ┌─Scriptwriter─┐  ┌─Storyboarder─┐     │
  │  │ Gemma4-26B │→ │  Gemma4-E4B  │→│  Gemma4-E4B   │     │
  │  └────────────┘  └──────────────┘  └───────────────┘    │
  │         │                                  │             │
  │         ▼                                  ▼             │
  │  ┌─PromptSmith──┐               ┌─VoiceAgent──┐         │
  │  │  Gemma4-E4B  │               │ GPT-SoVITS  │         │
  │  │ +Sulphur自带 │               │  (per shot) │         │
  │  │   enhancer   │               └─────┬───────┘         │
  │  └──────┬───────┘                     │                 │
  │         │                              │                 │
  │         ▼                              │                 │
  │  ┌─ShotProducer──────┐                │                 │
  │  │ ComfyUI+Sulphur 2 │  (T2V/I2V,     │                 │
  │  │ → clip_*.mp4 6-20s│   每片段独立)   │                 │
  │  └──────┬─────────────┘               │                 │
  │         │                              │                 │
  │         ▼                              ▼                 │
  │  ┌─QAAgent (可选)─┐             ┌─SubtitleAgent──┐      │
  │  │ Gemma4 vision  │             │  whisper.cpp   │      │
  │  │  审片/重试      │             └────────┬───────┘      │
  │  └──────┬──────────┘                      │              │
  │         │                                  │              │
  │         └────────┬─────────────────────────┘              │
  │                  ▼                                        │
  │           ┌─Compositor─┐                                  │
  │           │  FFmpeg    │  转场/对齐/字幕烧录/BGM         │
  │           └──────┬─────┘                                  │
  └──────────────────┼────────────────────────────────────────┘
                     ▼
              [ output/{date}/{task_id}.mp4 ]
                     │
                     ▼
        ┌── Publisher (可选) ──┐
        │  本地归档 / 自动上传 │
        └──────────────────────┘
```

**核心理念**：把视频生产拆成"**镜头工厂（每个镜头 6-20s 真视频片段）+ 自动剪辑师**"两段式流水线，由 Gemma 4 多智能体编排。

---

## 1. 现状盘点（基于真实情况）

| 组件 | 真实定位 | 关键能力 |
|---|---|---|
| **Sulphur 2 (SulphurAI/Sulphur-2-base)** | 基于 **LTX-Video 2.3 (22B DiT)** 微调的开源视频模型 | T2V + I2V + 兼容 LTX 2.3 全部工作流；自带 prompt enhancer (GGUF 量化)；提供 BF16 / FP8 / LoRA 三种发布形态；**最低 8GB 显存可跑**；原生支持 **6-20 秒**片段、1080p/2K/4K；ComfyUI 工作流已随仓库提供 (`workflows/ltx23_t2v distilled.json`) |
| **ComfyUI** | 视频/图像生成执行引擎 | HTTP API + WebSocket，可加载 Sulphur 2 workflow，支持 Mac MPS 后端 |
| **Gemma 4**（推测装的是 E4B 或 26B-A4B） | Google 2026.04 发布的最新开源 LLM | **原生 function calling + agentic workflow 优化**、原生多模态、Apache 2.0；规格：E2B / E4B（端侧）/ 26B-A4B（MoE 激活 3.8B）/ 31B |
| **Ollama** | LLM 推理后端（CLI + REST） | 已原生支持 `gemma4:e4b` / `gemma4:26b`，v0.20+ 修复了工具调用解析问题 |
| **LM Studio** | LLM 推理后端（GUI + OpenAI 兼容 API） | 备用入口，可作为故障切换 |

> ⚠️ **仍需你确认 1 项**：Mac 具体型号 / 统一内存大小？这决定我们用哪个量化版本（FP8 还是 GGUF Q4/Q8）和并发度。

---

## 2. 与 v0.1 设计的关键变更

| 维度 | v0.1（旧） | v1.0（新） |
|---|---|---|
| 视频形态 | 关键帧 + Ken Burns（降级方案） | **真视频片段**（Sulphur 2 直出 6-20s 动态视频） |
| 核心循环 | 一次跑完一条视频 | **生产线模型**：Idea → Shot → Stitch，可批量、可断点续跑、可并发 |
| LLM 选型 | Gemma 3 / Qwen 占位 | **Gemma 4 全家桶**：26B 做导演（强推理）、E4B 做执行（快、省） |
| Prompt 工程 | 自己写 PromptEngineer Agent | **复用 Sulphur 2 自带的 prompt_enhancer.gguf**，再加一层我们的 PromptSmith |
| Agent 协议 | 普通函数调用 | **走 Gemma 4 原生 function calling**，更稳定 |
| 拼接逻辑 | 静态图 + 转场 | **多视频片段对齐 + 转场 + 视觉一致性约束**（首尾帧衔接） |

---

## 3. 推荐架构

### 3.1 五层架构

```
┌──────────────────────────────────────────────────────────────┐
│ L5  Producer Loop：批量调度（队列/定时/触发器）               │
├──────────────────────────────────────────────────────────────┤
│ L4  Presentation：FastAPI + Web UI / CLI / 监控面板          │
├──────────────────────────────────────────────────────────────┤
│ L3  Orchestration：LangGraph 多智能体状态机                   │
├──────────────────────────────────────────────────────────────┤
│ L2  Agents：Director / Scriptwriter / Storyboarder /         │
│             PromptSmith / ShotProducer / QA /                │
│             Voice / Subtitle / Compositor / Publisher        │
├──────────────────────────────────────────────────────────────┤
│ L1  Adapters：                                                │
│      ├─ LLMProvider（Ollama / LM Studio 双后端）             │
│      ├─ ComfyUIClient（HTTP+WS，加载 Sulphur 2 workflow）    │
│      ├─ SulphurPromptEnhancer（GGUF 本地推理，llama.cpp）    │
│      ├─ TTSProvider（GPT-SoVITS / XTTS-v2）                  │
│      ├─ ASRProvider（whisper.cpp Metal）                     │
│      └─ VideoCompositor（FFmpeg + 转场预设）                 │
├──────────────────────────────────────────────────────────────┤
│ L0  Infra：本地服务进程 / SQLite / 文件系统 / asyncio Queue  │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 智能体团队（"剧组"模型）

| Agent | 角色 | 模型/工具 | 输入 → 输出 | 关键设计 |
|---|---|---|---|---|
| **DirectorAgent** | 导演（核心决策） | **Gemma 4:26B** | `topic + style_brief` → `ProductionPlan` | 全局规划：风格、镜头数、节奏、转场策略；负责调度其他 Agent；用 function calling 显式指挥 |
| **ScriptwriterAgent** | 编剧 | Gemma 4:E4B | `Plan` → `Script[Scene{narration, mood, duration}]` | 中文叙事；输出严格 JSON schema |
| **StoryboarderAgent** | 分镜师 | Gemma 4:E4B | `Script` → `Storyboard[Shot{visual_intent, camera, motion, duration}]` | 关键产出：每个 shot 的 6/12/20s 时长选择、镜头运动、首尾帧衔接意图 |
| **PromptSmithAgent** | 提示词专家 | Gemma 4:E4B + **Sulphur 自带 enhancer** | `Shot` → `{positive_prompt, negative_prompt, seed, lora?, duration, resolution}` | 两步走：① E4B 生成英文基础 prompt；② 调用 `prompt_enhancer.gguf` 二次增强（这是 Sulphur 2 推荐管线） |
| **ShotProducerAgent**（工具型） | 拍摄执行 | **ComfyUI + Sulphur 2** | `enhanced_prompt + prev_last_frame?` → `clip_{i}.mp4` | T2V 模式或 I2V 模式（用前一镜头末帧做首帧 → 视觉连续性）；失败重试 ≤3 次 |
| **VoiceAgent**（工具型） | 配音 | GPT-SoVITS | `narration` → `wav` | 与 ShotProducer 并行执行 |
| **SubtitleAgent**（工具型） | 字幕 | whisper.cpp | `wav` → `srt` | Mac Metal 加速 |
| **QAAgent**（可选） | 质检 | Gemma 4 多模态（看片） | `clip + script` → `{ok, reason, action}` | 不合格 → 触发重生（换 seed / 改 prompt） |
| **CompositorAgent**（工具型） | 剪辑师 | FFmpeg | `clips[] + audios[] + srts[]` → `final.mp4` | 时间轴对齐、转场（淡入/硬切/匹配剪辑）、字幕烧录、BGM 混音 |
| **PublisherAgent**（可选） | 发行 | — | `final.mp4` → 归档/上传 | 本地归档命名规范、可选对接平台 API（先留空） |

### 3.3 生产线核心数据流

```
                           ┌─ IdeaQueue (SQLite)
                           │     ↓ pop
                           │  [topic]
                           ▼
        ┌──────── DirectorAgent ────────┐
        │  decides: n_shots, duration   │
        │  per shot, style, BGM mood    │
        └────────────┬───────────────────┘
                     │ ProductionPlan
                     ▼
        ┌── Scriptwriter ──┐
        └────────┬─────────┘
                 │ Script
                 ▼
        ┌── Storyboarder ──┐
        └────────┬─────────┘
                 │ Storyboard
                 │
   ┌─────────────┼──────────────────────┐
   │ for each Shot (并行 or 串行)       │
   │                                    │
   │  PromptSmith ──▶ enhanced_prompt  │
   │       │                           │
   │       ▼                           │
   │  ShotProducer ──▶ clip_i.mp4     │   ◀── (I2V mode 用 prev shot 末帧)
   │       │                           │
   │       ▼                           │
   │  QAAgent ─── fail ──▶ retry      │
   │       │ ok                        │
   │       ▼                           │
   │  VoiceAgent ──▶ wav_i             │
   │  SubtitleAgent ──▶ srt_i          │
   └─────────────┬─────────────────────┘
                 ▼
            Compositor
                 │
                 ▼
            output.mp4
                 │
                 ▼
            Publisher
```

### 3.4 LangGraph State

```python
class PipelineState(TypedDict):
    task_id: str
    topic: str
    plan: ProductionPlan | None
    script: Script | None
    storyboard: Storyboard | None
    shots: list[ShotState]      # 每个含 prompt/clip_path/wav_path/srt_path/qa_status
    output_path: str | None
    metrics: dict                # 耗时、显存峰值、重试次数等
    errors: list[ErrorRecord]
```

每个 `ShotState`：
```python
class ShotState(TypedDict):
    idx: int
    visual_intent: str
    camera: str
    duration_sec: Literal[6, 12, 20]
    resolution: Literal["1080p", "2k", "4k"]
    positive_prompt: str
    negative_prompt: str
    enhanced_prompt: str         # 经 Sulphur enhancer 处理
    seed: int
    use_i2v: bool                # 是否用上一镜头末帧做首帧
    init_image: Path | None
    clip_path: Path | None
    wav_path: Path | None
    srt_path: Path | None
    qa: QAResult | None
    retry: int
```

---

## 4. 关键技术决策（基于真实事实）

### 4.1 Sulphur 2 部署形态

| 选项 | 文件 | 显存/UMA | 适合 |
|---|---|---|---|
| BF16 完整 | `sulphur_dev_bf16.safetensors` (46GB) | ≥48GB | M3 Ultra 64/96GB+ |
| **FP8 混合**（⭐推荐） | `sulphur_dev_fp8mixed.safetensors` (29GB) | 32-48GB | M2/M3/M4 Pro 32GB+，M Max 系列 |
| **GGUF 量化整合包** | 第三方 Sulphur-2-GGUF | **8-16GB** | M2/M3/M4 16GB（可跑但慢） |
| LoRA + 原 LTX 2.3 | `sulphur_lora_rank_768.safetensors` (10GB) + LTX 2.3 base | 中等 | 想保留切换原 LTX 能力 |

> **决策**：默认走 **FP8 + ComfyUI 自带 workflow**；低配 Mac 走 GGUF。

### 4.2 Gemma 4 选型

| Agent | 模型 | 理由 |
|---|---|---|
| Director | `gemma4:26b`（MoE 激活 3.8B） | 复杂规划、function calling 链路最稳 |
| Scriptwriter / Storyboarder / PromptSmith / QA-text | `gemma4:e4b` | 速度快、4B 等效，足够结构化输出 |
| QA-vision（可选） | `gemma4:e4b` 多模态 / 26B | Gemma 4 全系原生多模态，可直接看视频帧 |

**Function calling 注意点**：Ollama v0.20.3 才修复了 Gemma 4 的工具调用闭合标签问题，**确保 ollama ≥ 0.20.3**。

### 4.3 视觉连续性策略（生产线的核心）

视频片段拼接最大的问题是**镜头之间会跳变**。三档策略：

| 策略 | 做法 | 一致性 | 自由度 |
|---|---|---|---|
| **A. 纯 T2V + 强 prompt 一致性** | 每镜头独立 T2V，靠 PromptSmith 注入统一 style tag/角色描述 | 弱 | 高 |
| **B. I2V 链式（⭐推荐）** | shot[i+1] 用 shot[i] 末帧作为首帧（FFmpeg 提取最后一帧 → ComfyUI I2V workflow） | 强 | 中 |
| **C. 关键帧锚定** | 先 T2I 出整片关键帧集合，每段 I2V 锚定到对应关键帧 | 最强 | 低 |

> **决策**：默认 **B 模式**，DirectorAgent 在分镜中标记是否需要"硬切"来切换场景（硬切就回退到 T2V）。

### 4.4 编排框架

**LangGraph**（确认）：
- 原生支持状态机 + 条件分支 + 循环（QA 重试）+ HITL（人在环节点，可选）。
- 与 Gemma 4 function calling 无缝（通过 `bind_tools`）。
- 可视化（`graph.get_graph().draw_mermaid()`）。

### 4.5 触发与生产模式

四种生产模式同时支持（同一份 graph，不同入口）：

1. **CLI 单次**：`python -m src.cli run --topic "中国茶文化"`
2. **Web UI 交互**：FastAPI + 简易表单，实时显示进度
3. **批量队列**：`topics.jsonl` → 串行/并行消费
4. **定时生产**：cron / `schedule` 库 → 每天 N 条

---

## 5. 关键接口契约

### 5.1 ComfyUI 客户端（针对 Sulphur 2）

```python
class ComfyUIClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8188",
        workflow_t2v: dict,    # 加载自 Sulphur 2 仓库 workflows/ltx23_t2v distilled.json
        workflow_i2v: dict,    # LTX 2.3 i2v workflow
    ): ...

    async def generate_t2v(
        self,
        prompt: str,
        negative_prompt: str = "",
        duration_sec: Literal[6, 12, 20] = 6,
        resolution: Literal["1080p", "2k", "4k"] = "1080p",
        seed: int | None = None,
        mode: Literal["fast", "pro"] = "fast",
    ) -> Path: ...

    async def generate_i2v(
        self,
        init_image: Path,
        prompt: str,
        duration_sec: Literal[6, 12, 20] = 6,
        seed: int | None = None,
    ) -> Path: ...
```

实现要点：
- 加载 Sulphur 2 仓库自带的 `ltx23_t2v distilled.json` 作为模板，把 prompt/seed/duration/resolution 等节点参数动态替换后 POST `/prompt`；
- 通过 WS 监听 `executed`/`progress` 事件，进度同步到 LangGraph state；
- 失败自动 retry ≤3，OOM 时降级（4K→2K→1080p、20s→12s→6s）。

### 5.2 Sulphur Prompt Enhancer 适配器

```python
class SulphurPromptEnhancer:
    """调用 Sulphur 2 自带的 prompt_enhancer GGUF 模型，通过 llama.cpp/llama-cpp-python"""
    def __init__(self, gguf_path: Path = Path("models/sulphur_prompt_enhancer_model-q8_0.gguf")): ...
    async def enhance(self, raw_prompt: str, context: dict | None = None) -> str: ...
```

### 5.3 LLM 抽象（双后端可切换）

```python
class LLMProvider:
    def __init__(self, backend: Literal["ollama", "lmstudio"], model: str): ...
    async def chat(self, messages, **kwargs) -> str: ...
    async def chat_json(self, messages, schema: BaseModel) -> BaseModel: ...
    async def chat_with_tools(self, messages, tools: list[dict]) -> ToolCallResult: ...  # Gemma 4 原生
```

### 5.4 视觉连续性辅助

```python
class FrameExtractor:
    """从上一段 clip 提取末帧，作为下一段 I2V 的首帧"""
    async def extract_last_frame(self, video_path: Path, out_path: Path) -> Path: ...
```

### 5.5 生产线入口

```python
# 单条
async def produce_video(topic: str, config: Config) -> Path: ...

# 批量生产线
async def run_production_line(
    idea_source: IdeaSource,        # 队列/文件/RSS
    config: Config,
    concurrency: int = 1,           # Mac 通常 1，避免 ComfyUI 抢显存
) -> AsyncIterator[Path]: ...
```

---

## 6. 目录结构

```
f:/AI/Agents/
├── docs/
│   ├── 01-architecture-design.md         ← 本文档
│   ├── 02-m1-implementation-checklist.md ← 待出
│   └── adr/                              ← 架构决策记录
├── src/
│   ├── agents/
│   │   ├── director.py
│   │   ├── scriptwriter.py
│   │   ├── storyboarder.py
│   │   ├── prompt_smith.py
│   │   ├── shot_producer.py
│   │   ├── qa.py
│   │   ├── voice.py
│   │   ├── subtitle.py
│   │   ├── compositor.py
│   │   └── publisher.py
│   ├── adapters/
│   │   ├── llm.py                # Ollama / LM Studio
│   │   ├── comfyui.py            # Sulphur 2 workflow 注入
│   │   ├── sulphur_enhancer.py   # GGUF prompt enhancer
│   │   ├── tts.py                # GPT-SoVITS
│   │   ├── asr.py                # whisper.cpp
│   │   ├── compositor_ffmpeg.py
│   │   └── frame_extractor.py
│   ├── orchestrator/
│   │   ├── graph.py              # LangGraph 主图
│   │   ├── state.py
│   │   └── tools.py              # Gemma 4 function calling 工具定义
│   ├── pipeline/
│   │   ├── producer_loop.py      # 生产线主循环（批量/定时）
│   │   └── idea_source.py        # 队列/RSS/LLM 自生成主题
│   ├── api/
│   │   └── main.py               # FastAPI
│   ├── ui/                       # 简易前端
│   ├── cli.py
│   ├── config.py
│   └── db.py                     # SQLite 任务/历史
├── workflows/
│   ├── sulphur2_t2v.json         # 复制自 Sulphur 2 仓库
│   ├── sulphur2_i2v.json
│   └── _placeholders.md          # 节点 ID 占位符约定
├── models/                       # 不入 git
│   ├── sulphur_prompt_enhancer_model-q8_0.gguf
│   └── ...
├── assets/
│   ├── bgm/
│   ├── fonts/
│   └── style_presets/            # "电影感"、"二次元"、"纪录片" 等预设
├── output/
│   └── <yyyymmdd>/<task_id>/
│       ├── plan.json
│       ├── script.json
│       ├── storyboard.json
│       ├── shots/
│       │   ├── 01.mp4  01.wav  01.srt  01.last_frame.png
│       │   └── ...
│       ├── final.mp4
│       └── metrics.json
├── tests/
├── pyproject.toml
└── README.md
```

---

## 7. 里程碑路线图

| 迭代 | 目标 | 产出 | 工期 |
|---|---|---|---|
| **M1：单镜头打通** | CLI 输入主题 → Director+Scriptwriter+PromptSmith+ShotProducer → 1 个 6s 视频片段 | 端到端 hello world | 2-3 天 |
| **M2：多镜头成片** | LangGraph 完整图，3-5 镜头，I2V 链式连续性，配音+字幕+FFmpeg 合成 | 单条完整视频 | 3-4 天 |
| **M3：生产线模式** | IdeaQueue + 批量循环 + Web UI + 任务历史 + 监控面板 | **批量产线 v1** | 4-5 天 |
| **M4：质量与稳定性** | QAAgent 自动重试、OOM 降级、断点续跑、风格预设库、BGM 自动匹配 | 生产可用 | 持续 |
| **M5：增强**（可选） | Publisher（自动归档/上传）、定时任务、LLM 自生成主题、A/B seed 投票 | 全自动 | 持续 |

---

## 8. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| Sulphur 2 在 Mac 上 OOM | 阻塞 | FP8/GGUF 分级；OOM 自动降档（4K→2K→1080p；20s→12s→6s） |
| 镜头间跳变严重 | 观感差 | I2V 链式 + 风格 prompt 锁定 + DirectorAgent 显式标"硬切" |
| 单镜头生成 5-15 分钟，整片很慢 | 体验 | 全异步 + 进度 WS 推送；中间产物全部缓存；支持断点续跑（每步落 SQLite） |
| Gemma 4 function calling 不稳 | Agent 链断 | 锁 ollama ≥ 0.20.3；JSON 模式兜底（schema 强约束）；3 次失败降级到模板化 prompt |
| ComfyUI workflow 节点名漂移 | 升级即坏 | workflow 用占位符 + 节点 ID 映射表；CI 启动时自检 |
| 长时间运行内存泄漏 | 生产线挂掉 | 每生产 N 条重启 ComfyUI 子进程；supervisor 守护 |
| TTS 与视频对不齐 | 音画不同步 | Compositor 阶段先用 whisper 测音频时长 → 反馈给 Storyboarder 调整 duration；或直接以 TTS 时长为准、视频做变速 |

---

## 9. 硬件画像与默认参数（基于 Mac M1 / 32GB 统一内存）

> 已确认硬件：**Apple M1（首代 M 系列）+ 32GB 统一内存**。这是一台**够用但不富裕**的机器，需要做两个核心权衡：① 优先 GGUF 量化以留出余量给系统；② Sulphur 2 与 Gemma 4 不能同时占满显存——**串行调度，不并发**。

### 9.1 显存/内存预算（粗估）

| 用途 | 占用 | 何时驻留 |
|---|---|---|
| macOS 系统 + 后台 | ~6 GB | 常驻 |
| ComfyUI 进程基础 | ~2 GB | 常驻 |
| **Sulphur 2 GGUF Q4/Q5 主模型** | ~10–14 GB | 仅在出视频片段时加载 |
| Sulphur prompt enhancer GGUF Q8 | ~1 GB | 短暂加载 |
| **Gemma 4 (Ollama)** | E4B Q4 约 3 GB / 26B Q4 约 16 GB | 按需，**与 Sulphur 不同时驻留** |
| GPT-SoVITS / whisper.cpp / FFmpeg | ~2–3 GB | 短暂加载 |
| 安全余量 | ≥ 4 GB | — |

> **结论**：M1 32GB 上**不要追求 FP8 (29GB)**，会和系统抢内存导致 swap。**默认走 GGUF Q4_K_M / Q5_K_M**。

### 9.2 默认参数档位（写进 config）

```python
HARDWARE_PROFILE = "m1_32gb"

DEFAULT_VIDEO = {
    "resolution": "1080p",     # 不上 2K/4K（M1 算力不够，单段会要 30+ 分钟）
    "duration_sec": 6,         # 默认 6s，剧本明确要求才上 12s；20s 不用
    "mode": "fast",            # Sulphur Fast 模式，不用 Pro
    "fps": 24,
}

DEFAULT_LLM = {
    "director":      "gemma4:e4b",   # ⚠️ 不用 26B（16GB 显存会和 ComfyUI 撞车）
    "scriptwriter":  "gemma4:e4b",
    "storyboarder":  "gemma4:e4b",
    "prompt_smith":  "gemma4:e4b",
    "qa":            "gemma4:e4b",   # 多模态 vision 也走 E4B
}

CONCURRENCY = 1                       # 串行；ComfyUI 与 Ollama 互斥调度
SCHEDULER = "exclusive_locks"         # 见 §9.4
```

### 9.3 性能预期（M1 32GB / GGUF Q4 / 1080p / 6s）

| 步骤 | 单次耗时（粗估） |
|---|---|
| Director / Script / Storyboard / Prompt 全 LLM 阶段 | 30s – 2 min |
| **单镜头视频生成（Sulphur 2，1080p 6s）** | **5 – 12 min** |
| TTS（GPT-SoVITS，10s 配音） | 10 – 30s |
| Whisper 字幕 | 5 – 15s |
| FFmpeg 合成 | 30s – 2 min |
| **整片（5 镜头 × 6s = 30s 成片）** | **约 30 – 70 min** |

> 这意味着：M1 32GB 上**目标产能 ≈ 每天 20–40 条 30 秒短视频**（24 小时无人值守跑批）。

### 9.4 关键调度规则（M1 专属，写进 Orchestrator）

1. **互斥锁**：`comfyui_lock` 与 `ollama_lock`，同一时刻只能持有一个。
   - 进入 ShotProducer 阶段前：先 `ollama stop gemma4:e4b`（或调 `/api/unload`）→ 再启 ComfyUI 推理；
   - 视频片段产出后：卸载 ComfyUI 模型 → 再启 LLM 跑 QA / 下一镜头规划。
2. **OOM 三级降档**（自动触发）：
   - 一级：`1080p 6s` → 失败 → 切 `720p 6s`；
   - 二级：`720p 6s` → 失败 → 切 GGUF 更低量化（Q4_K_S）；
   - 三级：仍失败 → 任务标记失败、不无限重试。
3. **Producer Loop 节流**：每生产 5 条主动 `vm_stat` 检查，必要时调用 `purge`（macOS 释放内存命令）+ 重启 ComfyUI 子进程，避免长跑泄漏。
4. **后台运行优化**：
   - 关 macOS App Nap（针对 ComfyUI 和 Ollama 进程：`caffeinate -dims python -m src.pipeline.producer_loop`）；
   - 调 ComfyUI 启动参数：`--lowvram --use-split-cross-attention --force-fp16`（M1 友好）。

### 9.5 你需要在 Mac 上做的一次性准备

我会在 M1 实现清单里写成自动化脚本，但先列清单：

```
# 1. 检查 ComfyUI 已启用 MPS（默认即可，无需改）
# 2. 升级 ollama（关键！）
brew upgrade ollama          # 必须 ≥ 0.20.3，修复了 Gemma 4 工具调用 bug
ollama pull gemma4:e4b
# 26B 不下载，省 16GB 磁盘

# 3. 下载 Sulphur 2 GGUF 整合（不下 BF16/FP8）
#    具体走第三方 sulphur2-install-notes 提供的 GGUF 整合包
#    或：ComfyUI Manager → 搜 "ComfyUI-GGUF" 安装节点 → 下载 LTX 2.3 Q4 GGUF + Sulphur LoRA

# 4. 安装 ComfyUI Manager + ComfyUI-GGUF + ComfyUI-LTXVideo 节点
# 5. 复制 Sulphur 2 仓库自带 workflows/ltx23_t2v distilled.json 到本项目 workflows/
# 6. brew install ffmpeg whisper-cpp
# 7. uv venv && uv pip install -r requirements.txt
```

---

## 10. 下一步

我马上做三件事（不再等你确认）：
1. 输出 `02-m1-implementation-checklist.md`：M1 阶段每个文件的函数签名、`pyproject.toml` 依赖列表、`workflows/` 注入约定、ComfyUI workflow JSON 占位符映射表、互斥锁实现；
2. 建立项目骨架（空文件 + 类型定义 + 配置 + Mac 一次性准备脚本）；
3. 第一行可执行代码：`comfyui.py` + `cli.py` 跑通"输入一句话 → 出一段 6 秒 1080p Sulphur 2 视频"。
