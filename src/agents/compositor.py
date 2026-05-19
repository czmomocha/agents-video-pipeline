"""CompositorAgent —— 剪辑师（FFmpeg 拼接）。

M2-D-1：把所有成功渲染的 clip 拼成 final.mp4（无配音、无字幕）。
M2-D-2 会扩展：加配音、加字幕、加 BGM、xfade 转场。
"""
from __future__ import annotations

from pathlib import Path

from src.adapters.compositor_ffmpeg import auto_resolution, concat_clips
from src.config import Settings, load_settings, parse_resolution, task_output_dir
from src.orchestrator.state import PipelineState
from src.utils.logging import get_logger

log = get_logger()


async def run_compositor(
    state: PipelineState,
    *,
    settings: Settings | None = None,
    output_root: Path | None = None,
) -> Path:
    """把所有成功渲染的 clip 拼成 final.mp4。

    跳过失败镜头（仅 warning），保证至少有部分成片。
    """
    s = settings or load_settings()
    if not state.shots:
        raise RuntimeError("compositor requires shots")

    if output_root is None:
        output_root = task_output_dir(state.task_id)
    final_path = output_root / "final.mp4"

    # 收集所有成功渲染的 clip（按 idx 排序）
    clips: list[Path] = []
    skipped: list[int] = []
    for ss in sorted(state.shots, key=lambda x: x.idx):
        if ss.clip_path is not None and ss.clip_path.exists():
            clips.append(ss.clip_path)
        else:
            skipped.append(ss.idx)

    if not clips:
        raise RuntimeError(
            f"compositor: no successfully rendered clips. "
            f"All {len(state.shots)} shots failed."
        )
    if skipped:
        log.warning(f"[compositor] skipping failed shots: {skipped}")

    # 决定统一目标分辨率/fps
    # 优先级：plan.style.aspect_ratio + settings.default_resolution → 探测第一段 clip
    target_w, target_h, target_fps = await auto_resolution(clips)

    # 用 plan 指定的 aspect ratio 校正（横/竖屏）
    if state.plan and state.plan.style.aspect_ratio == "portrait":
        # 探测出来若是横屏，强制用配置的 portrait 分辨率
        if target_w > target_h:
            w, h = parse_resolution(s.default_resolution)
            target_w, target_h = h, w  # 转竖屏

    log.info(
        f"[compositor] composing {len(clips)} clips → {target_w}x{target_h}@{target_fps}fps"
    )

    out = await concat_clips(
        clips=clips,
        out_path=final_path,
        width=target_w,
        height=target_h,
        fps=target_fps,
        normalize=True,
    )

    state.output_path = out
    state.metrics["composited_shots"] = len(clips)
    state.metrics["skipped_shots"] = skipped
    state.metrics["final_resolution"] = f"{target_w}x{target_h}"
    state.metrics["final_fps"] = target_fps

    log.info(f"[compositor] ✓ final video → {out}")
    return out
