"""ScriptwriterAgent —— 编剧。

职责：
  把 ProductionPlan 转成中文配音稿（Script），按场分配时长，
  让所有 scene 时长之和接近 plan.total_duration_sec。

关键约束：
  - scene 数 == plan.n_shots（一对一对应，便于后续 Storyboarder 做映射）。
  - 每个 scene.duration_sec 默认 = plan.per_shot_duration_sec，但允许个别 ±2s 微调。
  - 中文叙事，可口语化，避免书面语。配音时长按"中文 4-5 字/秒"反推字数。
"""
from __future__ import annotations

from src.adapters.llm import LLMProvider
from src.config import Settings, load_settings
from src.orchestrator.state import ProductionPlan, Script
from src.utils.logging import get_logger

log = get_logger()


def _build_system_prompt(plan: ProductionPlan, settings: Settings) -> str:
    voice_clause = (
        "Each scene MUST include a Chinese narration line (口语化、自然、上口)."
        if plan.needs_voiceover
        else "Each scene's narration field can be a brief mood caption (≤15 chars), "
             "since voiceover is disabled."
    )
    return f"""You are the SCRIPTWRITER for a fully-automated local video production line.

You receive a ProductionPlan (already locked by the Director) and produce a Script —
a scene-by-scene narrative breakdown. The Storyboarder will turn each scene into one shot.

GLOBAL CONTEXT (locked by Director, do not change):
  - title:    {plan.title}
  - logline:  {plan.logline}
  - mood:     {plan.mood}
  - audience: {plan.audience}
  - pacing:   {plan.pacing}
  - total_duration_sec: {plan.total_duration_sec}
  - n_shots (= n_scenes): {plan.n_shots}
  - per_shot_duration_sec (default): {plan.per_shot_duration_sec}

REQUIREMENTS:
1. Output EXACTLY {plan.n_shots} scenes (one-to-one mapping with future shots).
2. scene.idx must run 1..{plan.n_shots} in order.
3. Sum of all scene.duration_sec MUST be within ±10% of {plan.total_duration_sec}.
4. Each scene.duration_sec should default to {plan.per_shot_duration_sec};
   you may vary individual scenes by ±2s if dramatically necessary,
   but keep the total in budget.
5. {voice_clause}
   - Chinese narration uses ~4-5 characters per second.
     A {plan.per_shot_duration_sec}s scene → roughly {plan.per_shot_duration_sec * 4}-{plan.per_shot_duration_sec * 5} 汉字.
6. scene.mood: short English tag (e.g. "serene", "tense", "playful").

NARRATIVE QUALITY:
- Tell a continuous story or a coherent thematic flow across the scenes.
- Open strong (hook in scene 1).
- Build → climax → resolve, even in 30 seconds.

Output: STRICT JSON matching the Script schema. No prose, no markdown.
Schema: {{"title": "...", "scenes": [{{"idx": 1, "narration": "...", "duration_sec": 6, "mood": "..."}}, ...]}}
"""


async def run_scriptwriter(
    plan: ProductionPlan,
    llm: LLMProvider,
    settings: Settings | None = None,
) -> Script:
    """根据 ProductionPlan 生成 Script。"""
    s = settings or load_settings()
    log.info(f"[scriptwriter] writing {plan.n_shots} scenes for {plan.title!r}")

    user_msg = (
        f"Produce the Script JSON for this plan.\n"
        f"Title: {plan.title}\n"
        f"Required scene count: {plan.n_shots}\n"
        f"Default per-scene duration: {plan.per_shot_duration_sec}s\n"
        f"Total budget: {plan.total_duration_sec}s\n"
    )

    script: Script = await llm.chat_json(  # type: ignore[assignment]
        messages=[
            {"role": "system", "content": _build_system_prompt(plan, s)},
            {"role": "user", "content": user_msg},
        ],
        schema=Script,
    )

    # 兜底：scene 数量不对的话，用 plan 的元信息补齐或截断
    if len(script.scenes) != plan.n_shots:
        log.warning(
            f"[scriptwriter] LLM returned {len(script.scenes)} scenes, "
            f"expected {plan.n_shots}. Adjusting."
        )
        script = _normalize_scene_count(script, plan)

    # 兜底：idx 重排
    for i, sc in enumerate(script.scenes, start=1):
        if sc.idx != i:
            log.warning(f"[scriptwriter] reindex scene {sc.idx} → {i}")
            sc.idx = i

    # 兜底：duration_sec clamp（防止 LLM 给出 0 或离谱大值）
    for sc in script.scenes:
        if sc.duration_sec < 2 or sc.duration_sec > 30:
            log.warning(f"[scriptwriter] clamp scene {sc.idx} duration {sc.duration_sec} → {plan.per_shot_duration_sec}")
            sc.duration_sec = plan.per_shot_duration_sec

    total = sum(sc.duration_sec for sc in script.scenes)
    log.info(f"[scriptwriter] {len(script.scenes)} scenes, total {total}s (target {plan.total_duration_sec}s)")
    return script


def _normalize_scene_count(script: Script, plan: ProductionPlan) -> Script:
    """场数与 plan.n_shots 不一致时强制对齐。"""
    from src.orchestrator.state import Scene

    scenes = list(script.scenes)
    if len(scenes) > plan.n_shots:
        scenes = scenes[: plan.n_shots]
    else:
        # 不够则用 plan 的默认值补齐
        for i in range(len(scenes) + 1, plan.n_shots + 1):
            scenes.append(
                Scene(
                    idx=i,
                    narration=f"（场景 {i}：{plan.logline}）",
                    duration_sec=plan.per_shot_duration_sec,
                    mood="neutral",
                )
            )
    return Script(title=script.title, scenes=scenes)
