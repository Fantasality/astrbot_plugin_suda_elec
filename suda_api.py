"""苏州大学宿舍水电缴费平台 API 客户端（免认证方案）。

逆向自 https://ny.hq.suda.edu.cn/prepaid/ 前端（uni-app）：

- 网关地址: https://ny.hq.suda.edu.cn/api
- 鉴权: 请求头 ``Authorization: Nla <平台内置 JWT>``（前端硬编码的公共设备令牌，
  仅标识接入端，不标识用户身份）；
- 用户态: 业务参数里携带 ``userId``（openId 形态）。服务端不校验 userId 的真实性，
  因此插件可以使用自生成虚拟账号完成 绑定房间 / 查询余额，完全跳过统一身份认证；
- 响应包裹: ``{"code": "0", "msg": "...", "data": ...}``，code != "0" 视为业务错误。

所有方法均为协程，线程安全由调用方（asyncio 事件循环）保证。
"""

from __future__ import annotations

import asyncio
import secrets
import time
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


class SudaApiError(Exception):
    """苏大平台业务错误（code != 0）或网络异常。"""


def generate_virtual_user_id() -> str:
    """生成一个稳定的虚拟账号 ID（openId 形态，不与真实用户冲突）。"""
    return f"astrbot-elec-{secrets.token_hex(4)}"


class SudaClient:
    """苏大宿舍电费平台客户端（免统一身份认证）。"""

    def __init__(
        self,
        user_id: str,
        *,
        timeout_seconds: float = 15.0,
        retries: int = 2,
        verify_ssl: bool = True,
        logger=None,
    ) -> None:
        self.user_id = user_id
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

    async def bind_room(self, account_no: str) -> bool:
        """把房间绑定到虚拟账号下（等同官方 App 的“绑定设备”）。"""
        data = await self._post(
            f"{_RECHARGE}/saveUserAccount",
            {"accountNo": str(account_no), "userId": self.user_id},
        )
        return bool(data)

    async def unbind_room(self, bind_id) -> bool:
        """解除绑定。bind_id 为 get_devices 返回的绑定记录 id（非 accountNo）。"""
        data = await self._post(f"{_RECHARGE}/deleteUserAccount", {"id": bind_id})
        return bool(data)

    async def get_devices(self) -> list[dict]:
        """查询虚拟账号绑定的全部房间（含最新余额），一次请求返回所有宿舍。

        每项包含: id(绑定记录id) / accountNo / roomName / buildName / location /
        jeSum(当前余额) / sybzje / sylje / isNormal / tip 等。
        """
        data = await self._post(f"{_RECHARGE}/getMyBandDev", {"userId": self.user_id})
        return data or []

    async def get_user_info(self) -> dict | None:
        """虚拟账号信息（一般无更多内容，保留用于连通性探测）。"""
        try:
            return await self._get(
                f"{_RECHARGE}/getUserInfo", {"userId": self.user_id}
            )
        except SudaApiError:
            return None

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


def _to_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def now_ts() -> float:
    return time.time()
