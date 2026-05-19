"""PromptSmith Agent —— 把镜头视觉意图（中/英）翻译成 Sulphur 2 英文 prompt。

M2 升级：接受 ProductionPlan 中的 VisualStyleLock + Shot 元信息，
        保证多镜头之间视觉一致（不靠运气）。

向后兼容：旧调用 `run_prompt_smith(raw_intent, llm)` 仍然可用（plan/shot 为 None）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from src.adapters.llm import LLMProvider
from src.adapters.sulphur_enhancer import SulphurPromptEnhancer
from src.orchestrator.state import ProductionPlan, Shot
from src.utils.logging import get_logger

log = get_logger()


SYSTEM_PROMPT = """You are a cinematic prompt engineer for Sulphur 2 (a video model based on LTX-Video 2.3).

Your task: convert ONE shot's visual intent (Chinese or English) into a structured English prompt pair.

You may receive a global VISUAL STYLE LOCK that all shots must inherit (art_style, color_palette,
lighting, camera_language). When provided, your `positive_prompt` MUST integrate every locked field
faithfully — otherwise shots will look inconsistent.

Output STRICT JSON with two keys:
- "positive_prompt": rich English, 60-150 words, MUST include:
    * subject + action  (from visual_intent)
    * camera shot type & motion  (from shot metadata if provided)
    * lighting + time of day  (inherit from style lock if provided)
    * art style + color palette  (inherit from style lock if provided)
    * mood & atmosphere
- "negative_prompt": short English, 10-30 words, common artifacts to avoid.

JSON object only. No prose, no markdown, no code fences.
"""


class PromptSmithOutput(BaseModel):
    positive_prompt: str = Field(min_length=20)
    negative_prompt: str = Field(default="")


def _build_user_message(
    raw_intent: str,
    target_duration: int,
    plan: ProductionPlan | None,
    shot: Shot | None,
) -> str:
    parts: list[str] = []

    if plan is not None:
        parts.append("=== GLOBAL VISUAL STYLE LOCK (must inherit) ===")
        parts.append(f"  art_style:       {plan.style.art_style}")
        parts.append(f"  color_palette:   {plan.style.color_palette}")
        parts.append(f"  lighting:        {plan.style.lighting}")
        parts.append(f"  camera_language: {plan.style.camera_language}")
        parts.append(f"  overall_mood:    {plan.mood}")
        parts.append("")

    if shot is not None:
        parts.append("=== THIS SHOT ===")
        parts.append(f"  idx:            {shot.idx}")
        parts.append(f"  visual_intent:  {shot.visual_intent}")
        parts.append(f"  camera_shot:    {shot.camera_shot}")
        parts.append(f"  camera_motion:  {shot.camera_motion}")
        parts.append(f"  duration:       {shot.duration_sec}s")
    else:
        parts.append(f"User intent: {raw_intent}")
        parts.append(f"Target video duration: {target_duration} seconds.")

    parts.append("")
    parts.append("Produce the JSON now.")
    return "\n".join(parts)


async def run_prompt_smith(
    raw_intent: str = "",
    llm: LLMProvider | None = None,
    enhancer: SulphurPromptEnhancer | None = None,
    target_duration: int = 6,
    *,
    plan: ProductionPlan | None = None,
    shot: Shot | None = None,
) -> PromptSmithOutput:
    """执行 PromptSmith。

    两种用法：
      1) 单体（M1 兼容）：run_prompt_smith(raw_intent="...", llm=...)
      2) 编排（M2）：    run_prompt_smith(plan=..., shot=..., llm=...)
    """
    if llm is None:
        raise ValueError("llm is required")

    intent_for_log = (shot.visual_intent if shot else raw_intent)[:80]
    log.info(f"[prompt_smith] intent={intent_for_log!r}")

    user_msg = _build_user_message(raw_intent, target_duration, plan, shot)

    out: PromptSmithOutput = await llm.chat_json(  # type: ignore[assignment]
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        schema=PromptSmithOutput,
    )
    log.debug(f"[prompt_smith] positive={out.positive_prompt[:80]!r}")

    if enhancer is not None and enhancer.available:
        duration = shot.duration_sec if shot else target_duration
        enhanced = await enhancer.enhance(out.positive_prompt, target_duration=duration)
        if enhanced and enhanced != out.positive_prompt:
            log.info("[prompt_smith] sulphur enhancer applied")
            out = PromptSmithOutput(
                positive_prompt=enhanced,
                negative_prompt=out.negative_prompt,
            )
    return out
