"""Voice & Subtitle Agent 单测：mock TTS/ASR + 真 FFmpeg。

注意：atempo 部分需要真实 FFmpeg；CI 上若无 ffmpeg 应 skip。
本测试仅做 mock-level 逻辑验证（不调真 FFmpeg）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.subtitle import run_subtitle_agent
from src.agents.voice import run_voice_agent
from src.orchestrator.state import PipelineState, ShotState


# ─── Fakes ───────────────────────────────────────────────────────


class FakeTTS:
    backend_name = "fake"

    def __init__(self, fail_idxs: set[int] | None = None):
        self.fail_idxs = fail_idxs or set()
        self.calls: list[Path] = []

    async def health(self) -> bool:
        return True

    async def synthesize(self, text, out_path, **_):
        # 用文本长度判断 idx（测试中 narration 形如 "shot N narration text..."）
        idx = self._guess_idx(out_path)
        if idx in self.fail_idxs:
            from src.adapters.tts import TTSError
            raise TTSError("simulated tts failure")
        self.calls.append(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF" + b"\x00" * 100)  # 假 wav 头
        return out_path

    @staticmethod
    def _guess_idx(p: Path) -> int:
        try:
            return int(p.stem.split("_")[0])
        except Exception:
            return 0


class FakeWhisper:
    def __init__(self, healthy: bool = True, fail: bool = False):
        self.healthy = healthy
        self.fail = fail
        self.calls: list[tuple[Path, Path]] = []

    async def health(self) -> bool:
        return self.healthy

    async def transcribe(self, wav, srt):
        if self.fail:
            from src.adapters.asr import ASRError
            raise ASRError("simulated whisper failure")
        self.calls.append((wav, srt))
        srt.parent.mkdir(parents=True, exist_ok=True)
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:06,000\nfake whisper transcription\n",
            encoding="utf-8",
        )
        return srt


# ─── Fixtures ────────────────────────────────────────────────────


def _make_state(n: int, tmp_path: Path, with_clips: bool = True) -> PipelineState:
    shots = []
    for i in range(1, n + 1):
        ss = ShotState(
            idx=i,
            visual_intent=f"intent {i}",
            duration_sec=6,
            narration=f"这是第{i}个镜头的旁白文字。",
        )
        if with_clips:
            clip = tmp_path / "shots" / f"{i:02d}.mp4"
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"fake-mp4")
            ss.clip_path = clip
        shots.append(ss)
    return PipelineState(task_id="test-task", topic="test", shots=shots)


# ─── Voice Agent ─────────────────────────────────────────────────


class TestVoiceAgent:
    @pytest.mark.asyncio
    async def test_skip_shots_without_narration(self, tmp_path, monkeypatch):
        state = _make_state(3, tmp_path)
        state.shots[1].narration = ""  # 中间镜头无旁白

        # 关掉 atempo（要真 ffmpeg），直接走 align_to_video=False 分支
        await _run_voice_no_align(state, tmp_path, FakeTTS(), monkeypatch)

        assert state.shots[0].wav_path is not None
        assert state.shots[1].wav_path is None  # 跳过
        assert state.shots[2].wav_path is not None

    @pytest.mark.asyncio
    async def test_tts_failure_falls_back_to_silent(self, tmp_path, monkeypatch):
        state = _make_state(3, tmp_path)
        tts = FakeTTS(fail_idxs={2})  # shot 2 失败

        await _run_voice_no_align(state, tmp_path, tts, monkeypatch)

        # 三个都应该有 wav（失败的那个走了 silent 兜底）
        assert all(ss.wav_path is not None for ss in state.shots)
        # shot 2 有错误记录
        assert any("tts:" in e for e in state.shots[1].errors)


async def _run_voice_no_align(state, tmp_path, tts, monkeypatch):
    """绕过 atempo（不依赖真 ffmpeg）。"""
    monkeypatch.setattr(
        "src.agents.voice.task_output_dir", lambda _tid: tmp_path
    )
    await run_voice_agent(
        state, tts=tts, output_root=tmp_path, align_to_video=False
    )


# ─── Subtitle Agent ──────────────────────────────────────────────


class TestSubtitleAgent:
    @pytest.mark.asyncio
    async def test_uses_whisper_when_available(self, tmp_path, monkeypatch):
        state = _make_state(2, tmp_path)
        # 给每个 shot 加 wav 路径
        for ss in state.shots:
            wav = tmp_path / "voice" / f"{ss.idx:02d}.wav"
            wav.parent.mkdir(parents=True, exist_ok=True)
            wav.write_bytes(b"RIFF\x00\x00")
            ss.wav_path = wav

        whisper = FakeWhisper(healthy=True, fail=False)
        monkeypatch.setattr(
            "src.agents.subtitle.task_output_dir", lambda _tid: tmp_path
        )

        await run_subtitle_agent(state, asr=whisper, output_root=tmp_path)

        assert all(ss.srt_path is not None for ss in state.shots)
        assert len(whisper.calls) == 2

    @pytest.mark.asyncio
    async def test_fallback_when_no_whisper(self, tmp_path, monkeypatch):
        state = _make_state(2, tmp_path)
        monkeypatch.setattr(
            "src.agents.subtitle.task_output_dir", lambda _tid: tmp_path
        )

        await run_subtitle_agent(state, asr=None, output_root=tmp_path)

        # 仍然生成 SRT（用文本均分兜底）
        assert all(ss.srt_path is not None for ss in state.shots)
        for ss in state.shots:
            content = ss.srt_path.read_text(encoding="utf-8")
            assert "-->" in content  # 有时间码

    @pytest.mark.asyncio
    async def test_whisper_failure_falls_back(self, tmp_path, monkeypatch):
        state = _make_state(2, tmp_path)
        for ss in state.shots:
            wav = tmp_path / "voice" / f"{ss.idx:02d}.wav"
            wav.parent.mkdir(parents=True, exist_ok=True)
            wav.write_bytes(b"RIFF\x00\x00")
            ss.wav_path = wav

        whisper = FakeWhisper(healthy=True, fail=True)
        monkeypatch.setattr(
            "src.agents.subtitle.task_output_dir", lambda _tid: tmp_path
        )

        await run_subtitle_agent(state, asr=whisper, output_root=tmp_path)

        # 仍有 srt（兜底了）
        assert all(ss.srt_path is not None for ss in state.shots)
        assert any("asr:" in e for e in state.shots[0].errors)

    @pytest.mark.asyncio
    async def test_skip_empty_narration(self, tmp_path, monkeypatch):
        state = _make_state(2, tmp_path)
        state.shots[0].narration = ""
        monkeypatch.setattr(
            "src.agents.subtitle.task_output_dir", lambda _tid: tmp_path
        )

        await run_subtitle_agent(state, asr=None, output_root=tmp_path)

        assert state.shots[0].srt_path is None
        assert state.shots[1].srt_path is not None


# ─── ASR helper（merge_srts / srt_from_text 单测） ──────────────


class TestSrtHelpers:
    def test_srt_from_text(self, tmp_path):
        from src.adapters.asr import srt_from_text

        out = tmp_path / "test.srt"
        srt_from_text("第一句话。第二句话！第三句", duration_sec=9.0, out_path=out)
        content = out.read_text(encoding="utf-8")
        assert "00:00:00" in content
        assert "-->" in content
        # 至少 3 个 block
        assert content.count("-->") >= 3

    def test_merge_srts(self, tmp_path):
        from src.adapters.asr import merge_srts

        # 创建两个独立 SRT
        s1 = tmp_path / "1.srt"
        s1.write_text(
            "1\n00:00:00,000 --> 00:00:06,000\nHello\n",
            encoding="utf-8",
        )
        s2 = tmp_path / "2.srt"
        s2.write_text(
            "1\n00:00:00,000 --> 00:00:06,000\nWorld\n",
            encoding="utf-8",
        )
        out = tmp_path / "merged.srt"
        merge_srts([(s1, 0.0, 6.0), (s2, 6.0, 12.0)], out)

        content = out.read_text(encoding="utf-8")
        # 第二段应该被偏移到 6s 后
        assert "00:00:06,000" in content
        assert "00:00:12,000" in content
        assert "Hello" in content
        assert "World" in content
