"""ShotProducer & Compositor 单测：mock ComfyUI runner & FFmpeg。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agents.shot_producer import run_shot_producer
from src.orchestrator.state import PipelineState, ShotState


# ─── Fakes ─────────────────────────────────────────────────────


class FakeT2V:
    def __init__(self, fail_idxs: set[int] | None = None):
        self.fail_idxs = fail_idxs or set()
        self.calls: list[dict] = []

    async def run(self, **kwargs) -> Path:
        self.calls.append(kwargs)
        save_to = kwargs["save_to"]
        idx = int(save_to.stem)
        if idx in self.fail_idxs:
            raise RuntimeError(f"simulated render failure at shot {idx}")
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_bytes(b"fake-mp4-bytes")
        return save_to


class FakeI2V:
    def __init__(self):
        self.calls: list[dict] = []

    async def run(self, **kwargs) -> Path:
        self.calls.append(kwargs)
        save_to = kwargs["save_to"]
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_bytes(b"fake-i2v-mp4-bytes")
        return save_to


class FakeScheduler:
    """no-op 调度器（不触发真实 free_memory）。"""

    from contextlib import asynccontextmanager

    def __init__(self):
        self.acquire_calls: list[str] = []

    @asynccontextmanager
    async def acquire_comfyui(self):  # type: ignore[misc]
        self.acquire_calls.append("comfyui")
        yield

    @asynccontextmanager
    async def acquire_ollama(self, model: str | None = None):  # type: ignore[misc]
        self.acquire_calls.append(f"ollama:{model}")
        yield


# ─── Tests ─────────────────────────────────────────────────────


def _make_state_with_shots(n: int, *, i2v_idxs: set[int] | None = None) -> PipelineState:
    i2v_idxs = i2v_idxs or set()
    shots = []
    for i in range(1, n + 1):
        shots.append(
            ShotState(
                idx=i,
                visual_intent=f"intent {i}",
                positive_prompt=f"a beautiful scene number {i}",
                negative_prompt="low quality",
                duration_sec=6,
                use_i2v_from_prev=(i in i2v_idxs),
                resolution="1080p",
                fps=24,
            )
        )
    return PipelineState(task_id="test-task", topic="test", shots=shots)


class TestShotProducer:
    @pytest.mark.asyncio
    async def test_first_shot_always_t2v(self, monkeypatch, tmp_path):
        """shot[1] 即使 use_i2v_from_prev=True 也必须走 T2V。"""
        monkeypatch.setattr(
            "src.agents.shot_producer.task_output_dir", lambda _tid: tmp_path
        )

        async def _fake_extract(_v, out):
            out.write_bytes(b"png")
            return out

        monkeypatch.setattr(
            "src.agents.shot_producer.extract_last_frame", _fake_extract
        )

        # 故意把 shot[1] 标 use_i2v_from_prev=True 测试兜底
        state = _make_state_with_shots(2, i2v_idxs={1, 2})
        t2v = FakeT2V()
        i2v = FakeI2V()
        scheduler = FakeScheduler()

        await run_shot_producer(
            state, t2v=t2v, i2v=i2v, scheduler=scheduler, output_root=tmp_path
        )

        assert len(t2v.calls) == 1  # 只有 shot[1] 走 T2V
        assert len(i2v.calls) == 1  # shot[2] 走 I2V（有 prev frame）

    @pytest.mark.asyncio
    async def test_all_t2v_when_i2v_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.agents.shot_producer.task_output_dir", lambda _tid: tmp_path
        )

        async def _fake_extract(_v, out):
            out.write_bytes(b"png")
            return out

        monkeypatch.setattr(
            "src.agents.shot_producer.extract_last_frame", _fake_extract
        )

        state = _make_state_with_shots(3, i2v_idxs={2, 3})
        t2v = FakeT2V()
        scheduler = FakeScheduler()

        await run_shot_producer(
            state, t2v=t2v, i2v=None, scheduler=scheduler, output_root=tmp_path
        )

        assert len(t2v.calls) == 3  # 全部 T2V
        assert all(ss.clip_path is not None for ss in state.shots)

    @pytest.mark.asyncio
    async def test_failed_shot_does_not_block_next(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.agents.shot_producer.task_output_dir", lambda _tid: tmp_path
        )

        async def _fake_extract(_v, out):
            out.write_bytes(b"png")
            return out

        monkeypatch.setattr(
            "src.agents.shot_producer.extract_last_frame", _fake_extract
        )

        state = _make_state_with_shots(3, i2v_idxs={2, 3})
        t2v = FakeT2V(fail_idxs={2})  # shot 2 故障
        scheduler = FakeScheduler()

        await run_shot_producer(
            state, t2v=t2v, i2v=None, scheduler=scheduler, output_root=tmp_path
        )

        assert state.shots[0].clip_path is not None
        assert state.shots[1].clip_path is None  # 失败
        assert "render failed" in (state.shots[1].errors[-1] if state.shots[1].errors else "")
        assert state.shots[2].clip_path is not None  # 第 3 个继续渲染

    @pytest.mark.asyncio
    async def test_extract_failure_falls_back_to_t2v(self, monkeypatch, tmp_path):
        """末帧提取失败时，下一镜头自动退化为 T2V。"""
        monkeypatch.setattr(
            "src.agents.shot_producer.task_output_dir", lambda _tid: tmp_path
        )

        from src.adapters.frame_extractor import FrameExtractError

        async def _failing_extract(_v, _out):
            raise FrameExtractError("simulated")

        monkeypatch.setattr(
            "src.agents.shot_producer.extract_last_frame", _failing_extract
        )

        state = _make_state_with_shots(2, i2v_idxs={2})
        t2v = FakeT2V()
        i2v = FakeI2V()
        scheduler = FakeScheduler()

        await run_shot_producer(
            state, t2v=t2v, i2v=i2v, scheduler=scheduler, output_root=tmp_path
        )

        # shot 1 T2V；shot 2 应该退化为 T2V（因为提取失败 prev_last_frame=None）
        assert len(t2v.calls) == 2
        assert len(i2v.calls) == 0

    @pytest.mark.asyncio
    async def test_skips_shot_without_prompt(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.agents.shot_producer.task_output_dir", lambda _tid: tmp_path
        )

        async def _fake_extract(_v, out):
            out.write_bytes(b"png")
            return out

        monkeypatch.setattr(
            "src.agents.shot_producer.extract_last_frame", _fake_extract
        )

        state = _make_state_with_shots(2)
        state.shots[0].positive_prompt = None  # 无 prompt
        t2v = FakeT2V()
        scheduler = FakeScheduler()

        await run_shot_producer(
            state, t2v=t2v, i2v=None, scheduler=scheduler, output_root=tmp_path
        )

        assert len(t2v.calls) == 1  # 只有 shot 2 渲染
        assert state.shots[0].clip_path is None
        assert any("no positive_prompt" in e for e in state.shots[0].errors)
