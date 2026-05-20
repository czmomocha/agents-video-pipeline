"""TTS 抽象层 —— 多后端可切换，全本地优先。

支持的后端：
  - "piper"     ：piper-tts，纯本地、轻量、零依赖打架（默认）
  - "edge"      ：edge-tts，优秀中文音质，但需联网（微软云）
  - "gpt_sovits"：GPT-SoVITS，最佳中文质量，但安装重
  - "silent"    ：兜底，生成静音 wav（让管线不中断）

调用契约：
  provider.synthesize(text, out_path, ...) -> Path
"""
from __future__ import annotations

import asyncio
import shutil
import struct
import wave
from pathlib import Path
from typing import Literal, Protocol

from src.config import Settings, load_settings
from src.utils.logging import get_logger

log = get_logger()


TTSBackend = Literal["piper", "edge", "gpt_sovits", "silent"]


class TTSError(Exception):
    pass


class TTSProvider(Protocol):
    """所有 TTS 后端的统一接口。"""

    backend_name: str

    async def health(self) -> bool: ...
    async def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> Path: ...


# ─────────────────────────────────────────────────────────────────
#  Silent backend：生成指定时长的静音 wav，作为兜底
# ─────────────────────────────────────────────────────────────────


class SilentTTS:
    backend_name = "silent"

    def __init__(self, default_duration_sec: float = 6.0, sample_rate: int = 22050):
        self.default_duration = default_duration_sec
        self.sample_rate = sample_rate

    async def health(self) -> bool:
        return True

    async def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        duration_sec: float | None = None,
    ) -> Path:
        dur = duration_sec or self.default_duration
        await asyncio.to_thread(self._write_silent_wav, out_path, dur)
        log.info(f"[tts.silent] {dur:.1f}s → {out_path.name}")
        return out_path

    def _write_silent_wav(self, out_path: Path, duration_sec: float) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        n_frames = int(duration_sec * self.sample_rate)
        with wave.open(str(out_path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)  # 16-bit
            f.setframerate(self.sample_rate)
            f.writeframes(struct.pack(f"<{n_frames}h", *([0] * n_frames)))


# ─────────────────────────────────────────────────────────────────
#  Piper backend：subprocess 调 `piper` 二进制（默认推荐）
# ─────────────────────────────────────────────────────────────────


class PiperTTS:
    """Piper TTS（rhasspy/piper），纯本地、轻量、CLI 调用。

    需要：
      brew install piper-tts   或   pip install piper-tts
      下载语音模型（.onnx + .json）到 models/piper/
    Mac 上推荐 zh_CN voices，参考：
      https://github.com/rhasspy/piper/blob/master/VOICES.md
    """

    backend_name = "piper"

    def __init__(self, model_path: Path | None = None, executable: str = "piper"):
        self.model_path = model_path
        self.exe = executable

    async def health(self) -> bool:
        if shutil.which(self.exe) is None:
            return False
        if self.model_path is None or not self.model_path.exists():
            return False
        return True

    async def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> Path:
        if not await self.health():
            raise TTSError(
                f"piper not ready (exe={self.exe!r}, model={self.model_path}). "
                f"Install: brew install piper-tts; download voice to models/piper/"
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.exe,
            "--model", str(self.model_path),
            "--output_file", str(out_path),
            "--length_scale", str(1.0 / speed),  # piper 的 length_scale 与 speed 反向
        ]
        log.debug(f"[tts.piper] {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(input=text.encode("utf-8"))
        if proc.returncode != 0 or not out_path.exists():
            raise TTSError(
                f"piper failed: rc={proc.returncode}\n"
                f"{stderr.decode(errors='ignore')[-500:]}"
            )
        log.info(f"[tts.piper] {len(text)} chars → {out_path.name}")
        return out_path


# ─────────────────────────────────────────────────────────────────
#  Edge-TTS backend：可选，需联网
# ─────────────────────────────────────────────────────────────────


class EdgeTTS:
    """Microsoft Edge TTS（云端，需联网）。zh-CN-XiaoxiaoNeural 等高质量音色。

    需要：pip install edge-tts
    注：违反"全本地"原则，仅作为 GPT-SoVITS 安装前的过渡选项。
    """

    backend_name = "edge"

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural"):
        self.default_voice = voice

    async def health(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False

    async def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> Path:
        try:
            import edge_tts
        except ImportError as e:
            raise TTSError("edge-tts not installed: pip install edge-tts") from e

        out_path.parent.mkdir(parents=True, exist_ok=True)
        # rate 形如 "+10%" / "-5%"
        rate_pct = int((speed - 1.0) * 100)
        rate_str = f"{'+' if rate_pct >= 0 else ''}{rate_pct}%"

        v = voice or self.default_voice
        # edge-tts 输出 mp3，转 wav 由 Compositor 兜底（FFmpeg 都吃）
        # 简单起见这里直接保存为 .mp3 后缀名也无妨——但为统一接口我们用 .wav 即可
        try:
            communicate = edge_tts.Communicate(text=text, voice=v, rate=rate_str)
            await communicate.save(str(out_path))
        except Exception as e:
            raise TTSError(f"edge-tts synthesize failed: {e}") from e
        log.info(f"[tts.edge] {len(text)} chars ({v}, rate={rate_str}) → {out_path.name}")
        return out_path


# ─────────────────────────────────────────────────────────────────
#  GPT-SoVITS backend：占位 stub，等 Mac 端就绪后实现
# ─────────────────────────────────────────────────────────────────


class GPTSoVITSStub:
    """GPT-SoVITS HTTP API stub。

    GPT-SoVITS 通常以 webui 或 api server 形式启动，监听本地端口。
    此类作为占位，待你 Mac 端启动后填入真实 URL/参数即可使用。
    """

    backend_name = "gpt_sovits"

    def __init__(self, base_url: str = "http://127.0.0.1:9880"):
        self.base_url = base_url

    async def health(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(self.base_url)
                return r.status_code < 500
        except Exception:
            return False

    async def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> Path:
        # TODO: 接入 GPT-SoVITS API（待 Mac 端就绪）
        raise TTSError(
            "GPT-SoVITS backend not implemented yet. "
            "Use --tts piper or --tts edge for now."
        )


# ─────────────────────────────────────────────────────────────────
#  工厂
# ─────────────────────────────────────────────────────────────────


def make_tts_provider(
    backend: TTSBackend = "piper",
    settings: Settings | None = None,
) -> TTSProvider:
    s = settings or load_settings()
    if backend == "piper":
        # 探测 models/piper/ 下的第一个 .onnx 文件
        piper_dir = s.models_dir / "piper"
        models = list(piper_dir.glob("*.onnx")) if piper_dir.exists() else []
        model = models[0] if models else None
        return PiperTTS(model_path=model)
    elif backend == "edge":
        return EdgeTTS()
    elif backend == "gpt_sovits":
        return GPTSoVITSStub()
    elif backend == "silent":
        return SilentTTS()
    else:
        raise ValueError(f"Unknown TTS backend: {backend}")


async def make_best_available_tts(
    settings: Settings | None = None,
    preference: list[TTSBackend] = None,  # type: ignore[assignment]
) -> TTSProvider:
    """按 preference 顺序探测可用的 TTS，返回第一个 health 通过的。"""
    if preference is None:
        preference = ["piper", "edge", "silent"]
    for backend in preference:
        p = make_tts_provider(backend, settings=settings)
        if await p.health():
            log.info(f"[tts] selected backend: {backend}")
            return p
    log.warning("[tts] no real TTS available, falling back to silent")
    return SilentTTS()
