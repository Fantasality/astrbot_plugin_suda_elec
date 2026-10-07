"""市场就绪校验：按 AstrBot 官方逻辑验证插件包格式。"""

import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from astrbot_plugin_suda_elec import suda_api, storage  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# 1. metadata.yaml 按 AstrBot validate_plugin_metadata 规则校验
import yaml  # noqa: E402

meta = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
REQUIRED = ("name", "desc", "version", "author")
missing = [f for f in REQUIRED if f not in meta]
check("metadata 必填字段(name/desc/version/author)", not missing, str(missing))
invalid = [
    f for f in REQUIRED
    if not isinstance(meta.get(f), str) or not meta.get(f, "").strip()
]
check("metadata 必填字段均为非空字符串", not invalid, str(invalid))
check("metadata.repo 指向 GitHub", str(meta.get("repo", "")).startswith("https://github.com/Fantasality/"))
check("metadata.astrbot_version 声明", isinstance(meta.get("astrbot_version"), str))
check("metadata.tags 提供且为列表", isinstance(meta.get("tags"), list) and len(meta["tags"]) > 0)
check("metadata.display_name/short_desc", bool(meta.get("display_name")) and bool(meta.get("short_desc")))
check("metadata.desc 以 # 开头(市场渲染为 md)", str(meta.get("desc", "")).startswith("#"))

# 2. 结构
for rel in ["main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt",
            "pages/dashboard/index.html", ".astrbot-plugin/i18n/zh-CN.json"]:
    check(f"存在 {rel}", (ROOT / rel).is_file())

# 3. _conf_schema.json 合法且类型齐全
schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
ok_schema = all(("type" in v and "name" in v and "description" in v) for v in schema.values())
check("_conf_schema.json 字段完整(type/name/description)", ok_schema, f"{len(schema)} 项")

# 4. Python 编译
r = subprocess.run(
    [sys.executable, "-m", "py_compile",
     *[str(ROOT / f) for f in ("main.py", "suda_api.py", "storage.py", "monitor.py", "__init__.py")]],
    capture_output=True, text=True,
)
check("py_compile 全部通过", r.returncode == 0, r.stderr[:200])

# 5. 模块逻辑
st = storage.Store(ROOT / "tmp_market_test")
st.upsert_room("T1", uid="u1")
st.set_room_alert_sessions("T1", ["a:1", "b:2"])
check("房间级预警路由存取", st.room_alert_sessions("T1") == ["a:1", "b:2"])
st.set_room_alert_sessions("T1", [])
check("清空路由=跟随全局", st.room_alert_sessions("T1") == [])
st.remove_room("T1")

# 6. zip 打包演练（内存）: 顶层目录 + metadata
import io  # noqa: E402

buf = io.BytesIO()
INCLUDE = ["metadata.yaml", "_conf_schema.json", "main.py", "suda_api.py", "storage.py",
           "monitor.py", "__init__.py", "README.md", "pages/dashboard/index.html",
           ".astrbot-plugin/i18n/zh-CN.json", ".astrbot-plugin/i18n/en-US.json"]
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    for rel in INCLUDE:
        z.write(ROOT / rel, f"astrbot_plugin_suda_elec/{rel}")
zb = zipfile.ZipFile(buf)
names = zb.namelist()
check("zip 结构: 顶层目录 + metadata", "astrbot_plugin_suda_elec/metadata.yaml" in names)
check("zip 完整性", zb.testzip() is None)

import shutil  # noqa: E402

shutil.rmtree(ROOT / "tmp_market_test", ignore_errors=True)

print("=" * 60)
print(f"MARKET READINESS: {len(PASS)} PASS, {len(FAIL)} FAIL {FAIL if FAIL else '→ READY ✅'}")
sys.exit(1 if FAIL else 0)
