"""命令行入口：M1 单镜头出片 + 环境自检。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from src.adapters.comfyui import make_comfy_client, make_sulphur_t2v_runner
from src.adapters.llm import make_llm
from src.adapters.sulphur_enhancer import make_sulphur_enhancer
from src.agents.prompt_smith import PromptSmithOutput, run_prompt_smith
from src.config import (
    load_node_mapping,
    load_settings,
    new_task_id,
    task_output_dir,
)
from src.utils.locks import HardwareScheduler
from src.utils.logging import get_logger

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Agents Video Pipeline CLI")
console = Console()
log = get_logger()


@app.command()
def shot(
    prompt: str = typer.Option(..., "--prompt", "-p", help="原始意图（中/英文均可）"),
    duration: int = typer.Option(6, "--duration", "-d", help="时长秒，6/12/20"),
    resolution: str = typer.Option("1080p", "--resolution", "-r", help="1080p / 720p"),
    seed: int = typer.Option(None, help="固定 seed 便于复现"),
    use_llm: bool = typer.Option(True, "--use-llm/--no-use-llm", help="走 PromptSmith→enhancer"),
    out: Path = typer.Option(None, "--out", "-o", help="输出 mp4 路径，默认 output/<date>/<tid>/shots/01.mp4"),
):
    """M1：单镜头出片。"""
    asyncio.run(_shot_cmd(prompt, duration, resolution, seed, use_llm, out))


async def _shot_cmd(
    prompt: str,
    duration: int,
    resolution: str,
    seed: int | None,
    use_llm: bool,
    out: Path | None,
) -> None:
    s = load_settings()

    # 输出路径
    if out is None:
        tid = new_task_id()
        out = task_output_dir(tid) / "shots" / "01.mp4"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)

    console.rule(f"[bold green]M1 Shot[/]  tid={out.parent.parent.name}")
    console.print(f"[dim]prompt:[/] {prompt}")
    console.print(f"[dim]duration:[/] {duration}s  [dim]resolution:[/] {resolution}  [dim]use_llm:[/] {use_llm}")

    # 初始化适配器
    comfy = make_comfy_client(s)
    llm = make_llm(s)
    scheduler = HardwareScheduler(comfy=comfy, ollama=llm, enabled=s.enable_mutex_locks)

    # —— 1. PromptSmith ——
    if use_llm:
        async with scheduler.acquire_ollama(llm.model):
            enhancer = make_sulphur_enhancer()
            ps_out = await run_prompt_smith(prompt, llm, enhancer, target_duration=duration)
        await llm.aclose()
    else:
        ps_out = PromptSmithOutput(positive_prompt=prompt, negative_prompt="")

    console.print(f"\n[bold cyan]Positive prompt:[/]\n{ps_out.positive_prompt}\n")
    if ps_out.negative_prompt:
        console.print(f"[bold magenta]Negative prompt:[/] {ps_out.negative_prompt}\n")

    # —— 2. ComfyUI / Sulphur 2 出片 ——
    async with scheduler.acquire_comfyui():
        runner = make_sulphur_t2v_runner(comfy=comfy, settings=s)

        def _progress(value: int, maxv: int):
            if maxv > 0 and value % max(1, maxv // 10) == 0:
                console.print(f"  [dim]progress:[/] {value}/{maxv}")

        clip = await runner.run(
            prompt=ps_out.positive_prompt,
            negative_prompt=ps_out.negative_prompt,
            duration_sec=duration,
            resolution=resolution,
            seed=seed,
            fps=s.default_fps,
            save_to=out,
            progress_cb=_progress,
        )

    await comfy.aclose()
    console.rule("[bold green]Done[/]")
    console.print(f"[bold]Output:[/] {clip}")


@app.command()
def plan(
    topic: str = typer.Option(..., "--topic", "-t", help="主题（中/英文均可）"),
    duration: int = typer.Option(None, "--duration", "-d", help="总时长提示（秒），可选"),
    style: str = typer.Option(None, "--style", "-s", help="风格提示，如 cinematic / anime"),
    out: Path = typer.Option(None, "--out", "-o", help="输出 plan.json，默认打印不落盘"),
):
    """M2-A：仅运行 Director，产出 ProductionPlan（不出片）。"""
    asyncio.run(_plan_cmd(topic, duration, style, out))


async def _plan_cmd(topic: str, duration: int | None, style: str | None, out: Path | None) -> None:
    from src.agents.director import run_director

    s = load_settings()
    llm = make_llm(s)

    console.rule(f"[bold green]Director[/]  topic={topic!r}")
    plan = await run_director(
        topic=topic,
        llm=llm,
        settings=s,
        target_duration_hint=duration,
        style_hint=style,
    )
    await llm.aclose()

    _print_plan(plan)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[dim]saved → {out}[/]")


@app.command(name="plan-and-prompts")
def plan_and_prompts(
    topic: str = typer.Option(..., "--topic", "-t", help="主题"),
    duration: int = typer.Option(None, "--duration", "-d", help="总时长提示（秒）"),
    use_enhancer: bool = typer.Option(False, "--use-enhancer/--no-enhancer", help="是否启用 Sulphur GGUF 增强"),
    save: Path = typer.Option(None, "--save", help="保存完整 PipelineState 到 JSON"),
):
    """M2-C：跑 LangGraph 完整链路 director→scriptwriter→storyboarder→prompt_smith。"""
    asyncio.run(_plan_and_prompts_cmd(topic, duration, use_enhancer, save))


async def _plan_and_prompts_cmd(
    topic: str,
    duration: int | None,
    use_enhancer: bool,
    save: Path | None,
) -> None:
    from src.orchestrator.graph import run_plan_and_prompts

    s = load_settings()
    llm = make_llm(s)
    enhancer = make_sulphur_enhancer() if use_enhancer else None

    console.rule(f"[bold green]Plan + Prompts[/]  topic={topic!r}")
    final = await run_plan_and_prompts(
        topic=topic,
        llm=llm,
        enhancer=enhancer,
        settings=s,
        target_duration_hint=duration,
    )
    await llm.aclose()

    if final.plan:
        _print_plan(final.plan)
    if final.script:
        _print_script(final.script)
    if final.storyboard:
        _print_storyboard(final.storyboard)

    console.rule("[bold cyan]Per-Shot Prompts[/]")
    for ss in final.shots:
        i2v_tag = "[yellow](I2V chain)[/]" if ss.use_i2v_from_prev else "[dim](T2V)[/]"
        console.print(
            f"\n[bold]Shot {ss.idx}[/]  {i2v_tag}  "
            f"({ss.duration_sec}s, {ss.camera_shot}/{ss.camera_motion})"
        )
        console.print(f"  [dim]intent:[/] {ss.visual_intent}")
        if ss.positive_prompt:
            console.print(f"  [bold cyan]+[/] {ss.positive_prompt}")
        if ss.negative_prompt:
            console.print(f"  [bold magenta]−[/] {ss.negative_prompt}")

    if save is not None:
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_text(final.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"\n[dim]saved → {save}[/]")


@app.command()
def script(
    plan_file: Path = typer.Option(..., "--plan", "-p", help="ProductionPlan JSON 文件路径"),
    out: Path = typer.Option(None, "--out", "-o", help="输出 script.json"),
):
    """M2-C：单独跑 Scriptwriter（输入：plan.json，输出：script.json）。"""
    asyncio.run(_script_cmd(plan_file, out))


async def _script_cmd(plan_file: Path, out: Path | None) -> None:
    from src.agents.scriptwriter import run_scriptwriter
    from src.orchestrator.state import ProductionPlan

    plan_obj = ProductionPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))

    s = load_settings()
    llm = make_llm(s)
    console.rule(f"[bold green]Scriptwriter[/]  title={plan_obj.title!r}")
    sc = await run_scriptwriter(plan_obj, llm=llm, settings=s)
    await llm.aclose()

    _print_script(sc)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(sc.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[dim]saved → {out}[/]")


@app.command()
def storyboard(
    plan_file: Path = typer.Option(..., "--plan", "-p", help="ProductionPlan JSON 文件"),
    script_file: Path = typer.Option(..., "--script", "-S", help="Script JSON 文件"),
    out: Path = typer.Option(None, "--out", "-o", help="输出 storyboard.json"),
):
    """M2-C：单独跑 Storyboarder（输入：plan.json + script.json）。"""
    asyncio.run(_storyboard_cmd(plan_file, script_file, out))


async def _storyboard_cmd(plan_file: Path, script_file: Path, out: Path | None) -> None:
    from src.agents.storyboarder import run_storyboarder
    from src.orchestrator.state import ProductionPlan, Script

    plan_obj = ProductionPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
    script_obj = Script.model_validate_json(script_file.read_text(encoding="utf-8"))

    s = load_settings()
    llm = make_llm(s)
    console.rule(f"[bold green]Storyboarder[/]  scenes={len(script_obj.scenes)}")
    sb = await run_storyboarder(plan_obj, script_obj, llm=llm, settings=s)
    await llm.aclose()

    _print_storyboard(sb)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(sb.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[dim]saved → {out}[/]")


def _print_plan(plan) -> None:
    """漂亮地打印 ProductionPlan。"""
    table = Table(title=f"ProductionPlan — {plan.title}", show_header=True, show_lines=False)
    table.add_column("Field", style="bold cyan")
    table.add_column("Value")

    table.add_row("Logline", plan.logline)
    table.add_row("Audience", plan.audience)
    table.add_row("Mood", plan.mood)
    table.add_row("Total duration", f"{plan.total_duration_sec}s")
    table.add_row("Shots", f"{plan.n_shots} × {plan.per_shot_duration_sec}s")
    table.add_row("Pacing", plan.pacing)
    table.add_row("Voiceover / Subs", f"{plan.needs_voiceover} / {plan.needs_subtitles}")
    table.add_row("BGM mood", plan.bgm_mood or "—")
    table.add_row("─ Style ─", "")
    table.add_row("  art_style", plan.style.art_style)
    table.add_row("  color_palette", plan.style.color_palette)
    table.add_row("  lighting", plan.style.lighting)
    table.add_row("  camera_language", plan.style.camera_language)
    table.add_row("  aspect_ratio", plan.style.aspect_ratio)
    console.print(table)


def _print_script(script) -> None:
    """漂亮地打印 Script（中文配音稿）。"""
    table = Table(
        title=f"Script — {script.title}  ({len(script.scenes)} scenes, "
              f"{sum(sc.duration_sec for sc in script.scenes)}s total)",
        show_lines=True,
    )
    table.add_column("#", style="bold")
    table.add_column("Dur", style="dim", justify="right")
    table.add_column("Mood", style="dim")
    table.add_column("Narration")
    for sc in script.scenes:
        table.add_row(str(sc.idx), f"{sc.duration_sec}s", sc.mood, sc.narration)
    console.print(table)


def _print_storyboard(storyboard) -> None:
    """漂亮地打印 Storyboard。"""
    i2v_count = sum(1 for sh in storyboard.shots if sh.use_i2v_from_prev)
    table = Table(
        title=f"Storyboard — {len(storyboard.shots)} shots  "
              f"(I2V chain: {i2v_count}/{len(storyboard.shots)})",
        show_lines=True,
    )
    table.add_column("#", style="bold")
    table.add_column("Dur", style="dim", justify="right")
    table.add_column("Shot", style="cyan")
    table.add_column("Motion", style="cyan")
    table.add_column("→Next", style="dim")
    table.add_column("I2V", justify="center")
    table.add_column("Visual Intent")
    for sh in storyboard.shots:
        i2v_mark = "[yellow]●[/]" if sh.use_i2v_from_prev else "[dim]○[/]"
        table.add_row(
            str(sh.idx),
            f"{sh.duration_sec}s",
            sh.camera_shot,
            sh.camera_motion,
            sh.transition_to_next,
            i2v_mark,
            sh.visual_intent,
        )
    console.print(table)


@app.command()
def render(
    topic: str = typer.Option(..., "--topic", "-t", help="主题（中/英文均可）"),
    duration: int = typer.Option(None, "--duration", "-d", help="总时长提示（秒）"),
    use_enhancer: bool = typer.Option(False, "--use-enhancer/--no-enhancer", help="启用 Sulphur GGUF 增强"),
    no_i2v: bool = typer.Option(False, "--no-i2v", help="禁用 I2V 链式（全部 T2V，调试用）"),
    tts: str = typer.Option(
        "auto",
        "--tts",
        help="TTS 后端：auto/piper/edge/gpt_sovits/silent/none。auto=按可用性自动选；none=跳过配音",
    ),
    no_subtitles: bool = typer.Option(False, "--no-subtitles", help="跳过字幕生成"),
    no_burn_subs: bool = typer.Option(False, "--no-burn-subs", help="不烧录字幕（但保留 .srt 文件）"),
    save_state: Path = typer.Option(None, "--save-state", help="保存 PipelineState 到 JSON"),
):
    """M2-D-2：端到端渲染（topic → final.mp4，含配音 + 字幕）。

    需要 Ollama + ComfyUI + FFmpeg 在线。可选：piper/edge-tts、whisper.cpp。
    """
    asyncio.run(_render_cmd(
        topic, duration, use_enhancer, no_i2v, tts, no_subtitles, no_burn_subs, save_state,
    ))


async def _render_cmd(
    topic: str,
    duration: int | None,
    use_enhancer: bool,
    no_i2v: bool,
    tts_choice: str,
    no_subtitles: bool,
    no_burn_subs: bool,
    save_state: Path | None,
) -> None:
    from src.adapters.asr import make_whisper_asr
    from src.adapters.comfyui import (
        make_comfy_client,
        make_sulphur_i2v_runner,
        make_sulphur_t2v_runner,
    )
    from src.adapters.tts import make_best_available_tts, make_tts_provider
    from src.orchestrator.graph import run_full_render

    s = load_settings()
    console.rule(f"[bold green]Full Render[/]  topic={topic!r}")

    # 准备依赖
    comfy = make_comfy_client(s)
    llm = make_llm(s)
    enhancer = make_sulphur_enhancer() if use_enhancer else None
    scheduler = HardwareScheduler(comfy=comfy, ollama=llm, enabled=s.enable_mutex_locks)

    t2v = make_sulphur_t2v_runner(comfy=comfy, settings=s)
    i2v = None
    if not no_i2v:
        try:
            i2v = make_sulphur_i2v_runner(comfy=comfy, settings=s)
            console.print("[dim]I2V chain enabled[/]")
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[yellow]I2V workflow not configured ({e}); falling back to T2V-only[/]")
    else:
        console.print("[yellow]I2V disabled by --no-i2v; all shots will be T2V[/]")

    # —— TTS ——
    tts = None
    if tts_choice == "none":
        console.print("[yellow]TTS disabled by --tts none; final video will be silent[/]")
    elif tts_choice == "auto":
        tts = await make_best_available_tts(s, preference=["piper", "edge", "silent"])
        console.print(f"[dim]TTS backend: {tts.backend_name}[/]")
    else:
        tts = make_tts_provider(tts_choice, settings=s)  # type: ignore[arg-type]
        if not await tts.health():
            console.print(f"[yellow]TTS backend {tts_choice!r} not ready; falling back to silent[/]")
            from src.adapters.tts import SilentTTS
            tts = SilentTTS()
        else:
            console.print(f"[dim]TTS backend: {tts_choice}[/]")

    # —— ASR ——
    asr = None
    if no_subtitles:
        console.print("[yellow]Subtitles disabled by --no-subtitles[/]")
    else:
        asr = make_whisper_asr(s)
        if await asr.health():
            console.print("[dim]Whisper.cpp ready[/]")
        else:
            console.print("[yellow]Whisper.cpp not available; SRT will use text-uniform fallback[/]")

    # —— 跑图 ——
    final = await run_full_render(
        topic=topic,
        llm=llm,
        t2v=t2v,
        i2v=i2v,
        scheduler=scheduler,
        enhancer=enhancer,
        tts=tts,
        asr=asr if not no_subtitles else None,
        burn_srt=not no_burn_subs and not no_subtitles,
        settings=s,
        target_duration_hint=duration,
    )

    await comfy.aclose()
    await llm.aclose()

    # 输出
    if final.plan:
        _print_plan(final.plan)
    if final.script:
        _print_script(final.script)
    if final.storyboard:
        _print_storyboard(final.storyboard)

    console.rule("[bold cyan]Render Result[/]")
    rendered = sum(1 for ss in final.shots if ss.clip_path is not None)
    voiced = sum(1 for ss in final.shots if ss.wav_path is not None)
    subbed = sum(1 for ss in final.shots if ss.srt_path is not None)
    console.print(f"  [bold]rendered:[/] {rendered}/{len(final.shots)} shots")
    console.print(f"  [bold]voiced:[/]   {voiced}/{len(final.shots)} shots")
    console.print(f"  [bold]subtitled:[/] {subbed}/{len(final.shots)} shots")
    for ss in final.shots:
        marks: list[str] = []
        if ss.clip_path:
            mode = "I2V" if ss.use_i2v_from_prev else "T2V"
            marks.append(f"[green]✓ {mode}[/]")
        else:
            marks.append("[red]✗ render[/]")
        if ss.wav_path:
            marks.append("[green]🔊[/]")
        if ss.srt_path:
            marks.append("[green]📝[/]")
        console.print(f"    shot {ss.idx}  " + "  ".join(marks))

    if final.output_path:
        console.rule("[bold green]✓ DONE[/]")
        console.print(f"[bold]Final video:[/] {final.output_path}")
        console.print(f"[dim]metrics:[/] {final.metrics}")
    else:
        console.rule("[bold red]✗ FAILED[/]")

    if save_state is not None:
        save_state.parent.mkdir(parents=True, exist_ok=True)
        save_state.write_text(final.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"[dim]state saved → {save_state}[/]")


@app.command()
def env():
    """检查环境（ComfyUI / Ollama / FFmpeg / 模型 / 节点映射）。"""
    asyncio.run(_env_cmd())


async def _env_cmd() -> None:
    s = load_settings()
    table = Table(title="Environment Check", show_lines=False)
    table.add_column("Item", style="bold")
    table.add_column("Status")
    table.add_column("Detail", style="dim")

    # 1. ComfyUI
    comfy = make_comfy_client(s)
    ok = await comfy.health()
    table.add_row("ComfyUI", "✅" if ok else "❌", s.comfyui_base_url)
    await comfy.aclose()

    # 2. Ollama + Gemma 4
    llm = make_llm(s)
    ok = await llm.health()
    table.add_row("Ollama / Gemma 4", "✅" if ok else "❌", f"{s.ollama_base_url}  model={llm.model}")
    await llm.aclose()

    # 3. FFmpeg
    import shutil
    ffmpeg = shutil.which("ffmpeg")
    table.add_row("FFmpeg", "✅" if ffmpeg else "❌", ffmpeg or "not found in PATH")

    # 4. Workflow JSON
    wf_path = s.workflows_dir / s.comfyui_workflow_t2v
    table.add_row(
        "Sulphur2 T2V workflow",
        "✅" if wf_path.exists() else "❌",
        str(wf_path),
    )

    # 5. Node mapping
    mapping = load_node_mapping("sulphur2_t2v")
    table.add_row(
        "Node mapping (config/node_mapping.yaml)",
        "✅" if mapping.is_t2v_ready() else "❌",
        f"pos={mapping.positive_prompt_node!r} sampler={mapping.sampler_node!r} latent={mapping.empty_latent_node!r}",
    )

    # 6. Sulphur enhancer GGUF
    table.add_row(
        "Sulphur prompt enhancer (GGUF)",
        "✅" if s.sulphur_enhancer_gguf and s.sulphur_enhancer_gguf.exists() else "⚠️ optional",
        str(s.sulphur_enhancer_gguf or "(not found in models/)"),
    )

    # 7. TTS backends（M2-D-2）
    from src.adapters.tts import make_tts_provider
    for backend in ("piper", "edge", "gpt_sovits"):
        try:
            p = make_tts_provider(backend, settings=s)  # type: ignore[arg-type]
            ok = await p.health()
            mark = "✅" if ok else "⚠️ optional"
            detail = ""
            if backend == "piper":
                models = list((s.models_dir / "piper").glob("*.onnx")) if (s.models_dir / "piper").exists() else []
                detail = f"models/piper/{models[0].name}" if models else "no .onnx in models/piper/"
            elif backend == "edge":
                detail = "edge-tts package" + (" (installed)" if ok else " (not installed)")
            elif backend == "gpt_sovits":
                detail = "stub only (M2-D-2 placeholder)"
            table.add_row(f"TTS: {backend}", mark, detail)
        except Exception as e:
            table.add_row(f"TTS: {backend}", "❌", f"err: {e}")

    # 8. Whisper.cpp（M2-D-2）
    from src.adapters.asr import make_whisper_asr
    asr = make_whisper_asr(s)
    asr_ok = await asr.health()
    table.add_row(
        "Whisper.cpp (subtitles)",
        "✅" if asr_ok else "⚠️ optional",
        f"exe={asr.exe} model={asr.model_path or '(not found in models/whisper/)'}",
    )

    console.print(table)


if __name__ == "__main__":
    app()
