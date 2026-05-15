"""LLM 抽象层：Ollama 主，LM Studio 备用。

M1 阶段只实现 Ollama 后端 + 基础 chat / chat_json。Function calling 在 M2 引入 LangGraph 时再接。
"""
from __future__ import annotations

import json
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from src.config import Settings, load_settings
from src.utils.logging import get_logger

log = get_logger()


class LLMProvider:
    """统一 LLM 接口。"""

    def __init__(
        self,
        backend: Literal["ollama", "lmstudio"] = "ollama",
        model: str = "gemma4:e4b",
        base_url: str | None = None,
    ):
        self.backend = backend
        self.model = model
        s = load_settings()
        if backend == "ollama":
            self.base_url = base_url or s.ollama_base_url
        else:
            self.base_url = base_url or s.lmstudio_base_url
        self._client = httpx.AsyncClient(timeout=180.0)

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        """普通对话。返回 assistant 文本。"""
        if self.backend == "ollama":
            r = await self._client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": kwargs.get("options", {}),
                },
            )
            r.raise_for_status()
            data = r.json()
            return data["message"]["content"]
        else:
            # LM Studio: OpenAI 兼容
            r = await self._client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    **{k: v for k, v in kwargs.items() if k != "options"},
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def chat_json(
        self,
        messages: list[dict],
        schema: type[BaseModel],
        max_retries: int = 2,
    ) -> BaseModel:
        """强制结构化输出（Ollama format=json + Pydantic 校验，失败自动重试）。"""
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            if self.backend == "ollama":
                r = await self._client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "format": "json",
                    },
                )
                r.raise_for_status()
                raw = r.json()["message"]["content"]
            else:
                r = await self._client.post(
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                    },
                )
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"]
            try:
                return schema.model_validate(json.loads(raw))
            except Exception as e:
                last_err = e
                log.warning(f"[llm] json parse failed (attempt {attempt + 1}): {e}; raw={raw[:200]!r}")
                # 把错误回灌进上下文请求重写
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            f"Your previous response failed JSON validation: {e}. "
                            "Return ONLY valid JSON matching the requested schema, no prose."
                        ),
                    },
                ]
        raise RuntimeError(f"LLM JSON output validation failed after retries: {last_err}")

    async def unload(self) -> None:
        """卸载模型释放 UMA。Ollama: keep_alive=0；LM Studio 暂不支持 API 卸载。"""
        if self.backend != "ollama":
            return
        try:
            # 通过 keep_alive=0 触发立即卸载
            await self._client.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "keep_alive": 0, "prompt": ""},
                timeout=10.0,
            )
        except Exception as e:
            log.debug(f"[llm.unload] ignored: {e}")

    async def health(self) -> bool:
        try:
            if self.backend == "ollama":
                r = await self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
                r.raise_for_status()
                tags = [m["name"] for m in r.json().get("models", [])]
                return any(self.model in t or t.startswith(self.model) for t in tags)
            else:
                r = await self._client.get(f"{self.base_url}/models", timeout=5.0)
                return r.status_code == 200
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()


def make_llm(settings: Settings | None = None, role: str = "default") -> LLMProvider:
    """根据 role 选模型。M1 阶段所有 role 统一走 gemma4:e4b。"""
    s = settings or load_settings()
    return LLMProvider(backend="ollama", model=s.ollama_model_default)
