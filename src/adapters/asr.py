"""ASR / 字幕生成 —— whisper.cpp（Apple Silicon Metal 加速）。

调用 whisper.cpp 的 CLI 入口（whisper-cli 或 main），输入 wav 输出 SRT。

兜底：如果 whisper.cpp 不可用，退化为按 narration 文本均匀分配时间码生成 SRT。
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from src.config import Settings, load_settings
from src.utils.logging import get_logger

log = get_logger()


class ASRError(Exception):
    pass


class WhisperCppASR:
    """whisper.cpp CLI 适配器。

    需要：
      brew install whisper-cpp  (提供 whisper-cli)
      或自行编译 https://github.com/ggerganov/whisper.cpp
      下载 GGML 模型（如 ggml-base.bin / ggml-medium.bin）到 models/whisper/
    """

    def __init__(
        self,
        executable: str = "whisper-cli",
        model_path: Path | None = None,
        language: str = "zh",
    ):
        self.exe = executable
        self.model_path = model_path
        self.language = language
        # 兼容旧的 `main` 二进制
        if shutil.which(self.exe) is None and shutil.which("main") is not None:
            self.exe = "main"

    async def health(self) -> bool:
        if shutil.which(self.exe) is None:
            return False
        if self.model_path is None or not self.model_path.exists():
            return False
        return True

    async def transcribe(self, wav_path: Path, srt_out: Path) -> Path:
        """把 wav 转录为 SRT 字幕文件。"""
        if not await self.health():
            raise ASRError(
                f"whisper-cli not ready (exe={self.exe!r}, model={self.model_path}). "
                f"Install: brew install whisper-cpp; download model to models/whisper/"
            )
        if not wav_path.exists():
            raise FileNotFoundError(f"wav not found: {wav_path}")

        srt_out.parent.mkdir(parents=True, exist_ok=True)
        # whisper-cli 的 --output-srt 会在 -of 指定的文件名基础上加 .srt 后缀
        of_base = srt_out.with_suffix("")  # 去掉 .srt 后缀作为 base
        cmd = [
            self.exe,
            "-m", str(self.model_path),
            "-f", str(wav_path),
            "-l", self.language,
            "--output-srt",
            "-of", str(of_base),
            "--no-prints",  # 减少日志噪音
        ]
        log.debug(f"[asr.whisper] {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        # whisper-cli 实际写到 of_base + ".srt"
        actual_path = of_base.with_suffix(".srt")
        if proc.returncode != 0 or not actual_path.exists():
            raise ASRError(
                f"whisper failed: rc={proc.returncode}\n"
                f"{stderr.decode(errors='ignore')[-500:]}"
            )
        # 如果实际路径与请求路径不同，rename
        if actual_path != srt_out:
            actual_path.rename(srt_out)
        log.info(f"[asr.whisper] {wav_path.name} → {srt_out.name}")
        return srt_out


# ─────────────────────────────────────────────────────────────────
#  Fallback：按文本均匀分配时间码
# ─────────────────────────────────────────────────────────────────


def srt_from_text(
    text: str,
    duration_sec: float,
    out_path: Path,
    *,
    max_chars_per_line: int = 18,
) -> Path:
    """把整段文本按字符数均匀分配到时长，生成简单 SRT。

    适用于 whisper 不可用、或想保留剧本原文的场景。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = text.strip()
    if not text:
        # 空文本写空 srt
        out_path.write_text("", encoding="utf-8")
        return out_path

    # 简单按标点切句
    import re
    parts = re.split(r"(?<=[。！？，,.!?；;])", text)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        parts = [text]

    # 进一步按字数限制切（避免单条字幕太长）
    lines: list[str] = []
    for p in parts:
        while len(p) > max_chars_per_line * 2:
            lines.append(p[: max_chars_per_line])
            p = p[max_chars_per_line:]
        lines.append(p)

    n = len(lines)
    per = duration_sec / n if n > 0 else duration_sec

    srt_blocks: list[str] = []
    for i, line in enumerate(lines):
        start = i * per
        end = (i + 1) * per
        srt_blocks.append(
            f"{i + 1}\n{_fmt_ts(start)} --> {_fmt_ts(end)}\n{line}\n"
        )
    out_path.write_text("\n".join(srt_blocks), encoding="utf-8")
    log.info(f"[asr.fallback] generated {n}-line SRT → {out_path.name}")
    return out_path


def _fmt_ts(sec: float) -> str:
    """SRT 时间格式：HH:MM:SS,mmm"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ─────────────────────────────────────────────────────────────────
#  工厂 + 合并多段 SRT 工具
# ─────────────────────────────────────────────────────────────────


def make_whisper_asr(settings: Settings | None = None) -> WhisperCppASR:
    s = settings or load_settings()
    whisper_dir = s.models_dir / "whisper"
    candidates = (
        list(whisper_dir.glob("ggml-medium*.bin"))
        + list(whisper_dir.glob("ggml-base*.bin"))
        + list(whisper_dir.glob("ggml-*.bin"))
    )
    model = candidates[0] if candidates else None
    return WhisperCppASR(model_path=model)


def merge_srts(
    srt_paths: list[tuple[Path, float, float]],
    out_path: Path,
) -> Path:
    """把多段独立 SRT 合并成一个全局时间轴的 SRT。

    Args:
        srt_paths: list of (srt_file, abs_start_sec, abs_end_sec)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    counter = 1
    for srt_file, abs_start, _abs_end in srt_paths:
        if not srt_file.exists():
            continue
        try:
            content = srt_file.read_text(encoding="utf-8")
        except Exception as e:
            log.warning(f"[srt_merge] read failed {srt_file}: {e}")
            continue

        for block in content.strip().split("\n\n"):
            lines = block.strip().split("\n")
            if len(lines) < 3:
                continue
            # 时间码行（可能在第 1 或 第 2 行，取决于是否有编号）
            ts_line_idx = 1 if "-->" in lines[1] else 0
            ts_line = lines[ts_line_idx]
            text_lines = lines[ts_line_idx + 1:]
            try:
                start_str, end_str = [t.strip() for t in ts_line.split("-->")]
                start_sec = _parse_ts(start_str) + abs_start
                end_sec = _parse_ts(end_str) + abs_start
            except Exception as e:
                log.warning(f"[srt_merge] parse ts failed: {ts_line!r} ({e})")
                continue
            blocks.append(
                f"{counter}\n{_fmt_ts(start_sec)} --> {_fmt_ts(end_sec)}\n"
                + "\n".join(text_lines)
                + "\n"
            )
            counter += 1

    out_path.write_text("\n".join(blocks), encoding="utf-8")
    log.info(f"[srt_merge] {len(srt_paths)} files → {out_path.name} ({counter - 1} blocks)")
    return out_path


def _parse_ts(s: str) -> float:
    """SRT 'HH:MM:SS,mmm' → 秒"""
    s = s.replace(".", ",")
    h, m, rest = s.split(":")
    if "," in rest:
        sec, ms = rest.split(",")
    else:
        sec, ms = rest, "0"
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0
