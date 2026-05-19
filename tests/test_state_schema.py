"""schema 单测：验证 ProductionPlan / Storyboard / ShotState 的字段约束。

不依赖 LLM / ComfyUI，纯结构层测试。
"""
from __future__ import annotations

import pytest

from src.orchestrator.state import (
    PipelineState,
    ProductionPlan,
    Scene,
    Script,
    Shot,
    ShotState,
    Storyboard,
    VisualStyleLock,
)


def _make_plan(**overrides) -> dict:
    base = {
        "topic": "中国茶文化的一天",
        "title": "一杯茶的旅程",
        "logline": "From mountain mist to morning cup, a quiet love letter to Chinese tea.",
        "audience": "25-40 岁城市白领，对生活方式内容感兴趣",
        "mood": "documentary",
        "total_duration_sec": 30,
        "n_shots": 5,
        "per_shot_duration_sec": 6,
        "pacing": "medium",
        "style": {
            "art_style": "cinematic photoreal documentary",
            "color_palette": "warm earth tones, soft greens, golden highlights",
            "lighting": "soft golden hour, diffused window light",
            "camera_language": "intimate handheld, occasional static wide",
            "aspect_ratio": "landscape",
        },
        "needs_voiceover": True,
        "needs_subtitles": True,
        "bgm_mood": "ambient cinematic",
    }
    base.update(overrides)
    return base


class TestProductionPlan:
    def test_valid_plan(self):
        plan = ProductionPlan.model_validate(_make_plan())
        assert plan.n_shots == 5
        assert plan.style.aspect_ratio == "landscape"

    def test_per_shot_duration_must_be_6_12_20(self):
        with pytest.raises(ValueError):
            ProductionPlan.model_validate(_make_plan(per_shot_duration_sec=7))

    def test_n_shots_upper_bound(self):
        with pytest.raises(ValueError):
            ProductionPlan.model_validate(_make_plan(n_shots=999))

    def test_total_duration_lower_bound(self):
        with pytest.raises(ValueError):
            ProductionPlan.model_validate(_make_plan(total_duration_sec=2))

    def test_round_trip_json(self):
        plan = ProductionPlan.model_validate(_make_plan())
        s = plan.model_dump_json()
        plan2 = ProductionPlan.model_validate_json(s)
        assert plan == plan2


class TestStoryboard:
    def test_shot_defaults(self):
        shot = Shot(idx=1, visual_intent="窗台上一杯绿茶冒着热气", duration_sec=6)
        assert shot.camera_shot == "medium"
        assert shot.camera_motion == "static"
        assert shot.transition_to_next == "cut"
        assert shot.use_i2v_from_prev is False

    def test_storyboard_round_trip(self):
        sb = Storyboard(
            shots=[
                Shot(idx=1, visual_intent="A", duration_sec=6),
                Shot(idx=2, visual_intent="B", duration_sec=6, use_i2v_from_prev=True),
            ]
        )
        sb2 = Storyboard.model_validate_json(sb.model_dump_json())
        assert len(sb2.shots) == 2
        assert sb2.shots[1].use_i2v_from_prev is True


class TestPipelineState:
    def test_minimal_construction(self):
        st = PipelineState(task_id="t1", topic="hello")
        assert st.plan is None
        assert st.shots == []
        assert st.errors == []

    def test_with_shots(self):
        st = PipelineState(
            task_id="t1",
            topic="hello",
            shots=[ShotState(idx=1), ShotState(idx=2)],
        )
        assert len(st.shots) == 2


class TestScript:
    def test_script_round_trip(self):
        s = Script(
            title="一杯茶的旅程",
            scenes=[
                Scene(idx=1, narration="清晨，山间薄雾未散。", duration_sec=6, mood="serene"),
                Scene(idx=2, narration="老师傅采下第一片茶叶。", duration_sec=6, mood="focused"),
            ],
        )
        s2 = Script.model_validate_json(s.model_dump_json())
        assert s == s2


class TestVisualStyleLock:
    def test_default_aspect_ratio(self):
        v = VisualStyleLock(
            art_style="cinematic",
            color_palette="warm",
            lighting="golden hour",
            camera_language="handheld",
        )
        assert v.aspect_ratio == "landscape"
