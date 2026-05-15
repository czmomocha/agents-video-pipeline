"""Sulphur 2 自带的 prompt enhancer (GGUF) 适配器。

用 llama-cpp-python 直接跑本地 GGUF 模型，不走 Ollama。
M1 阶段把它做成可选：若 GGUF 文件不存在则跳过增强、直接返回原 prompt。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from src.utils.logging import get_logger

log = get_logger()


_SYS_PROMPT = (
    "You are a video generation prompt enhancer. "
    "Rewrite the user's intent into a rich cinematic Sulphur 2 / LTX-Video 2.3 prompt. "
    "Include subject, action, camera (wide/close-up/dolly/etc), lighting, mood, color palette, "
    "and visual style. 60-150 words. English only. No JSON, plain prose."
)


class SulphurPromptEnhancer:
    """惰性加载，跑完即可释放（节省 UMA）。"""

    def __init__(self, gguf_path: Path | None, n_ctx: int = 4096):
        self.gguf_path = gguf_path
        self.n_ctx = n_ctx
        self._llm = None

    @property
    def available(self) -> bool:
        return self.gguf_path is not None and self.gguf_path.exists()

    async def enhance(self, raw_prompt: str, target_duration: int = 6) -> str:
        if not self.available:
            log.info("[enhancer] GGUF not found, skip enhancement")
            return raw_prompt

        if self._llm is None:
            await asyncio.to_thread(self._load)

        user = (
            f"User intent: {raw_prompt}\n"
            f"Target duration: {target_duration} seconds.\n"
            "Rewrite as a cinematic prompt:"
        )
        try:
            out = await asyncio.to_thread(self._infer, user)
            return out.strip() or raw_prompt
        except Exception as e:
            log.warning(f"[enhancer] failed, fallback to raw: {e}")
            return raw_prompt

    def _load(self) -> None:
        try:
            from llama_cpp import Llama  # type: ignore
        except ImportError as e:
            log.warning(f"[enhancer] llama-cpp-python 未安装：{e}")
            self._llm = False  # 标记不可用
            return
        log.info(f"[enhancer] loading {self.gguf_path}")
        self._llm = Llama(
            model_path=str(self.gguf_path),
            n_ctx=self.n_ctx,
            n_threads=4,
            n_gpu_layers=-1,  # M1 Metal
            verbose=False,
        )

    def _infer(self, user: str) -> str:
        if not self._llm:
            return ""
        resp = self._llm.create_chat_completion(  # type: ignore
            messages=[
                {"role": "system", "content": _SYS_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=400,
            temperature=0.7,
            top_p=0.9,
        )
        return resp["choices"][0]["message"]["content"]

    async def aclose(self) -> None:
        if self._llm:
            del self._llm
            self._llm = None


def make_sulphur_enhancer(gguf_path: Path | None = None) -> SulphurPromptEnhancer:
    if gguf_path is None:
        from src.config import load_settings
        s = load_settings()
        gguf_path = s.sulphur_enhancer_gguf
    return SulphurPromptEnhancer(gguf_path=gguf_path)
