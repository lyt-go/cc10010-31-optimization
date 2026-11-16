"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from app.seed import SEED_ROWS

# 输出列校验快照是跨模块共享的内部存储，不参与运营概览的模块计数。
INTERNAL_MODULES = {"columnsnapshot"}


class Store:
    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        source = SEED_ROWS if rows is None else rows
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [copy.deepcopy(row) for row in items] for name, items in source.items()
        }
        # 快照的“查重 -> 分配版本 -> 激活”必须是一个原子动作；RLock 允许业务服务在
        # 持有事务锁时继续调用 store 自身的方法。
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """跨模块共享的进程内事务锁（内存仓储的并发边界）。"""
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def snapshot_rows(self) -> list[dict[str, Any]]:
        """输出列校验快照的底层表；仅由快照服务在持锁状态下写入。"""
        return self._tables.setdefault("columnsnapshot", [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
