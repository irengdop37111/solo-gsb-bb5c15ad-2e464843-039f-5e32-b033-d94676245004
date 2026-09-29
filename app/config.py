"""应用配置。

所有配置均通过环境变量提供，均可在 compose 的 environment 中覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path


class Settings:
    # SQLite 数据库文件路径（容器内 /data 挂载为命名卷）
    db_path: str = os.getenv("COC_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "custody.db"))

    # 首次启动且库为空时是否写入演示数据
    seed_demo: bool = os.getenv("COC_SEED_DEMO", "1") not in ("0", "false", "False", "")

    # uvicorn 监听地址与端口
    host: str = os.getenv("COC_HOST", "0.0.0.0")
    port: int = int(os.getenv("COC_PORT", "8080"))

    # 同一发生时间下事件的先后判定窗口（秒）：签收必须严格晚于移交
    # （这里仅作文档用途，实际判定为 occurred_at 严格大于）
    strict_order: bool = True


settings = Settings()
