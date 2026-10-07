"""本地记录与状态持久化（JSON 文件，原子写入）。

文件布局（AstrBot 插件数据目录 plugin_data/astrbot_plugin_suda_elec/ 下）：

- ``state.json``   监控配置：虚拟账号、房间清单（含单宿舍阈值覆盖）、预警状态
- ``records.json`` 余额历史：``{account_no: [{ts, balance, balance_std, balance_acc, is_normal}]}``

所有写入均为「写临时文件 + os.replace」的原子操作，避免断电/崩溃导致数据损坏。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path


def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    os.replace(tmp, path)


class Store:
    def __init__(self, data_dir: Path | str, logger=None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.records_path = self.data_dir / "records.json"
        self._lock = asyncio.Lock()
        self._logger = logger

        self.state: dict = self._load(self.state_path, self._default_state())
        self.records: dict[str, list[dict]] = self._load(self.records_path, {})

    # ------------------------------------------------------------- 默认结构

    @staticmethod
    def _default_state() -> dict:
        return {
            "monitor_user_id": "",
            "rooms": {},  # account_no -> {name, location, bind_id, threshold, added_at}
            "alert_state": {},  # account_no -> {"last_alert_ts": float, "last_balance": float}
        }

    @staticmethod
    def _load(path: Path, default):
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:  # noqa: BLE001 - 损坏文件回退默认值
            pass
        return default

    # ------------------------------------------------------------- 持久化

    async def save_state(self) -> None:
        async with self._lock:
            _atomic_write_json(self.state_path, self.state)

    async def save_records(self) -> None:
        async with self._lock:
            _atomic_write_json(self.records_path, self.records)

    # ------------------------------------------------------------- 虚拟账号

    def get_monitor_user_id(self) -> str:
        return str(self.state.get("monitor_user_id") or "")

    def set_monitor_user_id(self, user_id: str) -> None:
        self.state["monitor_user_id"] = user_id

    # ------------------------------------------------------------- 房间管理

    def list_rooms(self) -> list[dict]:
        """返回房间配置列表（按添加时间排序）。"""
        rooms = self.state.get("rooms") or {}
        out = []
        for account_no, info in rooms.items():
            item = dict(info)
            item["account_no"] = account_no
            out.append(item)
        out.sort(key=lambda r: r.get("added_at") or 0)
        return out

    def get_room(self, account_no: str) -> dict | None:
        info = (self.state.get("rooms") or {}).get(str(account_no))
        if info is None:
            return None
        item = dict(info)
        item["account_no"] = str(account_no)
        return item

    def upsert_room(
        self,
        account_no: str,
        *,
        name: str = "",
        location: str = "",
        bind_id=None,
        threshold: float | None = None,
    ) -> dict:
        rooms = self.state.setdefault("rooms", {})
        key = str(account_no)
        info = rooms.get(key) or {
            "name": name,
            "location": location,
            "added_at": time.time(),
        }
        if name:
            info["name"] = name
        if location:
            info["location"] = location
        if bind_id is not None:
            info["bind_id"] = bind_id
        if threshold is not None:
            info["threshold"] = float(threshold)
        rooms[key] = info
        return dict(info, account_no=key)

    def remove_room(self, account_no: str) -> bool:
        rooms = self.state.setdefault("rooms", {})
        key = str(account_no)
        if key in rooms:
            rooms.pop(key, None)
            self.state.get("alert_state", {}).pop(key, None)
            self.records.pop(key, None)
            return True
        return False

    def room_threshold(self, account_no: str, global_threshold: float) -> float:
        info = (self.state.get("rooms") or {}).get(str(account_no)) or {}
        try:
            if info.get("threshold") is not None:
                return float(info["threshold"])
        except (TypeError, ValueError):
            pass
        return float(global_threshold)

    # ------------------------------------------------------------- 记录

    def append_record(
        self,
        account_no: str,
        *,
        balance,
        balance_std=None,
        balance_acc=None,
        is_normal: bool = True,
        max_records: int = 2000,
    ) -> None:
        key = str(account_no)
        history = self.records.setdefault(key, [])
        history.append(
            {
                "ts": time.time(),
                "balance": balance,
                "balance_std": balance_std,
                "balance_acc": balance_acc,
                "is_normal": is_normal,
            }
        )
        if max_records > 0 and len(history) > max_records:
            del history[: len(history) - max_records]

    def get_records(self, account_no: str, limit: int | None = None) -> list[dict]:
        history = self.records.get(str(account_no)) or []
        if limit and len(history) > limit:
            return history[-limit:]
        return list(history)

    # ------------------------------------------------------------- 预警状态

    def get_alert_state(self, account_no: str) -> dict:
        return dict((self.state.get("alert_state") or {}).get(str(account_no)) or {})

    def set_alert_state(self, account_no: str, **kwargs) -> None:
        alert_state = self.state.setdefault("alert_state", {})
        key = str(account_no)
        cur = alert_state.get(key) or {}
        cur.update(kwargs)
        alert_state[key] = cur
