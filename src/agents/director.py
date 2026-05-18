"""DirectorAgent —— 整个剧组的"导演"。

职责：
  1. 接收用户主题（中/英），产出 ProductionPlan（整片全局规划）。
  2. 决定：风格、镜头数、单镜头时长、节奏、是否配音、视觉风格锁。
  3. 受硬件约束（M1 32GB）影响默认值：单镜头默认 6 秒，n_shots ≤ 8。

约束写在 system prompt 里，让 LLM 自己理解硬件预算并产出合理规划。
"""
from __future__ import annotations

from src.adapters.llm import LLMProvider
from src.config import Settings, load_settings
from src.orchestrator.state import ProductionPlan
from src.utils.logging import get_logger

log = get_logger()


def _build_system_prompt(settings: Settings) -> str:
    """根据硬件画像动态生成 system prompt。"""
    hw = settings.hardware_profile

    if hw == "m1_32gb":
        hw_constraints = (
            "Hardware profile: Mac M1 / 32GB unified memory.\n"
            "  - per_shot_duration_sec MUST be 6 (only choose 12 if absolutely critical).\n"
            "  - n_shots: 3-8 (sweet spot 5).\n"
            "  - total_duration_sec: 18-60 (1 minute max for now).\n"
            "  - aspect_ratio: prefer 'landscape' or 'portrait'.\n"
        )
    else:
        hw_constraints = (
            "Hardware profile: generic.\n"
            "  - per_shot_duration_sec: 6/12/20.\n"
            "  - n_shots: 3-15.\n"
        )

    return f"""You are the DIRECTOR of a fully-automated local video production line.

You receive a user's topic (Chinese or English) and produce a `ProductionPlan` —
the global blueprint that governs every downstream agent (Scriptwriter, \
Storyboarder, PromptSmith, ShotProducer, Compositor).

Your responsibilities:
1. **Decide the creative concept**: title, logline, mood, audience.
2. **Plan the structure**: total duration, number of shots, per-shot duration, pacing.
3. **Lock the visual style** (CRITICAL — guarantees consistency across shots):
   - art_style, color_palette, lighting, camera_language, aspect_ratio.
   These will be inherited by EVERY shot's prompt. Make them specific and concrete.
4. **Decide auxiliary tracks**: voiceover yes/no, subtitles yes/no, BGM mood.

{hw_constraints}

Quality bar:
- The visual style lock must be detailed enough that two random shots produced
  from independent prompts still look like they belong to the same film.
- The mood and pacing must match the topic semantically.
- Logline ≤ 25 words, must convey the hook.

Output: STRICT JSON matching the ProductionPlan schema. No prose, no markdown.
"""


async def run_director(
    topic: str,
    llm: LLMProvider,
    settings: Settings | None = None,
    target_duration_hint: int | None = None,
    style_hint: str | None = None,
) -> ProductionPlan:
    """执行 Director Agent。

    Args:
        topic: 用户原始主题（中/英）。
        llm: 已初始化的 LLMProvider（建议 gemma4:e4b 或 26b）。
        settings: 全局配置（用于硬件约束注入）。
        target_duration_hint: 用户对总时长的提示（秒），可选。
        style_hint: 用户对风格的提示，如 "cinematic" / "anime"，可选。

    Returns:
        ProductionPlan：经 schema 校验后的计划对象。
    """
    s = settings or load_settings()
    log.info(f"[director] topic={topic!r}  hint(dur={target_duration_hint}, style={style_hint})")

    user_parts = [f"Topic: {topic}"]
    if target_duration_hint:
        user_parts.append(f"Target total duration hint: ~{target_duration_hint} seconds.")
    if style_hint:
        user_parts.append(f"Style hint: {style_hint}")
    user_parts.append("Produce the ProductionPlan JSON now.")

    plan: ProductionPlan = await llm.chat_json(  # type: ignore[assignment]
        messages=[
            {"role": "system", "content": _build_system_prompt(s)},
            {"role": "user", "content": "\n".join(user_parts)},
        ],
        schema=ProductionPlan,
    )

    # M1 硬约束兜底（即使 LLM 不听话也强制纠正）
    if s.hardware_profile == "m1_32gb":
        if plan.per_shot_duration_sec > 12:
            log.warning(f"[director] enforcing per_shot_duration 6s (was {plan.per_shot_duration_sec}s)")
            plan.per_shot_duration_sec = 6
        if plan.n_shots > 8:
            log.warning(f"[director] capping n_shots to 8 (was {plan.n_shots})")
            plan.n_shots = 8

    log.info(
        f"[director] plan: title={plan.title!r}  mood={plan.mood}  "
        f"n_shots={plan.n_shots}  per_shot={plan.per_shot_duration_sec}s  "
        f"total={plan.total_duration_sec}s"
    )
    return plan
