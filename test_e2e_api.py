"""SudaClient 真实网络端到端测试（只读+临时绑定，结束即解绑清理）。"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from suda_api import SudaApiError, SudaClient, generate_virtual_user_id  # noqa: E402


async def main() -> int:
    client = SudaClient(generate_virtual_user_id(), timeout_seconds=20)
    try:
        # 1. 校区
        regions = await client.get_regions()
        assert regions and regions[0].get("id"), "校区列表为空"
        print(f"[1] 校区 OK: {[r['name'] for r in regions]}")
        region_id = regions[3]["id"]  # 独墅湖校区北区
        region_name = regions[3]["name"]

        # 2. 楼栋
        buildings = await client.get_buildings(region_id)
        assert buildings, "楼栋列表为空"
        print(f"[2] 楼栋 OK: {region_name} {len(buildings)} 栋，如 {[b['name'] for b in buildings[:3]]}")
        building = buildings[0]

        # 3. 房间
        rooms = await client.get_rooms(building["id"])
        assert rooms, "房间列表为空"
        print(f"[3] 房间 OK: {building['name']} {len(rooms)} 间，如 {[r['name'] for r in rooms[:3]]}")
        room = rooms[0]

        # 4. 绑定
        ok = await client.bind_room(room["id"])
        assert ok, "绑定失败"
        print(f"[4] 绑定 OK: {room['name']}（accountNo={room['id']}）")

        # 5. 查询余额
        devices = await client.get_devices()
        assert devices, "绑定列表为空"
        info = client.summarize_device(devices[0])
        assert info["account_no"] == str(room["id"])
        print(
            f"[5] 余额查询 OK: {info['location']} 余额={info['balance']} "
            f"标准余额={info['balance_std']} 累计={info['balance_acc']} isNormal={info['is_normal']}"
        )

        # 6. 解绑清理
        bind_id = info["bind_id"]
        assert await client.unbind_room(bind_id), "解绑失败"
        devices_after = await client.get_devices()
        assert not devices_after, "解绑后列表应清空"
        print("[6] 解绑清理 OK")

        print("E2E ALL PASSED")
        return 0
    except SudaApiError as exc:
        print(f"E2E FAILED: {exc}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
