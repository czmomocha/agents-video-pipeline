"""PromptSmith Agent：把用户意图（中/英）转成 Sulphur 2 用的英文 prompt。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from src.adapters.llm import LLMProvider
from src.adapters.sulphur_enhancer import SulphurPromptEnhancer
from src.utils.logging import get_logger

log = get_logger()


SYSTEM_PROMPT = """You are a cinematic prompt engineer for Sulphur 2 (LTX-Video 2.3).

Convert user intent (Chinese or English) into a structured prompt pair.

Output STRICT JSON with two keys:
- "positive_prompt": rich English, 60-150 words, include subject+action, camera, lighting, style, color palette, mood.
- "negative_prompt": short English, 10-30 words, artifacts to avoid.

JSON object only. No prose, no markdown.
"""


class PromptSmithOutput(BaseModel):
    positive_prompt: str = Field(min_length=20)
    negative_prompt: str = Field(default="")


async def run_prompt_smith(
    raw_intent: str,
    llm: LLMProvider,
    enhancer: SulphurPromptEnhancer | None = None,
    target_duration: int = 6,
) -> PromptSmithOutput:
    log.info(f"[prompt_smith] raw_intent={raw_intent!r}")
    user_msg = (
        f"User intent: {raw_intent}\n"
        f"Target video duration: {target_duration} seconds.\n"
        "Produce the JSON now."
    )
    out: PromptSmithOutput = await llm.chat_json(  # type: ignore[assignment]
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        schema=PromptSmithOutput,
    )
    log.debug(f"[prompt_smith] positive={out.positive_prompt[:80]!r}")

    if enhancer is not None and enhancer.available:
        enhanced = await enhancer.enhance(out.positive_prompt, target_duration=target_duration)
        if enhanced and enhanced != out.positive_prompt:
            log.info("[prompt_smith] sulphur enhancer applied")
            out = PromptSmithOutput(
                positive_prompt=enhanced,
                negative_prompt=out.negative_prompt,
            )
    return out
