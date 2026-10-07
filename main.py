"""AstrBot 插件：苏大宿舍电费监控。

免统一身份认证监控苏州大学宿舍水电缴费平台（ny.hq.suda.edu.cn）的剩余电费：

1. **免认证查询**: 平台的“校区→楼栋→房间”浏览与绑定接口不校验用户身份，插件使用
   自生成虚拟账号绑定任意宿舍即可查询余额，支持同时挂载多个宿舍；
2. **定时轮询**: 默认每 30 分钟查询一次，余额历史落盘（plugin_data 下 JSON 文件）；
3. **WebUI 插件页**: 余额曲线（Canvas 动画）、添加/移除宿舍、预警设置；
4. **低额预警**: 余额低于阈值（全局 + 单宿舍覆盖）时向指定会话推送预警，模板可配；
5. **Bot 工具**: elec_query / elec_watchlist 两个 LLM 函数工具 + /elec 命令族。
"""

from __future__ import annotations

import secrets
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import request as web_request
from astrbot.core.message.message_event_result import MessageChain

from .monitor import DEFAULT_TEMPLATE, Monitor
from .storage import Store
from .suda_api import SudaApiError, SudaClient, generate_virtual_user_id

PLUGIN_NAME = "astrbot_plugin_suda_elec"

USAGE = (
    "苏大电费用法：\n"
    "  /elec                  查询所有挂载宿舍的余额\n"
    "  /elec check            立即轮询一次并回报\n"
    "  /elec list             查看监控列表与配置\n"
    "  /elec threshold <宿舍号或关键词> <元>   设置单宿舍预警阈值\n"
    "  /elec alert here       把当前会话设为预警接收目标\n"
    "  /elec remove <宿舍号>   移除监控\n"
    "  /elec help             显示本帮助\n"
    "添加宿舍请在 AstrBot WebUI 的插件页面中操作（校区→楼栋→房间）。"
)


class Main(Star):
    """苏大宿舍电费监控插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.store: Store | None = None
        self.client: SudaClient | None = None
        self.monitor: Monitor | None = None

    # ========================================================== 生命周期

    async def initialize(self) -> None:
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = Store(data_dir, logger=logger)

        user_id = self.store.get_monitor_user_id()
        if not user_id:
            cfg_user = str(self.config.get("monitor_user_id") or "").strip()
            user_id = cfg_user or generate_virtual_user_id()
            self.store.set_monitor_user_id(user_id)
            await self.store.save_state()

        self.client = SudaClient(
            user_id,
            logger=logger,
        )
        self.monitor = Monitor(self)

        self._register_web_apis()
        self.monitor.start()
        logger.info(
            f"[苏大电费] 插件已激活。虚拟账号 {user_id}，"
            f"数据目录 {data_dir}"
        )

    async def terminate(self) -> None:
        if self.monitor:
            await self.monitor.stop()
        if self.client:
            await self.client.close()
        logger.info("[苏大电费] 插件已停用。")

    # ========================================================== 内部工具

    def _cfg(self, key: str, default):
        return self.config.get(key, default)

    def _save_config(self) -> None:
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                saver()
            except Exception:  # noqa: BLE001
                logger.exception("[苏大电费] 保存配置失败")

    def build_message_chain(self, text: str) -> MessageChain:
        return MessageChain().message(text)

    def _latest_balance(self, account_no: str):
        records = self.store.get_records(account_no, limit=1)
        return records[-1]["balance"] if records else None

    def _room_snapshot(self, room: dict) -> dict:
        account_no = room["account_no"]
        records = self.store.get_records(account_no)
        latest = records[-1] if records else None
        prev = records[-2] if len(records) > 1 else None
        stats = self._room_stats(account_no)
        threshold = self.store.room_threshold(account_no, self._cfg("threshold", 20.0))
        return {
            "account_no": account_no,
            "name": room.get("name") or "",
            "location": room.get("location") or room.get("name") or "",
            "bind_id": room.get("bind_id"),
            "threshold": threshold,
            "balance": latest["balance"] if latest else None,
            "balance_std": latest.get("balance_std") if latest else None,
            "balance_acc": latest.get("balance_acc") if latest else None,
            "is_normal": latest.get("is_normal", True) if latest else True,
            "prev_balance": prev["balance"] if prev else None,
            "delta_24h": stats.get("delta_24h"),
            "min_balance": stats.get("min"),
            "max_balance": stats.get("max"),
            "record_count": len(records),
            "last_ts": latest["ts"] if latest else None,
            "monitored_since": records[0]["ts"] if records else None,
        }

    def _room_stats(self, account_no: str) -> dict:
        records = self.store.get_records(account_no)
        if not records:
            return {}
        balances = [r["balance"] for r in records if r.get("balance") is not None]
        if not balances:
            return {}
        cutoff = datetime.now().timestamp() - 86400
        day_ago = None
        for r in records:
            if r["ts"] >= cutoff:
                day_ago = r["balance"]
                break
        latest = balances[-1]
        return {
            "min": min(balances),
            "max": max(balances),
            "delta_24h": (latest - day_ago) if day_ago is not None else None,
        }

    # ========================================================== Web API

    def _register_web_apis(self) -> None:
        prefix = f"/{PLUGIN_NAME}"
        apis = [
            (f"{prefix}/overview", ["GET"], "监控总览", self._api_overview),
            (f"{prefix}/records", ["GET"], "余额历史", self._api_records),
            (f"{prefix}/regions", ["GET"], "校区列表", self._api_regions),
            (f"{prefix}/buildings", ["GET"], "楼栋列表", self._api_buildings),
            (f"{prefix}/rooms", ["GET"], "房间列表", self._api_rooms),
            (f"{prefix}/rooms/add", ["POST"], "添加监控宿舍", self._api_rooms_add),
            (f"{prefix}/rooms/remove", ["POST"], "移除监控宿舍", self._api_rooms_remove),
            (f"{prefix}/rooms/threshold", ["POST"], "单宿舍阈值", self._api_rooms_threshold),
            (f"{prefix}/settings", ["GET", "POST"], "读取/保存预警设置", self._api_settings),
            (f"{prefix}/refresh", ["POST"], "立即轮询", self._api_refresh),
        ]
        for route, methods, desc, handler in apis:
            self.context.register_web_api(route, handler, methods, desc)

    @staticmethod
    def _ok(data=None) -> dict:
        return {"status": "ok", "data": data}

    @staticmethod
    def _err(message: str) -> dict:
        return {"status": "error", "message": message}

    async def _api_overview(self):
        try:
            rooms = self.store.list_rooms()
            payload = {
                "monitor_user_id": self.store.get_monitor_user_id(),
                "interval_minutes": self._cfg("interval_minutes", 30),
                "threshold": self._cfg("threshold", 20.0),
                "alert_enabled": bool(self._cfg("alert_enabled", True)),
                "alert_sessions": str(self._cfg("alert_sessions", "") or ""),
                "alert_template": str(self._cfg("alert_template", "") or DEFAULT_TEMPLATE),
                "alert_cooldown_minutes": self._cfg("alert_cooldown_minutes", 180),
                "last_poll_ts": self.monitor.last_poll_ts if self.monitor else 0,
                "last_poll_ok": self.monitor.last_poll_ok if self.monitor else None,
                "last_error": self.monitor.last_error if self.monitor else "",
                "rooms": [self._room_snapshot(r) for r in rooms],
            }
            return self._ok(payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[苏大电费] overview 失败")
            return self._err(str(exc))

    async def _api_records(self):
        account_no = str(web_request.query.get("account_no", "") or "")
        if not account_no:
            return self._err("缺少 account_no")
        try:
            limit = int(web_request.query.get("limit", 0) or 0)
        except ValueError:
            limit = 0
        records = self.store.get_records(account_no, limit=limit or None)
        room = self.store.get_room(account_no)
        return self._ok(
            {
                "account_no": account_no,
                "room": room,
                "records": records,
            }
        )

    async def _api_regions(self):
        try:
            return self._ok(await self.client.get_regions())
        except SudaApiError as exc:
            return self._err(str(exc))

    async def _api_buildings(self):
        region_id = str(web_request.query.get("region_id", "") or "")
        if not region_id:
            return self._err("缺少 region_id")
        try:
            return self._ok(await self.client.get_buildings(region_id))
        except SudaApiError as exc:
            return self._err(str(exc))

    async def _api_rooms(self):
        building_id = str(web_request.query.get("building_id", "") or "")
        if not building_id:
            return self._err("缺少 building_id")
        try:
            return self._ok(await self.client.get_rooms(building_id))
        except SudaApiError as exc:
            return self._err(str(exc))

    async def _api_rooms_add(self):
        body = await web_request.json(default={}) or {}
        account_no = str(body.get("account_no", "") or "").strip()
        if not account_no:
            return self._err("缺少 account_no（房间 id）")
        name = str(body.get("name", "") or "").strip()
        location = str(body.get("location", "") or "").strip()
        threshold = body.get("threshold")
        try:
            existing = {str(d.get("accountNo") or "") for d in await self.client.get_devices()}
            if account_no not in existing:
                ok = await self.client.bind_room(account_no)
                if not ok:
                    return self._err("平台绑定失败，请稍后重试")
            bind_id = None
            for d in await self.client.get_devices():
                if str(d.get("accountNo") or "") == account_no:
                    info = self.client.summarize_device(d)
                    name = name or info["room_name"]
                    location = location or info["location"]
                    bind_id = info["bind_id"]
                    break
            self.store.upsert_room(
                account_no, name=name, location=location, bind_id=bind_id,
                threshold=float(threshold) if threshold is not None else None,
            )
            await self.store.save_state()
            self.monitor.wake()
            return self._ok(self._room_snapshot(self.store.get_room(account_no)))
        except SudaApiError as exc:
            return self._err(str(exc))

    async def _api_rooms_remove(self):
        body = await web_request.json(default={}) or {}
        account_no = str(body.get("account_no", "") or "").strip()
        room = self.store.get_room(account_no)
        if not room:
            return self._err("该宿舍不在监控列表中")
        bind_id = room.get("bind_id")
        if bind_id is not None:
            try:
                await self.client.unbind_room(bind_id)
            except SudaApiError as exc:
                logger.warning(f"[苏大电费] 平台解绑失败（本地仍会移除）: {exc}")
        self.store.remove_room(account_no)
        await self.store.save_state()
        return self._ok({"removed": account_no})

    async def _api_rooms_threshold(self):
        body = await web_request.json(default={}) or {}
        account_no = str(body.get("account_no", "") or "").strip()
        room = self.store.get_room(account_no)
        if not room:
            return self._err("该宿舍不在监控列表中")
        try:
            threshold = float(body.get("threshold"))
        except (TypeError, ValueError):
            threshold = None
        self.store.upsert_room(account_no, threshold=threshold)
        await self.store.save_state()
        return self._ok(self._room_snapshot(self.store.get_room(account_no)))

    async def _api_settings(self):
        if web_request.method == "GET":
            return self._ok(
                {
                    "interval_minutes": self._cfg("interval_minutes", 30),
                    "threshold": self._cfg("threshold", 20.0),
                    "alert_enabled": bool(self._cfg("alert_enabled", True)),
                    "alert_sessions": str(self._cfg("alert_sessions", "") or ""),
                    "alert_template": str(
                        self._cfg("alert_template", "") or DEFAULT_TEMPLATE
                    ),
                    "alert_cooldown_minutes": self._cfg("alert_cooldown_minutes", 180),
                }
            )
        body = await web_request.json(default={}) or {}
        try:
            if "interval_minutes" in body:
                self.config["interval_minutes"] = max(5, int(body["interval_minutes"]))
            if "threshold" in body:
                self.config["threshold"] = float(body["threshold"])
            if "alert_enabled" in body:
                self.config["alert_enabled"] = bool(body["alert_enabled"])
            if "alert_sessions" in body:
                self.config["alert_sessions"] = str(body["alert_sessions"] or "").strip()
            if "alert_template" in body:
                self.config["alert_template"] = str(body["alert_template"] or "")
            if "alert_cooldown_minutes" in body:
                self.config["alert_cooldown_minutes"] = max(
                    0, int(body["alert_cooldown_minutes"])
                )
        except (TypeError, ValueError) as exc:
            return self._err(f"参数不合法: {exc}")
        self._save_config()
        self.monitor.wake()
        return self._ok({"saved": True})

    async def _api_refresh(self):
        try:
            summary = await self.monitor.poll_once()
            return self._ok(summary)
        except SudaApiError as exc:
            return self._err(str(exc))

    # ========================================================== 命令

    @filter.command("elec")
    async def elec(self, event: AstrMessageEvent):
        """苏大宿舍电费监控主命令：/elec help|check|list|threshold|alert|remove"""
        tokens = [t for t in event.get_message_str().split() if t]
        # 去掉唤醒词与命令本身
        idx = next((i for i, t in enumerate(tokens) if t.lower().lstrip("/") == "elec"), None)
        args = tokens[idx + 1 :] if idx is not None else []
        action = (args[0].lower() if args else "").strip()

        if action in ("", "help", "?", "-h"):
            yield event.plain_result(USAGE)
            return

        if action == "list":
            rooms = self.store.list_rooms()
            if not rooms:
                yield event.plain_result(
                    "尚未挂载任何宿舍。请在 AstrBot WebUI 的「苏大宿舍电费监控」页面添加。"
                )
                return
            lines = [f"已挂载 {len(rooms)} 间宿舍："]
            for r in rooms:
                snap = self._room_snapshot(r)
                bal = "未知" if snap["balance"] is None else f"{snap['balance']:g} 元"
                lines.append(
                    f"  • {snap['location']}（{snap['account_no']}）余额 {bal}，"
                    f"阈值 {snap['threshold']:g} 元"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("check", "now"):
            yield event.plain_result("正在查询苏大平台…")
            try:
                summary = await self.monitor.poll_once()
            except SudaApiError as exc:
                yield event.plain_result(f"❌ 查询失败：{exc}")
                return
            if not summary["rooms"]:
                yield event.plain_result("尚未挂载任何宿舍，请先在 WebUI 插件页添加。")
                return
            lines = ["✅ 查询完成："]
            for item in summary["rooms"]:
                room = self.store.get_room(item["account_no"])
                loc = (room or {}).get("location") or item["account_no"]
                if item["status"] == "ok":
                    lines.append(f"  • {loc}：{item['balance']:g} 元")
                else:
                    lines.append(f"  • {loc}：⚠️ 平台暂无数据")
            for a in summary["alerts"]:
                lines.append(f"  ⚠️ {a['account_no']} 已触发预警（{a['balance']:g} 元）")
            yield event.plain_result("\n".join(lines))
            return

        if action == "alert":
            sub = args[1].lower() if len(args) > 1 else ""
            if sub != "here":
                yield event.plain_result(
                    "用法：/elec alert here —— 把当前会话设为预警接收目标"
                )
                return
            umo = event.unified_msg_origin
            cur = str(self._cfg("alert_sessions", "") or "")
            sessions = [s.strip() for s in cur.replace("，", ",").split(",") if s.strip()]
            if umo not in sessions:
                sessions.append(umo)
            self.config["alert_sessions"] = ",".join(sessions)
            self._save_config()
            yield event.plain_result(
                f"✅ 已把当前会话加入预警接收列表（共 {len(sessions)} 个）。"
            )
            return

        if action == "threshold":
            if len(args) < 3:
                yield event.plain_result(
                    "用法：/elec threshold <宿舍号或名称关键词> <元>，例如：/elec threshold 101 15"
                )
                return
            keyword = args[1]
            try:
                value = float(args[2])
            except ValueError:
                yield event.plain_result("阈值必须是数字。")
                return
            target = self._find_room(keyword)
            if not target:
                yield event.plain_result(f"未找到匹配「{keyword}」的宿舍，试试 /elec list。")
                return
            self.store.upsert_room(target["account_no"], threshold=value)
            await self.store.save_state()
            yield event.plain_result(
                f"✅ {target.get('location') or target['account_no']} 的预警阈值已设为 {value:g} 元。"
            )
            return

        if action == "remove":
            if len(args) < 2:
                yield event.plain_result("用法：/elec remove <宿舍号或名称关键词>")
                return
            target = self._find_room(" ".join(args[1:]))
            if not target:
                yield event.plain_result("未找到匹配的宿舍。")
                return
            bind_id = target.get("bind_id")
            if bind_id is not None:
                try:
                    await self.client.unbind_room(bind_id)
                except SudaApiError:
                    pass
            self.store.remove_room(target["account_no"])
            await self.store.save_state()
            yield event.plain_result(
                f"✅ 已移除 {target.get('location') or target['account_no']} 的监控。"
            )
            return

        yield event.plain_result(USAGE)

    def _find_room(self, keyword: str) -> dict | None:
        keyword = keyword.strip()
        rooms = self.store.list_rooms()
        for room in rooms:
            if room["account_no"] == keyword:
                return room
        lowered = keyword.lower()
        for room in rooms:
            haystack = f"{room.get('name', '')} {room.get('location', '')}".lower()
            if lowered in haystack:
                return room
        return None

    # ========================================================== LLM 工具

    @filter.llm_tool("elec_query")
    async def elec_query(self, event: AstrMessageEvent, query: str = ""):
        """查询苏州大学宿舍电费监控数据。返回宿舍名、位置、当前余额、阈值、24小时变化、监控以来最高/最低余额等统计。

        Args:
            query(string): 宿舍名称或房间号关键词，可为空表示查询全部挂载的宿舍。例如 "101" 或 "独墅湖"。
        """
        rooms = self.store.list_rooms()
        if not rooms:
            return "当前没有挂载任何宿舍。请先在 AstrBot WebUI 的插件页面添加宿舍监控。"
        keyword = (query or "").strip()
        if keyword:
            matched = []
            lowered = keyword.lower()
            for room in rooms:
                hay = f"{room.get('name', '')} {room.get('location', '')} {room['account_no']}".lower()
                if lowered in hay:
                    matched.append(room)
            if not matched:
                return f"没有找到匹配「{keyword}」的宿舍。已挂载：{', '.join(r.get('location') or r['account_no'] for r in rooms)}"
            rooms = matched
        lines = []
        for room in rooms:
            snap = self._room_snapshot(room)
            bal = "未知" if snap["balance"] is None else f"{snap['balance']:g} 元"
            line = f"{snap['location']}（{snap['account_no']}）：余额 {bal}"
            if snap["delta_24h"] is not None:
                sign = "+" if snap["delta_24h"] >= 0 else ""
                line += f"，24h 变化 {sign}{snap['delta_24h']:g} 元"
            if snap["max_balance"] is not None:
                line += f"，区间 {snap['min_balance']:g}~{snap['max_balance']:g} 元"
            line += f"，预警阈值 {snap['threshold']:g} 元"
            if snap["balance"] is not None and snap["balance"] < snap["threshold"]:
                line += " ⚠️ 已低于阈值，建议尽快充值"
            lines.append(line)
        header = f"苏大宿舍电费（{'关键词: ' + keyword if keyword else '全部 ' + str(len(rooms)) + ' 间'}）："
        return header + "\n" + "\n".join(lines)

    @filter.llm_tool("elec_watchlist")
    async def elec_watchlist(self, event: AstrMessageEvent):
        """查询苏大电费监控插件的运行状态：挂载宿舍数、轮询间隔、预警配置、上次轮询时间与结果。

        Args:
        """
        rooms = self.store.list_rooms()
        last_poll = (
            datetime.fromtimestamp(self.monitor.last_poll_ts).strftime("%Y-%m-%d %H:%M:%S")
            if self.monitor and self.monitor.last_poll_ts
            else "尚未轮询"
        )
        status = "正常" if (self.monitor and self.monitor.last_poll_ok) else (
            "异常: " + (self.monitor.last_error or "") if self.monitor else "未知"
        )
        return (
            f"苏大电费监控状态：\n"
            f"- 挂载宿舍：{len(rooms)} 间\n"
            f"- 轮询间隔：{self._cfg('interval_minutes', 30)} 分钟\n"
            f"- 全局预警阈值：{self._cfg('threshold', 20.0)} 元，"
            f"预警{'开启' if self._cfg('alert_enabled', True) else '关闭'}\n"
            f"- 预警会话：{self._cfg('alert_sessions', '') or '未设置'}\n"
            f"- 上次轮询：{last_poll}（{status}）"
        )

    @filter.on_llm_request()
    async def inject_usage_hint(self, event: AstrMessageEvent, req) -> None:
        """向系统提示词注入电费工具的使用说明。"""
        hint = (
            "\n\n[苏大电费插件]\n"
            "你可以使用 elec_query 工具查询苏州大学宿舍的剩余电费（宿舍名、位置、余额、"
            "统计与预警阈值），使用 elec_watchlist 查看监控运行状态。当用户提到宿舍电费、"
            "电量、余额、充值提醒相关问题时优先使用这两个工具。\n"
        )
        try:
            if hint not in (req.system_prompt or ""):
                req.system_prompt = (req.system_prompt or "") + hint
        except Exception:  # noqa: BLE001
            pass
