"""ComfyUI HTTP + WebSocket 客户端 + Sulphur 2 T2V 高层封装。

M1 阶段只实现：health / free_memory / submit_workflow / wait_for_completion / fetch_output_video。
"""
from __future__ import annotations

import asyncio
import json
import random
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets
from tenacity import retry, stop_after_attempt, wait_exponential

from src.adapters.workflow_template import (
    inject_t2v_params,
    load_workflow,
    validate_mapping,
)
from src.config import (
    NodeMapping,
    Settings,
    duration_to_frames,
    load_node_mapping,
    load_settings,
    parse_resolution,
)
from src.utils.logging import get_logger

log = get_logger()


class ComfyUIError(Exception):
    pass


class ComfyUIOOMError(ComfyUIError):
    """显存不足，触发降档。"""


class ComfyUIClient:
    """ComfyUI 底层 HTTP/WS 客户端。"""

    def __init__(self, base_url: str, client_id: str):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self._http = httpx.AsyncClient(timeout=60.0)

    async def health(self) -> bool:
        try:
            r = await self._http.get(f"{self.base_url}/system_stats", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    async def free_memory(self, unload_models: bool = True, free_memory: bool = True) -> None:
        """释放 ComfyUI 占用的 UMA / 显存。"""
        try:
            await self._http.post(
                f"{self.base_url}/free",
                json={"unload_models": unload_models, "free_memory": free_memory},
                timeout=10.0,
            )
        except Exception as e:
            log.debug(f"[comfy.free_memory] ignored: {e}")

    async def submit_workflow(self, workflow: dict) -> str:
        """POST /prompt → prompt_id。"""
        r = await self._http.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow, "client_id": self.client_id},
        )
        if r.status_code != 200:
            raise ComfyUIError(f"submit failed: {r.status_code} {r.text}")
        data = r.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyUIError(f"no prompt_id in response: {data}")
        log.info(f"[comfy] submitted prompt_id={prompt_id}")
        return prompt_id

    async def wait_for_completion(
        self,
        prompt_id: str,
        timeout: float = 1800.0,
        progress_cb=None,
    ) -> dict:
        """通过 WS 监听执行完成，返回 history[prompt_id]。"""
        ws_url = self.base_url.replace("http", "ws") + f"/ws?clientId={self.client_id}"
        log.debug(f"[comfy] connecting WS: {ws_url}")
        try:
            async with asyncio.timeout(timeout):
                async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
                    while True:
                        raw = await ws.recv()
                        if isinstance(raw, bytes):
                            continue  # binary preview，忽略
                        msg = json.loads(raw)
                        mtype = msg.get("type")
                        data = msg.get("data", {})

                        if mtype == "progress" and progress_cb:
                            progress_cb(data.get("value", 0), data.get("max", 1))

                        if mtype == "execution_error":
                            err_msg = str(data)
                            if _is_oom(err_msg):
                                raise ComfyUIOOMError(err_msg)
                            raise ComfyUIError(f"execution_error: {err_msg}")

                        if mtype == "executing":
                            # node=None 且 prompt_id 匹配 → 整个 workflow 执行完成
                            if data.get("node") is None and data.get("prompt_id") == prompt_id:
                                break
        except asyncio.TimeoutError:
            raise ComfyUIError(f"workflow timeout after {timeout}s")

        # 获取 history
        return await self._fetch_history(prompt_id)

    async def _fetch_history(self, prompt_id: str) -> dict:
        r = await self._http.get(f"{self.base_url}/history/{prompt_id}", timeout=30.0)
        r.raise_for_status()
        data = r.json()
        if prompt_id not in data:
            raise ComfyUIError(f"history missing for prompt_id={prompt_id}")
        return data[prompt_id]

    async def fetch_output_video(self, history: dict, save_to: Path, save_node_id: str) -> Path:
        """从 history 中找视频/图片输出，下载到 save_to。"""
        outputs = history.get("outputs", {})
        node_out = outputs.get(save_node_id)
        if not node_out:
            # 兜底：扫所有节点
            for nid, out in outputs.items():
                if any(k in out for k in ("gifs", "videos", "images")):
                    node_out = out
                    save_node_id = nid
                    break
        if not node_out:
            raise ComfyUIError(f"no output found in history. nodes={list(outputs.keys())}")

        items = (
            node_out.get("gifs")
            or node_out.get("videos")
            or node_out.get("images")
            or []
        )
        if not items:
            raise ComfyUIError(f"node {save_node_id} has empty media list: {node_out}")

        item = items[0]
        params = {
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        }
        log.info(f"[comfy] downloading {params['filename']}")
        r = await self._http.get(f"{self.base_url}/view", params=params, timeout=300.0)
        r.raise_for_status()
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_bytes(r.content)
        return save_to

    async def aclose(self) -> None:
        await self._http.aclose()


def _is_oom(err: str) -> bool:
    err_l = err.lower()
    return any(
        kw in err_l
        for kw in ("out of memory", "oom", "mps backend out", "metal", "cuda out of memory")
    )


# ─────────────────────────────────────────────────────────────────
#  高层封装：Sulphur 2 T2V Runner（含 OOM 自动降档）
# ─────────────────────────────────────────────────────────────────


class SulphurT2VRunner:
    """Sulphur 2 文生视频高层接口。"""

    def __init__(
        self,
        comfy: ComfyUIClient,
        workflow_template: dict,
        mapping: NodeMapping,
        settings: Settings | None = None,
    ):
        self._comfy = comfy
        self._wf = workflow_template
        self._mapping = mapping
        self._s = settings or load_settings()

        errs = validate_mapping(workflow_template, mapping, mode="t2v")
        if errs:
            raise ValueError(
                "Sulphur2 T2V workflow 节点映射校验失败：\n  - "
                + "\n  - ".join(errs)
                + "\n请按 workflows/_placeholders.md 配置 config/node_mapping.yaml"
            )

    async def run(
        self,
        prompt: str,
        negative_prompt: str = "",
        duration_sec: int = 6,
        resolution: str = "1080p",
        seed: int | None = None,
        fps: int = 24,
        save_to: Path | None = None,
        progress_cb=None,
    ) -> Path:
        """执行 T2V，返回视频文件路径。OOM 时按 oom_fallback_resolutions 自动降档。"""
        seed = seed if seed is not None else random.randint(1, 2**31 - 1)
        negative = negative_prompt or self._s.default_negative_prompt
        if save_to is None:
            from src.config import task_output_dir
            save_to = task_output_dir() / "shots" / "01.mp4"

        chain = list(self._s.oom_fallback_resolutions)
        if resolution in chain:
            # 让请求的分辨率排首位
            chain.remove(resolution)
            chain.insert(0, resolution)
        else:
            chain = [resolution, *chain]

        last_err: Exception | None = None
        for attempt_res in chain:
            try:
                return await self._run_once(
                    prompt=prompt,
                    negative=negative,
                    duration_sec=duration_sec,
                    resolution=attempt_res,
                    seed=seed,
                    fps=fps,
                    save_to=save_to,
                    progress_cb=progress_cb,
                )
            except ComfyUIOOMError as e:
                last_err = e
                log.warning(f"[sulphur] OOM at {attempt_res}, falling back...")
                await self._comfy.free_memory()
                await asyncio.sleep(2)
                continue
        raise ComfyUIOOMError(f"All fallback resolutions failed: {last_err}")

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=2, min=2, max=10),
        reraise=True,
    )
    async def _run_once(
        self,
        *,
        prompt: str,
        negative: str,
        duration_sec: int,
        resolution: str,
        seed: int,
        fps: int,
        save_to: Path,
        progress_cb,
    ) -> Path:
        width, height = parse_resolution(resolution)
        num_frames = duration_to_frames(duration_sec, fps)

        log.info(
            f"[sulphur] T2V start: {width}x{height} {duration_sec}s ({num_frames}f) "
            f"seed={seed} prompt={prompt[:60]!r}..."
        )

        wf = inject_t2v_params(
            self._wf,
            self._mapping,
            positive=prompt,
            negative=negative,
            width=width,
            height=height,
            num_frames=num_frames,
            seed=seed,
            fps=fps,
        )

        prompt_id = await self._comfy.submit_workflow(wf)
        history = await self._comfy.wait_for_completion(
            prompt_id,
            timeout=self._s.comfyui_request_timeout_sec,
            progress_cb=progress_cb,
        )
        out = await self._comfy.fetch_output_video(
            history, save_to=save_to, save_node_id=self._mapping.save_video_node
        )
        log.info(f"[sulphur] T2V done → {out}")
        return out


# ─────────────────────────────────────────────────────────────────
#  工厂
# ─────────────────────────────────────────────────────────────────


def make_comfy_client(settings: Settings | None = None) -> ComfyUIClient:
    s = settings or load_settings()
    return ComfyUIClient(base_url=s.comfyui_base_url, client_id=s.comfyui_client_id)


def make_sulphur_t2v_runner(
    comfy: ComfyUIClient | None = None,
    settings: Settings | None = None,
) -> SulphurT2VRunner:
    s = settings or load_settings()
    comfy = comfy or make_comfy_client(s)
    wf_path = s.workflows_dir / s.comfyui_workflow_t2v
    workflow = load_workflow(wf_path)
    mapping = load_node_mapping("sulphur2_t2v")
    return SulphurT2VRunner(comfy, workflow, mapping, settings=s)
