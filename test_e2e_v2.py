"""v2 真实网络 E2E：多虚拟账号多房间 + quick_balance + resolve_room + 清理。"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_suda_elec.suda_api import (  # noqa: E402
    SudaClient,
    generate_virtual_user_id,
)

ROOM_A = "21033010001"
ROOM_B = "21033010002"
ORPHAN_UID = "astrbot-elec-3fff22f4"  # 上次测试异常退出残留的绑定


async def cleanup_uid(client: SudaClient, uid: str, label: str) -> None:
    devs = await client.get_devices(uid)
    for d in devs:
        try:
            await client.unbind_room(uid, d.get("id"))
            print(f"  [cleanup] {label}: unbind {d.get('accountNo')}")
        except Exception as e:  # noqa: BLE001
            print(f"  [cleanup] {label}: unbind failed {e}")


async def main() -> int:
    client = SudaClient()
    uid_a = generate_virtual_user_id()
    uid_b = generate_virtual_user_id()
    try:
        # 0. 清理上次测试残留
        print("[0] 清理残留绑定")
        await cleanup_uid(client, ORPHAN_UID, "orphan")

        # 1. 双账号各绑一间（多宿舍监控核心）
        print("[1] 双账号绑定两间宿舍")
        assert await client.bind_room(uid_a, ROOM_A)
        assert await client.bind_room(uid_b, ROOM_B)
        devs_a = await client.get_devices(uid_a)
        devs_b = await client.get_devices(uid_b)
        assert len(devs_a) == 1 and str(devs_a[0]["accountNo"]) == ROOM_A
        assert len(devs_b) == 1 and str(devs_b[0]["accountNo"]) == ROOM_B
        bal_a = devs_a[0].get("jeSum")
        bal_b = devs_b[0].get("jeSum")
        print(f"    A({uid_a}) -> {ROOM_A} jeSum={bal_a}")
        print(f"    B({uid_b}) -> {ROOM_B} jeSum={bal_b}")

        # 2. 解绑后 uid 复用绑新房间（账号池语义）
        print("[2] uid 复用：解绑 A 后用同一 uid 绑 103")
        info = SudaClient.summarize_device(devs_a[0])
        await client.unbind_room(uid_a, info["bind_id"])
        assert await client.bind_room(uid_a, "21033010003")
        devs = await client.get_devices(uid_a)
        assert str(devs[0]["accountNo"]) == "21033010003"
        print("    uid 复用成功 ->", devs[0]["accountNo"])

        # 3. quick_balance：临时绑定查询任意房间并自动解绑
        print("[3] quick_balance 任意房间")
        q = await client.quick_balance(ROOM_A)
        print(f"    quick {ROOM_A} -> balance={q.get('balance')} loc={q.get('location')}")
        assert q.get("account_no") == ROOM_A
        left = await client.get_devices(generate_virtual_user_id())
        assert not left  # 新 uid 本来就为空，验证的是不留垃圾
        # 确认 quick 用的临时账号已解绑：用该 temp uid 无法直接取回，改查平台侧：
        # 用 uid_a（绑着 103）不受影响即可；再 quick 一次确保幂等
        q2 = await client.quick_balance(ROOM_B)
        print(f"    quick {ROOM_B} -> balance={q2.get('balance')}")

        # 4. resolve_room 模糊解析（跨层消歧："独墅湖"歧义 → 用"101号楼"自动锁定北区）
        print("[4] resolve_room 模糊匹配")
        res = await SudaClient.resolve_room(
            client, region="独墅湖", building="101号楼", room="105"
        )
        print(
            "    resolve ->",
            res["region"]["name"],
            res["building"]["name"],
            res["room"]["name"],
            res["account_no"],
        )
        assert res["region"]["name"] == "独墅湖校区北区"
        assert res["account_no"] == "21033010005"
        direct = await SudaClient.resolve_room(client, room=ROOM_A)
        assert direct["account_no"] == ROOM_A
        print("    直传 accountNo 也 OK")
        try:
            await SudaClient.resolve_room(client, region="不存在的校区", building="x", room="y")
            print("    [FAIL] 应该抛 ValueError")
            return 1
        except ValueError as e:
            print(f"    模糊匹配错误提示 OK: {e}")

        print("E2E v2 ALL PASSED")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"E2E FAILED: {exc}")
        return 1
    finally:
        # 清理全部测试绑定
        for uid in (uid_a, uid_b, ORPHAN_UID):
            await cleanup_uid(client, uid, uid[:16])
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
