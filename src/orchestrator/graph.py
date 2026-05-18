"""LangGraph 编排图（M2 起点）。

当前版本（feature/m2-director）实现的最小图：

    [start] → director → fanout_shots → prompt_smith(per shot) → [end]

- director：从 topic 产出 ProductionPlan。
- fanout_shots：根据 plan.n_shots 创建 N 个空 ShotState（visual_intent 暂为空）。
- prompt_smith_per_shot：对每个 shot 调用 PromptSmith（继承 plan.style）。

后续 M2 工作（不在本次实现中）：
- 在 director 之后插入 scriptwriter / storyboarder（替换 fanout_shots，让 storyboard
  来填充 visual_intent / camera_shot / camera_motion 等真实字段）。
- 在 prompt_smith 之后串 shot_producer → qa → voice → subtitle → compositor。
"""
from __future__ import annotations

from typing import Any

from src.adapters.llm import LLMProvider
from src.adapters.sulphur_enhancer import SulphurPromptEnhancer
from src.agents.director import run_director
from src.agents.prompt_smith import run_prompt_smith
from src.config import Settings, load_settings
from src.orchestrator.state import PipelineState, Shot, ShotState
from src.utils.logging import get_logger

log = get_logger()


# ─────────────────────────────────────────────────────────────────
#  节点函数
# ─────────────────────────────────────────────────────────────────


async def node_director(
    state: PipelineState,
    *,
    llm: LLMProvider,
    settings: Settings,
) -> dict[str, Any]:
    """节点：调用 Director，产出 ProductionPlan。"""
    plan = await run_director(state.topic, llm=llm, settings=settings)
    return {"plan": plan}


async def node_fanout_shots(state: PipelineState) -> dict[str, Any]:
    """节点：根据 plan 创建 N 个占位 ShotState。

    M2 后续会被 storyboarder 节点替代——届时 visual_intent / camera_*
    等字段由 storyboard 真实填充。本节点仅作骨架占位。
    """
    if state.plan is None:
        raise RuntimeError("fanout_shots requires plan")
    plan = state.plan
    shots: list[ShotState] = []
    for i in range(plan.n_shots):
        shots.append(
            ShotState(
                idx=i + 1,
                visual_intent=f"Shot {i + 1} of '{plan.title}' — {plan.logline}",
                camera_shot="medium",
                camera_motion="static",
                duration_sec=plan.per_shot_duration_sec,
            )
        )
    log.info(f"[fanout] created {len(shots)} shot placeholders")
    return {"shots": shots}


async def node_prompt_smith(
    state: PipelineState,
    *,
    llm: LLMProvider,
    enhancer: SulphurPromptEnhancer | None = None,
) -> dict[str, Any]:
    """节点：对每个 ShotState 调用 PromptSmith（顺序执行，避免抢 LLM）。"""
    if state.plan is None or not state.shots:
        raise RuntimeError("prompt_smith requires plan + shots")

    plan = state.plan
    updated: list[ShotState] = []
    for ss in state.shots:
        # 把 ShotState 转成临时 Shot 对象给 PromptSmith
        shot = Shot(
            idx=ss.idx,
            visual_intent=ss.visual_intent,
            camera_shot=ss.camera_shot,  # type: ignore[arg-type]
            camera_motion=ss.camera_motion,  # type: ignore[arg-type]
            duration_sec=ss.duration_sec,
        )
        ps_out = await run_prompt_smith(
            llm=llm, enhancer=enhancer, plan=plan, shot=shot
        )
        ss.positive_prompt = ps_out.positive_prompt
        ss.negative_prompt = ps_out.negative_prompt
        updated.append(ss)
        log.info(f"[prompt_smith] shot {ss.idx}/{plan.n_shots} ✓")

    return {"shots": updated}


# ─────────────────────────────────────────────────────────────────
#  图构建
# ─────────────────────────────────────────────────────────────────


def build_plan_and_prompts_graph(
    *,
    llm: LLMProvider,
    enhancer: SulphurPromptEnhancer | None = None,
    settings: Settings | None = None,
):
    """构建 director→fanout→prompt_smith 的最小图。

    依赖（langgraph）按需导入，未安装时给出清晰提示。
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as e:
        raise ImportError(
            "LangGraph 未安装。请：uv pip install 'agents-video-pipeline[orchestration]' "
            "或 uv add langgraph langchain-ollama"
        ) from e

    s = settings or load_settings()

    # 用闭包注入依赖（LangGraph 节点签名约定为 state -> dict）
    async def _director(state: PipelineState):
        return await node_director(state, llm=llm, settings=s)

    async def _fanout(state: PipelineState):
        return await node_fanout_shots(state)

    async def _prompt_smith(state: PipelineState):
        return await node_prompt_smith(state, llm=llm, enhancer=enhancer)

    g = StateGraph(PipelineState)
    g.add_node("director", _director)
    g.add_node("fanout_shots", _fanout)
    g.add_node("prompt_smith", _prompt_smith)

    g.add_edge(START, "director")
    g.add_edge("director", "fanout_shots")
    g.add_edge("fanout_shots", "prompt_smith")
    g.add_edge("prompt_smith", END)

    return g.compile()


async def run_plan_and_prompts(
    topic: str,
    *,
    llm: LLMProvider,
    enhancer: SulphurPromptEnhancer | None = None,
    settings: Settings | None = None,
    target_duration_hint: int | None = None,
) -> PipelineState:
    """便捷入口：跑一遍 director→fanout→prompt_smith 图。"""
    from src.config import new_task_id

    graph = build_plan_and_prompts_graph(llm=llm, enhancer=enhancer, settings=settings)
    init = PipelineState(task_id=new_task_id(), topic=topic)
    if target_duration_hint:
        init.metrics["target_duration_hint"] = target_duration_hint

    # LangGraph compiled graph 会返回 dict（CompiledStateGraph 默认以 dict 形式合并）
    final_dict: dict = await graph.ainvoke(init)

    # 把 dict 还原回 PipelineState
    return PipelineState.model_validate(final_dict)
