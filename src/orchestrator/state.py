"""管线全量数据模型（一次性把 M2 需要的 schema 全部定义齐）。

数据流：
  topic ──▶ ProductionPlan ──▶ Script ──▶ Storyboard ──▶ shots[] ──▶ final.mp4

设计原则：
- 所有 schema 都用 Pydantic v2，Agent 输出可直接 model_validate_json。
- 每层都明确列出"全局风格锁"字段（visual_style_lock / character_lock），
  这是保证多镜头视觉一致性的关键（详见 architecture §4.3 视觉连续性策略）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────────
#  L1 — Plan（Director 输出，整片全局规划）
# ─────────────────────────────────────────────────────────────────


VideoOrientation = Literal["landscape", "portrait", "square"]
VideoMood = Literal[
    "cinematic", "documentary", "vlog", "anime", "fantasy",
    "horror", "comedy", "romantic", "epic", "minimalist", "surreal",
]


class VisualStyleLock(BaseModel):
    """全片统一的视觉风格锁——所有 shot 的 prompt 都必须继承这些字段。"""

    art_style: str = Field(description="如 'cinematic photoreal' / 'studio ghibli anime'")
    color_palette: str = Field(description="如 'warm earth tones' / 'cyan & magenta neon'")
    lighting: str = Field(description="如 'soft golden hour' / 'high-contrast moonlight'")
    camera_language: str = Field(description="如 'handheld, intimate' / 'static wide cinematic'")
    aspect_ratio: VideoOrientation = "landscape"


class ProductionPlan(BaseModel):
    """Director 输出：整片的全局规划。"""

    topic: str
    title: str = Field(description="一句话标题")
    logline: str = Field(description="一句话核心创意 / 故事钩子")
    audience: str = Field(description="目标受众，如 '25-40 岁城市白领、对生活方式内容感兴趣'")
    mood: VideoMood
    total_duration_sec: int = Field(ge=6, le=600, description="整片目标时长（秒）")
    n_shots: int = Field(ge=1, le=20, description="总镜头数")
    per_shot_duration_sec: int = Field(description="单镜头默认时长，6/12 之一（M1 32GB 推荐 6）")
    pacing: Literal["slow", "medium", "fast"] = "medium"
    style: VisualStyleLock
    needs_voiceover: bool = True
    needs_subtitles: bool = True
    bgm_mood: str = Field(default="", description="背景音乐情绪标签，如 'ambient cinematic'")

    @field_validator("per_shot_duration_sec")
    @classmethod
    def _check_shot_duration(cls, v: int) -> int:
        if v not in (6, 12, 20):
            raise ValueError(f"per_shot_duration_sec must be 6/12/20, got {v}")
        return v


# ─────────────────────────────────────────────────────────────────
#  L2 — Script（Scriptwriter 输出，分场配音稿）
# ─────────────────────────────────────────────────────────────────


class Scene(BaseModel):
    """一场（对应一个镜头的叙事内容）。"""

    idx: int = Field(ge=1)
    narration: str = Field(description="该场配音文本（中文）")
    duration_sec: int = Field(ge=2, le=30)
    mood: str = Field(default="", description="该场情绪基调")


class Script(BaseModel):
    """Scriptwriter 输出。"""

    title: str
    scenes: list[Scene]


# ─────────────────────────────────────────────────────────────────
#  L3 — Storyboard（Storyboarder 输出，每个镜头的视觉意图）
# ─────────────────────────────────────────────────────────────────


CameraShotType = Literal[
    "wide", "medium", "close-up", "extreme-close-up",
    "over-the-shoulder", "POV", "establishing", "aerial",
]
CameraMotion = Literal[
    "static", "pan-left", "pan-right", "tilt-up", "tilt-down",
    "dolly-in", "dolly-out", "handheld", "tracking", "crane",
]


class Shot(BaseModel):
    """一个镜头的视觉意图（中文意图，PromptSmith 会把它翻成英文 Sulphur prompt）。"""

    idx: int = Field(ge=1)
    visual_intent: str = Field(description="中文画面描述，主体+动作+环境")
    camera_shot: CameraShotType = "medium"
    camera_motion: CameraMotion = "static"
    duration_sec: int = Field(ge=2, le=30)
    transition_to_next: Literal["cut", "fade", "match-cut", "dissolve"] = "cut"
    use_i2v_from_prev: bool = Field(
        default=False,
        description="是否用上一镜头末帧做首帧（保证视觉连续性）",
    )


class Storyboard(BaseModel):
    """Storyboarder 输出。"""

    shots: list[Shot]


# ─────────────────────────────────────────────────────────────────
#  L4 — ShotState（生产线运行时状态，每个镜头的执行轨迹）
# ─────────────────────────────────────────────────────────────────


class ShotState(BaseModel):
    """单镜头从规划到出片的全过程状态。"""

    idx: int
    # 来自 Storyboard
    visual_intent: str = ""
    camera_shot: str = "medium"
    camera_motion: str = "static"
    duration_sec: int = 6
    use_i2v_from_prev: bool = False

    # PromptSmith 产物
    positive_prompt: str | None = None
    negative_prompt: str = ""
    seed: int | None = None

    # 渲染参数
    resolution: str = "1080p"
    fps: int = 24

    # 产物路径
    init_image_path: Path | None = None  # I2V 用的首帧（来自上一镜头末帧）
    clip_path: Path | None = None
    last_frame_path: Path | None = None
    wav_path: Path | None = None
    srt_path: Path | None = None

    # 元信息
    retry: int = 0
    errors: list[str] = []

    model_config = {"arbitrary_types_allowed": True}


# ─────────────────────────────────────────────────────────────────
#  L5 — PipelineState（LangGraph 全局状态）
# ─────────────────────────────────────────────────────────────────


class PipelineState(BaseModel):
    """整条管线的运行状态，LangGraph 节点之间传递这个对象。"""

    task_id: str
    topic: str

    plan: ProductionPlan | None = None
    script: Script | None = None
    storyboard: Storyboard | None = None
    shots: list[ShotState] = []

    output_path: Path | None = None
    errors: list[str] = []

    # 度量
    metrics: dict = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}
