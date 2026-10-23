#!/usr/bin/env python3
"""sqltk-core 回归测试：执行计划结构化 / 风险评级 / 缺失索引建议（P3）。

不依赖 pytest / 真实 MySQL·Postgres（用 fixture 行列直接喂解析器，
sqlite 部分用真实文件库端到端）。直接运行：

    python tests/test_plan.py
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
# 大表阈值降到 1e4（默认 1e5），让 5e4 行的测试表落入"大表"区间；同时覆盖阈值可配置行为
os.environ.setdefault("DBT_LARGE_TABLE_ROWS", "10000")

import dbcore.plan as PLAN  # noqa: E402
from dbcore.connectors import explain  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def section(title: str) -> None:
    print("\n# " + title)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))
        print("  ✗ FAIL:", name, "::", detail)


# ==================================================================
section("mysql 表格式 EXPLAIN 解析 + 风险 + 缺失索引建议")
_COLS = ["id", "select_type", "table", "type", "possible_keys", "key", "rows", "Extra"]
_ROWS = [
    [1, "SIMPLE", "orders", "ALL", None, None, 1000000, "Using where"],
    [1, "SIMPLE", "users", "ref", "idx_uid", "idx_uid", 1, ""],
]
_m = PLAN.plan_from_explain(
    _ROWS, _COLS, "mysql",
    sql="SELECT * FROM orders o JOIN users u ON o.uid=u.id WHERE o.amount>10",
    table_rows=1000000,
)
check("mysql 全扫 flag 命中", _m["flags"].get("full_scan") is True, str(_m["flags"]))
check("mysql 大表(1e6)全扫风险=high", _m["risk_level"] == "high", _m["risk_level"])
check("mysql 别名解析后给出 orders.amount 建议",
      any("orders" in s and "amount" in s for s in _m["suggestions"]), str(_m["suggestions"]))

# 小表：同一条全扫只评 ok
_m_small = PLAN.plan_from_explain(_ROWS, _COLS, "mysql",
                                  sql="SELECT * FROM orders WHERE amount>10", table_rows=500)
check("mysql 小表(500)全扫风险=ok", _m_small["risk_level"] == "ok", _m_small["risk_level"])

# 未知行数：全扫只评 warn（无法判断数据量）
_m_unknown = PLAN.plan_from_explain(_ROWS, _COLS, "mysql",
                                    sql="SELECT * FROM orders WHERE amount>10")
check("mysql 未知行数全扫风险=warn", _m_unknown["risk_level"] == "warn", _m_unknown["risk_level"])

# 走索引的查询不产生缺失索引建议
_m_idx = PLAN.plan_from_explain(
    [[1, "SIMPLE", "orders", "ref", "idx_amount", "idx_amount", 3, ""]],
    _COLS, "mysql", sql="SELECT * FROM orders WHERE amount=10", table_rows=1000000)
check("mysql 走索引不报全扫", _m_idx["flags"].get("full_scan") is not True, str(_m_idx["flags"]))
check("mysql 走索引无缺失索引建议", _m_idx["suggestions"] == [], str(_m_idx["suggestions"]))

# ==================================================================
section("postgres 文本 EXPLAIN 解析 + 风险 + 缺失索引建议")
_PG = ["Seq Scan on orders  (cost=0.00..18500.00 rows=980000 width=40)",
       "  Filter: (amount > 10)"]
_pg = PLAN.plan_from_explain([[l] for l in _PG], ["QUERY PLAN"], "postgres",
                              sql="SELECT * FROM orders WHERE amount>10", table_rows=1000000)
check("pg 全扫 flag 命中", _pg["flags"].get("full_scan") is True, str(_pg["flags"]))
check("pg 大表(1e6)全扫风险=high", _pg["risk_level"] == "high", _pg["risk_level"])
check("pg 给出 orders.amount 建议",
      any("orders" in s and "amount" in s for s in _pg["suggestions"]), str(_pg["suggestions"]))

# filesort / 临时表 风险抬升
_PG_SORT = ["Sort  (cost=... rows=100)", "  Sort Key: amount",
            "  Seq Scan on orders  (cost=... rows=100)"]
_pgs = PLAN.plan_from_explain([[l] for l in _PG_SORT], ["QUERY PLAN"], "postgres",
                               sql="SELECT * FROM orders ORDER BY amount", table_rows=1000000)
check("pg 含 filesort 风险>=warn", _pgs["risk_level"] in ("warn", "high"), _pgs["risk_level"])

# ==================================================================
section("sqlite 真实执行计划（端到端）")
_tmp = tempfile.mkdtemp()
_db = os.path.join(_tmp, "t.db")
_con = sqlite3.connect(_db)
_con.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, name TEXT, val INT)")
_con.executemany("INSERT INTO big (name,val) VALUES (?,?)",
                 [(f"n{i}", i) for i in range(50000)])
_con.execute("CREATE INDEX idx_name ON big(name)")
_con.execute("ANALYZE")
_con.commit()
_con.close()

_url = "sqlite://" + _db
_r_scan = explain(_url, "SELECT * FROM big WHERE val = 123", table_rows=50000)
check("sqlite 全扫被识别", _r_scan["plan"]["flags"].get("full_scan") is True, str(_r_scan["plan"]["flags"]))
check("sqlite 大表(5e4)全扫风险=high", _r_scan["risk_level"] == "high", _r_scan["risk_level"])
check("sqlite 给出 big.val 缺失索引建议",
      any("big" in s and "val" in s for s in _r_scan["plan_suggestions"]), str(_r_scan["plan_suggestions"]))
check("sqlite 透出 estimated_rows", isinstance(_r_scan.get("estimated_rows"), int))

_r_seek = explain(_url, "SELECT * FROM big WHERE name = 'n5'")
check("sqlite 索引查找不算全扫", _r_seek["plan"]["flags"].get("full_scan") is not True,
      str(_r_seek["plan"]["flags"]))
check("sqlite 索引查找风险=ok", _r_seek["risk_level"] == "ok", _r_seek["risk_level"])
check("sqlite 索引查询无多余建议", _r_seek["plan_suggestions"] == [], str(_r_seek["plan_suggestions"]))

# 缺表行数时只评 warn
_r_scan_unknown = explain(_url, "SELECT * FROM big WHERE val = 123")
check("sqlite 未知行数全扫风险=warn", _r_scan_unknown["risk_level"] == "warn",
      _r_scan_unknown["risk_level"])

# ==================================================================
print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
if FAILED:
    for name, detail in FAILED:
        print("  -", name, "::", detail)
    raise SystemExit(1)
