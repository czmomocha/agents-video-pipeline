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
        """强制结构化输出（Ollama format=json + Pydantic 校验，失败自动重试）。

        鲁棒性增强：
          1. 在 system prompt 末尾追加目标 schema（小模型经常脑补字段名）。
          2. 解析时容错"扒壳"——如果 LLM 把字段套在 `{"<SchemaName>": {...}}` 之类
             单键外壳里，自动解出来再校验。
        """
        # 1) 把 schema 注入 system prompt，让 LLM 真正"看见"字段定义
        messages = _inject_schema_hint(messages, schema)

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
                payload = _unwrap_payload(json.loads(raw), schema)
                return schema.model_validate(payload)
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
                            "Return ONLY a flat JSON object that DIRECTLY matches the schema's "
                            "top-level fields. Do NOT wrap it in an outer key like "
                            f"'{schema.__name__}' or 'data'. No prose, no markdown."
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


# ─────────────────────────────────────────────────────────────────
#  Helpers：让小模型更稳地输出严格 JSON
# ─────────────────────────────────────────────────────────────────


def _inject_schema_hint(messages: list[dict], schema: type[BaseModel]) -> list[dict]:
    """把目标 schema 的 JSON Schema 追加到 system prompt，并强调"扁平、无外壳"。

    小模型（如 gemma 4B）经常会把输出包成 `{"<SchemaName>": {...}}` 或自创嵌套
    分组（如 creative_concept），把字段定义直接给它能显著降低这类幻觉。
    """
    try:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    except Exception:
        schema_json = ""

    hint = (
        f"\n\nYou MUST output a SINGLE flat JSON object that DIRECTLY matches "
        f"the following JSON Schema (no outer wrapper key like "
        f"'{schema.__name__}' or 'data', no extra grouping like 'creative_concept'):\n"
        f"{schema_json}\n"
        f"All required fields must appear at the TOP LEVEL of the object."
    )

    new_messages = [dict(m) for m in messages]
    if new_messages and new_messages[0].get("role") == "system":
        new_messages[0]["content"] = (new_messages[0].get("content") or "") + hint
    else:
        new_messages.insert(0, {"role": "system", "content": hint.lstrip()})
    return new_messages


def _unwrap_payload(data: Any, schema: type[BaseModel]) -> Any:
    """容错"扒壳"：如果 LLM 把字段包在单键外壳里，自动取出内层。

    覆盖三类常见误包：
      1. {"<SchemaName>": {...}}        # 用 schema 类名做 key
      2. {"data": {...}} / {"result": {...}} / {"output": {...}}  # 通用包装
      3. 顶层只有一个 key，且 value 是 dict —— 兜底解一层

    注意：仅当解完后字段更接近 schema 时才采用，避免把合法输入"过度剥离"。
    """
    if not isinstance(data, dict):
        return data

    required_fields = {
        name for name, f in schema.model_fields.items() if f.is_required()
    }

    def _looks_like_target(d: dict) -> bool:
        # 至少命中一半 required 字段，认为这是目标层
        if not required_fields:
            return True
        hits = sum(1 for k in required_fields if k in d)
        return hits >= max(1, len(required_fields) // 2)

    # 已经像目标层了，直接返回
    if _looks_like_target(data):
        return data

    # 1) 命名外壳：SchemaName / data / result / output / payload
    candidate_keys = [
        schema.__name__,
        schema.__name__.lower(),
        "data",
        "result",
        "output",
        "payload",
    ]
    for k in candidate_keys:
        if k in data and isinstance(data[k], dict) and _looks_like_target(data[k]):
            log.warning(f"[llm] unwrapping outer key {k!r} to match schema {schema.__name__}")
            return data[k]

    # 2) 顶层只有一个 dict 子项 —— 兜底解一层
    if len(data) == 1:
        only_value = next(iter(data.values()))
        if isinstance(only_value, dict) and _looks_like_target(only_value):
            log.warning(f"[llm] unwrapping single-key wrapper to match schema {schema.__name__}")
            return only_value

    # 没法救，原样返回让 pydantic 报错（错误信息更利于调试）
    return data
