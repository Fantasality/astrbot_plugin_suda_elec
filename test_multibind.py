"""关键实验：多绑定支持 + getRechargeDev 行为 + 解绑粒度。"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from suda_api import SudaClient, generate_virtual_user_id  # noqa: E402

ROOM_A = "21033010001"  # 独墅湖北区 101号楼 101
ROOM_B = "21033010002"  # 102
ROOM_C = "21033010003"  # 103


async def main() -> None:
    uid = generate_virtual_user_id()
    client = SudaClient(uid)
    try:
        print(f"virtual uid = {uid}")
        # 1. 连绑三个房间
        for r in (ROOM_A, ROOM_B, ROOM_C):
            ok = await client.bind_room(r)
            print(f"bind {r} -> {ok}")

        # 2. getMyBandDev 返回几个？
        devs = await client.get_devices()
        print(f"getMyBandDev count = {len(devs)}")
        for d in devs:
            print("  -", d.get("accountNo"), d.get("roomName"), "jeSum=", d.get("jeSum"))

        # 3. getRechargeDev 在已绑定状态下是否可用？
        try:
            data = await client._get(
                "/v2/wechat/szdx/rechargeApp/getRechargeDev",
                {"accountNo": ROOM_A, "userId": uid},
            )
            print("getRechargeDev(bound) ->", data if not isinstance(data, dict) else {k: data.get(k) for k in ("accountNo", "roomName", "jeSum")})
        except Exception as e:  # noqa: BLE001
            print("getRechargeDev(bound) error:", e)

        # 4. 解绑其中一个，看另外两个是否保留
        if devs:
            target = next((d for d in devs if str(d.get("accountNo")) == ROOM_B), None)
            if target:
                ok = await client.unbind_room(target.get("id"))
                print(f"unbind {ROOM_B} -> {ok}")
        devs2 = await client.get_devices()
        print(f"after unbind count = {len(devs2)}: {[d.get('accountNo') for d in devs2]}")

        # 清理：全部解绑
        for d in await client.get_devices():
            await client.unbind_room(d.get("id"))
        print("cleanup done, left:", len(await client.get_devices()))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
