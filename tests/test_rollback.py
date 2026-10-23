#!/usr/bin/env python3
"""sqltk-core 回归测试：回滚语句生成（真实 SQLite 端到端）

不依赖 pytest / 真实 MySQL / Postgres。直接运行：

    python tests/test_rollback.py

验证 plan_rollback 在事务内捕获前镜像并生成精确反向 DML，且**自身绝不修改目标库**。
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))

os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")

from dbcore.connectors import run_in_transaction, run_readonly  # noqa: E402
from dbcore.protocol import ToolError  # noqa: E402
from dbcore.rollback import plan_rollback  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  << {detail[:300] if detail else ''}")


def section(title: str) -> None:
    print(f"\n── {title}")


# ==================================================================
section("0. 准备真实 SQLite 文件库")
TMP = tempfile.mkdtemp(prefix="dbt-rollback-")
DB = os.path.join(TMP, "t.db").replace("\\", "/")
URL = "sqlite:///" + DB
init = sqlite3.connect(DB)
init.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT, val INTEGER, dt TEXT)")
init.executemany(
    "INSERT INTO t(id,name,val,dt) VALUES (?,?,?,?)",
    [(1, "a", 10, "2024-01-01"), (2, "b", 20, "2024-01-02"), (3, "c", 30, "2024-01-03")],
)
init.commit()
init.close()
check("库与连接串就绪", URL.startswith("sqlite:///") and os.path.exists(DB), URL)

# ==================================================================
section("1. UPDATE -> 反向 UPDATE（前镜像旧值）")
sql = "UPDATE t SET val = 999 WHERE id = 1"
plan = plan_rollback(URL, sql)
check("方法为 image-capture", plan.method == "image-capture", plan.method)
check("op 为 UPDATE", plan.op == "UPDATE", plan.op)
check("捕获 1 行前镜像", plan.captured_rows == 1, str(plan.captured_rows))
check("反向含旧值 val=10", '"val" = 10' in plan.statements[0], plan.statements[0])
check("反向不含新值 999", "999" not in plan.statements[0], plan.statements[0])
# 库未被改动
val1 = run_readonly(URL, "SELECT val FROM t WHERE id=1")["rows"][0][0]
check("plan_rollback 未改动库(val=10)", val1 == 10, str(val1))

# ==================================================================
section("2. DELETE -> 反向 INSERT（全列前镜像）")
sql = "DELETE FROM t WHERE id = 2"
plan = plan_rollback(URL, sql)
check("op 为 DELETE", plan.op == "DELETE", plan.op)
check("捕获 1 行前镜像", plan.captured_rows == 1, str(plan.captured_rows))
check("反向是 INSERT", plan.statements[0].startswith("INSERT INTO t"), plan.statements[0])
check("反向含被删行数据 name=b", "'b'" in plan.statements[0] and "20" in plan.statements[0], plan.statements[0])
cnt2 = run_readonly(URL, "SELECT COUNT(*) FROM t WHERE id=2")["rows"][0][0]
check("plan_rollback 未改动库(id=2 仍在)", cnt2 == 1, str(cnt2))

# ==================================================================
section("3. INSERT -> 静态 DELETE WHERE")
sql = "INSERT INTO t (id, name, val) VALUES (100, 'x', 5)"
plan = plan_rollback(URL, sql)
check("方法为 static", plan.method == "static", plan.method)
check("op 为 INSERT", plan.op == "INSERT", plan.op)
expected = "DELETE FROM t WHERE \"id\" = 100 AND \"name\" = 'x' AND \"val\" = 5"
check("静态反向 DELETE 正确", plan.statements[0] == expected, plan.statements[0])
cnt100 = run_readonly(URL, "SELECT COUNT(*) FROM t WHERE id=100")["rows"][0][0]
check("静态分支未连库执行(库未变)", cnt100 == 0, str(cnt100))

# ==================================================================
section("4. DDL 被拒绝（非事务安全，应走影子表）")
try:
    plan_rollback(URL, "ALTER TABLE t ADD c INT")
    check("DDL 被拒绝", False, "未抛错")
except ToolError:
    check("DDL 被拒绝", True)

# ==================================================================
section("5. 生成的回滚语句可执行（语法/语义烟雾）")
sql = "UPDATE t SET val = 999 WHERE id = 1"
plan = plan_rollback(URL, sql)
res = run_in_transaction(URL, plan.statements, commit=False)
check("反向 UPDATE 在事务内可执行", res["rolled_back"], res.get("verdict", ""))

# ==================================================================
section("6. 端到端可还原（先真删，再跑反向 INSERT 还原）")
sql = "DELETE FROM t WHERE id = 3"
plan = plan_rollback(URL, sql)  # 抓前镜像，事务内回滚，库不变
run_in_transaction(URL, [sql], commit=True)  # 真正删除
after_del = run_readonly(URL, "SELECT COUNT(*) FROM t WHERE id=3")["rows"][0][0]
check("DELETE 已真实生效", after_del == 0, str(after_del))
run_in_transaction(URL, plan.statements, commit=True)  # 反向 INSERT 还原
after_restore = run_readonly(URL, "SELECT COUNT(*) FROM t WHERE id=3")["rows"][0][0]
check("反向 INSERT 已还原该行", after_restore == 1, str(after_restore))
# 还原后整行与原值一致
row = run_readonly(URL, "SELECT id,name,val,dt FROM t WHERE id=3")["rows"][0]
check("还原后字段值正确", row == [3, "c", 30, "2024-01-03"], str(row))

# ==================================================================
print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
for f in FAILED:
    print("  FAIL:", f)
sys.exit(1 if FAILED else 0)
