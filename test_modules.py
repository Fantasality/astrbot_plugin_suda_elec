"""storage / suda_api 模块的独立单元测试（不依赖 astrbot）。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_suda_elec import suda_api  # noqa: E402
from astrbot_plugin_suda_elec import storage  # noqa: E402

schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
assert all(("type" in v and "default" in v) for v in schema.values()), "schema 缺字段"
print("schema OK:", len(schema), "项配置")

print("virtual uid sample:", suda_api.generate_virtual_user_id())

st = storage.Store(ROOT / "tmp_store_test")
st.upsert_room("21033010001", name="101", location="独墅湖 101")
st.append_record("21033010001", balance=-0.27)
st.upsert_room("21033010001", threshold=10.5)
assert st.room_threshold("21033010001", 20.0) == 10.5, "阈值覆盖失败"
assert st.room_threshold("other", 20.0) == 20.0, "全局阈值回退失败"
print("rooms:", st.list_rooms())
print("records:", st.get_records("21033010001"))
assert st.remove_room("21033010001")
assert not st.list_rooms() and not st.get_records("21033010001")
print("remove OK")

# summarize_device 字段归一化（v2 静态方法）
sample = {
    "id": 73017, "accountNo": "21033010001", "roomdm": "21033010001",
    "roomName": "101", "buildName": "101号楼", "location": "独墅湖校区北区101号楼101",
    "sybzje": 0.0, "sylje": -0.27, "jeSum": -0.27, "isNormal": True, "tip": None,
}
info = suda_api.SudaClient.summarize_device(sample)
assert info["account_no"] == "21033010001" and info["balance"] == -0.27
assert info["bind_id"] == 73017 and info["is_normal"] is True
print("summarize_device OK:", info)

# 账号池分配/复用
st.upsert_room("A", uid=st.allocate_uid())
uid1 = st.list_rooms()[0]["uid"]
uid2 = st.allocate_uid()
assert uid1 != uid2, "账号池应分配不同 uid"
st.remove_room("A")
uid3 = st.allocate_uid()
assert uid3 == uid1, "空闲 uid 应被复用"
print("uid pool OK:", st.all_uids())

# alert sessions 存取
st.set_alert_sessions("a:b:c,x:y:z")
assert st.get_alert_sessions() == "a:b:c,x:y:z"
print("alert sessions OK")

import shutil
shutil.rmtree(ROOT / "tmp_store_test", ignore_errors=True)
print("ALL MODULE TESTS PASSED")
