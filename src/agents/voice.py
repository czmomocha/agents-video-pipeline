"""VoiceAgent —— 配音生成。

为每个 ShotState.narration 调用 TTS Provider，输出 wav，
再用 atempo 拉伸到与 shot.duration_sec 对齐（音画同步）。
"""
from __future__ import annotations

from pathlib import Path

from src.adapters.compositor_ffmpeg import atempo_to_duration, probe_audio_duration
from src.adapters.tts import SilentTTS, TTSError, TTSProvider
from src.config import Settings, load_settings, task_output_dir
from src.orchestrator.state import PipelineState, ShotState
from src.utils.logging import get_logger

log = get_logger()


async def run_voice_agent(
    state: PipelineState,
    *,
    tts: TTSProvider,
    settings: Settings | None = None,
    output_root: Path | None = None,
    align_to_video: bool = True,
) -> list[ShotState]:
    """为每个有 narration 的 shot 生成 wav。

    Args:
        align_to_video: 是否用 atempo 把 wav 时长对齐到 shot.duration_sec
                        （M2-D-2 默认 True：策略 A 视频固定，TTS 变速）
    """
    s = settings or load_settings()
    if not state.shots:
        raise RuntimeError("voice agent requires shots")

    if output_root is None:
        output_root = task_output_dir(state.task_id)
    voice_dir = output_root / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)

    n = len(state.shots)
    success = 0
    silent_fallback = SilentTTS()

    for ss in state.shots:
        if not ss.narration.strip():
            log.info(f"[voice] shot {ss.idx}/{n} no narration, skip")
            continue

        raw_path = voice_dir / f"{ss.idx:02d}_raw.wav"
        final_path = voice_dir / f"{ss.idx:02d}.wav"

        # 1) 合成 raw
        try:
            await tts.synthesize(ss.narration, raw_path)
        except TTSError as e:
            log.error(f"[voice] shot {ss.idx} TTS failed: {e}; using silent fallback")
            ss.errors.append(f"tts: {e}")
            await silent_fallback.synthesize(  # type: ignore[call-arg]
                ss.narration, raw_path,
                duration_sec=float(ss.duration_sec),
            )

        # 2) atempo 对齐到视频时长（可选）
        try:
            if align_to_video:
                _, applied_speed = await atempo_to_duration(
                    raw_path,
                    final_path,
                    target_duration_sec=float(ss.duration_sec),
                )
                ss.wav_path = final_path
                if abs(applied_speed - 1.0) > 0.05:
                    log.info(
                        f"[voice] shot {ss.idx} speed-adjusted to {applied_speed:.2f}x"
                    )
            else:
                # 不对齐：直接用 raw
                raw_path.rename(final_path)
                ss.wav_path = final_path
            success += 1
        except Exception as e:
            log.warning(f"[voice] shot {ss.idx} atempo failed: {e}; using raw")
            # 至少用 raw 兜底
            if raw_path.exists():
                ss.wav_path = raw_path

    log.info(f"[voice] generated {success}/{n} voice clips with backend={tts.backend_name}")
    return state.shots
