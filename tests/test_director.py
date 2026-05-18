"""DirectorAgent 单测：mock LLM，验证业务逻辑（硬约束兜底、prompt 拼装）。

不依赖真实 LLM 服务，可在 CI 上运行。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from src.agents.director import run_director
from src.config import load_settings
from src.orchestrator.state import ProductionPlan


class FakeLLM:
    """伪造 LLMProvider：固定返回预设 plan。"""

    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.last_messages: list[dict] | None = None

    async def chat_json(self, messages, schema, **_):
        self.last_messages = messages
        return schema.model_validate(self.response)

    async def aclose(self):
        pass


def _fake_plan_dict(**overrides) -> dict:
    base = {
        "topic": "中国茶文化",
        "title": "一杯茶的旅程",
        "logline": "Quiet love letter to Chinese tea.",
        "audience": "25-40 岁城市白领",
        "mood": "documentary",
        "total_duration_sec": 30,
        "n_shots": 5,
        "per_shot_duration_sec": 6,
        "pacing": "medium",
        "style": {
            "art_style": "cinematic photoreal",
            "color_palette": "warm earth tones",
            "lighting": "soft golden hour",
            "camera_language": "intimate handheld",
            "aspect_ratio": "landscape",
        },
        "needs_voiceover": True,
        "needs_subtitles": True,
        "bgm_mood": "ambient",
    }
    base.update(overrides)
    return base


class TestRunDirector:
    @pytest.mark.asyncio
    async def test_basic(self):
        llm = FakeLLM(_fake_plan_dict())
        plan = await run_director("中国茶文化", llm=llm)
        assert isinstance(plan, ProductionPlan)
        assert plan.n_shots == 5

    @pytest.mark.asyncio
    async def test_m1_caps_n_shots_to_8(self):
        """M1 32GB 硬约束：即使 LLM 返回 n_shots=15 也要被压回 8。"""
        llm = FakeLLM(_fake_plan_dict(n_shots=15))
        plan = await run_director("test", llm=llm)
        assert plan.n_shots == 8

    @pytest.mark.asyncio
    async def test_m1_caps_per_shot_duration(self):
        """M1 硬约束：per_shot > 12 强制改 6。"""
        llm = FakeLLM(_fake_plan_dict(per_shot_duration_sec=20))
        plan = await run_director("test", llm=llm)
        assert plan.per_shot_duration_sec == 6

    @pytest.mark.asyncio
    async def test_user_message_includes_hints(self):
        llm = FakeLLM(_fake_plan_dict())
        await run_director(
            "中国茶文化",
            llm=llm,
            target_duration_hint=45,
            style_hint="anime",
        )
        assert llm.last_messages is not None
        user_msg = next(m for m in llm.last_messages if m["role"] == "user")
        assert "45" in user_msg["content"]
        assert "anime" in user_msg["content"]

    @pytest.mark.asyncio
    async def test_system_prompt_contains_hardware_constraint(self):
        s = load_settings()
        # 强制走 m1_32gb 路径
        s.hardware_profile = "m1_32gb"
        llm = FakeLLM(_fake_plan_dict())
        await run_director("test", llm=llm, settings=s)
        sys_msg = next(m for m in llm.last_messages if m["role"] == "system")
        assert "M1" in sys_msg["content"]
        assert "32GB" in sys_msg["content"]
