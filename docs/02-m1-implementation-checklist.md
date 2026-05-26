# M1 阶段实现清单（单镜头打通）

> 配套文档：`01-architecture-design.md` v1.0
> 硬件：Mac M1 / 32GB
> 目标：**`python -m src.cli shot --prompt "..."` → 输出一段 6s 1080p Sulphur 2 视频**
> 工期：2-3 天

---

## 0. M1 验收标准（Definition of Done）

- [x] 项目骨架完整，`uv sync` 一键装齐依赖
- [x] `scripts/check_env.py` 自检通过：ComfyUI 可达 / Ollama 可达 / Gemma 4 模型在线 / FFmpeg 在线
- [x] `python -m src.cli shot --prompt "a foggy mountain at dawn, cinematic"` 产出 `output/<date>/<task_id>/shots/01.mp4` ✅ **2026-05-27 跑通**：`--no-use-llm --resolution 720p --duration 2` → 720p × 48f → ~15.5 分钟（M1 32GB）
- [ ] `python -m src.cli shot --prompt "..." --use-llm` 走完 PromptSmith（Gemma 4 E4B）→ Sulphur enhancer → ComfyUI 全链路 ⏳ **待验**
- [x] 互斥锁生效：日志中能看到 `[scheduler] acquired comfyui` ✅ **已观察**（实际实现里 release 是隐式的）
- [ ] OOM 时自动降档（1080p → 720p）有日志体现 ⏳ **未触发**（首次冒烟用 720p 安全参数没踩到边界）

> 📍 **2026-05-27 进度备忘**：M1 核心链路已通。剩余两条 DoD（`--use-llm` 全链路 / OOM 降档）可在 M2 之前补做，也可以放进 M2 一起跑（M2 本来就要跑完整 LLM 链）。
>
> **原始 DoD 要求** "12 分钟内产出 6s 1080p 视频" —— **这条在 M1 32GB 上不现实**，实测 1080p 极易 OOM。修正后的实际可达档位：**720p × 2s ≈ 15 min**（单镜头）。1080p 留给后续有更强机器时验证。

---

## 1. 文件清单与函数签名

### 1.1 `pyproject.toml`（依赖锁定）

```toml
[project]
name = "agents-video-pipeline"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",            # ComfyUI HTTP
    "websockets>=12.0",       # ComfyUI WS 进度
    "pydantic>=2.7",          # schema
    "pydantic-settings>=2.3", # config
    "loguru>=0.7",            # 日志
    "typer>=0.12",            # CLI
    "rich>=13.7",             # 终端 UI
    "ollama>=0.3",            # Ollama Python client
    "llama-cpp-python>=0.3",  # 跑 sulphur_prompt_enhancer.gguf
    "ffmpeg-python>=0.2",
    "anyio>=4.4",
    "tenacity>=8.5",          # 重试
]

[project.optional-dependencies]
m2-plus = [                   # M2 之后才需要
    "langgraph>=0.2",
    "langchain>=0.3",
    "fastapi>=0.115",
    "uvicorn>=0.30",
]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "ruff>=0.5"]

[tool.ruff]
line-length = 110
target-version = "py311"
```

### 1.2 `src/config.py`

```python
class Settings(BaseSettings):
    # 路径
    project_root: Path
    workflows_dir: Path
    models_dir: Path
    output_dir: Path

    # ComfyUI
    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_workflow_t2v: str = "sulphur2_t2v.json"
    comfyui_workflow_i2v: str = "sulphur2_i2v.json"
    comfyui_client_id: str          # 启动时随机生成

    # Ollama
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model_default: str = "gemma4:e4b"   # ⚠️ M1 32GB 不用 26B

    # Sulphur prompt enhancer
    sulphur_enhancer_gguf: Path     # models/sulphur_prompt_enhancer_model-q8_0.gguf

    # 视频默认参数（M1 档）
    default_resolution: Literal["1080p", "720p"] = "1080p"
    default_duration_sec: Literal[6, 12] = 6
    default_fps: int = 24
    default_mode: Literal["fast", "pro"] = "fast"

    # 调度
    hardware_profile: Literal["m1_32gb", "generic"] = "m1_32gb"
    enable_mutex_locks: bool = True
    oom_fallback_chain: list[str] = ["1080p", "720p"]

def load_settings() -> Settings: ...
```

### 1.3 `src/orchestrator/state.py`（M1 只用 ShotState）

```python
class ShotState(BaseModel):
    idx: int = 1
    visual_intent: str = ""
    duration_sec: int = 6
    resolution: str = "1080p"
    raw_prompt: str
    enhanced_prompt: str | None = None
    negative_prompt: str = ""
    seed: int | None = None
    mode: str = "fast"
    clip_path: Path | None = None
    retry: int = 0
    errors: list[str] = []
```

### 1.4 `src/utils/locks.py` —— **M1 核心：互斥锁**

```python
class HardwareScheduler:
    """ComfyUI 与 Ollama 互斥占用统一内存。"""
    def __init__(self, ollama_client, comfyui_client): ...

    @asynccontextmanager
    async def acquire_comfyui(self):
        """进入前先 unload Ollama 模型，退出后不主动加载（按需）。"""
        await self._unload_ollama()
        try: yield
        finally: pass

    @asynccontextmanager
    async def acquire_ollama(self, model: str):
        """进入前先释放 ComfyUI 显存，退出后保留 Ollama 模型常驻直到下次切换。"""
        await self._free_comfyui()
        try: yield
        finally: pass

    async def _unload_ollama(self): ...   # POST /api/generate keep_alive=0
    async def _free_comfyui(self): ...    # POST /free {"unload_models": true, "free_memory": true}
```

### 1.5 `src/adapters/llm.py`

```python
class LLMProvider:
    backend: Literal["ollama", "lmstudio"]
    model: str
    async def chat(self, messages: list[dict], **kwargs) -> str: ...
    async def chat_json(self, messages: list[dict], schema: type[BaseModel]) -> BaseModel: ...
    async def chat_with_tools(self, messages, tools: list[dict]) -> dict: ...

def make_llm(settings: Settings, role: str = "default") -> LLMProvider: ...
```

### 1.6 `src/adapters/sulphur_enhancer.py`

```python
class SulphurPromptEnhancer:
    def __init__(self, gguf_path: Path, n_ctx: int = 4096): ...
    async def enhance(self, raw_prompt: str, target_duration: int = 6) -> str: ...
    async def close(self): ...
```

### 1.7 `src/adapters/comfyui.py` —— **M1 重头戏**

```python
class ComfyUIClient:
    def __init__(self, base_url: str, client_id: str): ...

    async def health(self) -> bool: ...
    async def free_memory(self, unload_models=True, free_memory=True) -> None: ...

    async def submit_workflow(self, workflow: dict) -> str:
        """POST /prompt → prompt_id"""

    async def wait_for_completion(self, prompt_id: str, timeout: float = 1800) -> dict:
        """WS 监听 status/progress/executed/execution_error，返回 history[prompt_id]"""

    async def fetch_output_video(self, history: dict, save_to: Path) -> Path:
        """从 history 中找到 SaveVideo 节点输出，GET /view 下载到 save_to"""

class SulphurT2VRunner:
    """对 Sulphur 2 T2V workflow 的高层封装，负责：占位符注入 + OOM 降档"""
    def __init__(self, comfy: ComfyUIClient, workflow_template: dict, mapping: NodeMapping): ...

    async def run(
        self,
        prompt: str,
        negative_prompt: str = "",
        duration_sec: int = 6,
        resolution: str = "1080p",
        seed: int | None = None,
        mode: str = "fast",
        save_to: Path = ...,
    ) -> Path: ...
    # 内部：按 settings.oom_fallback_chain 在 OOM 时自动降档重试
```

### 1.8 `src/adapters/workflow_template.py` —— **占位符注入**

```python
class NodeMapping(BaseModel):
    """workflows/_placeholders.md 中约定的节点 ID 映射"""
    positive_prompt_node: str       # e.g. "6"
    negative_prompt_node: str       # e.g. "7"
    sampler_node: str               # 用于注入 seed
    empty_latent_node: str          # 用于注入 width/height/frames
    save_video_node: str            # 用于读取输出文件名

def inject_t2v_params(
    workflow: dict,
    mapping: NodeMapping,
    *,
    positive: str,
    negative: str,
    width: int,
    height: int,
    num_frames: int,
    seed: int,
    fps: int = 24,
) -> dict: ...

def load_workflow(path: Path) -> dict: ...
def parse_resolution(r: str) -> tuple[int, int]: ...   # "1080p" → (1920, 1080)
def duration_to_frames(sec: int, fps: int = 24) -> int: ...
```

### 1.9 `src/agents/prompt_smith.py`（M1 的最简 Agent）

```python
SYSTEM_PROMPT = """You are a cinematic prompt engineer for Sulphur 2 (LTX-Video 2.3).
Convert user intent into:
- positive_prompt: rich English, 50-120 words, include subject, action, camera, lighting, style.
- negative_prompt: short, common artifacts.
Output strict JSON: {"positive_prompt": "...", "negative_prompt": "..."}"""

class PromptSmithOutput(BaseModel):
    positive_prompt: str
    negative_prompt: str

async def run_prompt_smith(
    raw_intent: str,
    llm: LLMProvider,
    enhancer: SulphurPromptEnhancer | None = None,
) -> PromptSmithOutput: ...
```

### 1.10 `src/cli.py`

```python
app = typer.Typer()

@app.command()
def shot(
    prompt: str = typer.Option(..., help="原始意图，中文/英文均可"),
    duration: int = typer.Option(6),
    resolution: str = typer.Option("1080p"),
    use_llm: bool = typer.Option(True, help="是否走 PromptSmith → enhancer 链路"),
    out: Path = typer.Option(None),
):
    """M1: 单镜头出片"""
    asyncio.run(_shot_cmd(...))

@app.command()
def env():
    """检查环境（ComfyUI / Ollama / FFmpeg / models）"""
    asyncio.run(_env_check())

if __name__ == "__main__":
    app()
```

### 1.11 `scripts/check_env.py`

```python
async def main():
    # 1. ComfyUI /system_stats
    # 2. Ollama /api/tags 含 gemma4:e4b
    # 3. ffmpeg --version
    # 4. workflows/sulphur2_t2v.json 存在
    # 5. models/sulphur_prompt_enhancer_*.gguf 存在
    # 6. 输出 ✅/❌ 表格
```

### 1.12 `scripts/setup_mac.sh`（一次性 Mac 准备）

```bash
#!/usr/bin/env bash
set -e
brew install ffmpeg uv
brew upgrade ollama || brew install ollama
ollama --version  # 校验 ≥ 0.20.3
ollama pull gemma4:e4b
echo "请手动完成："
echo "  1. ComfyUI 安装 ComfyUI-Manager / ComfyUI-GGUF / ComfyUI-LTXVideo 节点"
echo "  2. 下载 Sulphur 2 GGUF 整合包到 ComfyUI/models/"
echo "  3. 复制 Sulphur 2 仓库的 workflows/ltx23_t2v\\ distilled.json 到 workflows/sulphur2_t2v.json"
echo "  4. 下载 sulphur_prompt_enhancer_model-q8_0.gguf 到 models/"
```

### 1.13 `workflows/_placeholders.md`

文档化"我们的代码会注入哪些节点"，让你（人工）打开 ComfyUI workflow JSON 把节点 ID 填到 `config.yaml` 里——**避免代码写死节点 ID**（不同 workflow 版本节点 ID 会变）。

---

## 2. M1 执行调用链（一次 `cli shot` 走完）

```
cli.shot
  └─ load_settings()
  └─ HardwareScheduler(ollama, comfy)
  └─ if use_llm:
        async with scheduler.acquire_ollama("gemma4:e4b"):
            llm = make_llm(settings, "prompt_smith")
            ps_out = await run_prompt_smith(prompt, llm, enhancer)
     else:
        ps_out = PromptSmithOutput(positive=prompt, negative="")
  └─ async with scheduler.acquire_comfyui():
        runner = SulphurT2VRunner(comfy, workflow, mapping)
        clip_path = await runner.run(
            ps_out.positive_prompt,
            ps_out.negative_prompt,
            duration_sec=duration,
            resolution=resolution,
            save_to=output/<date>/<task_id>/shots/01.mp4,
        )
  └─ 终端打印 clip_path + 元信息（耗时、最终分辨率、是否降档）
```

---

## 3. ComfyUI Workflow 节点 ID 映射（人工填一次）

> Sulphur 2 仓库 `workflows/ltx23_t2v distilled.json` 的实际节点 ID 因版本而异。**M1 第一步**：你打开 ComfyUI Web UI 加载该 workflow，把 5 个节点的 ID 抄给我，写入 `config/node_mapping.yaml`：
>
> ```yaml
> sulphur2_t2v:
>   positive_prompt_node: "6"
>   negative_prompt_node: "7"
>   sampler_node: "..."        # KSampler / LTXSampler 节点的 ID
>   empty_latent_node: "..."   # EmptyLTXLatentVideo / EmptyLatentVideo 的 ID
>   save_video_node: "..."     # SaveVideo / VHS_VideoCombine 的 ID
> ```

代码读这个 yaml，**永不写死节点 ID**。

---

## 4. M1 完成后立即能做的事

- 命令行 `python -m src.cli shot --prompt "..."` 出片
- 切 720p：`--resolution 720p`（验证降档）
- 关掉 LLM：`--no-use-llm`（隔离 ComfyUI 链路调试）
- 跑批 5 条：`for p in topics; do cli shot --prompt "$p"; done`（验证互斥锁不死锁）

---

## 5. 进入 M2 的入口

M1 把"**单镜头工厂**"这一格做扎实。M2 的工作是把它套进 LangGraph 多节点状态机，加上 Director/Scriptwriter/Storyboarder 三个 LLM Agent + I2V 链式 + 配音 + 字幕 + FFmpeg 合成。
