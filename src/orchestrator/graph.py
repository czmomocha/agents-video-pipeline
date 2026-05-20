"""LangGraph 编排图（M2-D-2）。

两个图：
  1. plan_and_prompts：纯 LLM 规划链（不需要 ComfyUI）
     director → scriptwriter → storyboarder → prompt_smith

  2. full_render：完整生产链（需要 ComfyUI + FFmpeg + 可选 TTS/ASR）
     director → scriptwriter → storyboarder → prompt_smith
              → shot_producer → voice → subtitle → compositor

后续 M3 工作：
- 加入 checkpointer 实现断点续跑。
- 加入 BGM 节点。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.adapters.asr import WhisperCppASR
from src.adapters.comfyui import (
    ComfyUIClient,
    SulphurI2VRunner,
    SulphurT2VRunner,
)
from src.adapters.llm import LLMProvider
from src.adapters.sulphur_enhancer import SulphurPromptEnhancer
from src.adapters.tts import TTSProvider
from src.agents.compositor import run_compositor
from src.agents.director import run_director
from src.agents.prompt_smith import run_prompt_smith
from src.agents.scriptwriter import run_scriptwriter
from src.agents.shot_producer import run_shot_producer
from src.agents.storyboarder import run_storyboarder
from src.agents.subtitle import run_subtitle_agent
from src.agents.voice import run_voice_agent
from src.config import Settings, load_settings
from src.orchestrator.state import PipelineState, ShotState
from src.utils.locks import HardwareScheduler
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
    plan = await run_director(state.topic, llm=llm, settings=settings)
    return {"plan": plan}


async def node_scriptwriter(
    state: PipelineState,
    *,
    llm: LLMProvider,
    settings: Settings,
) -> dict[str, Any]:
    if state.plan is None:
        raise RuntimeError("scriptwriter requires plan")
    script = await run_scriptwriter(state.plan, llm=llm, settings=settings)
    return {"script": script}


async def node_storyboarder(
    state: PipelineState,
    *,
    llm: LLMProvider,
    settings: Settings,
) -> dict[str, Any]:
    if state.plan is None or state.script is None:
        raise RuntimeError("storyboarder requires plan + script")
    storyboard = await run_storyboarder(
        state.plan, state.script, llm=llm, settings=settings
    )

    # 把 storyboard 投影到 ShotState 列表（管线运行时状态）
    # 通过 idx 与 script.scenes 配对，把 narration 也带进去（供 Voice Agent 用）
    narration_by_idx: dict[int, str] = {}
    if state.script is not None:
        narration_by_idx = {sc.idx: sc.narration for sc in state.script.scenes}

    shots: list[ShotState] = []
    for sh in storyboard.shots:
        shots.append(
            ShotState(
                idx=sh.idx,
                visual_intent=sh.visual_intent,
                camera_shot=sh.camera_shot,
                camera_motion=sh.camera_motion,
                duration_sec=sh.duration_sec,
                use_i2v_from_prev=sh.use_i2v_from_prev,
                narration=narration_by_idx.get(sh.idx, ""),
            )
        )
    return {"storyboard": storyboard, "shots": shots}


async def node_prompt_smith(
    state: PipelineState,
    *,
    llm: LLMProvider,
    enhancer: SulphurPromptEnhancer | None = None,
) -> dict[str, Any]:
    """对每个 ShotState 调用 PromptSmith（顺序执行，避免抢 LLM）。"""
    if state.plan is None or state.storyboard is None or not state.shots:
        raise RuntimeError("prompt_smith requires plan + storyboard + shots")

    # 通过 idx 把 ShotState 与 Storyboard.Shot 配对（便于把镜头语言传给 PromptSmith）
    sb_by_idx = {sh.idx: sh for sh in state.storyboard.shots}

    updated: list[ShotState] = []
    for ss in state.shots:
        sb_shot = sb_by_idx.get(ss.idx)
        if sb_shot is None:
            log.warning(f"[prompt_smith] no storyboard shot found for ShotState idx={ss.idx}")
            updated.append(ss)
            continue

        ps_out = await run_prompt_smith(
            llm=llm,
            enhancer=enhancer,
            plan=state.plan,
            shot=sb_shot,
        )
        ss.positive_prompt = ps_out.positive_prompt
        ss.negative_prompt = ps_out.negative_prompt
        updated.append(ss)
        log.info(f"[prompt_smith] shot {ss.idx}/{len(state.shots)} ✓")

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
    """构建 director → scriptwriter → storyboarder → prompt_smith 的完整图。

    依赖（langgraph）按需导入，未安装时给出清晰提示。
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as e:
        raise ImportError(
            "LangGraph 未安装。请：uv sync 或 uv add langgraph"
        ) from e

    s = settings or load_settings()

    async def _director(state: PipelineState):
        return await node_director(state, llm=llm, settings=s)

    async def _scriptwriter(state: PipelineState):
        return await node_scriptwriter(state, llm=llm, settings=s)

    async def _storyboarder(state: PipelineState):
        return await node_storyboarder(state, llm=llm, settings=s)

    async def _prompt_smith(state: PipelineState):
        return await node_prompt_smith(state, llm=llm, enhancer=enhancer)

    g = StateGraph(PipelineState)
    g.add_node("director", _director)
    g.add_node("scriptwriter", _scriptwriter)
    g.add_node("storyboarder", _storyboarder)
    g.add_node("prompt_smith", _prompt_smith)

    g.add_edge(START, "director")
    g.add_edge("director", "scriptwriter")
    g.add_edge("scriptwriter", "storyboarder")
    g.add_edge("storyboarder", "prompt_smith")
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
    """便捷入口：跑一遍 director→scriptwriter→storyboarder→prompt_smith 图。"""
    from src.config import new_task_id

    graph = build_plan_and_prompts_graph(llm=llm, enhancer=enhancer, settings=settings)
    init = PipelineState(task_id=new_task_id(), topic=topic)
    if target_duration_hint:
        init.metrics["target_duration_hint"] = target_duration_hint

    final_dict: dict = await graph.ainvoke(init)
    return PipelineState.model_validate(final_dict)


# ─────────────────────────────────────────────────────────────────
#  M2-D-1：完整渲染链节点
# ─────────────────────────────────────────────────────────────────


async def node_shot_producer(
    state: PipelineState,
    *,
    t2v: SulphurT2VRunner,
    i2v: SulphurI2VRunner | None,
    scheduler: HardwareScheduler,
    settings: Settings,
) -> dict[str, Any]:
    shots = await run_shot_producer(
        state, t2v=t2v, i2v=i2v, scheduler=scheduler, settings=settings
    )
    return {"shots": shots}


async def node_compositor(
    state: PipelineState,
    *,
    settings: Settings,
    burn_srt: bool = True,
) -> dict[str, Any]:
    out = await run_compositor(state, settings=settings, burn_srt=burn_srt)
    return {"output_path": out, "metrics": state.metrics}


async def node_voice(
    state: PipelineState,
    *,
    tts: TTSProvider,
    settings: Settings,
) -> dict[str, Any]:
    shots = await run_voice_agent(state, tts=tts, settings=settings)
    return {"shots": shots}


async def node_subtitle(
    state: PipelineState,
    *,
    asr: WhisperCppASR | None,
    settings: Settings,
) -> dict[str, Any]:
    shots = await run_subtitle_agent(state, asr=asr, settings=settings)
    return {"shots": shots}


# ─────────────────────────────────────────────────────────────────
#  M2-D-2：完整渲染图（含 voice + subtitle）
# ─────────────────────────────────────────────────────────────────


def build_full_render_graph(
    *,
    llm: LLMProvider,
    t2v: SulphurT2VRunner,
    i2v: SulphurI2VRunner | None,
    scheduler: HardwareScheduler,
    enhancer: SulphurPromptEnhancer | None = None,
    tts: TTSProvider | None = None,
    asr: WhisperCppASR | None = None,
    burn_srt: bool = True,
    settings: Settings | None = None,
):
    """完整生产链：director → scriptwriter → storyboarder → prompt_smith
                  → shot_producer → voice → subtitle → compositor

    需要 ComfyUI + FFmpeg 在线。可选：TTS（默认 piper/silent）+ ASR（默认文本均分）。
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as e:
        raise ImportError(
            "LangGraph 未安装。请：uv sync 或 uv add langgraph"
        ) from e

    s = settings or load_settings()

    async def _director_with_lock(state: PipelineState):
        async with scheduler.acquire_ollama(llm.model):
            return await node_director(state, llm=llm, settings=s)

    async def _scriptwriter(state: PipelineState):
        async with scheduler.acquire_ollama(llm.model):
            return await node_scriptwriter(state, llm=llm, settings=s)

    async def _storyboarder(state: PipelineState):
        async with scheduler.acquire_ollama(llm.model):
            return await node_storyboarder(state, llm=llm, settings=s)

    async def _prompt_smith(state: PipelineState):
        async with scheduler.acquire_ollama(llm.model):
            return await node_prompt_smith(state, llm=llm, enhancer=enhancer)

    async def _shot_producer(state: PipelineState):
        # shot_producer 内部已用 scheduler.acquire_comfyui
        return await node_shot_producer(
            state, t2v=t2v, i2v=i2v, scheduler=scheduler, settings=s
        )

    async def _voice(state: PipelineState):
        if tts is None:
            log.info("[graph] TTS not provided, skipping voice node")
            return {}
        # TTS 走 CPU/Metal，不与 ComfyUI 抢主显存；不加 GPU 锁
        return await node_voice(state, tts=tts, settings=s)

    async def _subtitle(state: PipelineState):
        # whisper.cpp 走 Metal/CPU，与 ComfyUI 不冲突；不加 GPU 锁
        return await node_subtitle(state, asr=asr, settings=s)

    async def _compositor(state: PipelineState):
        return await node_compositor(state, settings=s, burn_srt=burn_srt)

    g = StateGraph(PipelineState)
    g.add_node("director", _director_with_lock)
    g.add_node("scriptwriter", _scriptwriter)
    g.add_node("storyboarder", _storyboarder)
    g.add_node("prompt_smith", _prompt_smith)
    g.add_node("shot_producer", _shot_producer)
    g.add_node("voice", _voice)
    g.add_node("subtitle", _subtitle)
    g.add_node("compositor", _compositor)

    g.add_edge(START, "director")
    g.add_edge("director", "scriptwriter")
    g.add_edge("scriptwriter", "storyboarder")
    g.add_edge("storyboarder", "prompt_smith")
    g.add_edge("prompt_smith", "shot_producer")
    g.add_edge("shot_producer", "voice")
    g.add_edge("voice", "subtitle")
    g.add_edge("subtitle", "compositor")
    g.add_edge("compositor", END)

    return g.compile()


async def run_full_render(
    topic: str,
    *,
    llm: LLMProvider,
    t2v: SulphurT2VRunner,
    i2v: SulphurI2VRunner | None,
    scheduler: HardwareScheduler,
    enhancer: SulphurPromptEnhancer | None = None,
    tts: TTSProvider | None = None,
    asr: WhisperCppASR | None = None,
    burn_srt: bool = True,
    settings: Settings | None = None,
    target_duration_hint: int | None = None,
) -> PipelineState:
    """端到端：从 topic 到 final.mp4。"""
    from src.config import new_task_id

    graph = build_full_render_graph(
        llm=llm,
        t2v=t2v,
        i2v=i2v,
        scheduler=scheduler,
        enhancer=enhancer,
        tts=tts,
        asr=asr,
        burn_srt=burn_srt,
        settings=settings,
    )
    init = PipelineState(task_id=new_task_id(), topic=topic)
    if target_duration_hint:
        init.metrics["target_duration_hint"] = target_duration_hint

    final_dict: dict = await graph.ainvoke(init)
    return PipelineState.model_validate(final_dict)


# 导出
__all__ = [
    "build_plan_and_prompts_graph",
    "build_full_render_graph",
    "run_plan_and_prompts",
    "run_full_render",
    "ShotState",
]
