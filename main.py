"""AstrBot 插件：苏大宿舍电费监控（v2）。

免统一身份认证监控苏州大学宿舍水电缴费平台（ny.hq.suda.edu.cn）的剩余电费：

1. **免认证查询**: 平台的“校区→楼栋→房间”浏览与绑定接口不校验用户身份，插件使用
   自生成虚拟账号查询任意宿舍余额；
   **平台限制一个账号只能绑定一个房间**，因此多宿舍监控 = 账号池，每间宿舍一个
   独立虚拟账号（storage.allocate_uid 管理，解绑后可复用）；
2. **定时轮询**: 默认每 30 分钟逐房间查询，余额历史落盘（plugin_data 下 JSON）；
3. **WebUI 插件页**: 余额曲线（Canvas 动画）、添加/移除宿舍、**快速查询任意房间**、
   预警设置；
4. **低额预警**: 余额低于阈值（全局 + 单宿舍覆盖）时向所有预警会话推送，模板可配；
5. **Bot 工具**: elec_query（含 since 日期查询）/ elec_lookup（报名字秒查任意房间）/
   elec_add（对话添加监控）/ elec_remove / elec_watchlist + /elec 命令族。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import request as web_request
from astrbot.core.message.message_event_result import MessageChain

from .monitor import DEFAULT_TEMPLATE, Monitor
from .suda_api import SudaApiError, SudaClient, generate_virtual_user_id
from .storage import Store

PLUGIN_NAME = "astrbot_plugin_suda_elec"

USAGE = (
    "苏大电费用法：\n"
    "  /elec                     查询所有挂载宿舍的余额\n"
    "  /elec check               立即轮询一次并回报\n"
    "  /elec list                查看监控列表与配置\n"
    "  /elec find <校区> <楼栋> <房间>   快速查任意房间余额（不加入监控）\n"
    "  /elec add <校区> <楼栋> <房间>    添加新监控\n"
    "  /elec remove <宿舍号或关键词>     移除监控\n"
    "  /elec threshold <宿舍号或关键词> <元>  设置单宿舍预警阈值\n"
    "  /elec alert here          把当前会话加入预警接收列表（每人都可加）\n"
    "  /elec alert list          查看预警接收会话\n"
    "  /elec alert remove <序号>  移除某个预警会话\n"
    "  /elec alert test          给所有预警会话发一条测试消息\n"
    "  /elec help                显示本帮助"
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
        self.client = SudaClient(logger=logger)
        self.monitor = Monitor(self)

        if not self.store.get_alert_sessions():
            legacy = str(self.config.get("alert_sessions") or "").strip()
            if legacy:
                self.store.set_alert_sessions(legacy)

        self._register_web_apis()
        self.monitor.start()
        logger.info(
            f"[苏大电费] 插件已激活 v2。虚拟账号池 {len(self.store.all_uids())} 个，"
            f"挂载 {len(self.store.list_rooms())} 间宿舍，数据目录 {data_dir}"
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
            "uid": room.get("uid") or "",
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

    def _resolve_or_reply(self) -> tuple[dict | None, str]:
        """解析本次请求的 account_no（query 或 path）。"""
        account_no = str(web_request.query.get("account_no", "") or "").strip()
        return (account_no, "")

    # ========================================================== Web API

    def _register_web_apis(self) -> None:
        prefix = f"/{PLUGIN_NAME}"
        apis = [
            (f"{prefix}/overview", ["GET"], "监控总览", self._api_overview),
            (f"{prefix}/records", ["GET"], "余额历史", self._api_records),
            (f"{prefix}/quick_balance", ["GET"], "快速查询任意房间余额", self._api_quick_balance),
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
                "monitor_user_id": self.store.all_uids()[0]
                if self.store.all_uids()
                else "",
                "room_count": len(rooms),
                "interval_minutes": self._cfg("interval_minutes", 30),
                "threshold": self._cfg("threshold", 20.0),
                "alert_enabled": bool(self._cfg("alert_enabled", True)),
                "alert_sessions": self.store.get_alert_sessions()
                or str(self._cfg("alert_sessions", "") or ""),
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
        return self._ok({"account_no": account_no, "room": room, "records": records})

    async def _api_quick_balance(self):
        """快速查询任意房间余额（临时账号，查询完立即解绑，不留存）。"""
        account_no = str(web_request.query.get("account_no", "") or "").strip()
        if not account_no:
            return self._err("缺少 account_no")
        try:
            info = await self.client.quick_balance(account_no)
            return self._ok(info)
        except SudaApiError as exc:
            return self._err(str(exc))

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
            already = self.store.get_room(account_no)
            uid = already.get("uid") if already else None
            if not uid:
                uid = self.store.allocate_uid()
            bind_id = None
            try:
                devices = await self.client.get_devices(uid)
                cur = next((d for d in devices if str(d.get("accountNo") or "") == account_no), None)
                if cur is not None:
                    bind_id = cur.get("id")
                elif devices:
                    # uid 被别的房间占着（异常态），先解绑
                    for d in devices:
                        try:
                            await self.client.unbind_room(uid, d.get("id"))
                        except SudaApiError:
                            pass
                if bind_id is None:
                    ok = await self.client.bind_room(uid, account_no)
                    if not ok:
                        return self._err("平台绑定失败，请稍后重试")
            except SudaApiError as exc:
                if "只能绑定一个房间" in str(exc):
                    # 换一个新 uid 重试一次
                    uid = generate_virtual_user_id()
                    self.store.allocate_uid()
                    try:
                        await self.client.bind_room(uid, account_no)
                    except SudaApiError as exc2:
                        return self._err(str(exc2))
                else:
                    return self._err(str(exc))
            # 拉取绑定后的真实名称/位置
            for d in await self.client.get_devices(uid):
                if str(d.get("accountNo") or "") == account_no:
                    info = self.client.summarize_device(d)
                    name = name or info["room_name"]
                    location = location or info["location"]
                    bind_id = info["bind_id"]
                    break
            self.store.upsert_room(
                account_no,
                name=name,
                location=location,
                bind_id=bind_id,
                uid=uid,
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
        uid = room.get("uid")
        bind_id = room.get("bind_id")
        if uid and bind_id is not None:
            try:
                await self.client.unbind_room(uid, bind_id)
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
                    "alert_sessions": self.store.get_alert_sessions()
                    or str(self._cfg("alert_sessions", "") or ""),
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
                value = str(body["alert_sessions"] or "").strip()
                self.store.set_alert_sessions(value)
                self.config["alert_sessions"] = value
            if "alert_template" in body:
                self.config["alert_template"] = str(body["alert_template"] or "")
            if "alert_cooldown_minutes" in body:
                self.config["alert_cooldown_minutes"] = max(
                    0, int(body["alert_cooldown_minutes"])
                )
        except (TypeError, ValueError) as exc:
            return self._err(f"参数不合法: {exc}")
        self._save_config()
        await self.store.save_state()
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
        """苏大宿舍电费监控主命令（/elec help 查看全部子命令）"""
        try:
            async for result in self._elec_dispatch(event):
                yield result
        except Exception as exc:  # noqa: BLE001 - 命令层兜底，保证一定有回音
            logger.exception("[苏大电费] 命令执行异常")
            yield event.plain_result(f"❌ 命令执行出错：{exc}")

    async def _elec_dispatch(self, event: AstrMessageEvent):
        tokens = [t for t in event.get_message_str().split() if t]
        idx = next(
            (i for i, t in enumerate(tokens) if t.lower().lstrip("/") == "elec"), None
        )
        args = tokens[idx + 1 :] if idx is not None else []
        action = (args[0].lower() if args else "").strip()

        if action in ("", "help", "?", "-h"):
            yield event.plain_result(USAGE)
            return

        if action == "list":
            rooms = self.store.list_rooms()
            if not rooms:
                yield event.plain_result(
                    "尚未挂载任何宿舍。可以说「帮我监控 独墅湖 101号楼 101」"
                    "或在 WebUI 插件页面添加。"
                )
                return
            lines = [f"已挂载 {len(rooms)} 间宿舍："]
            for r in rooms:
                snap = self._room_snapshot(r)
                bal = "未知" if snap["balance"] is None else f"{snap['balance']:g} 元"
                since = (
                    datetime.fromtimestamp(snap["monitored_since"]).strftime("%m-%d")
                    if snap["monitored_since"]
                    else "—"
                )
                lines.append(
                    f"  • {snap['location']}（{snap['account_no']}）余额 {bal}，"
                    f"阈值 {snap['threshold']:g} 元，自 {since} 起监控"
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
                yield event.plain_result("尚未挂载任何宿舍。")
                return
            lines = ["✅ 查询完成："]
            for item in summary["rooms"]:
                room = self.store.get_room(item["account_no"])
                loc = (room or {}).get("location") or item["account_no"]
                if item["status"] == "ok":
                    lines.append(f"  • {loc}：{item['balance']:g} 元")
                else:
                    lines.append(f"  • {loc}：⚠️ {item.get('error', '平台暂无数据')}")
            for a in summary["alerts"]:
                lines.append(f"  ⚠️ {a['account_no']} 已触发预警（{a['balance']:g} 元）")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("find", "query"):
            # /elec find <校区> <楼栋> <房间>
            rest = args[1:]
            if len(rest) < 3:
                yield event.plain_result(
                    "用法：/elec find <校区> <楼栋> <房间>，例如：/elec find 独墅湖 101号楼 101"
                )
                return
            yield event.plain_result("正在查询…")
            reply = await self._lookup_text(rest[0], rest[1], "".join(rest[2:]))
            yield event.plain_result(reply)
            return

        if action == "add":
            # /elec add <校区> <楼栋> <房间>
            rest = args[1:]
            if len(rest) < 3:
                yield event.plain_result(
                    "用法：/elec add <校区> <楼栋> <房间>，例如：/elec add 独墅湖 101号楼 101"
                )
                return
            yield event.plain_result("正在添加监控…")
            reply, ok = await self._add_room_text(rest[0], rest[1], "".join(rest[2:]))
            yield event.plain_result(reply)
            return

        if action == "remove":
            if len(args) < 2:
                yield event.plain_result("用法：/elec remove <宿舍号或名称关键词>")
                return
            target = self.store.find_room_by_keyword(" ".join(args[1:]))
            if not target:
                yield event.plain_result("未找到匹配的宿舍，试试 /elec list。")
                return
            uid = target.get("uid")
            bind_id = target.get("bind_id")
            if uid and bind_id is not None:
                try:
                    await self.client.unbind_room(uid, bind_id)
                except SudaApiError:
                    pass
            self.store.remove_room(target["account_no"])
            await self.store.save_state()
            yield event.plain_result(
                f"✅ 已移除 {target.get('location') or target['account_no']} 的监控。"
            )
            return

        if action == "threshold":
            if len(args) < 3:
                yield event.plain_result(
                    "用法：/elec threshold <宿舍号或名称关键词> <元>"
                )
                return
            try:
                value = float(args[2])
            except ValueError:
                yield event.plain_result("阈值必须是数字。")
                return
            target = self.store.find_room_by_keyword(args[1])
            if not target:
                yield event.plain_result(f"未找到匹配「{args[1]}」的宿舍，试试 /elec list。")
                return
            self.store.upsert_room(target["account_no"], threshold=value)
            await self.store.save_state()
            yield event.plain_result(
                f"✅ {target.get('location') or target['account_no']} 的预警阈值已设为 {value:g} 元。"
            )
            return

        if action == "alert":
            async for result in self._alert_dispatch(event, args[1:]):
                yield result
            return

        yield event.plain_result(USAGE)

    async def _alert_dispatch(self, event: AstrMessageEvent, sub_args):
        sub = (sub_args[0].lower() if sub_args else "").strip()

        if sub == "here":
            umo = event.unified_msg_origin
            sessions = self._sessions_list()
            if umo not in sessions:
                sessions.append(umo)
            self._write_sessions(sessions)
            yield event.plain_result(
                f"✅ 当前会话已加入预警接收列表（共 {len(sessions)} 个）。\n"
                f"群里每个同学都可以各自发一次 /elec alert here，都会收到低额提醒。"
            )
            return

        if sub in ("list", "show"):
            sessions = self._sessions_list()
            if not sessions:
                yield event.plain_result(
                    "还没有预警接收会话。在这里发 /elec alert here 即可加入。"
                )
                return
            lines = [f"预警接收会话（{len(sessions)} 个）："]
            for i, s in enumerate(sessions, 1):
                lines.append(f"  {i}. {s}")
            yield event.plain_result("\n".join(lines))
            return

        if sub in ("remove", "rm", "del"):
            if len(sub_args) < 2 or not sub_args[1].strip().isdigit():
                yield event.plain_result("用法：/elec alert remove <序号>（序号见 /elec alert list）")
                return
            idx = int(sub_args[1]) - 1
            sessions = self._sessions_list()
            if 0 <= idx < len(sessions):
                removed = sessions.pop(idx)
                self._write_sessions(sessions)
                yield event.plain_result(f"✅ 已移除 {removed}")
            else:
                yield event.plain_result("序号不存在，试试 /elec alert list。")
            return

        if sub == "test":
            sessions = self._sessions_list()
            if not sessions:
                yield event.plain_result("还没有预警接收会话，先发 /elec alert here。")
                return
            msg = "🔔 苏大电费测试消息：预警通道正常。"
            ok_n = 0
            for s in sessions:
                try:
                    if await self.context.send_message(s, self.build_message_chain(msg)):
                        ok_n += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[苏大电费] 测试消息发送失败({s}): {exc}")
            yield event.plain_result(f"✅ 测试消息已发送 {ok_n}/{len(sessions)} 个会话。")
            return

        yield event.plain_result(
            "用法：/elec alert here | list | remove <序号> | test"
        )

    # --------------------------------------------------------- 会话存取

    def _sessions_list(self) -> list[str]:
        raw = self.store.get_alert_sessions() or str(self._cfg("alert_sessions", "") or "")
        return [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()]

    def _write_sessions(self, sessions: list[str]) -> None:
        value = ",".join(sessions)
        self.store.set_alert_sessions(value)
        self.config["alert_sessions"] = value
        self._save_config()

    # --------------------------------------------------------- 查询/添加文本入口

    async def _lookup_text(self, region: str, building: str, room: str) -> str:
        try:
            res = await SudaClient.resolve_room(
                self.client, region=region, building=building, room=room
            )
            info = await self.client.quick_balance(res["account_no"])
        except ValueError as exc:
            return f"❌ {exc}"
        except SudaApiError as exc:
            return f"❌ 查询失败：{exc}"
        loc = " ".join(
            x
            for x in (res["region"].get("name"), res["building"].get("name"), res["room"].get("name"))
            if x
        )
        bal = info.get("balance")
        bal_text = "暂无数据（可能未装表或已退宿）" if bal is None else f"{bal:g} 元"
        extra = ""
        monitored = self.store.get_room(res["account_no"])
        if monitored:
            stats = self._room_stats(res["account_no"])
            records = self.store.get_records(res["account_no"])
            extra = f"（已在监控中，自 {datetime.fromtimestamp(records[0]['ts']).strftime('%m-%d')} 起，"
            if stats.get("delta_24h") is not None:
                extra += f"24h 变化 {stats['delta_24h']:+g} 元）"
            else:
                extra += f"共 {len(records)} 条记录）"
        return f"⚡ {loc}\n当前余额：{bal_text}{extra}"

    async def _add_room_text(self, region: str, building: str, room: str) -> tuple[str, bool]:
        try:
            res = await SudaClient.resolve_room(
                self.client, region=region, building=building, room=room
            )
            account_no = res["account_no"]
            already = self.store.get_room(account_no)
            if already:
                snap = self._room_snapshot(already)
                bal = "未知" if snap["balance"] is None else f"{snap['balance']:g} 元"
                return (
                    f"ℹ️ {res['room'].get('name')} 已在监控中（余额 {bal}），无需重复添加。",
                    True,
                )
            uid = self.store.allocate_uid()
            await self.client.bind_room(uid, account_no)
            bind_id = None
            info = None
            for d in await self.client.get_devices(uid):
                if str(d.get("accountNo") or "") == account_no:
                    info = self.client.summarize_device(d)
                    bind_id = info["bind_id"]
                    break
            loc = " ".join(
                x
                for x in (res["region"].get("name"), res["building"].get("name"), res["room"].get("name"))
                if x
            )
            self.store.upsert_room(
                account_no,
                name=res["room"].get("name") or "",
                location=loc,
                bind_id=bind_id,
                uid=uid,
            )
            await self.store.save_state()
            self.monitor.wake()
            bal = info["balance"] if info else None
            bal_text = "待下次轮询获取" if bal is None else f"{bal:g} 元"
            return (
                f"✅ 已添加监控：{loc}（{account_no}）\n当前余额：{bal_text}\n"
                f"提醒：每间宿舍需要一个独立虚拟账号，当前账号池 {len(self.store.all_uids())} 个。",
                True,
            )
        except ValueError as exc:
            return f"❌ {exc}", False
        except SudaApiError as exc:
            return f"❌ 添加失败：{exc}", False

    # ========================================================== LLM 工具

    @filter.llm_tool("elec_query")
    async def elec_query(self, event: AstrMessageEvent, query: str = "", since: str = ""):
        """查询苏州大学宿舍电费监控数据。返回宿舍名、位置、当前余额、阈值、24小时变化、监控以来最高/最低余额等统计。

        Args:
            query(string): 宿舍名称或房间号关键词，可为空表示查询全部挂载的宿舍。例如 "101" 或 "独墅湖"。
            since(string): 可选。从哪一天开始统计，格式 YYYY-MM-DD（如 "2026-10-01"）或 "7d" 表示最近7天。提供后将返回该日期以来的首末余额与变化。
        """
        rooms = self.store.list_rooms()
        if not rooms:
            return "当前没有挂载任何宿舍。可以说「帮我监控 独墅湖 101号楼 101」或在 WebUI 插件页面添加。"
        keyword = (query or "").strip()
        if keyword:
            lowered = keyword.lower()
            matched = [
                r
                for r in rooms
                if lowered
                in f"{r.get('name', '')} {r.get('location', '')} {r['account_no']}".lower()
            ]
            if not matched:
                return f"没有找到匹配「{keyword}」的宿舍。已挂载：{', '.join(r.get('location') or r['account_no'] for r in rooms)}"
            rooms = matched

        since_ts, since_text = self._parse_since(since)
        lines = []
        for room in rooms:
            snap = self._room_snapshot(room)
            bal = "未知" if snap["balance"] is None else f"{snap['balance']:g} 元"
            line = f"{snap['location']}（{snap['account_no']}）：余额 {bal}"
            if since_ts:
                line += self._since_text(room["account_no"], since_ts, since_text)
            else:
                if snap["delta_24h"] is not None:
                    line += f"，24h 变化 {snap['delta_24h']:+g} 元"
                if snap["max_balance"] is not None:
                    line += f"，区间 {snap['min_balance']:g}~{snap['max_balance']:g} 元"
            line += f"，预警阈值 {snap['threshold']:g} 元"
            if snap["balance"] is not None and snap["balance"] < snap["threshold"]:
                line += " ⚠️ 已低于阈值，建议尽快充值"
            lines.append(line)
        header = f"苏大宿舍电费（{'关键词: ' + keyword if keyword else '全部 ' + str(len(rooms)) + ' 间'}）："
        return header + "\n" + "\n".join(lines)

    @filter.llm_tool("elec_lookup")
    async def elec_lookup(self, event: AstrMessageEvent, region: str, building: str, room: str):
        """立即查询苏州大学任意宿舍的当前电费余额（无需提前监控，输入校区、楼栋、房间即可秒查）。

        Args:
            region(string): 校区名称或关键词，如 "独墅湖"、"天赐庄本部"、"阳澄湖"、"未来"。
            building(string): 楼栋名称或关键词，如 "101号楼"。
            room(string): 房间号，如 "101"；也可以直接给 15 位房间编号 accountNo。
        """
        return await self._lookup_text(region, building, room)

    @filter.llm_tool("elec_add")
    async def elec_add(self, event: AstrMessageEvent, region: str, building: str, room: str):
        """添加一间苏州大学宿舍到电费监控（输入校区、楼栋、房间号）。添加后定时记录余额并在低于阈值时预警。

        Args:
            region(string): 校区名称或关键词，如 "独墅湖"。
            building(string): 楼栋名称或关键词，如 "101号楼"。
            room(string): 房间号，如 "101"；也可以直接给 15 位房间编号 accountNo。
        """
        text, _ok = await self._add_room_text(region, building, room)
        return text

    @filter.llm_tool("elec_remove")
    async def elec_remove(self, event: AstrMessageEvent, query: str):
        """移除一间宿舍的电费监控。

        Args:
            query(string): 宿舍名称、位置或房间编号关键词，如 "101号楼101"。
        """
        target = self.store.find_room_by_keyword(query)
        if not target:
            rooms = "、".join(r.get("location") or r["account_no"] for r in self.store.list_rooms())
            return f"没有找到匹配「{query}」的宿舍。当前监控：{rooms or '（无）'}"
        uid = target.get("uid")
        bind_id = target.get("bind_id")
        if uid and bind_id is not None:
            try:
                await self.client.unbind_room(uid, bind_id)
            except SudaApiError:
                pass
        self.store.remove_room(target["account_no"])
        await self.store.save_state()
        return f"✅ 已移除 {target.get('location') or target['account_no']} 的监控。"

    @filter.llm_tool("elec_watchlist")
    async def elec_watchlist(self, event: AstrMessageEvent):
        """查询苏大电费监控插件的运行状态：挂载宿舍数、轮询间隔、预警配置与接收会话数、上次轮询时间与结果。

        Args:
        """
        rooms = self.store.list_rooms()
        last_poll = (
            datetime.fromtimestamp(self.monitor.last_poll_ts).strftime("%Y-%m-%d %H:%M:%S")
            if self.monitor and self.monitor.last_poll_ts
            else "尚未轮询"
        )
        status = (
            "正常"
            if (self.monitor and self.monitor.last_poll_ok)
            else ("异常: " + (self.monitor.last_error or "") if self.monitor else "未知")
        )
        return (
            f"苏大电费监控状态：\n"
            f"- 挂载宿舍：{len(rooms)} 间\n"
            f"- 虚拟账号池：{len(self.store.all_uids())} 个（平台限制一账号绑一房间）\n"
            f"- 轮询间隔：{self._cfg('interval_minutes', 30)} 分钟\n"
            f"- 全局预警阈值：{self._cfg('threshold', 20.0)} 元，"
            f"预警{'开启' if self._cfg('alert_enabled', True) else '关闭'}\n"
            f"- 预警会话：{len(self._sessions_list())} 个\n"
            f"- 上次轮询：{last_poll}（{status}）"
        )

    # --------------------------------------------------------- since 解析

    def _parse_since(self, since: str) -> tuple[float | None, str]:
        s = (since or "").strip()
        if not s:
            return None, ""
        now = datetime.now()
        low = s.lower()
        if low.endswith("d") and low[:-1].isdigit():
            days = int(low[:-1])
            ts = (now - timedelta(days=days)).timestamp()
            return ts, f"最近 {days} 天"
        if low.endswith("h") and low[:-1].isdigit():
            hours = int(low[:-1])
            ts = (now - timedelta(hours=hours)).timestamp()
            return ts, f"最近 {hours} 小时"
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%Y.%m.%d"):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.year == 1900:
                    dt = dt.replace(year=now.year)
                return dt.timestamp(), dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None, ""

    def _since_text(self, account_no: str, since_ts: float, since_text: str) -> str:
        records = self.store.get_records_since(account_no, since_ts)
        if not records:
            return f"，{since_text}以来无记录"
        first, last = records[0], records[-1]
        if first["balance"] is None or last["balance"] is None:
            return f"，{since_text}以来 {len(records)} 条记录"
        delta = last["balance"] - first["balance"]
        used = ""
        return (
            f"，{since_text}以来 {len(records)} 条记录："
            f"{first['balance']:g} → {last['balance']:g} 元（变化 {delta:+g} 元）"
        ).replace(used, "")

    @filter.on_llm_request()
    async def inject_usage_hint(self, event: AstrMessageEvent, req) -> None:
        """向系统提示词注入电费工具的使用说明。"""
        hint = (
            "\n\n[苏大电费插件]\n"
            "你可以使用以下苏州大学宿舍电费工具：\n"
            "- elec_lookup(region, building, room)：报校区+楼栋+房间号立即查询任意宿舍余额（无需监控）；\n"
            "- elec_add(region, building, room)：添加宿舍到定时监控；\n"
            "- elec_query(query, since)：查询已监控宿舍的余额与统计，since 可指定从某日起的变化；\n"
            "- elec_remove(query)：移除监控；elec_watchlist()：查看插件状态。\n"
            "用户提到宿舍电费/电量/余额/充值提醒时优先使用这些工具。添加监控或查询时，"
            "若用户提供的是「校区 楼栋 房间号」三元组，直接作为参数调用。\n"
        )
        try:
            if hint not in (req.system_prompt or ""):
                req.system_prompt = (req.system_prompt or "") + hint
        except Exception:  # noqa: BLE001
            pass
