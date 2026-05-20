"""SubtitleAgent —— 字幕生成。

优先：whisper.cpp 转录每段 wav 得到时间码精准的 SRT
兜底：用 ShotState.narration + duration_sec 文本均分生成 SRT
"""
from __future__ import annotations

from pathlib import Path

from src.adapters.asr import (
    ASRError,
    WhisperCppASR,
    srt_from_text,
)
from src.config import Settings, load_settings, task_output_dir
from src.orchestrator.state import PipelineState, ShotState
from src.utils.logging import get_logger

log = get_logger()


async def run_subtitle_agent(
    state: PipelineState,
    *,
    asr: WhisperCppASR | None,
    settings: Settings | None = None,
    output_root: Path | None = None,
) -> list[ShotState]:
    """为每个 shot 生成独立的 SRT 文件。

    Args:
        asr: WhisperCppASR 实例；为 None 或 health 失败时全部走文本均分兜底。
    """
    s = settings or load_settings()
    if not state.shots:
        raise RuntimeError("subtitle agent requires shots")

    if output_root is None:
        output_root = task_output_dir(state.task_id)
    srt_dir = output_root / "srt"
    srt_dir.mkdir(parents=True, exist_ok=True)

    asr_available = asr is not None and await asr.health() if asr else False
    if not asr_available:
        log.info("[subtitle] whisper not available, using text-uniform fallback")

    success = 0
    for ss in state.shots:
        if not ss.narration.strip():
            continue

        srt_path = srt_dir / f"{ss.idx:02d}.srt"

        # 优先 whisper（需要 wav 已就绪）
        if asr_available and ss.wav_path is not None and ss.wav_path.exists():
            try:
                await asr.transcribe(ss.wav_path, srt_path)
                ss.srt_path = srt_path
                success += 1
                continue
            except (ASRError, FileNotFoundError) as e:
                log.warning(f"[subtitle] shot {ss.idx} whisper failed: {e}; fallback")
                ss.errors.append(f"asr: {e}")

        # 文本均分兜底
        try:
            srt_from_text(
                ss.narration,
                duration_sec=float(ss.duration_sec),
                out_path=srt_path,
            )
            ss.srt_path = srt_path
            success += 1
        except Exception as e:
            log.error(f"[subtitle] shot {ss.idx} fallback failed: {e}")
            ss.errors.append(f"srt: {e}")

    log.info(f"[subtitle] generated {success}/{len(state.shots)} SRT files")
    return state.shots
