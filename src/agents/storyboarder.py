"""StoryboarderAgent —— 分镜师。

职责：
  把 Plan + Script 翻译成 Storyboard：每个 scene 对应一个 Shot，
  含视觉意图、镜头类型、镜头运动、转场、I2V 链式标记。

关键智能：决定 use_i2v_from_prev
  - True：与上一镜头视觉连续（同地点/同主体/连续动作） → 用末帧做下一镜头首帧
  - False：硬切（场景切换/时间跳跃/主体变化） → 纯 T2V

第一个 shot (idx=1) 永远是 use_i2v_from_prev=False，由 Python 兜底强制。
"""
from __future__ import annotations

from src.adapters.llm import LLMProvider
from src.config import Settings, load_settings
from src.orchestrator.state import ProductionPlan, Script, Storyboard
from src.utils.logging import get_logger

log = get_logger()


def _build_system_prompt(plan: ProductionPlan, settings: Settings) -> str:
    return f"""You are the STORYBOARDER for a fully-automated local video production line.

You receive a ProductionPlan + Script and produce a Storyboard — one Shot per Scene,
with visual intent, camera language, transitions, and visual-continuity decisions.

GLOBAL VISUAL STYLE LOCK (already decided by Director, ALL shots must respect):
  - art_style:       {plan.style.art_style}
  - color_palette:   {plan.style.color_palette}
  - lighting:        {plan.style.lighting}
  - camera_language: {plan.style.camera_language}
  - aspect_ratio:    {plan.style.aspect_ratio}
  - mood:            {plan.mood}
  - pacing:          {plan.pacing}

REQUIREMENTS:
1. Output EXACTLY {plan.n_shots} shots, idx 1..{plan.n_shots}, in order.
2. shot.idx must match the corresponding scene.idx.
3. shot.duration_sec must equal scene.duration_sec.
4. shot.visual_intent: Chinese, 1-2 sentences, MUST describe:
   * subject (who/what is in frame)
   * action (what they do)
   * environment (where, time of day)
   AVOID rephrasing the narration verbatim — describe the IMAGE.

CAMERA LANGUAGE (use enum values exactly):
  camera_shot:   wide / medium / close-up / extreme-close-up /
                 over-the-shoulder / POV / establishing / aerial
  camera_motion: static / pan-left / pan-right / tilt-up / tilt-down /
                 dolly-in / dolly-out / handheld / tracking / crane

VISUAL CONTINUITY (CRITICAL — decides I2V vs T2V at render time):
  - Set use_i2v_from_prev = TRUE when this shot is a continuation of the previous one
    (same location, same subject, continuous action, micro time gap).
    The next-shot will use this shot's last frame as initial frame to keep visual coherence.
  - Set use_i2v_from_prev = FALSE for HARD CUTS:
    * scene change (new location/time)
    * subject change
    * dramatic visual contrast desired
  - The FIRST shot (idx=1) MUST have use_i2v_from_prev = FALSE.
  - Aim for 40-70% of shots to use I2V chain in a typical narrative.
    Pure T2V every shot looks fragmented; pure I2V loses cinematic rhythm.

TRANSITIONS (from this shot to next, set on shot N to drive cut from N→N+1):
  cut       — instant (most common)
  match-cut — visual rhyme between shots (use sparingly, max 1-2 per video)
  fade      — emotional pause / scene break
  dissolve  — soft time/place transition
  The LAST shot's transition_to_next is unused (output anyway, will be ignored).

QUALITY:
- Make the storyboard FLOW. Vary shot types (not 5 medium shots in a row).
- Open with an establishing/wide shot when introducing a place.
- Use close-ups for emotional beats.
- Match camera_motion to pacing ({plan.pacing}).

Output: STRICT JSON matching the Storyboard schema. No prose, no markdown.
"""


async def run_storyboarder(
    plan: ProductionPlan,
    script: Script,
    llm: LLMProvider,
    settings: Settings | None = None,
) -> Storyboard:
    """根据 Plan + Script 生成 Storyboard。"""
    s = settings or load_settings()
    log.info(f"[storyboarder] storyboarding {len(script.scenes)} scenes")

    # 把 script 序列化喂给 LLM
    scenes_block = "\n".join(
        f"  - idx={sc.idx}  duration={sc.duration_sec}s  mood={sc.mood}\n"
        f"    narration: {sc.narration}"
        for sc in script.scenes
    )
    user_msg = (
        f"Produce the Storyboard JSON for this script.\n"
        f"Total shots required: {len(script.scenes)}\n\n"
        f"Scenes:\n{scenes_block}\n\n"
        f"Remember: shot[i].idx == scene[i].idx, shot[i].duration_sec == scene[i].duration_sec.\n"
        f"shot[1].use_i2v_from_prev MUST be false."
    )

    storyboard: Storyboard = await llm.chat_json(  # type: ignore[assignment]
        messages=[
            {"role": "system", "content": _build_system_prompt(plan, s)},
            {"role": "user", "content": user_msg},
        ],
        schema=Storyboard,
    )

    # —— 兜底校验与修正 ——

    # 1. 数量对齐
    if len(storyboard.shots) != len(script.scenes):
        log.warning(
            f"[storyboarder] shot count mismatch: got {len(storyboard.shots)}, "
            f"expected {len(script.scenes)}. Truncating/padding."
        )
        storyboard = _normalize_shot_count(storyboard, plan, script)

    # 2. idx 与 duration 强制与 script 对齐
    for shot, scene in zip(storyboard.shots, script.scenes):
        if shot.idx != scene.idx:
            log.warning(f"[storyboarder] reindex shot {shot.idx} → {scene.idx}")
            shot.idx = scene.idx
        if shot.duration_sec != scene.duration_sec:
            log.warning(
                f"[storyboarder] duration mismatch shot {shot.idx}: "
                f"{shot.duration_sec}s → {scene.duration_sec}s (using script value)"
            )
            shot.duration_sec = scene.duration_sec

    # 3. 第一个镜头不能 I2V
    if storyboard.shots and storyboard.shots[0].use_i2v_from_prev:
        log.warning("[storyboarder] forcing shot[1].use_i2v_from_prev = False")
        storyboard.shots[0].use_i2v_from_prev = False

    # 4. 统计 I2V 链式比例（仅记录指标）
    i2v_count = sum(1 for sh in storyboard.shots if sh.use_i2v_from_prev)
    log.info(
        f"[storyboarder] {len(storyboard.shots)} shots, "
        f"I2V chained: {i2v_count}/{len(storyboard.shots)} "
        f"({100 * i2v_count // max(1, len(storyboard.shots))}%)"
    )
    return storyboard


def _normalize_shot_count(
    storyboard: Storyboard, plan: ProductionPlan, script: Script
) -> Storyboard:
    """shot 数与 script.scenes 不一致时强制对齐。"""
    from src.orchestrator.state import Shot

    shots = list(storyboard.shots)
    n_target = len(script.scenes)
    if len(shots) > n_target:
        shots = shots[:n_target]
    else:
        for i in range(len(shots) + 1, n_target + 1):
            scene = script.scenes[i - 1]
            shots.append(
                Shot(
                    idx=i,
                    visual_intent=scene.narration,  # 兜底用 narration 当意图
                    camera_shot="medium",
                    camera_motion="static",
                    duration_sec=scene.duration_sec,
                    transition_to_next="cut",
                    use_i2v_from_prev=False,
                )
            )
    return Storyboard(shots=shots)
