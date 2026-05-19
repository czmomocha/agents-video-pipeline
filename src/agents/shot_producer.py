"""ShotProducerAgent —— 拍摄执行（Sulphur 2 渲染调度）。

输入：state.shots[] (含 positive_prompt / negative_prompt) + state.storyboard
输出：每个 ShotState 填上 clip_path + last_frame_path

逻辑：
  for shot in shots (串行):
      if shot.idx == 1 OR not shot.use_i2v_from_prev OR prev_last_frame missing:
          T2V → clip_N.mp4
      else:
          I2V(prev_last_frame, prompt) → clip_N.mp4
      extract_last_frame(clip_N.mp4) → clip_N_last.png
"""
from __future__ import annotations

from pathlib import Path

from src.adapters.comfyui import (
    ComfyUIClient,
    SulphurI2VRunner,
    SulphurT2VRunner,
)
from src.adapters.frame_extractor import FrameExtractError, extract_last_frame
from src.config import Settings, load_settings, task_output_dir
from src.orchestrator.state import PipelineState, ShotState
from src.utils.locks import HardwareScheduler
from src.utils.logging import get_logger

log = get_logger()


async def run_shot_producer(
    state: PipelineState,
    *,
    t2v: SulphurT2VRunner,
    i2v: SulphurI2VRunner | None,
    scheduler: HardwareScheduler,
    settings: Settings | None = None,
    output_root: Path | None = None,
) -> list[ShotState]:
    """串行执行所有镜头渲染。

    Args:
        i2v: 若为 None，则全部走 T2V（用于环境未准备好 I2V workflow 的场景）
    """
    s = settings or load_settings()
    if not state.shots:
        raise RuntimeError("shot_producer requires shots")

    # 输出根目录（每条 task 一个目录）
    if output_root is None:
        output_root = task_output_dir(state.task_id)
    shots_dir = output_root / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    prev_last_frame: Path | None = None
    n = len(state.shots)
    for ss in state.shots:
        clip_path = shots_dir / f"{ss.idx:02d}.mp4"
        last_frame_path = shots_dir / f"{ss.idx:02d}_last.png"

        if not ss.positive_prompt:
            err = f"shot {ss.idx} has no positive_prompt; skipping render"
            log.warning(f"[shot_producer] {err}")
            ss.errors.append(err)
            continue

        # 决定 T2V 还是 I2V
        can_i2v = (
            ss.use_i2v_from_prev
            and ss.idx > 1
            and prev_last_frame is not None
            and prev_last_frame.exists()
            and i2v is not None
        )

        async with scheduler.acquire_comfyui():
            try:
                if can_i2v:
                    log.info(f"[shot_producer] {ss.idx}/{n} I2V from {prev_last_frame.name}")
                    out = await i2v.run(
                        prompt=ss.positive_prompt,
                        init_image_path=prev_last_frame,
                        negative_prompt=ss.negative_prompt,
                        duration_sec=ss.duration_sec,
                        seed=ss.seed,
                        fps=ss.fps,
                        save_to=clip_path,
                    )
                else:
                    if ss.use_i2v_from_prev and not can_i2v:
                        log.warning(
                            f"[shot_producer] {ss.idx}/{n} requested I2V but "
                            f"prev frame unavailable → fallback to T2V"
                        )
                    log.info(f"[shot_producer] {ss.idx}/{n} T2V")
                    out = await t2v.run(
                        prompt=ss.positive_prompt,
                        negative_prompt=ss.negative_prompt,
                        duration_sec=ss.duration_sec,
                        resolution=ss.resolution,
                        seed=ss.seed,
                        fps=ss.fps,
                        save_to=clip_path,
                    )
                ss.clip_path = out
            except Exception as e:
                err = f"render failed shot {ss.idx}: {type(e).__name__}: {e}"
                log.error(f"[shot_producer] {err}")
                ss.errors.append(err)
                ss.retry += 1
                # 不 break：继续渲染其他镜头，最后由 Compositor 决定要不要跳过
                prev_last_frame = None
                continue

        # 提取末帧（用于下一镜头 I2V 链式）
        try:
            await extract_last_frame(clip_path, last_frame_path)
            ss.last_frame_path = last_frame_path
            prev_last_frame = last_frame_path
        except (FrameExtractError, FileNotFoundError) as e:
            log.warning(f"[shot_producer] extract last frame failed: {e}")
            prev_last_frame = None  # 下一镜头自动退化为 T2V

    succeeded = sum(1 for ss in state.shots if ss.clip_path is not None)
    log.info(f"[shot_producer] rendered {succeeded}/{n} shots successfully")
    return state.shots
