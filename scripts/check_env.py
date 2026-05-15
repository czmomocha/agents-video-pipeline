"""快速环境自检（同 `python -m src.cli env`），可独立运行。"""
from __future__ import annotations

import asyncio

from src.cli import _env_cmd  # type: ignore

if __name__ == "__main__":
    asyncio.run(_env_cmd())
