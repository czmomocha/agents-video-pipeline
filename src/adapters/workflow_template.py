"""ComfyUI workflow 模板加载与节点参数注入。

约定：代码不写死任何节点 ID，所有 ID 来自 config/node_mapping.yaml。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from src.config import NodeMapping
from src.utils.logging import get_logger

log = get_logger()


def load_workflow(path: Path) -> dict:
    """加载 ComfyUI workflow JSON（API 格式）。"""
    if not path.exists():
        raise FileNotFoundError(f"Workflow not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        wf = json.load(f)
    if not isinstance(wf, dict):
        raise ValueError(f"Invalid workflow JSON (expected object): {path}")
    return wf


def validate_mapping(workflow: dict, mapping: NodeMapping, *, mode: str = "t2v") -> list[str]:
    """校验配置的节点 ID 在 workflow 中确实存在。返回错误列表（空 = 通过）。"""
    errors: list[str] = []
    required: list[str] = []
    if mode == "t2v":
        required = [
            mapping.positive_prompt_node,
            mapping.negative_prompt_node,
            mapping.sampler_node,
            mapping.empty_latent_node,
            mapping.save_video_node,
        ]
    elif mode == "i2v":
        required = [
            mapping.positive_prompt_node,
            mapping.negative_prompt_node,
            mapping.sampler_node,
            mapping.load_image_node,
            mapping.save_video_node,
        ]
    for nid in required:
        if not nid:
            errors.append("某个必填节点 ID 为空（见 config/node_mapping.yaml）")
            continue
        if nid not in workflow:
            errors.append(f"节点 ID '{nid}' 在 workflow 中不存在")
    return errors


def inject_t2v_params(
    workflow: dict,
    mapping: NodeMapping,
    *,
    positive: str,
    negative: str,
    width: int,
    height: int,
    num_frames: int,
    seed: int,
    fps: int = 24,
) -> dict:
    """复制 workflow 并注入 T2V 参数。原 workflow 不被修改。"""
    wf = copy.deepcopy(workflow)

    # 1. 提示词
    _set_input(wf, mapping.positive_prompt_node, "text", positive)
    _set_input(wf, mapping.negative_prompt_node, "text", negative)

    # 2. seed
    _inject_seed(wf, mapping.sampler_node, seed)

    # 3. 分辨率与帧数
    _inject_latent_params(wf, mapping.empty_latent_node, width, height, num_frames)

    # 4. fps
    _inject_fps(wf, mapping.save_video_node, fps)

    return wf


def inject_i2v_params(
    workflow: dict,
    mapping: NodeMapping,
    *,
    positive: str,
    negative: str,
    init_image_path: Path | str,
    num_frames: int,
    seed: int,
    fps: int = 24,
) -> dict:
    """复制 workflow 并注入 I2V 参数（首帧图片驱动）。

    注意：I2V 的分辨率由首帧 init_image 自身决定，因此不再注入 width/height。
    若 workflow 中 LoadImage 节点要求绝对路径，调用方需先把图片放进
    ComfyUI/input/ 目录或传入绝对路径——具体取决于使用的 LoadImage 变体。
    """
    wf = copy.deepcopy(workflow)

    # 1. 提示词
    _set_input(wf, mapping.positive_prompt_node, "text", positive)
    _set_input(wf, mapping.negative_prompt_node, "text", negative)

    # 2. seed
    _inject_seed(wf, mapping.sampler_node, seed)

    # 3. 首帧图片路径（LoadImage 节点）
    if not mapping.load_image_node:
        raise ValueError("I2V workflow 需要 load_image_node，但未配置")
    image_inputs = wf[mapping.load_image_node]["inputs"]
    image_str = str(init_image_path)
    # LoadImage / LoadImageFromPath / VHS_LoadImagePath 的常见键名
    for k in ("image", "image_path", "filename", "path"):
        if k in image_inputs:
            image_inputs[k] = image_str
            break
    else:
        log.warning(
            f"[inject] load_image node {mapping.load_image_node} "
            f"没找到 image/image_path/filename/path 键，强制写入 'image'"
        )
        image_inputs["image"] = image_str

    # 4. 帧数（I2V 也需要控制片段长度）
    # I2V 工作流可能没有显式的 EmptyLatent 节点，此时通过 sampler 的 length 键尝试
    if mapping.empty_latent_node and mapping.empty_latent_node in wf:
        _inject_latent_params(wf, mapping.empty_latent_node, None, None, num_frames)
    else:
        sampler_inputs = wf[mapping.sampler_node]["inputs"]
        for k in ("length", "num_frames", "video_length", "frame_count"):
            if k in sampler_inputs:
                sampler_inputs[k] = num_frames
                break

    # 5. fps
    _inject_fps(wf, mapping.save_video_node, fps)

    return wf


# ─── helpers ────────────────────────────────────────────────────


def _inject_seed(wf: dict, sampler_node: str, seed: int) -> None:
    sampler_inputs = wf[sampler_node]["inputs"]
    for seed_key in ("seed", "noise_seed"):
        if seed_key in sampler_inputs:
            sampler_inputs[seed_key] = seed
            return
    log.warning(f"[inject] sampler node {sampler_node} 没找到 seed/noise_seed 键")


def _inject_latent_params(
    wf: dict,
    latent_node: str,
    width: int | None,
    height: int | None,
    num_frames: int,
) -> None:
    latent_inputs = wf[latent_node]["inputs"]
    if width is not None and "width" in latent_inputs:
        latent_inputs["width"] = width
    if height is not None and "height" in latent_inputs:
        latent_inputs["height"] = height
    for k in ("length", "num_frames", "video_length", "frame_count"):
        if k in latent_inputs:
            latent_inputs[k] = num_frames
            return
    log.warning(f"[inject] latent node {latent_node} 没找到帧数键")


def _inject_fps(wf: dict, save_node: str, fps: int) -> None:
    save_inputs = wf[save_node]["inputs"]
    for k in ("fps", "frame_rate"):
        if k in save_inputs:
            save_inputs[k] = fps
            return


def _set_input(wf: dict, node_id: str, key: str, value) -> None:
    if node_id not in wf:
        raise KeyError(f"节点 {node_id} 不在 workflow 中")
    wf[node_id].setdefault("inputs", {})[key] = value
