"""本地记录与状态持久化（JSON 文件，原子写入）。

文件布局（AstrBot 插件数据目录 plugin_data/astrbot_plugin_suda_elec/ 下）：

- ``state.json``   监控配置：虚拟账号池、房间清单（每房间一个 uid）、预警配置与状态
- ``records.json`` 余额历史：``{account_no: [{ts, balance, balance_std, balance_acc, is_normal}]}``

平台限制：一个账号（userId）只能绑定一个房间。因此每间宿舍分配一个独立虚拟
账号 uid，账号池可回收复用（解绑后 uid 可再次投喂给新房间）。

所有写入均为「写临时文件 + os.replace」的原子操作，避免断电/崩溃导致数据损坏。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from .suda_api import generate_virtual_user_id


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
        self._migrate()

    # ------------------------------------------------------------- 默认结构

    @staticmethod
    def _default_state() -> dict:
        return {
            "monitor_user_id": "",       # 兼容保留：首个虚拟账号
            "uid_pool": [],              # 已创建的全部虚拟账号（含空闲的）
            "rooms": {},                 # account_no -> {name, location, bind_id, threshold, added_at, uid}
            "alert_state": {},           # account_no -> {"last_alert_ts": float, ...}
            "alert_sessions": "",        # 预警会话列表（逗号分隔），store 为唯一事实来源
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

    def _migrate(self) -> None:
        """旧版本 state 兼容：房间缺 uid 时补分配（首个复用 monitor_user_id）。"""
        changed = False
        rooms = self.state.setdefault("rooms", {})
        pool = self.state.setdefault("uid_pool", [])
        legacy_uid = str(self.state.get("monitor_user_id") or "")
        first_done = False
        for info in rooms.values():
            if not info.get("uid"):
                if not first_done and legacy_uid:
                    info["uid"] = legacy_uid
                else:
                    info["uid"] = generate_virtual_user_id()
                    pool.append(info["uid"])
                first_done = True
                changed = True
        if legacy_uid and legacy_uid not in pool:
            pool.insert(0, legacy_uid)
            changed = True
        if not isinstance(self.state.get("alert_sessions"), str):
            self.state["alert_sessions"] = ""
            changed = True
        if changed and self._logger:
            self._logger.info("[苏大电费] state.json 已迁移到多账号账号池结构")

    # ------------------------------------------------------------- 持久化

    async def save_state(self) -> None:
        async with self._lock:
            _atomic_write_json(self.state_path, self.state)

    async def save_records(self) -> None:
        async with self._lock:
            _atomic_write_json(self.records_path, self.records)

    # ------------------------------------------------------------- 账号池

    def allocate_uid(self) -> str:
        """分配一个空闲虚拟账号：优先复用未被任何房间引用的池内账号。"""
        pool = self.state.setdefault("uid_pool", [])
        used = {info.get("uid") for info in (self.state.get("rooms") or {}).values()}
        for uid in pool:
            if uid not in used:
                return uid
        uid = generate_virtual_user_id()
        pool.append(uid)
        return uid

    def all_uids(self) -> list[str]:
        return list(self.state.setdefault("uid_pool", []))

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

    def find_room_by_keyword(self, keyword: str) -> dict | None:
        """按账号号或名称/位置关键词查找房间。"""
        kw = (keyword or "").strip()
        if not kw:
            return None
        rooms = self.list_rooms()
        for room in rooms:
            if room["account_no"] == kw:
                return room
        lowered = kw.lower()
        for room in rooms:
            hay = f"{room.get('name', '')} {room.get('location', '')}".lower()
            if lowered in hay:
                return room
        return None

    def upsert_room(
        self,
        account_no: str,
        *,
        name: str = "",
        location: str = "",
        bind_id=None,
        threshold: float | None = None,
        uid: str | None = None,
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
        if uid:
            info["uid"] = uid
        rooms[key] = info
        return dict(info, account_no=key)

    def remove_room(self, account_no: str) -> bool:
        rooms = self.state.setdefault("rooms", {})
        key = str(account_no)
        if key in rooms:
            rooms.pop(key, None)
            self.state.get("alert_state", {}).pop(key, None)
            self.records.pop(key, None)
            # uid 保留在池中复用
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

    def get_records_since(self, account_no: str, since_ts: float) -> list[dict]:
        return [r for r in (self.records.get(str(account_no)) or []) if r.get("ts", 0) >= since_ts]

    # ------------------------------------------------------------- 预警配置/状态

    def get_alert_sessions(self) -> str:
        return str(self.state.get("alert_sessions") or "")

    def set_alert_sessions(self, value: str) -> None:
        self.state["alert_sessions"] = str(value or "")

    def get_alert_state(self, account_no: str) -> dict:
        return dict((self.state.get("alert_state") or {}).get(str(account_no)) or {})

    def set_alert_state(self, account_no: str, **kwargs) -> None:
        alert_state = self.state.setdefault("alert_state", {})
        key = str(account_no)
        cur = alert_state.get(key) or {}
        cur.update(kwargs)
        alert_state[key] = cur
