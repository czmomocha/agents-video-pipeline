"""CompositorAgent —— 剪辑师（FFmpeg 拼接）。

M2-D-2：升级支持音轨 + 字幕烧录。

流程：
  1. 收集成功渲染的 clip + 对应 wav + 对应 srt
  2. 合并所有 SRT 为全局时间轴 SRT（用于最终烧录）
  3. concat_clips（带音轨）
  4. burn_subtitles（如启用且有合并 SRT）
"""
from __future__ import annotations

from pathlib import Path

from src.adapters.asr import merge_srts
from src.adapters.compositor_ffmpeg import (
    auto_resolution,
    burn_subtitles,
    concat_clips,
)
from src.config import Settings, load_settings, parse_resolution, task_output_dir
from src.orchestrator.state import PipelineState
from src.utils.logging import get_logger

log = get_logger()


async def run_compositor(
    state: PipelineState,
    *,
    settings: Settings | None = None,
    output_root: Path | None = None,
    burn_srt: bool = True,
) -> Path:
    """把所有成功渲染的 clip 拼成 final.mp4（含音轨 + 字幕）。

    Args:
        burn_srt: 是否把字幕烧录到视频上（True：硬字幕；False：仅产出 .srt 文件）
    """
    s = settings or load_settings()
    if not state.shots:
        raise RuntimeError("compositor requires shots")

    if output_root is None:
        output_root = task_output_dir(state.task_id)
    final_path = output_root / "final.mp4"

    # 1. 收集成功渲染的 clip（按 idx 排序），同步收集对应 wav / srt
    sorted_shots = sorted(state.shots, key=lambda x: x.idx)
    clips: list[Path] = []
    audios: list[Path | None] = []
    skipped: list[int] = []

    # 合并 SRT 用：(srt_path, abs_start_sec, abs_end_sec)
    srt_pieces: list[tuple[Path, float, float]] = []
    cursor = 0.0

    for ss in sorted_shots:
        if ss.clip_path is not None and ss.clip_path.exists():
            clips.append(ss.clip_path)
            audios.append(ss.wav_path if ss.wav_path and ss.wav_path.exists() else None)
            if ss.srt_path is not None and ss.srt_path.exists():
                srt_pieces.append((ss.srt_path, cursor, cursor + ss.duration_sec))
            cursor += ss.duration_sec
        else:
            skipped.append(ss.idx)

    if not clips:
        raise RuntimeError(
            f"compositor: no successfully rendered clips. "
            f"All {len(state.shots)} shots failed."
        )
    if skipped:
        log.warning(f"[compositor] skipping failed shots: {skipped}")

    has_audio = any(a is not None for a in audios)
    log.info(
        f"[compositor] composing {len(clips)} clips "
        f"(audio: {sum(1 for a in audios if a)}/{len(audios)}, "
        f"srt: {len(srt_pieces)}/{len(clips)})"
    )

    # 2. 决定统一目标分辨率/fps
    target_w, target_h, target_fps = await auto_resolution(clips)
    if state.plan and state.plan.style.aspect_ratio == "portrait":
        if target_w > target_h:
            w, h = parse_resolution(s.default_resolution)
            target_w, target_h = h, w

    # 3. 合并 SRT（全局时间轴）
    merged_srt: Path | None = None
    if srt_pieces:
        merged_srt = output_root / "subtitles.srt"
        merge_srts(srt_pieces, merged_srt)

    # 4. concat（带音轨，若有）
    concat_out = final_path if not (burn_srt and merged_srt) else (output_root / "_concat_no_subs.mp4")
    await concat_clips(
        clips=clips,
        out_path=concat_out,
        width=target_w,
        height=target_h,
        fps=target_fps,
        normalize=True,
        audio_per_clip=audios if has_audio else None,
    )

    # 5. 字幕烧录（可选）
    if burn_srt and merged_srt is not None:
        try:
            await burn_subtitles(concat_out, final_path, merged_srt)
            # 删除中间产物
            try:
                concat_out.unlink()
            except OSError:
                pass
        except Exception as e:
            log.warning(f"[compositor] subtitle burn failed: {e}; using non-burned output")
            # 把 concat 输出 rename 为 final
            if concat_out.exists() and concat_out != final_path:
                concat_out.rename(final_path)

    state.output_path = final_path
    state.metrics["composited_shots"] = len(clips)
    state.metrics["skipped_shots"] = skipped
    state.metrics["final_resolution"] = f"{target_w}x{target_h}"
    state.metrics["final_fps"] = target_fps
    state.metrics["has_audio"] = has_audio
    state.metrics["has_subtitles"] = bool(merged_srt)
    state.metrics["subtitles_burned"] = bool(burn_srt and merged_srt)

    log.info(f"[compositor] ✓ final video → {final_path}")
    return final_path
