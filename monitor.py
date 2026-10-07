"""定时轮询与低额预警（v2：多虚拟账号架构）。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from .suda_api import SudaApiError, SudaClient

DEFAULT_TEMPLATE = (
    "⚠️ 宿舍电费预警\n{location}\n当前余额：{balance} 元（低于阈值 {threshold} 元）\n"
    "请尽快前往 ny.hq.suda.edu.cn/prepaid/ 充值！"
)


class Monitor:
    """周期性查询所有挂载宿舍的余额，落盘记录并按阈值触发预警。

    平台限制一个账号只能绑定一个房间，因此轮询时逐房间使用其专属 uid 查询。
    """

    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._manual_wake: asyncio.Event = asyncio.Event()
        self.last_poll_ts: float = 0.0
        self.last_poll_ok: bool | None = None
        self.last_error: str = ""

    # ------------------------------------------------------------- 配置读取

    def _cfg(self, key: str, default):
        return self.plugin.config.get(key, default)

    def _interval_seconds(self) -> float:
        try:
            minutes = float(self._cfg("interval_minutes", 30))
        except (TypeError, ValueError):
            minutes = 30.0
        return max(5.0, minutes) * 60.0

    def _cooldown_seconds(self) -> float:
        try:
            minutes = float(self._cfg("alert_cooldown_minutes", 180))
        except (TypeError, ValueError):
            minutes = 180.0
        return max(0.0, minutes) * 60.0

    # ------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._manual_wake.clear()
            self._task = asyncio.create_task(self._run(), name="suda-elec-monitor")

    async def stop(self) -> None:
        self._stop.set()
        self._manual_wake.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None

    def wake(self) -> None:
        """外部请求立即轮询（WebUI / 命令触发）。"""
        self._manual_wake.set()

    # ------------------------------------------------------------- 主循环

    async def _run(self) -> None:
        await asyncio.sleep(3)
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 主循环永不退出
                self.last_error = str(exc)
                if self.plugin.logger:
                    self.plugin.logger.error(f"[苏大电费] 轮询异常: {exc}")
            try:
                await asyncio.wait_for(self._wait_any(), timeout=self._interval_seconds())
            except asyncio.TimeoutError:
                pass

    async def _wait_any(self) -> None:
        """等待 stop 或 manual_wake 任一触发。"""
        stop_task = asyncio.ensure_future(self._stop.wait())
        wake_task = asyncio.ensure_future(self._manual_wake.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop_task.cancel()
            wake_task.cancel()
            self._manual_wake.clear()

    # ------------------------------------------------------------- 单次轮询

    async def poll_once(self) -> dict:
        """逐房间查询 → 写记录 → 阈值检查 → 发预警。返回本次摘要。"""
        store = self.plugin.store
        client: SudaClient = self.plugin.client
        rooms = store.list_rooms()
        summary = {"polled": 0, "rooms": [], "alerts": []}
        self.last_poll_ts = time.time()
        if not rooms:
            self.last_poll_ok = True
            self.last_error = ""
            await store.save_state()
            await store.save_records()
            return summary

        had_error = False
        max_records = int(self._cfg("max_records_per_room", 2000) or 2000)

        for i, room in enumerate(rooms):
            account_no = room["account_no"]
            uid = room.get("uid") or store.allocate_uid()
            if i:
                await asyncio.sleep(0.8)  # 对平台友好：房间间小间隔
            try:
                devices = await client.get_devices(uid)
            except SudaApiError as exc:
                had_error = True
                self.last_error = str(exc)
                summary["rooms"].append(
                    {"account_no": account_no, "status": "error", "error": str(exc)}
                )
                if self.plugin.logger:
                    self.plugin.logger.warning(
                        f"[苏大电费] 房间 {account_no} 查询失败: {exc}"
                    )
                continue

            dev = next(
                (d for d in devices if str(d.get("accountNo") or "") == account_no),
                None,
            )
            if dev is None:
                # uid 上没有这个房间的绑定（可能被平台清理），补绑
                summary["rooms"].append({"account_no": account_no, "status": "missing"})
                await self._try_rebind(uid, account_no)
                continue

            info = client.summarize_device(dev)
            store.upsert_room(
                account_no,
                name=info["room_name"],
                location=info["location"],
                bind_id=info["bind_id"],
                uid=uid,
            )
            store.append_record(
                account_no,
                balance=info["balance"],
                balance_std=info["balance_std"],
                balance_acc=info["balance_acc"],
                is_normal=info["is_normal"],
                max_records=max_records,
            )
            summary["polled"] += 1
            summary["rooms"].append(
                {"account_no": account_no, "status": "ok", "balance": info["balance"]}
            )
            alert = await self._check_threshold(account_no, info)
            if alert:
                summary["alerts"].append(alert)

        await store.save_state()
        await store.save_records()
        self.last_poll_ok = not had_error
        if not had_error:
            self.last_error = ""
        return summary

    async def _try_rebind(self, uid: str, account_no: str) -> None:
        client: SudaClient = self.plugin.client
        try:
            ok = await client.bind_room(uid, account_no)
            if ok:
                devices = await client.get_devices(uid)
                for d in devices:
                    if str(d.get("accountNo") or "") == account_no:
                        info = client.summarize_device(d)
                        self.plugin.store.upsert_room(
                            account_no,
                            name=info["room_name"],
                            location=info["location"],
                            bind_id=info["bind_id"],
                            uid=uid,
                        )
                        break
        except SudaApiError as exc:
            if self.plugin.logger:
                self.plugin.logger.warning(
                    f"[苏大电费] 房间 {account_no} 补绑失败: {exc}"
                )

    # ------------------------------------------------------------- 预警

    async def _check_threshold(self, account_no: str, info: dict) -> dict | None:
        if not bool(self._cfg("alert_enabled", True)):
            return None
        balance = info["balance"]
        if balance is None:
            return None
        threshold = self.plugin.store.room_threshold(
            account_no, self._cfg("threshold", 20.0)
        )
        if balance >= threshold:
            return None

        store = self.plugin.store
        state = store.get_alert_state(account_no)
        last_ts = float(state.get("last_alert_ts") or 0)
        cooldown = self._cooldown_seconds()
        now = time.time()
        if cooldown > 0 and now - last_ts < cooldown:
            return None

        template = str(self._cfg("alert_template", "") or DEFAULT_TEMPLATE)
        message = self._render_template(template, info, threshold)
        # 会话路由：房间专属（一对多）优先，否则跟随全局（多对一）
        room_sessions = store.room_alert_sessions(account_no)
        sessions = room_sessions if room_sessions else self._alert_sessions()
        delivered = []
        for session in sessions:
            try:
                ok = await self.plugin.context.send_message(
                    session, self.plugin.build_message_chain(message)
                )
                if ok:
                    delivered.append(session)
            except Exception as exc:  # noqa: BLE001
                if self.plugin.logger:
                    self.plugin.logger.warning(
                        f"[苏大电费] 预警发送失败({session}): {exc}"
                    )

        store.set_alert_state(
            account_no,
            last_alert_ts=now,
            last_alert_balance=balance,
            last_alert_delivered=delivered,
        )
        result = {
            "account_no": account_no,
            "balance": balance,
            "threshold": threshold,
            "delivered": delivered,
        }
        if self.plugin.logger:
            self.plugin.logger.info(
                f"[苏大电费] 低额预警: {info['location']} 余额 {balance} < {threshold}，"
                f"送达 {len(delivered)}/{len(sessions)} 个会话"
            )
        return result

    def _render_template(self, template: str, info: dict, threshold: float) -> str:
        return (
            template.replace("{room}", info.get("room_name") or "")
            .replace("{location}", info.get("location") or info.get("room_name") or "")
            .replace("{balance}", f"{info['balance']:g}")
            .replace("{threshold}", f"{threshold:g}")
            .replace(
                "{time}",
                datetime.now().strftime("%Y-%m-%d %H:%M"),
            )
        )

    def _alert_sessions(self) -> list[str]:
        """预警会话：store 为唯一事实来源，config 作为兼容回退。"""
        raw = self.plugin.store.get_alert_sessions()
        if not raw:
            raw = str(self._cfg("alert_sessions", "") or "")
        return [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]
