"""末帧提取（FFmpeg）—— 用于 I2V 链式：把上一镜头的最后一帧作为下一镜头的首帧。

不引入 OpenCV / Pillow，零额外依赖。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from src.utils.logging import get_logger

log = get_logger()


class FrameExtractError(Exception):
    pass


async def extract_last_frame(video_path: Path, out_path: Path) -> Path:
    """从视频中提取最后一帧，保存为 PNG。

    使用 FFmpeg 的 `sseof -1` + 单帧抽取，比按帧号定位更鲁棒
    （不需要先探测总帧数）。
    """
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")
    if shutil.which("ffmpeg") is None:
        raise FrameExtractError("ffmpeg not found in PATH")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # -sseof -1: 从倒数 1 秒的位置开始
    # -update 1 -frames:v 1: 只输出最后一帧
    cmd = [
        "ffmpeg",
        "-y",
        "-sseof", "-1",
        "-i", str(video_path),
        "-update", "1",
        "-frames:v", "1",
        "-q:v", "2",
        str(out_path),
    ]
    log.debug(f"[frame_extract] {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0 or not out_path.exists():
        raise FrameExtractError(
            f"ffmpeg returncode={proc.returncode}: {stderr.decode(errors='ignore')[-500:]}"
        )

    log.info(f"[frame_extract] {video_path.name} → {out_path.name}")
    return out_path


async def probe_video(video_path: Path) -> dict:
    """用 ffprobe 探测视频信息（width/height/fps/duration）。

    返回字典；ffprobe 不存在或失败时返回空 dict（不抛异常）。
    """
    if shutil.which("ffprobe") is None:
        return {}
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,duration",
        "-of", "csv=p=0",
        str(video_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return {}
    try:
        line = stdout.decode().strip()
        parts = line.split(",")
        w = int(parts[0])
        h = int(parts[1])
        # r_frame_rate 形如 "24/1"
        fr = parts[2]
        if "/" in fr:
            num, den = fr.split("/")
            fps = float(num) / float(den) if float(den) != 0 else float(num)
        else:
            fps = float(fr)
        dur = float(parts[3]) if len(parts) > 3 and parts[3] else 0.0
        return {"width": w, "height": h, "fps": fps, "duration": dur}
    except Exception as e:
        log.warning(f"[probe] parse failed: {e}")
        return {}
