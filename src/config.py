"""全局配置（M1 锁定为 Mac M1 / 32GB 默认值）。"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class NodeMapping(BaseModel):
    """单个 workflow 的节点 ID 映射。空字符串表示未配置。"""

    positive_prompt_node: str = ""
    negative_prompt_node: str = ""
    sampler_node: str = ""
    empty_latent_node: str = ""
    save_video_node: str = ""
    load_image_node: str = ""  # 仅 I2V 使用

    def is_t2v_ready(self) -> bool:
        return all([
            self.positive_prompt_node,
            self.negative_prompt_node,
            self.sampler_node,
            self.empty_latent_node,
            self.save_video_node,
        ])


class Settings(BaseSettings):
    """生产线全局配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="AGENTS_",
        extra="ignore",
    )

    # —— 路径 ——
    project_root: Path = PROJECT_ROOT
    workflows_dir: Path = PROJECT_ROOT / "workflows"
    models_dir: Path = PROJECT_ROOT / "models"
    output_dir: Path = PROJECT_ROOT / "output"
    config_dir: Path = PROJECT_ROOT / "config"
    logs_dir: Path = PROJECT_ROOT / "logs"

    # —— ComfyUI ——
    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_workflow_t2v: str = "sulphur2_t2v.json"
    comfyui_workflow_i2v: str = "sulphur2_i2v.json"
    comfyui_client_id: str = Field(default_factory=lambda: f"agents-{uuid.uuid4().hex[:8]}")
    comfyui_request_timeout_sec: float = 1800.0  # 单次推理最长 30 min

    # —— Ollama ——
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model_default: str = "gemma4:e4b"  # M1 32GB 不用 26B

    # —— LM Studio（备用后端） ——
    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"

    # —— Sulphur prompt enhancer ——
    sulphur_enhancer_gguf: Path | None = None  # 自动探测 models/ 目录下文件
    sulphur_enhancer_n_ctx: int = 4096

    # —— 视频默认参数（M1 档） ——
    default_resolution: Literal["1080p", "720p"] = "1080p"
    default_duration_sec: Literal[6, 12] = 6
    default_fps: int = 24
    default_mode: Literal["fast", "pro"] = "fast"
    default_negative_prompt: str = (
        "low quality, blurry, distorted, watermark, text, signature, "
        "extra limbs, deformed, ugly, bad anatomy"
    )

    # —— 调度（M1 核心） ——
    hardware_profile: Literal["m1_32gb", "generic"] = "m1_32gb"
    enable_mutex_locks: bool = True
    oom_fallback_resolutions: list[str] = ["1080p", "720p"]
    oom_fallback_durations: list[int] = [6]  # M1 不用 12s 兜底

    # —— 日志 ——
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


# 全局单例
_settings: Settings | None = None


def load_settings() -> Settings:
    """惰性加载并副作用地创建必要目录。"""
    global _settings
    if _settings is not None:
        return _settings

    s = Settings()

    # 自动探测 enhancer 路径
    if s.sulphur_enhancer_gguf is None:
        candidates = list(s.models_dir.glob("sulphur_prompt_enhancer*.gguf"))
        if candidates:
            s.sulphur_enhancer_gguf = candidates[0]

    # 创建必要目录
    for d in (s.output_dir, s.logs_dir, s.config_dir, s.models_dir, s.workflows_dir):
        d.mkdir(parents=True, exist_ok=True)

    _settings = s
    return s


def load_node_mapping(workflow_key: str = "sulphur2_t2v") -> NodeMapping:
    """从 config/node_mapping.yaml 加载某个 workflow 的节点 ID 映射。"""
    s = load_settings()
    yaml_path = s.config_dir / "node_mapping.yaml"
    if not yaml_path.exists():
        return NodeMapping()
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    return NodeMapping(**(data.get(workflow_key) or {}))


def parse_resolution(r: str) -> tuple[int, int]:
    """'1080p' → (1920, 1080)；'720p' → (1280, 720)。"""
    presets = {
        "720p": (1280, 720),
        "1080p": (1920, 1080),
        "2k": (2560, 1440),
        "4k": (3840, 2160),
    }
    key = r.lower()
    if key not in presets:
        raise ValueError(f"Unsupported resolution: {r}")
    return presets[key]


def duration_to_frames(duration_sec: int, fps: int = 24) -> int:
    """LTX 系常用：6s @ 24fps = 144 帧。"""
    return duration_sec * fps


def new_task_id() -> str:
    """形如 20260514-2207-a1b2c3。"""
    import datetime as _dt
    now = _dt.datetime.now()
    return f"{now:%Y%m%d-%H%M}-{uuid.uuid4().hex[:6]}"


def task_output_dir(task_id: str | None = None) -> Path:
    """output/<yyyymmdd>/<task_id>/"""
    s = load_settings()
    import datetime as _dt
    tid = task_id or new_task_id()
    d = s.output_dir / _dt.datetime.now().strftime("%Y%m%d") / tid
    (d / "shots").mkdir(parents=True, exist_ok=True)
    return d
