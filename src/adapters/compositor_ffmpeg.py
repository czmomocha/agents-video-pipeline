"""FFmpeg 合成器 —— 把多段 mp4 拼成最终视频。

策略：
  1. 先把所有 clip 规范化（统一分辨率/fps/编码），保存到 tmp 目录
  2. 用 concat demuxer 拼接（最快，零质量损失）
  3. 转场：默认硬切；M2-D-1 暂不实现 fade/dissolve（FFmpeg xfade 拼接复杂，
     M2-D-2 加 voice 时一并实现）
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from src.adapters.frame_extractor import probe_video
from src.utils.logging import get_logger

log = get_logger()


class CompositorError(Exception):
    pass


async def normalize_clip(
    src: Path,
    dst: Path,
    *,
    width: int,
    height: int,
    fps: int,
) -> Path:
    """把单个 clip 规范化到统一参数（避免 concat 时分辨率/fps 不一致导致失败）。

    使用 libx264 + yuv420p（最广兼容）。CRF=18 接近无损。
    """
    if shutil.which("ffmpeg") is None:
        raise CompositorError("ffmpeg not found in PATH")

    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
               f"fps={fps},format=yuv420p",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-an",  # M2-D-1 无音频；M2-D-2 加配音时改
        str(dst),
    ]
    log.debug(f"[compositor.normalize] {src.name} → {dst.name} ({width}x{height}@{fps})")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not dst.exists():
        raise CompositorError(
            f"normalize failed for {src}: rc={proc.returncode}\n"
            f"{stderr.decode(errors='ignore')[-500:]}"
        )
    return dst


async def concat_clips(
    clips: list[Path],
    out_path: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 24,
    normalize: bool = True,
) -> Path:
    """把多段 clip 拼成最终视频。

    Args:
        clips: 视频片段路径列表（顺序即拼接顺序）
        out_path: 输出 mp4 路径
        width/height/fps: 统一规范化目标参数
        normalize: 是否预规范化（True 更稳，False 更快）
    """
    if not clips:
        raise CompositorError("no clips to concat")
    if shutil.which("ffmpeg") is None:
        raise CompositorError("ffmpeg not found in PATH")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="compositor_") as tmp_str:
        tmp = Path(tmp_str)

        # 1. 规范化（如果开启）
        if normalize:
            normalized: list[Path] = []
            for i, clip in enumerate(clips):
                if not clip.exists():
                    log.warning(f"[compositor] skip missing clip: {clip}")
                    continue
                norm_path = tmp / f"norm_{i:03d}.mp4"
                await normalize_clip(clip, norm_path, width=width, height=height, fps=fps)
                normalized.append(norm_path)
            if not normalized:
                raise CompositorError("all clips missing after normalization stage")
            inputs = normalized
        else:
            inputs = [c for c in clips if c.exists()]
            if not inputs:
                raise CompositorError("no existing clips to concat")

        # 2. 写 concat list 文件
        list_file = tmp / "concat.txt"
        with list_file.open("w", encoding="utf-8") as f:
            for p in inputs:
                # FFmpeg concat demuxer 需要单引号包裹的路径，且转义内部单引号
                escaped = str(p).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        # 3. concat
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",  # 已规范化，直接 copy（极快）
            str(out_path),
        ]
        log.info(f"[compositor] concat {len(inputs)} clips → {out_path}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not out_path.exists():
            raise CompositorError(
                f"concat failed: rc={proc.returncode}\n"
                f"{stderr.decode(errors='ignore')[-500:]}"
            )

    log.info(f"[compositor] done → {out_path}  ({out_path.stat().st_size // 1024} KB)")
    return out_path


async def auto_resolution(clips: list[Path]) -> tuple[int, int, int]:
    """自动从 clips 中探测一个统一的分辨率/fps（取众数 or 第一个）。"""
    for clip in clips:
        if clip.exists():
            info = await probe_video(clip)
            if info:
                w = info["width"]
                h = info["height"]
                fps = int(round(info["fps"])) or 24
                return w, h, fps
    return 1920, 1080, 24
