"""硬件资源调度：ComfyUI 与 Ollama 在 Mac M1 32GB 上互斥占用统一内存。

调度模型：
- 任意时刻只允许 ComfyUI **或** Ollama 中的一个持有 GPU/UMA 主导权。
- 切换时主动卸载对方的模型，避免 swap、OOM、Metal 抢资源。
- 用 asyncio.Lock 保证并发安全（即使串行也加锁，方便未来加并发）。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from src.utils.logging import get_logger

if TYPE_CHECKING:
    from src.adapters.comfyui import ComfyUIClient
    from src.adapters.llm import LLMProvider


log = get_logger()


class HardwareScheduler:
    """ComfyUI / Ollama 互斥锁。"""

    def __init__(
        self,
        comfy: "ComfyUIClient | None" = None,
        ollama: "LLMProvider | None" = None,
        enabled: bool = True,
    ):
        self._comfy = comfy
        self._ollama = ollama
        self._enabled = enabled
        self._lock = asyncio.Lock()
        self._holder: str | None = None  # "comfyui" | "ollama" | None

    @asynccontextmanager
    async def acquire_comfyui(self):
        """进入 ComfyUI 视频生成。先卸载 Ollama 模型释放 UMA。"""
        async with self._lock:
            if self._enabled and self._holder != "comfyui":
                await self._unload_ollama()
                self._holder = "comfyui"
                log.info("[scheduler] acquired comfyui")
            try:
                yield
            finally:
                # 不在出锁时主动 free comfyui，留给下次切换前再做
                # 这样连续多个 ComfyUI 任务无需反复加载模型
                pass

    @asynccontextmanager
    async def acquire_ollama(self, model: str | None = None):
        """进入 LLM 推理。先释放 ComfyUI 显存。"""
        async with self._lock:
            if self._enabled and self._holder != "ollama":
                await self._free_comfyui()
                self._holder = "ollama"
                log.info(f"[scheduler] acquired ollama (model={model})")
            try:
                yield
            finally:
                pass

    async def _unload_ollama(self):
        if not self._enabled or self._ollama is None:
            return
        try:
            await self._ollama.unload()
            log.debug("[scheduler] ollama unloaded")
        except Exception as e:
            log.warning(f"[scheduler] ollama unload failed (ignored): {e}")

    async def _free_comfyui(self):
        if not self._enabled or self._comfy is None:
            return
        try:
            await self._comfy.free_memory(unload_models=True, free_memory=True)
            log.debug("[scheduler] comfyui memory freed")
        except Exception as e:
            log.warning(f"[scheduler] comfyui free failed (ignored): {e}")
