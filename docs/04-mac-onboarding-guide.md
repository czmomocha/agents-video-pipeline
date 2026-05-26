# Mac 本机联调验证指南（小白友好版）

> **适用对象**：ComfyUI 完全陌生的项目使用者
> **目标**：把 GitHub 上的代码 clone 下来，一步一步把整个视频生产线跑通
> **预计耗时**：3-5 小时（其中模型下载约 2 小时，看你网速）
> **硬件**：Mac M1 / 32GB 统一内存（其他 Apple Silicon 机型类似）
> **配套版本**：v0.4.0-talkie

---

## 阅读前先打个招呼

这份文档把整个联调过程拆成 **8 个阶段**，每个阶段独立可验证。**不要跳步**，每完成一步就测一次，发现问题立刻停下定位（而不是一口气装完所有东西最后发现哪儿都不对）。

文档约定：

- 命令前的 `$` 表示在 Mac 终端里跑，不要把 `$` 复制进去
- ✅ = 应该看到的现象（成功标志）
- ❌ = 失败标志，遇到时去对应的"故障排查"段
- 💡 = 小白容易踩的坑，提前提醒
- ⏸️ = 这一步耗时较久，可以去喝杯水

每个阶段结尾有一个**验收标准**，过了再进下一阶段。

---

## 阶段 0：搞清楚你要做什么

整套系统的工作方式，用大白话说一遍：

```
你说一句话
    │
    ▼
"导演" Agent 用 LLM 写出整片计划（标题/风格/镜头数）
    │
    ▼
"编剧" Agent 写出每个镜头的中文配音稿
    │
    ▼
"分镜师" Agent 决定每个镜头怎么拍（运镜、是否延续上一镜头画面）
    │
    ▼
"提示词工程师" Agent 把每个镜头翻译成英文 prompt
    │
    ▼
"摄影师" Agent → ComfyUI 用 Sulphur 2 模型逐镜头出视频片段
    │
    ▼
"配音师" Agent → Piper TTS 给每个镜头配中文旁白
    │
    ▼
"字幕师" Agent → Whisper.cpp 给配音生成字幕
    │
    ▼
"剪辑师" Agent → FFmpeg 把所有片段拼成最终视频
    │
    ▼
   final.mp4 （含画面 + 配音 + 字幕）
```

整个流程**全部跑在你 Mac 本地**，不联网（Piper 默认本地模式下）。

我们要做的事情，就是**把这条流水线上每一环都装起来、连起来、测一遍**。

---

## 阶段 1：基础工具准备（约 30 分钟）

> 目标：装齐 git / Python 3.11 / Homebrew / ffmpeg / ollama / uv

### 1.1 检查 Mac 是否已经有 Homebrew

```bash
$ which brew
```

✅ 输出 `/opt/homebrew/bin/brew` 之类 → 跳到 1.2
❌ 提示 `not found` → 执行：

```bash
$ /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

装完按提示把 brew 加入 PATH（终端会告诉你具体命令）。

### 1.2 装齐工具链

```bash
$ brew install git ffmpeg uv ollama whisper-cpp
```

每个工具的作用：

| 工具 | 作用 | 体积 |
|---|---|---|
| `git` | 拉代码 | ~50MB |
| `ffmpeg` | 视频/音频处理 | ~80MB |
| `uv` | Python 包管理（比 pip 快 10 倍） | ~30MB |
| `ollama` | 本地 LLM 推理（Gemma 4 跑这个上面） | ~200MB |
| `whisper-cpp` | 本地语音识别（生成字幕）| ~10MB |
| | **注意**：Homebrew 包名是 `whisper-cpp`，但安装后的命令是 `whisper-cli` | |

### 1.2.1 安装 Piper TTS（中文配音）

Piper TTS 需要通过 pip 安装（不是 Homebrew）：

```bash
$ pip install piper-tts
```

或者如果你已经创建了虚拟环境：

```bash
$ source .venv/bin/activate
$ pip install piper-tts
```

### 1.3 验证

```bash
$ git --version            # ✅ 应该 ≥ 2.30
$ ffmpeg -version          # ✅ 应该 ≥ 6.0
$ uv --version             # ✅ ≥ 0.4.0
$ ollama --version         # ⚠️ 必须 ≥ 0.20.3（修复了 Gemma 4 工具调用 bug）
$ whisper-cli --help       # ✅ 输出帮助
$ piper --help             # ✅ 输出帮助
```

❌ ollama 版本不够：`brew upgrade ollama`

❌ `whisper-cli` 命令不存在但 `main` 命令存在：旧版 whisper-cpp，**继续就行**，代码会自动探测两种命令名

### 验收

```bash
$ which git ffmpeg uv ollama whisper-cli piper
```

应该 6 行都有路径输出。否则去对应工具单独排查。

---

## 阶段 2：拉代码 + Python 环境（约 5 分钟）

### 2.1 选一个目录放项目

```bash
$ mkdir -p ~/projects && cd ~/projects
```

> 💡 不要放在桌面或下载里，模型文件几百 MB，放 Time Machine 备份目录会浪费空间。

### 2.2 克隆仓库

```bash
$ git clone https://github.com/czmomocha/agents-video-pipeline.git
$ cd agents-video-pipeline
```

✅ 看到一堆文件，包括 `README.md`、`pyproject.toml`、`src/`、`docs/`

### 2.3 创建 Python 虚拟环境并装依赖

```bash
$ uv venv --python 3.11
$ uv sync
```

> ⏸️ 第一次会下载并构建 `llama-cpp-python`，大约 2-5 分钟。喝口水。

✅ 最后看到 `Resolved N packages` + `Installed N packages`

❌ 装 `llama-cpp-python` 失败：可以临时编辑 `pyproject.toml`，把 `"llama-cpp-python>=0.3"` 这一行注释掉再 `uv sync`（这个库只用于 Sulphur prompt enhancer，是可选功能）

### 2.4 验证 Python 环境

```bash
$ source .venv/bin/activate     # 激活虚拟环境
$ python --version              # ✅ 3.11.x
$ python -c "import langgraph; from importlib.metadata import version; print('langgraph', version('langgraph'))"   # ✅ 输出版本号
```

> 💡 **以后所有命令都要在激活的虚拟环境里跑**。每次新开终端记得 `cd ~/projects/agents-video-pipeline && source .venv/bin/activate`

### 2.5 跑单元测试（不需要任何外部服务）

```bash
$ pytest tests/ -v
```

✅ 应该看到约 27 项测试**全绿**。**这一步如果不通过，说明代码本身有问题，立刻贴日志给我**。

### 验收

```bash
$ python -m src.cli --help
```

✅ 看到 7 个子命令：`env / plan / script / storyboard / plan-and-prompts / shot / render`

---

## 阶段 3：Ollama + Gemma 4 准备（约 30 分钟）

> 目标：让 LLM 能用，跑通"导演 / 编剧 / 分镜师 / 提示词工程师"四个 Agent

### 3.1 启动 Ollama 服务

```bash
$ ollama serve &
```

或者直接打开 macOS 上的 Ollama.app（菜单栏会有羊驼图标）。

✅ 浏览器访问 http://localhost:11434/ 看到 `Ollama is running`

### 3.2 下载 Gemma 4 E4B 模型

```bash
$ ollama pull gemma4:e4b
```

> ⏸️ 模型大约 5-6 GB，下载 10-30 分钟（看网速）。
>
> 💡 **不要下 26B 版本**！26B 需要 16GB 显存，会和 ComfyUI 抢内存导致 swap。E4B 已经够用。

### 3.3 测试一下 LLM 是否工作

```bash
$ ollama run gemma4:e4b "用一句话介绍中国茶文化"
```

✅ 应该几秒内输出一段中文。按 `Ctrl-D` 退出交互。

### 3.4 用项目代码测试

```bash
$ python -m src.cli plan -t "中国茶文化的一天" -d 30 --out /tmp/test_plan.json
```

> ⏸️ 这一步会调一次 Gemma 4，约 30 秒-2 分钟。

✅ 终端会打印一张 Rich 表格，标题是 `ProductionPlan — XXX`，里面有 logline / mood / shots / 风格锁等字段。文件保存到 `/tmp/test_plan.json`。

❌ 报错 `ConnectionError`：Ollama 服务没起来，回去 3.1
❌ 报错 `model not found`：3.2 没拉模型成功，重跑
❌ JSON 解析失败：Ollama 版本太老，`brew upgrade ollama`

### 3.5 跑完整规划链（不需要 ComfyUI）

```bash
$ python -m src.cli plan-and-prompts -t "雨夜便利店里的相遇" --save /tmp/state.json
```

> ⏸️ 这一步会连续调 4 次 Gemma 4，大约 2-5 分钟。

✅ 终端依次打印：
- ProductionPlan 表
- Script 表（5 个中文配音稿场景）
- Storyboard 表（5 个镜头 + 镜头类型/运镜/转场/I2V 标记）
- 5 段英文 PromptSmith 输出

### 验收

✅ `cli plan-and-prompts` 完整跑通，**没有任何 ❌ 报错**
✅ `/tmp/state.json` 文件存在，能用 `cat /tmp/state.json | head -50` 看到完整 JSON

如果这阶段不通过，**项目的 LLM 链路就有问题**，**先停下来**找我处理。

---

## 阶段 4：ComfyUI 安装与启动（约 30-60 分钟）

> 目标：把 ComfyUI 和 Sulphur 2 模型装好，能在浏览器里出一段视频
> 这是整个过程**最容易踩坑**的阶段，请耐心。

### 4.0 选择 ComfyUI 安装方式（推荐 CLI 源码版）

**结论：本项目强烈推荐使用 CLI 源码版（`git clone`）。如果你已经装过 ComfyUI.dmg 桌面版，不用卸载，但接下来请按 4.1 起从源码版重新装一份，并且**不要让两个 ComfyUI 同时跑**。

#### 为什么本项目要用 CLI 源码版

| 维度 | ComfyUI.dmg 桌面版 | CLI 源码版 ⭐ |
|---|---|---|
| 模型目录位置 | App 沙盒/隐藏路径，难定位 | `~/AI/ComfyUI/models/` 清晰可控 |
| 放置 Sulphur 2 等大模型 | 要先找到隐藏路径，容易踩坑 | 直接 `cp` 到 `models/checkpoints/` |
| 自定义节点（LTXVideo / GGUF 等） | 部分版本兼容性不完整 | `git clone` 到 `custom_nodes/`，标准做法 |
| HTTP API | 不一定默认开启，端口可能不同 | 标准 `python main.py`，稳定监听 8188 |
| 调试 | App 内 Python 不可见，崩了只能重启 | 终端能看到完整 traceback |
| Python 环境 | App 自带，可能跟项目 venv 冲突 | 独立 venv，零污染 |
| 与本项目对接 | 节点 ID 映射、workflow JSON 路径都要现摸 | 文档/代码都按源码版写，零适配 |

**简单说**：阶段 5（节点 ID 映射）和阶段 6（端到端跑通）的所有路径假设都是基于源码版的，桌面版会让你多踩一倍坑。

#### 已经装过 ComfyUI.dmg 怎么办？

不需要卸载，桌面版可以保留（将来玩 AI 绘画还能用），只要满足两条：

1. **联调本项目时，关掉 ComfyUI.dmg**，避免占用 8188 端口
2. **不要两个 ComfyUI 同时加载大模型**（M1 32GB 统一内存吃不消，会 swap/OOM）

```text
联调期间只开一个 ComfyUI：
- 要么只开桌面版（不推荐用于本项目）
- 要么只开源码版（推荐）
绝对不要两个一起开
```

#### 如何确认桌面版没在占用端口

如果你不确定 ComfyUI.dmg 是否还在跑，先关掉它，然后跑：

```bash
$ lsof -i :8188
```

- 没有任何输出 → 端口空闲，可以放心继续 4.1 装源码版
- 有 `ComfyUI` / `Electron` 等进程 → 先在 Dock / 任务管理器里关掉

#### 端口冲突的兜底方案

如果由于某些原因你必须让源码版跑在别的端口（例如 8189）：

```bash
$ python main.py --listen 127.0.0.1 --port 8189
```

并在项目根目录的 `.env` 里加：

```bash
AGENTS_COMFYUI_BASE_URL=http://127.0.0.1:8189
```

> 但默认情况下，请直接让源码版用 **8188**，文档里所有命令都是基于这个端口的。

#### 接下来

请直接进入 **4.1 决定 ComfyUI 装在哪**，按 4.1 → 4.4 装源码版。装完后再回头跑 `python -m src.cli env`，那时 `ComfyUI` 那行才应该 ✅。

### 4.1 决定 ComfyUI 装在哪

ComfyUI 是一个独立项目，**不放在我们这个仓库里面**。建议：

```bash
$ mkdir -p ~/AI && cd ~/AI
```

### 4.2 克隆 ComfyUI

```bash
$ git clone https://github.com/comfyanonymous/ComfyUI.git
$ cd ComfyUI
```

### 4.3 装 ComfyUI 的 Python 依赖

ComfyUI 有自己独立的 Python 环境（**不要和我们项目的 `.venv` 混用**，依赖完全不同）。

#### ⚠️ 这一步极易踩 3 个坑，先看再做

| 坑 | 现象 | 根因 |
|---|---|---|
| 1. 没有 `python3.11` 命令 | `zsh: command not found: python3.11` | macOS 系统不自带，本项目用 `uv` 管 Python |
| 2. venv 里没 pip | 后续 `pip install` 跑去 conda base 装了 | `uv venv` 默认不安装 pip，需要 `--seed` |
| 3. conda base 默认激活 | 装出来的 torch 在 `~/miniconda3/...` 不在 venv | 激活 venv 前 conda base 还在，`pip` 走了 conda 的 |

下面这套命令把三个坑全绕过。**严格按顺序执行**：

```bash
# 第 1 步：彻底退出 conda base（即使没用 conda 也跑一下，无害）
$ conda deactivate 2>/dev/null
$ conda deactivate 2>/dev/null    # 跑两次确保 base 也退掉

# 第 2 步：用 uv 建 venv（--seed 关键，会把 pip 装进去）
$ cd ~/AI/ComfyUI         # 或你 git clone 的实际路径
$ uv venv --python 3.11 --seed venv

# 第 3 步：激活
$ source venv/bin/activate

# 第 4 步：验证 pip 真的来自 venv（关键防坑！）
$ which pip
# ✅ 应输出: <你的ComfyUI路径>/venv/bin/pip
# ❌ 如果输出 /Users/<你>/miniconda3/bin/pip 或别的，停下来不要继续装
```

> 💡 没有 `uv`？项目根目录已经装过了，直接用 `~/.local/bin/uv` 也行；或 `brew install uv`。

#### 安装依赖

确认 `which pip` 指向 venv 之后：

```bash
# 用 python -m pip 强制走 venv 的 pip（双保险）
$ python -m pip install --upgrade pip
$ python -m pip install -r requirements.txt
```

> ⏸️ 装 PyTorch 大约 5-10 分钟。
>
> 💡 macOS 12+ 系统上 PyTorch 会自动启用 MPS（Metal）后端，无需配置。

#### 装完立刻验证（必须！）

```bash
$ python -c "import torch; print('torch', torch.__version__); print('  at', torch.__file__)"
```

✅ **预期输出**：

```text
torch 2.x.x
  at /Users/<你>/Desktop/AI/ComfyUI/venv/lib/python3.11/site-packages/torch/__init__.py
```

❌ **如果 `at` 路径里出现 `miniconda3`**：说明依赖被装到 conda base 了（典型坑 3）。修复方式：

```bash
# 1) 在 conda base 里清掉误装的污染
$ conda activate base
$ pip uninstall -y torch torchvision torchaudio transformers tokenizers \
    safetensors aiohttp einops kornia kornia_rs av blake3 spandrel \
    torchsde pydantic-settings simpleeval comfyui-embedded-docs \
    comfyui-workflow-templates comfyui-workflow-templates-core \
    comfyui-workflow-templates-media-api comfyui-workflow-templates-media-image \
    comfyui-workflow-templates-media-other comfyui-workflow-templates-media-video
$ conda deactivate

# 2) 回到 4.3 第 1 步重做（这次照顺序走，别跳）
```

> ⚠️ 上述卸载列表只针对"被 ComfyUI requirements.txt 误装到 conda base"的场景。如果你 conda base 里有别的项目在用 `torch` / `transformers`，**先别清这两个**，留着也不影响 ComfyUI venv（venv 是隔离的）。

### 4.4 第一次启动 ComfyUI（无模型测试）

```bash
$ python main.py --listen 127.0.0.1 --port 8188
```

> ⏸️ 第一次启动会编译一些算子，大约 30 秒。

✅ 看到日志最后写 `Starting server` + `To see the GUI go to: http://127.0.0.1:8188`

浏览器打开 http://127.0.0.1:8188

✅ 看到 ComfyUI 的工作流界面（一堆方框节点 + 连线）

❌ 报错 `address already in use`：换个端口 `--port 8189`，记下端口号
❌ 启动卡住超过 5 分钟：Ctrl-C 杀掉重启
❌ 浏览器打不开：检查防火墙，或者 ComfyUI 监听的不是 127.0.0.1

**先把 ComfyUI 关掉**（终端按 Ctrl-C），下面要装关键插件。

### 4.5 装 ComfyUI Manager（图形化管插件）

ComfyUI Manager 是个"应用商店"，让你免去手动 git clone 各种自定义节点。

```bash
$ cd ~/AI/ComfyUI/custom_nodes
$ git clone https://github.com/ltdrdata/ComfyUI-Manager.git
```

### 4.6 装 LTX-Video 节点（**关键，Sulphur 2 跑这个上面**）

```bash
$ git clone https://github.com/Lightricks/ComfyUI-LTXVideo.git
```

回到 ComfyUI 根目录，重启服务：

```bash
$ cd ~/AI/ComfyUI
$ source venv/bin/activate
$ python main.py --listen 127.0.0.1 --port 8188 --use-split-cross-attention --force-fp16
```

> 💡 `--use-split-cross-attention --force-fp16` 是 M1 友好的启动参数，能省内存。

### 4.7 装 ComfyUI-GGUF 节点（让 Sulphur 2 GGUF 量化模型能加载）

刷新浏览器 http://127.0.0.1:8188

✅ 应该能看到右上角多了个 **Manager** 按钮

点 **Manager** → **Custom Nodes Manager** → 搜索框输入 `GGUF` → 点 **ComfyUI-GGUF** 旁边的 **Install**

✅ 安装完点 **Restart**（页面顶部会出现）

### 4.8 下载 Sulphur 2 模型

> ⏸️ 这是**最耗时的一步**，模型文件约 12 GB（GGUF Q4 量化版）。提前预留磁盘空间。

打开浏览器去 https://huggingface.co/SulphurAI/Sulphur-2-base/tree/main

下载这两个文件之一（任选一个，**别都下，没必要**）：

| 文件名 | 大小 | 推荐 |
|---|---|---|
| `sulphur_dev_fp8mixed.safetensors` | 29.2 GB | M1 32GB **吃力**，跑得动但会 swap |
| 第三方 GGUF Q4_K_M 整合版 | ~12 GB | ⭐ M1 32GB 推荐，速度可接受 |

**M1 32GB 强烈建议走 GGUF 路线**：搜索 "Sulphur-2 GGUF" 找第三方整合包，例如：
- https://huggingface.co/calcuis/sulphur-2-gguf （或 city96/Sulphur-2-gguf 等）
- 下载 `sulphur-2-Q4_K_M.gguf` 即可

下载完放到：

```bash
~/AI/ComfyUI/models/checkpoints/sulphur-2-Q4_K_M.gguf
# 如果你下的是 .safetensors:
~/AI/ComfyUI/models/checkpoints/sulphur_dev_fp8mixed.safetensors
```

> 💡 GGUF 文件需要放 `models/checkpoints/` 还是 `models/unet/`？看下载页面说明；如果不确定**两个目录都各放一个软链**：
> ```bash
> $ ln -s ../checkpoints/sulphur-2-Q4_K_M.gguf ~/AI/ComfyUI/models/unet/
> ```

### 4.9 第一次在 ComfyUI 里出图（验证安装）

> 这一步是为了让你确认 ComfyUI 本身能用，**不涉及我们项目代码**。

ComfyUI 已经有内置的 LTX-Video 工作流模板：

1. 浏览器 ComfyUI 页面顶部菜单：**Workflow → Browse Templates → Video → LTX-Video Text-to-Video**
2. 点击载入
3. 在画布上找到 **CheckpointLoaderSimple** 或 **UnetLoaderGGUF** 节点（看你装的是哪种模型）
4. 把 ckpt_name 改成你下载的 Sulphur 2 文件
5. 在正向 prompt 框输入：`a foggy mountain at dawn, cinematic, slow camera dolly`
6. 点页面右上角 **Queue Prompt** 按钮

> ⏸️ 第一次出 6 秒视频大约需要 **5-15 分钟**（取决于分辨率）。期间风扇会狂转。

✅ 队列跑完，画布最后一个节点（VHS_VideoCombine 或 SaveAnimatedWEBP）会显示一个视频预览

❌ OOM：把分辨率改小（768×432），帧数改少（24f = 1 秒）
❌ 节点报红：Manager → Install Missing Custom Nodes
❌ 模型加载失败：检查文件路径是否对

> 📍 **实战补丁（2026-05 M1 32GB 实测）**
>
> 第一次跑通用的极小参数：**512×320 × 49 帧（约 2 秒）× 30 步**，约 **3-5 分钟出片**。这是 M1 32GB 最稳的"基线参数"，**第一次画布跑通就用这一组**，别贪 1080p。
>
> 注意：ComfyUI 内置的 LTX-Video 模板默认正向 prompt 是英文示例（如 "A compact modern delivery drone..."），**别忘了改成你自己的 prompt** 再 Queue Prompt，否则你只是验证了模板默认值。
>
> 工作流里 LTXV 系列的 latent 节点（`EmptyLTXVLatentVideo`）用的帧数键名是 **`length`**（不是 `num_frames` 或 `video_length`），项目代码已兼容这个键名。

### 4.10 把 workflow 保存为我们项目要用的格式

ComfyUI 工作流分两种导出：
- **普通保存**（`Save`）：UI 用的格式，**不能用**
- **API 格式保存**（`Save (API Format)`）：代码调用要的格式 ⭐

操作：
1. ComfyUI 页面菜单：**Workflow → Export (API)**
2. 浏览器会下载一个 JSON 文件（默认名 `workflow_api.json`）
3. 把它**复制**到我们项目的 workflows 目录：

```bash
$ cp ~/Downloads/workflow_api.json ~/projects/agents-video-pipeline/workflows/sulphur2_t2v.json
```

> 💡 注意是 `cp`（复制），不要 `mv`（剪切）。原文件留着以后改 workflow。

> 📍 **实战补丁（2026-05 M1 32GB 实测）**
>
> **强烈建议两份 JSON 都导一份**，分工明确：
>
> | 导出方式 | 用途 | 建议文件名 |
> |---|---|---|
> | `Workflow → Export` | 含画布坐标，给人看/继续在 ComfyUI 里调试 | `ltxv_t2v_milestone.json` |
> | `Workflow → Export (API Format)` | **项目代码用**（POST 给 `/prompt` 接口的就是这个）| `ltxv_t2v_milestone_api.json` → 复制成 `sulphur2_t2v.json` |
>
> ⚠️ **API Format 这个菜单项默认隐藏！** 必须先在 ComfyUI 设置里勾 **`Enable Dev mode Options`**，`Workflow` 菜单才会多出 `Export (API Format)` 这一项。
>
> 两份 JSON 结构**完全不一样**，普通格式带 UI 元信息（`nodes`/`links`/`groups` 顶层数组），API 格式顶层就是 `node_id → node_def` 的扁平 map。**只有 API 格式能让 ComfyUI 服务端 `/prompt` 接口收**。
>
> 校验导出的是不是 API 格式：
> ```bash
> $ python3 -c "import json; wf=json.load(open('workflows/sulphur2_t2v.json')); print('nodes:', len(wf), '| keys[0]:', list(wf.keys())[0])"
> # ✅ API 格式应输出类似: nodes: 13 | keys[0]: 6   (顶层就是节点 ID)
> # ❌ 错的会输出: keys[0]: nodes 之类，或顶层是数组
> ```

### 4.11 同样导出 I2V 工作流（图生视频，可选但推荐）

I2V 工作流让镜头之间画面延续。如果你这次只想先跑通基础链，可以**跳过 I2V**（后续 `cli render` 加 `--no-i2v` 参数就行）。

如果要做 I2V：

1. ComfyUI 菜单：**Workflow → Browse Templates → Video → LTX-Video Image-to-Video**
2. 替换模型为 Sulphur 2
3. 测试出一段 I2V（随便丢张图试试）
4. 跑通后 **Workflow → Export (API)** → `workflow_api_i2v.json`
5. 复制到项目：

```bash
$ cp ~/Downloads/workflow_api_i2v.json ~/projects/agents-video-pipeline/workflows/sulphur2_i2v.json
```

### 验收

✅ ComfyUI 在 http://127.0.0.1:8188 运行
✅ 在 ComfyUI 里手动出过至少 1 段 6 秒的 Sulphur 2 视频
✅ `~/projects/agents-video-pipeline/workflows/sulphur2_t2v.json` 文件存在
（可选）✅ `~/projects/agents-video-pipeline/workflows/sulphur2_i2v.json` 文件存在

**ComfyUI 保持开着**，下面的步骤都需要它在线。

---

## 阶段 5：配置节点 ID 映射（约 10 分钟）

> 目标：让我们的 Python 代码知道你导出的 workflow 里哪个节点是"正向 prompt"、哪个是"采样器"……

这是**必须人工做一次**的工作。我们的代码不写死节点 ID（每次 ComfyUI 升级 ID 都会变），所以由你把"角色 → ID"的映射告诉代码。

### 5.1 在 ComfyUI 里打开节点编号显示

ComfyUI 浏览器界面：右上角齿轮图标 ⚙ → **Settings** → 搜索 `node ID` → 勾选 **Show Node IDs**

### 5.2 重新加载你刚才导出的 T2V workflow

菜单：**Workflow → Open** → 选 `~/Downloads/workflow_api.json`（或者直接拖进画布）

✅ 现在每个节点的左上角都显示一个数字（如 `#6`、`#42`）

### 5.3 找出 5 个关键节点的 ID

逐个找下面 5 个节点，**抄下它们的 ID**：

| 你要找的节点 | 可能显示为 | 作用 |
|---|---|---|
| **正向 prompt** | `CLIPTextEncode`，连到 sampler 的 `positive` 输入 | 注入正向提示词 |
| **负向 prompt** | `CLIPTextEncode`，连到 sampler 的 `negative` 输入 | 注入负向提示词 |
| **采样器** | `KSampler` / `SamplerCustom` / `LTXVideoSampler` | 注入 seed |
| **空 latent / 视频尺寸** | `EmptyLTXLatentVideo` / `EmptyLatentVideo` / `EmptyLatentImage` | 注入 width/height/帧数 |
| **保存视频** | `VHS_VideoCombine` / `SaveVideo` / `SaveAnimatedWEBP` | 读取输出文件名 |

> 💡 **怎么区分两个 CLIPTextEncode 哪个是正向哪个是负向？**
> 看它的输出连到 sampler 节点的哪个输入端口：
> - 连到 `positive` → 正向
> - 连到 `negative` → 负向
> ComfyUI 里可以双击 CLIPTextEncode 节点的 widget 看到它当前的 prompt 文本，正向通常是英文描述场景，负向是 "low quality, blurry" 之类的。

> 📍 **实战补丁：不用回浏览器数 ID，一行 python 全扫出来**
>
> 既然 4.10 已经把 API 格式的 JSON 拿到了（`workflows/sulphur2_t2v.json`），直接扫这个文件比在画布上一个个数节点快得多：
>
> ```bash
> $ cd ~/projects/agents-video-pipeline
> $ python3 -c "
> import json
> wf = json.load(open('workflows/sulphur2_t2v.json'))
> for nid, node in wf.items():
>     ct = node.get('class_type', '')
>     title = node.get('_meta', {}).get('title', '')
>     keys = list(node.get('inputs', {}).keys())
>     print(f'  [{nid:>4}] {ct:<32} title={title!r:<28} inputs={keys[:6]}')
> print()
> print('=== 文本节点的 text 内容（区分 positive / negative） ===')
> for nid, node in wf.items():
>     if 'TextEncode' in node.get('class_type', ''):
>         t = node.get('inputs', {}).get('text', '')
>         if isinstance(t, str):
>             print(f'  [{nid}] text = {t[:100]!r}')
> "
> ```
>
> 输出里直接按角色对号入座：
> - **`positive_prompt_node`**：`CLIPTextEncode`，title 含 "Positive" 或 text 是英文场景描述
> - **`negative_prompt_node`**：`CLIPTextEncode`，title 含 "Negative" 或 text 是 `"low quality, worst quality, ..."`
> - **`sampler_node`**：`SamplerCustom` / `KSampler` / `LTXVideoSampler`，inputs 有 `seed` 或 `noise_seed`
> - **`empty_latent_node`**：`EmptyLTXVLatentVideo` / `EmptyLatentVideo`，inputs 有 `width` `height` `length`（或 `num_frames`）
> - **`save_video_node`**：`SaveVideo` / `VHS_VideoCombine`，inputs 有 `filename_prefix`

### 5.4 填写 config/node_mapping.yaml

```bash
$ cd ~/projects/agents-video-pipeline
$ open config/node_mapping.yaml -e   # 用 TextEdit 打开
# 或者：
$ vim config/node_mapping.yaml
```

把刚才找到的 ID 填进去（**ID 必须是字符串，加引号**）：

```yaml
sulphur2_t2v:
  positive_prompt_node: "6"      # 改成你看到的实际 ID
  negative_prompt_node: "7"      # 改成你看到的实际 ID
  sampler_node: "12"             # 改成你看到的实际 ID
  empty_latent_node: "9"         # 改成你看到的实际 ID
  save_video_node: "20"          # 改成你看到的实际 ID

# I2V 如果跳过就保持空字符串
sulphur2_i2v:
  positive_prompt_node: ""
  negative_prompt_node: ""
  sampler_node: ""
  load_image_node: ""
  save_video_node: ""
  empty_latent_node: ""
```

### 5.5 如果做了 I2V，同样填 sulphur2_i2v 段位

I2V 比 T2V 多一个 **load_image_node**（`LoadImage` 节点），用来读取首帧。

⚠️ 重要：I2V 工作流通常**没有 EmptyLatentVideo 节点**（latent 由 init image 决定），所以 `empty_latent_node` 可以留空。

### 5.6 验证配置

```bash
$ python -m src.cli env
```

✅ 看到一张表格，**至少**这几行应该是 ✅：
- ComfyUI ✅
- Ollama / Gemma 4 ✅
- FFmpeg ✅
- Sulphur2 T2V workflow ✅
- Node mapping (config/node_mapping.yaml) ✅

❌ Node mapping 那行 ❌：检查 yaml 缩进、引号、ID 是否真的是数字字符串

### 验收

```bash
$ python -m src.cli env
```

T2V 链路 5 个项全 ✅。其他（GGUF enhancer / Whisper / Piper）允许 ⚠️ optional，下一阶段处理。

---

## 阶段 6：第一次端到端跑通（无 I2V，约 30-90 分钟）

> 这是**项目核心目标的第一次真实验证**。期待但不要紧张：第一次大概率会踩坑。

### 6.1 先用最稳定的配置跑

```bash
$ cd ~/projects/agents-video-pipeline
$ source .venv/bin/activate
$ python -m src.cli render \
    -t "中国茶文化的一天" \
    --no-i2v \
    --tts none \
    --no-subtitles \
    --save-state /tmp/render-test1.json
```

参数解释：
- `--no-i2v`：跳过 I2V 链式（即使你配了 I2V，第一次也别开）
- `--tts none`：跳过配音，先验证视频链路
- `--no-subtitles`：跳过字幕
- `--save-state`：保存完整运行状态便于排查

> ⏸️ 这一步会跑很久，大约 30-90 分钟（5 个镜头 × 5-15 分钟/镜头 + LLM 时间 + FFmpeg 时间）。
>
> 💡 风扇会狂转，**不要让 Mac 进入睡眠**：
> ```bash
> $ caffeinate -dims python -m src.cli render ...
> ```
> 这样系统不会因为 idle 而睡眠/降频。

### 6.2 期间可以观察什么

终端会持续打印日志，依次看到：
- `[scheduler] acquired ollama` → LLM 节点开始
- `[director] plan: ...` → 导演输出
- `[scriptwriter] N scenes, total Ns` → 编剧输出
- `[storyboarder] N shots, I2V chained: 0/N` → 分镜输出（--no-i2v 时全 T2V）
- `[prompt_smith] shot N/M ✓` → 提示词逐个产出
- `[scheduler] acquired comfyui` → 切换到渲染
- `[sulphur] T2V start: 1920x1080 6s ...` → 第一个镜头开始渲染
- `[comfy] submitted prompt_id=...` + 进度日志
- `[sulphur] T2V done → ...` → 第一个镜头完成
- `[frame_extract] ...` → 末帧提取
- 重复 N 次（每个镜头）
- `[shot_producer] rendered N/M shots successfully`
- `[compositor] composing N clips → 1920x1080@24fps`
- `[compositor] ✓ final video → ...`

### 6.3 成功的样子

✅ 终端最后会有：
```
✓ DONE
Final video: /Users/你/projects/agents-video-pipeline/output/20260520-XXXX-XXXXXX/final.mp4
metrics: {'composited_shots': 5, ...}
```

✅ 用 QuickTime 打开那个 final.mp4，能看到 30 秒左右的视频，**无配音、无字幕，但画面连贯**。

### 6.4 常见失败 & 处理

#### 故障 A：第一镜头就报 `KeyError: 'length'` 或 `'num_frames'`

**原因**：你导出的 workflow 里 latent 节点用的键名我们没兜底。

**处理**：
1. 把日志贴给我，我加一行兜底
2. 或者临时方案：在 ComfyUI 里手动改 latent 节点把"length"改成"num_frames"再重新导出

#### 故障 B：`ComfyUIError: execution_error: ...node X failed...`

**原因**：你的 workflow 里某个节点（往往是 LoadCheckpointGGUF 之类）模型名写死了，但代码注入的 prompt 文本被塞错位置。

**处理**：把 ComfyUI 控制台（不是浏览器）的报错日志贴给我看。

#### 故障 C：`OOM` / `mps backend out of memory`

**原因**：M1 32GB 跑 1080p 6 秒太勉强。

**处理**：
- 先试 720p：暂时改 `src/config.py` 里 `default_resolution = "720p"`
- 还不行就 480p（需要在配置里加这个分辨率）
- 或者关掉 Ollama 模型：`ollama stop gemma4:e4b`，每个镜头之间手动重启

#### 故障 D：渲染慢到几小时一个镜头

**原因**：Sulphur 2 模型在你机器上太重。

**处理**：
- 检查是不是用了 fp8 而不是 GGUF Q4
- 检查 ComfyUI 启动参数是否加了 `--use-split-cross-attention --force-fp16`
- 如果还慢，考虑用 LTX-Video 2.3 base 模型（不是 Sulphur 2，但工作流兼容）

#### 故障 E：单镜头成功，但 Compositor 失败

**原因**：FFmpeg 拼接时分辨率/fps 不一致。

**处理**：贴 `[compositor]` 段日志给我，常见是某个镜头分辨率不对。

### 验收

✅ `output/<date>/<task_id>/final.mp4` 文件存在且能播放
✅ 视频大致是 30 秒、5 段画面、没有声音

如果到这一步通了，**项目最核心的视觉链路就跑通了**！

> 📍 **实战补丁：M1 32GB 实测产能数据（2026-05）**
>
> 在跑完整 `cli render` 之前，**强烈建议先单跑一次 `cli shot`** 验证项目代码 → ComfyUI → 出片这一段链路本身通：
>
> ```bash
> $ caffeinate -dims python -m src.cli shot \
>     --prompt "a foggy mountain at dawn, cinematic" \
>     --no-use-llm \
>     --resolution 720p \
>     --duration 2
> ```
>
> 这个组合是 M1 32GB 的"安全区"参数，**故意避开两个雷区**：
>
> - `--no-use-llm`：跳过 PromptSmith → enhancer 一段，第一次冒烟先隔离 LLM 链路
> - `--resolution 720p --duration 2`：1080p × 6s 在 M1 上**极易 OOM 或时长爆炸**
>
> **实测耗时（M1 32GB）：**
>
> | 配置 | 单镜头耗时 | 备注 |
> |---|---|---|
> | 720p × 48f (2s) × 30 步 | **~15.5 分钟** | 单镜头基线，跑通这个再做完整 render |
> | 720p × 144f (6s) × 30 步 | **~45 分钟**（推算） | 默认 6s，但风险高 |
> | 1080p × 144f (6s) × 30 步 | **大概率 OOM** | 不建议在 M1 上尝试 |
>
> 推论：`cli render` 跑 5 镜头 720p × 6s ≈ **3-4 小时**。如果想压时间到 1 小时内：
> - 改 `--duration` 默认值为 2（5 × 15min ≈ 75min）
> - 或把分辨率配置改到 540p / 480p
>
> **节点 ID 速查（仅供 LTX-Video 内置 T2V 模板参考，你导出的实际 ID 可能不同）：**
>
> ```yaml
> sulphur2_t2v:
>   positive_prompt_node: "6"     # CLIPTextEncode (Positive Prompt)
>   negative_prompt_node: "7"     # CLIPTextEncode (Negative Prompt)
>   sampler_node: "72"            # SamplerCustom（注意：用 noise_seed，不是 seed）
>   empty_latent_node: "70"       # EmptyLTXVLatentVideo
>   save_video_node: "79"         # SaveVideo
> ```
>
> ⚠️ **小坑预警**：LTX-Video 内置 T2V 模板的 `SaveVideo` 节点**没有 `fps` / `frame_rate` 输入键**（fps 在另一个 `CreateVideo` 节点里，画布上设默认 24）。项目代码 `_inject_fps` 找不到 fps 键会**静默不注入**，不影响 M1 出片。

---

## 阶段 7：加配音 + 字幕（约 30 分钟准备 + 一次完整跑通）

### 7.1 下载 Piper 中文语音模型

```bash
$ cd ~/projects/agents-video-pipeline
$ mkdir -p models/piper
$ cd models/piper
$ curl -L -O "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx"
$ curl -L -O "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json"
```

> ⏸️ 模型约 60MB，1-3 分钟。

### 7.2 测试 Piper 单独工作

```bash
$ echo "你好世界，这是一段测试" | piper \
    --model models/piper/zh_CN-huayan-medium.onnx \
    --output_file /tmp/test_voice.wav
```

```bash
$ afplay /tmp/test_voice.wav   # 用系统默认播放器播放
```

✅ 听到一段中文女声"你好世界，这是一段测试"

❌ 报错 model not found：路径不对，检查 `models/piper/` 下是不是有两个文件（.onnx + .onnx.json）

### 7.3 下载 Whisper.cpp 模型

```bash
$ cd ~/projects/agents-video-pipeline
$ mkdir -p models/whisper
$ cd models/whisper
$ curl -L -O "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
```

> ⏸️ 约 140MB。
>
> 💡 想要更准确可以下 `ggml-medium.bin`（1.5GB）。base 已经够用。

### 7.4 测试 Whisper.cpp 单独工作

```bash
$ whisper-cli -m models/whisper/ggml-base.bin -f /tmp/test_voice.wav -l zh --output-srt -of /tmp/test
$ cat /tmp/test.srt
```

✅ 看到 SRT 字幕内容，含时间戳和"你好世界"等文字

### 7.5 验证项目自检

```bash
$ python -m src.cli env
```

✅ 现在多了几行：
- TTS: piper ✅
- Whisper.cpp (subtitles) ✅

### 7.6 完整跑通（含配音+字幕）

```bash
$ caffeinate -dims python -m src.cli render \
    -t "雨夜便利店里的相遇" \
    --no-i2v \
    --tts piper \
    --save-state /tmp/render-test2.json
```

> ⏸️ 时间和阶段 6 差不多，多 1-3 分钟（TTS+ASR）。

✅ 最终视频应该有：
- 5 段视觉画面（无 I2V 链式，硬切）
- 中文配音（Piper 女声"花颜"）
- 烧录在画面上的中文字幕（白字黑边，居中靠下）

### 7.7 看日志确认链路

```
[voice] generated 5/5 voice clips with backend=piper
[atempo] clamped speed 1.62 → 1.50; audio will be 4.0s vs target 6.00s
   ↑ 这是正常的：Piper 默认语速对 6 秒镜头来说太慢，atempo 加速到 1.5x
[asr.whisper] 01.wav → 01.srt
[compositor] composing 5 clips (audio: 5/5, srt: 5/5)
[compositor.burn_srt] _concat_no_subs.mp4 + subtitles.srt → final.mp4
✓ final video → ...
```

### 验收

✅ 用 QuickTime 打开 final.mp4，能看到画面 + 听到中文旁白 + 看到中文字幕
✅ 时长大约 30 秒，画面、声音、字幕大致对齐

**到这一步，你的需求 100% 实现了。** 🎉

---

## 阶段 8：加 I2V 链式（可选，约 30 分钟）

I2V 让相邻镜头之间画面延续（不再是硬切），观感更像电影。

### 8.1 确认你已经导出了 I2V workflow

```bash
$ ls workflows/
sulphur2_t2v.json   ← 必须有
sulphur2_i2v.json   ← 这一阶段需要
```

如果没有，回阶段 4.11 导出。

### 8.2 配置 I2V 节点 ID

打开 ComfyUI，加载 I2V workflow（`workflow_api_i2v.json`），找到 5 个节点 ID，填进 `config/node_mapping.yaml` 的 `sulphur2_i2v` 段位：

```yaml
sulphur2_i2v:
  positive_prompt_node: "6"
  negative_prompt_node: "7"
  sampler_node: "12"
  load_image_node: "21"      # 新增：LoadImage 节点
  save_video_node: "20"
  empty_latent_node: ""      # I2V 通常没有，留空
```

### 8.3 验证 I2V 配置

```bash
$ python -m src.cli env
```

如果代码加了 I2V 自检（当前未实现），会显示 ✅；否则跳到下一步。

### 8.4 跑带 I2V 的完整链

```bash
$ caffeinate -dims python -m src.cli render \
    -t "夕阳下的海岸线" \
    --tts piper \
    --save-state /tmp/render-test3.json
```

注意**没有** `--no-i2v` 了。

### 8.5 看日志确认 I2V 是否生效

```
[shot_producer] 1/5 T2V       ← 第一个永远 T2V
[frame_extract] 01.mp4 → 01_last.png
[shot_producer] 2/5 I2V from 01_last.png   ← I2V 触发！
[frame_extract] 02.mp4 → 02_last.png
[shot_producer] 3/5 I2V from 02_last.png
...
```

✅ 至少应该有 1-3 个 `I2V from ...` 日志（不会全 5 个都 I2V，因为 storyboarder 会标硬切）

❌ 全是 T2V（没看到 I2V）：检查 `cli env` 输出 + storyboarder 日志，可能是 `Storyboard.shots[i].use_i2v_from_prev` 都是 False（LLM 偏保守，可重试一次或提高 storyboarder 提示词的 I2V 比例）

❌ I2V 报 `LoadImage failed`：你的 LoadImage 节点要求图片在 `ComfyUI/input/` 目录而非绝对路径，**贴日志给我**，我加自动复制兜底

### 8.6 比较两段视频

把 `--no-i2v` 版本和带 I2V 的视频都用 QuickTime 打开，**前者镜头切换硬切，后者部分镜头之间会有视觉延续**。

### 验收

✅ 出片含至少 1 个 I2V 链式镜头
✅ 日志清晰显示哪些镜头是 T2V、哪些是 I2V

---

## 阶段 9：小结 + 排错速查表

到这里，你应该已经：

- ✅ 全栈装好（git / Python / Ollama / Gemma 4 / ComfyUI / Sulphur 2 / Piper / Whisper.cpp / FFmpeg）
- ✅ 跑通了 LLM 规划链
- ✅ 跑通了端到端无声成片
- ✅ 跑通了端到端有声+字幕成片
- ✅ （可选）跑通了 I2V 链式

### 9.1 单条视频生产时间预期

| 配置 | 单条 30s 视频耗时 |
|---|---|
| `--no-i2v --tts none --no-subtitles` | 25-50 min（最快） |
| `--no-i2v --tts piper`（含字幕） | 30-60 min |
| 带 I2V + tts piper | 35-75 min（最完整） |

### 9.2 长期使用建议

```bash
# 起一个长跑会话，防止 Mac 睡眠
$ caffeinate -dims zsh

# 在这个会话里跑：
$ source ~/projects/agents-video-pipeline/.venv/bin/activate
$ cd ~/projects/agents-video-pipeline

# 一次跑多条（手动）
$ for topic in "主题1" "主题2" "主题3"; do
    python -m src.cli render -t "$topic" --tts piper
  done
```

### 9.3 常见错误速查表

| 错误信息（关键词） | 阶段 | 解决方法 |
|---|---|---|
| `Connection refused` 11434 | 3 | `ollama serve` 没起 |
| `Connection refused` 8188 | 4-8 | ComfyUI 没开 |
| `model not found gemma4:e4b` | 3 | `ollama pull gemma4:e4b` |
| `FileNotFoundError sulphur2_t2v.json` | 5 | 没把导出的 API workflow 复制到 workflows/ |
| `节点 ID 'X' 不存在` | 5 | yaml 里的 ID 和 workflow JSON 不一致 |
| `KeyError: 'length'` 或类似 | 6 | workflow 节点 input 键名不在我们三键名兜底里，贴日志给我 |
| `OOM` / `out of memory` | 6 | 降分辨率或换 GGUF Q4 |
| `LoadImage failed: file not found` | 8 | I2V 的 LoadImage 节点要求 ComfyUI/input/ 路径，贴日志给我 |
| `pytest tests/` 不通过 | 2 | 代码层面问题，贴日志给我 |
| atempo `clamped speed` warning | 7 | 正常现象，TTS 语速被限幅 |

### 9.4 我帮不上忙的几个领域

- ComfyUI 版本兼容性问题（你需要在 ComfyUI Discord/Issues 找答案）
- macOS 系统设置（防火墙、休眠）
- 模型下载速度（看你网络）

### 9.5 想要更好的输出

- 改 Piper 音色：去 https://github.com/rhasspy/piper/blob/master/VOICES.md 选别的中文音色
- 用 Whisper medium：下 `ggml-medium.bin` 替代 `ggml-base.bin`
- 提高画质：试试 1080p（M1 32GB 边界），或者下 Sulphur 2 fp8 版本（更慢但更细腻）
- 改风格：在 `cli render` 加 `-s "anime"` 或 `-s "documentary"` 等风格提示

---

## 阶段 10：联调反馈给 AI

如果你按这个文档跑通了 → **直接告诉我 "联调通过"**，我们继续 M3（批量生产线）。

如果中途卡在某一步：
1. **不要继续往下做**，停在那里
2. 把以下信息发给我：
   - 卡在文档的哪个阶段哪个步骤
   - 终端的完整错误日志（最少最后 30 行）
   - 你的环境特殊性（比如：M1 / M2 Pro / 内存大小 / macOS 版本）

我会按"故障 A/B/C"的格式给你下一步操作。

---

## 附录：手册风格的命令清单

把这个钉在备忘录里：

```bash
# 进入工作环境（每次新开终端）
cd ~/projects/agents-video-pipeline
source .venv/bin/activate

# 检查所有依赖状态
python -m src.cli env

# 只跑 LLM 规划链（不出视频，调试用）
python -m src.cli plan-and-prompts -t "你的主题"

# 完整端到端（含配音+字幕）
caffeinate -dims python -m src.cli render -t "你的主题"

# 调试模式：跳过 I2V 和配音
python -m src.cli render -t "你的主题" --no-i2v --tts none --no-subtitles

# 看输出
ls -lah output/
open output/<最新日期>/<任务id>/final.mp4
```

```bash
# 把这条加到 ~/.zshrc 让以后启动更快
alias avp='cd ~/projects/agents-video-pipeline && source .venv/bin/activate'
```

加完之后，以后只要：
```bash
$ avp
$ caffeinate -dims python -m src.cli render -t "你的主题"
```

完事。

---

**祝联调顺利。** 🚀
