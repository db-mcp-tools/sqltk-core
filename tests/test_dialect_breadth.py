#!/usr/bin/env python3
"""sqltk-core 回归测试：多方言归一广度（P5a）。

验证：别名归一、review-only / connectable 能力边界、新方言可被审查引擎消费、
review-only 方言连接时给出清晰报错。直接运行：

    python tests/test_dialect_breadth.py
"""

from __future__ import annotations

import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")

from dbcore.dialect import (  # noqa: E402
    CONNECTABLE,
    connector_status,
    normalize_dialect,
)
from dbcore.protocol import ToolError  # noqa: E402

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
section("别名归一：新增方言与既有兼容系")
_cases = [
    ("sqlserver", "tsql"), ("mssql", "tsql"), ("tsql", "tsql"),
    ("bq", "bigquery"), ("bigquery", "bigquery"),
    ("snowflake", "snowflake"), ("hive", "hive"),
    ("spark", "spark"), ("spark2", "spark2"), ("databricks", "databricks"),
    ("presto", "presto"), ("trino", "trino"), ("redshift", "redshift"),
    ("duckdb", "duckdb"), ("teradata", "teradata"), ("athena", "athena"),
    # 既有兼容系不回归
    ("doris", "mysql"), ("starrocks", "mysql"), ("ob", "mysql"), ("tidb", "mysql"),
    ("dm", "oracle"), ("gaussdb", "postgres"), ("ck", "clickhouse"),
]
for alias, want in _cases:
    try:
        got = normalize_dialect(alias)
        check(f"{alias} -> {want}", got == want, f"got {got}")
    except ValueError as e:
        check(f"{alias} -> {want}", False, str(e))

# 未知方言仍然报错
try:
    normalize_dialect("not-a-dialect")
    check("未知方言报 ValueError", False, "未抛错")
except ValueError:
    check("未知方言报 ValueError", True)

# ==================================================================
section("能力边界：connectable vs review-only")
check("CONNECTABLE 恰为 mysql/postgres/sqlite",
      set(CONNECTABLE) == {"mysql", "postgres", "sqlite"}, str(CONNECTABLE))
for d in CONNECTABLE:
    st = connector_status(d)
    check(f"{d} connectable", st["connectable"] and st["mode"] == "connectable", str(st))
for d in ("snowflake", "bigquery", "trino", "duckdb", "tsql", "clickhouse", "oracle"):
    st = connector_status(d)
    check(f"{d} review-only", not st["connectable"] and st["mode"] == "review-only", str(st))

# ==================================================================
section("新方言可被审查引擎消费（review 真正跑起来）")
sys.path[:0] = [
    str(_HERE.parent.parent / "mcp-sql-review" / "src"),
    str(_HERE.parent.parent / "sqltk-store" / "src"),
    str(_HERE.parent.parent / "sqltk-know" / "src"),
]
from sql_review.rulebook.review import review_sql  # noqa: E402

for d in ("snowflake", "bigquery", "trino", "duckdb", "tsql", "hive", "spark", "redshift"):
    r = review_sql("SELECT * FROM t WHERE b = 1", dialect=d)
    check(f"{d} 审查返回结构化结果",
          isinstance(r.verdict, str) and isinstance(r.findings, list),
          f"verdict={r.verdict}")
# 静态规则在新方言上照常触发（SELECT * → SEL001）
r = review_sql("SELECT * FROM t", dialect="snowflake")
check("snowflake 上 SEL001 触发", any(f.rule_id == "SEL001" for f in r.findings),
      str([f.rule_id for f in r.findings]))

# ==================================================================
section("review-only 方言连接时给出清晰报错")
from dbcore.connectors import run_readonly  # noqa: E402

try:
    run_readonly("snowflake://u:p@host:443/db", "SELECT 1")
    check("snowflake 连接被拒", False, "未抛错")
except ValueError as e:
    check("snowflake 连接被拒", "review-only" in str(e) and "mysql" in str(e), str(e)[:100])
except ToolError as e:
    check("snowflake 连接被拒", "review-only" in str(e), str(e)[:100])

# ==================================================================
print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
if FAILED:
    for name, detail in FAILED:
        print("  -", name, "::", detail)
    raise SystemExit(1)
