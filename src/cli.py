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

    console.print(table)


if __name__ == "__main__":
    app()
