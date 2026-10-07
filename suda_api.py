"""苏州大学宿舍水电缴费平台 API 客户端（免认证方案 v2）。

逆向自 https://ny.hq.suda.edu.cn/prepaid/ 前端（uni-app）：

- 网关地址: https://ny.hq.suda.edu.cn/api
- 鉴权: 请求头 ``Authorization: Nla <平台内置 JWT>``（前端硬编码的公共设备令牌，
  仅标识接入端，不标识用户身份）；
- 用户态: 业务参数里携带 ``userId``（openId 形态）。服务端不校验 userId 的真实性，
  因此插件可以使用自生成虚拟账号完成 绑定房间 / 查询余额，完全跳过统一身份认证；
- **平台限制: 每个账号（userId）只能绑定一个房间**（多绑返回 code=-1
  "只能绑定一个房间"）。因此多宿舍监控 = 每间宿舍一个独立虚拟账号，由插件侧
  账号池管理；
- 响应包裹: ``{"code": "0", "msg": "...", "data": ...}``，code != "0" 视为业务错误。
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import aiohttp

API_BASE = "https://ny.hq.suda.edu.cn/api"

# 前端 index bundle 中硬编码的公共设备 JWT（Nla 前缀 + token）。
# 它只是"接入设备令牌"，所有合法客户端共用，不承载用户身份。
_DEVICE_TOKEN = (
    "Nla eyJhbGciOiJIUzI1NiIsInppcCI6IkRFRiJ9."
    "eNosjDsOwjAQRO-yBQWKUfxNNhVINFBwB9uxFUchINlWQIi7swXlm5k3H8jVwQC3xe6ueZmOZd72o"
    "uUCGjh5_6hruZyp58T5nUu4E6yLPWzBT7ZQGl5PGBS2KDpptGyg1jTSKPreiegd60ePTHEeGVobmH"
    "IqOi1DkGhIT3Qy8E5r89fnkshGRPj-AAAA__8."
    "ouLTXj7i0EIhbiJJUMm46gYKPvf65pN8I1wW2qzjVz4"
)

_RECHARGE = "/v2/wechat/szdx/rechargeApp"

ERR_ONLY_ONE_ROOM = "只能绑定一个房间"


class SudaApiError(Exception):
    """苏大平台业务错误（code != 0）或网络异常。"""


def generate_virtual_user_id() -> str:
    """生成一个稳定的虚拟账号 ID（openId 形态，不与真实用户冲突）。"""
    return f"astrbot-elec-{secrets.token_hex(4)}"


class SudaClient:
    """苏大宿舍电费平台客户端（免统一身份认证，支持多虚拟账号）。

    v2: 平台限制一个账号只能绑定一个房间，因此所有绑定/查询方法都显式
    接收 ``uid``（虚拟账号），由上层（storage/monitor）负责账号池分配。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        retries: int = 2,
        verify_ssl: bool = True,
        logger=None,
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._retries = max(0, retries)
        self._verify_ssl = verify_ssl
        self._logger = logger
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------- 生命周期

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=None if self._verify_ssl else False)
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                connector=connector,
                headers={
                    "Authorization": _DEVICE_TOKEN,
                    "Content-Type": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
                    ),
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------- 底层请求

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        url = f"{API_BASE}{path}"
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            session = await self._ensure_session()
            try:
                async with session.request(
                    method, url, json=json_body, params=params
                ) as resp:
                    if resp.status != 200:
                        raise SudaApiError(f"HTTP {resp.status} from {path}")
                    payload = await resp.json(content_type=None)
                code = str(payload.get("code", ""))
                if code != "0":
                    raise SudaApiError(
                        f"平台返回错误 code={code} msg={payload.get('msg')}"
                    )
                return payload.get("data")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < self._retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
        raise SudaApiError(f"请求 {path} 失败: {last_exc}") from last_exc

    async def _post(self, path: str, data: dict | None = None) -> Any:
        return await self._request("POST", path, json_body=data or {})

    async def _get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    # ------------------------------------------------------------- 浏览接口

    async def get_regions(self) -> list[dict]:
        """校区列表。[{id, name}]"""
        data = await self._post(f"{_RECHARGE}/getBuildList")
        return data or []

    async def get_buildings(self, region_id: str) -> list[dict]:
        """校区下的楼栋列表。[{id, name}]"""
        data = await self._post(f"{_RECHARGE}/getBuildOrFloor", {"id": str(region_id)})
        return data or []

    async def get_rooms(self, building_id: str) -> list[dict]:
        """楼栋下的房间列表。[{id, name}]，id 即 accountNo。"""
        data = await self._post(f"{_RECHARGE}/getRoomAccount", {"id": str(building_id)})
        return data or []

    # ------------------------------------------------------------- 绑定与查询

    async def bind_room(self, uid: str, account_no: str) -> bool:
        """把房间绑定到指定虚拟账号下。一个 uid 只能绑定一个房间。"""
        data = await self._post(
            f"{_RECHARGE}/saveUserAccount",
            {"accountNo": str(account_no), "userId": uid},
        )
        return bool(data)

    async def unbind_room(self, uid: str, bind_id) -> bool:
        """解除绑定。bind_id 为 get_devices 返回的绑定记录 id（非 accountNo）。"""
        data = await self._post(
            f"{_RECHARGE}/deleteUserAccount", {"id": bind_id, "userId": uid}
        )
        return bool(data)

    async def get_devices(self, uid: str) -> list[dict]:
        """查询指定虚拟账号绑定的房间（含最新余额）。

        单账号最多一个房间，返回 0 或 1 项。
        每项包含: id(绑定记录id) / accountNo / roomName / buildName / location /
        jeSum(当前余额) / sybzje / sylje / isNormal / tip 等。
        """
        data = await self._post(f"{_RECHARGE}/getMyBandDev", {"userId": uid})
        return data or []

    async def quick_balance(self, account_no: str) -> dict:
        """免留存查询任意房间当前余额：临时账号绑定 → 查询 → 立即解绑。"""
        uid = generate_virtual_user_id()
        await self.bind_room(uid, account_no)
        try:
            devices = await self.get_devices(uid)
        except SudaApiError:
            await self._try_unbind_all(uid)
            raise
        info = None
        for d in devices:
            if str(d.get("accountNo") or "") == str(account_no):
                info = self.summarize_device(d)
                break
        if info is None:
            info = {"account_no": str(account_no), "balance": None}
        ok = False
        try:
            if info.get("bind_id") is not None:
                ok = await self.unbind_room(uid, info["bind_id"])
        except SudaApiError:
            ok = False
        if not ok:
            # 解绑失败兜底：再试一次全部清理
            await self._try_unbind_all(uid)
        return info

    async def _try_unbind_all(self, uid: str) -> None:
        try:
            for d in await self.get_devices(uid):
                try:
                    await self.unbind_room(uid, d.get("id"))
                except SudaApiError:
                    pass
        except SudaApiError:
            pass

    # ------------------------------------------------------------- 工具

    @staticmethod
    def summarize_device(dev: dict) -> dict:
        """把平台设备数据归一化为插件内部结构。"""
        return {
            "bind_id": dev.get("id"),
            "account_no": str(dev.get("accountNo") or dev.get("roomdm") or ""),
            "room_name": dev.get("roomName") or "",
            "build_name": dev.get("buildName") or "",
            "location": dev.get("location")
            or " ".join(
                x for x in (dev.get("regionName"), dev.get("buildName"), dev.get("roomName")) if x
            ),
            "balance": _to_float(dev.get("jeSum")),
            "balance_std": _to_float(dev.get("sybzje")),
            "balance_acc": _to_float(dev.get("sylje")),
            "is_normal": bool(dev.get("isNormal", True)),
            "tip": dev.get("tip"),
        }

    @staticmethod
    async def resolve_room(
        client: "SudaClient",
        *,
        region: str | None = None,
        building: str | None = None,
        room: str | None = None,
    ) -> dict:
        """按名称模糊解析 校区→楼栋→房间，返回 {region, building, room, account_no}。

        消歧策略：跨层联合匹配——校区有歧义（如"独墅湖"）时，用楼栋在各候选校区
        中继续匹配，只有唯一校区能匹配出该楼栋才通过；反之亦然。
        room 也可以直接传 15 位 accountNo（此时其余参数可省略）。
        解析失败抛 ValueError（带可读原因）。
        """
        room_q = (room or "").strip()
        if room_q.isdigit() and len(room_q) >= 10:
            return {
                "region": {"id": "", "name": region or ""},
                "building": {"id": "", "name": building or ""},
                "room": {"id": room_q, "name": room_q},
                "account_no": room_q,
            }

        regions = await client.get_regions()
        region_cands = _candidates(regions, region, "校区")
        if not region_cands:
            raise _no_match_error(regions, region, "校区")

        # 校区（可能多个）→ 楼栋联合匹配
        building_hits: list[tuple[dict, list[dict]]] = []
        building_err: ValueError | None = None
        for r in region_cands:
            buildings = await client.get_buildings(r["id"])
            cands = _candidates(buildings, building, "楼栋")
            if cands:
                building_hits.append((r, cands))
            elif building_err is None:
                building_err = _no_match_error(buildings, building, "楼栋", context=r.get("name"))

        if len(building_hits) > 1:
            names = "、".join(r.get("name", "") for r, _ in building_hits)
            raise ValueError(f"楼栋「{building}」在多个校区都存在（{names}），请指定校区")
        if not building_hits:
            raise building_err or ValueError(f"没有找到楼栋「{building}」")
        r, b_cands = building_hits[0]
        if len(b_cands) > 1:
            raise ValueError(
                f"楼栋「{building}」匹配到 {len(b_cands)} 个: "
                + "、".join(str(it.get("name")) for it in b_cands[:6])
                + "，请说得更具体"
            )
        b = b_cands[0]

        rooms = await client.get_rooms(b["id"])
        m = _pick(rooms, room_q, "房间")
        return {
            "region": r,
            "building": b,
            "room": m,
            "account_no": str(m["id"]),
        }


def _candidates(items: list[dict], keyword: str, label: str) -> list[dict]:
    """按关键词返回候选列表：全名一致 > id 一致 > 包含匹配。空关键词返回全部。"""
    kw = (keyword or "").strip()
    if not kw:
        return list(items)
    exact = [it for it in items if str(it.get("name", "")).strip() == kw]
    if exact:
        return exact
    id_match = [it for it in items if str(it.get("id", "")) == kw]
    if id_match:
        return id_match
    return [it for it in items if kw in str(it.get("name", ""))]


def _no_match_error(items: list[dict], keyword: str, label: str, context: str = "") -> ValueError:
    kw = (keyword or "").strip()
    prefix = f"{context} " if context else ""
    if not kw:
        return ValueError(f"请提供{label}名称")
    return ValueError(
        f"{prefix}没有找到{label}「{kw}」，可选: "
        + "、".join(str(it.get("name")) for it in items[:12])
        + ("…" if len(items) > 12 else "")
    )


def _pick(items: list[dict], keyword: str, label: str) -> dict:
    """在 [{id, name}] 里按关键词模糊匹配；唯一或前缀唯一才接受。"""
    cands = _candidates(items, keyword, label)
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        raise ValueError(
            f"{label}「{keyword}」匹配到 {len(cands)} 个: "
            + "、".join(str(it.get("name")) for it in cands[:6])
            + "，请说得更具体"
        )
    raise _no_match_error(items, keyword, label)


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
