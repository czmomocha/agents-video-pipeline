"""FFmpeg 合成器 —— 把多段 mp4 拼成最终视频。

策略：
  1. 先把所有 clip 规范化（统一分辨率/fps/编码 +可选音轨）到 tmp 目录
  2. 用 concat demuxer 拼接
  3. 字幕：可选烧录到最终输出（subtitles 滤镜，需要二次编码）
  4. 转场：M2-D-1/2 默认硬切；xfade 留给后续

M2-D-2 增量：
  - normalize_clip_with_audio：把 clip + 配音 wav（可变速）合成单段 mp4
  - burn_subtitles：把 SRT 烧录进视频
  - probe_audio_duration：探测 wav/mp3 时长
  - atempo_clip：对 wav 做时间拉伸到目标时长
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


# ─── 探测 ──────────────────────────────────────────────────────


async def probe_audio_duration(audio_path: Path) -> float:
    """返回音频时长（秒）。失败返回 0.0。"""
    if shutil.which("ffprobe") is None or not audio_path.exists():
        return 0.0
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(audio_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except Exception:
        return 0.0


# ─── 音频变速 ──────────────────────────────────────────────────


async def atempo_to_duration(
    src: Path,
    dst: Path,
    target_duration_sec: float,
    *,
    max_speedup: float = 1.5,
    max_slowdown: float = 0.7,
) -> tuple[Path, float]:
    """把音频时长拉伸/压缩到 target_duration_sec。

    使用 FFmpeg atempo 滤镜（保音调变速）。超出 [max_slowdown, max_speedup] 范围
    时不再拉伸，按上下限处理（保证听感不崩）。

    Returns:
        (dst, applied_speed)：实际应用的速率（可能因为限幅与目标有偏差）
    """
    if shutil.which("ffmpeg") is None:
        raise CompositorError("ffmpeg not found")
    src_dur = await probe_audio_duration(src)
    if src_dur <= 0:
        raise CompositorError(f"failed to probe audio duration: {src}")

    raw_speed = src_dur / target_duration_sec  # 1.2 = 加速 20%
    speed = min(max(raw_speed, max_slowdown), max_speedup)
    if abs(speed - raw_speed) > 0.01:
        log.warning(
            f"[atempo] clamped speed {raw_speed:.2f} → {speed:.2f}; "
            f"audio will be {src_dur / speed:.2f}s vs target {target_duration_sec:.2f}s"
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    # atempo 单次只支持 0.5-2.0，超出范围需要级联（这里限幅在 0.7-1.5 故无需级联）
    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-filter:a", f"atempo={speed:.4f}",
        "-c:a", "pcm_s16le",  # 统一为 PCM 便于后续 mux
        "-ar", "44100",
        "-ac", "1",
        str(dst),
    ]
    log.debug(f"[atempo] {src.name} ({src_dur:.2f}s) → {dst.name} ({target_duration_sec:.2f}s, speed={speed:.3f})")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not dst.exists():
        raise CompositorError(
            f"atempo failed: rc={proc.returncode}\n"
            f"{stderr.decode(errors='ignore')[-500:]}"
        )
    return dst, speed


# ─── 规范化（视频，可选带音频） ──────────────────────────────


async def normalize_clip(
    src: Path,
    dst: Path,
    *,
    width: int,
    height: int,
    fps: int,
    audio_path: Path | None = None,
) -> Path:
    """规范化单个 clip 到统一参数。

    Args:
        audio_path: 可选音频；若给了则混入视频音轨（取代原视频音轨）。
    """
    if shutil.which("ffmpeg") is None:
        raise CompositorError("ffmpeg not found in PATH")

    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={fps},format=yuv420p"
    )

    cmd: list[str] = ["ffmpeg", "-y", "-i", str(src)]
    if audio_path is not None and audio_path.exists():
        cmd += ["-i", str(audio_path)]

    cmd += [
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
    ]

    if audio_path is not None and audio_path.exists():
        cmd += [
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
        ]
    else:
        cmd += ["-an"]  # 无音频
    cmd.append(str(dst))

    log.debug(f"[compositor.normalize] {src.name} → {dst.name} ({width}x{height}@{fps}, audio={audio_path is not None})")
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


# ─── 字幕烧录 ──────────────────────────────────────────────────


async def burn_subtitles(
    src: Path,
    dst: Path,
    srt_path: Path,
    *,
    font_size: int = 28,
    primary_color: str = "&H00FFFFFF",  # 白
    outline_color: str = "&H00000000",  # 黑描边
    outline: int = 2,
) -> Path:
    """把 SRT 烧录到视频上（二次编码，质量轻微损失）。"""
    if shutil.which("ffmpeg") is None:
        raise CompositorError("ffmpeg not found")
    if not srt_path.exists():
        raise CompositorError(f"srt not found: {srt_path}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    # FFmpeg 的 subtitles 滤镜 path 在 Windows 要做特殊转义（避开 "C:\" 中的冒号）
    # Linux/Mac 直接转 / 即可
    srt_str = str(srt_path).replace("\\", "/").replace(":", r"\:")
    style = (
        f"FontSize={font_size},"
        f"PrimaryColour={primary_color},"
        f"OutlineColour={outline_color},"
        f"Outline={outline},BorderStyle=1"
    )
    vf = f"subtitles='{srt_str}':force_style='{style}'"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-c:a", "copy",
        str(dst),
    ]
    log.info(f"[compositor.burn_srt] {src.name} + {srt_path.name} → {dst.name}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not dst.exists():
        raise CompositorError(
            f"subtitle burn failed: rc={proc.returncode}\n"
            f"{stderr.decode(errors='ignore')[-500:]}"
        )
    return dst


# ─── 拼接 ──────────────────────────────────────────────────────


async def concat_clips(
    clips: list[Path],
    out_path: Path,
    *,
    width: int = 1920,
    height: int = 1080,
    fps: int = 24,
    normalize: bool = True,
    audio_per_clip: list[Path | None] | None = None,
) -> Path:
    """把多段 clip 拼成最终视频。

    Args:
        audio_per_clip: 与 clips 等长的列表，每项是该 clip 的配音 wav；
                        None 元素表示该 clip 无配音（静音）。
    """
    if not clips:
        raise CompositorError("no clips to concat")
    if shutil.which("ffmpeg") is None:
        raise CompositorError("ffmpeg not found in PATH")
    if audio_per_clip is not None and len(audio_per_clip) != len(clips):
        raise CompositorError(
            f"audio_per_clip length {len(audio_per_clip)} != clips length {len(clips)}"
        )

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
                audio = audio_per_clip[i] if audio_per_clip is not None else None
                await normalize_clip(
                    clip, norm_path,
                    width=width, height=height, fps=fps,
                    audio_path=audio,
                )
                normalized.append(norm_path)
            if not normalized:
                raise CompositorError("all clips missing after normalization stage")
            inputs = normalized
        else:
            inputs = [c for c in clips if c.exists()]
            if not inputs:
                raise CompositorError("no existing clips to concat")

        # 2. 写 concat list
        list_file = tmp / "concat.txt"
        with list_file.open("w", encoding="utf-8") as f:
            for p in inputs:
                escaped = str(p).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        # 3. concat
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
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
    """自动从 clips 中探测一个统一的分辨率/fps。"""
    for clip in clips:
        if clip.exists():
            info = await probe_video(clip)
            if info:
                w = info["width"]
                h = info["height"]
                fps = int(round(info["fps"])) or 24
                return w, h, fps
    return 1920, 1080, 24
