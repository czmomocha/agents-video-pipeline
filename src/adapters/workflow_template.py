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

    # 2. seed（KSampler / LTXSampler 的常见键）
    sampler_inputs = wf[mapping.sampler_node]["inputs"]
    for seed_key in ("seed", "noise_seed"):
        if seed_key in sampler_inputs:
            sampler_inputs[seed_key] = seed
            break
    else:
        log.warning(f"[inject] sampler node {mapping.sampler_node} 没找到 seed/noise_seed 键")

    # 3. 分辨率与帧数（EmptyLTXLatentVideo 类节点）
    latent_inputs = wf[mapping.empty_latent_node]["inputs"]
    if "width" in latent_inputs:
        latent_inputs["width"] = width
    if "height" in latent_inputs:
        latent_inputs["height"] = height
    # LTX 用 length / num_frames / video_length 三种键之一
    for k in ("length", "num_frames", "video_length", "frame_count"):
        if k in latent_inputs:
            latent_inputs[k] = num_frames
            break
    else:
        log.warning(f"[inject] latent node {mapping.empty_latent_node} 没找到帧数键")

    # 4. fps（SaveVideo 节点常见键）
    save_inputs = wf[mapping.save_video_node]["inputs"]
    for k in ("fps", "frame_rate"):
        if k in save_inputs:
            save_inputs[k] = fps
            break

    return wf


def _set_input(wf: dict, node_id: str, key: str, value) -> None:
    if node_id not in wf:
        raise KeyError(f"节点 {node_id} 不在 workflow 中")
    wf[node_id].setdefault("inputs", {})[key] = value
