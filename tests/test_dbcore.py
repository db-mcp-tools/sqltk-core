#!/usr/bin/env python3
"""dbt-core 回归测试：协议信封与 SQL 基础设施

不依赖 pytest / 真实数据库。直接运行：

    python tests/test_dbcore.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
# 开发期：兄弟仓库用源码目录；正式使用请 pip install（见 README）
for _sib in ("dbt-store", "dbt-know", "dbt-mcp-kit"):
    _p = _HERE.parent.parent / _sib / "src"
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")
_TMPDIR = tempfile.mkdtemp(prefix="dbt-dbcore-")
os.environ.setdefault("DBT_STORE", os.path.join(_TMPDIR, "store.db"))

from dbcore.protocol import Result, ToolError  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  << {detail[:300] if detail else ''}")


def section(title: str) -> None:
    print(f"\n── {title}")


section("1. 协议层 dbt.result/v1")
# ==================================================================
from dbcore.protocol import (  # noqa: E402
    Result, ToolError, ERROR_CODES, new_correlation_id,
    worst_verdict, severity_to_verdict, SCHEMA,
)

r = Result(ok=True, mcp="test", tool="t", data={"a": 1}, verdict="warn", confidence="high")
r.add_evidence("ast", "src", "fact", "sup").next("other", "tool", x=1, reason="why")
d = r.to_dict()
check("信封包含必需字段",
      all(k in d for k in ("ok", "schema", "mcp", "tool", "verdict", "confidence",
                           "tier", "evidence", "next_actions", "warnings", "error", "data")),
      str(list(d)))
check("schema 常量正确", d["schema"] == SCHEMA, d["schema"])
check("evidence 已记录", len(d["evidence"]) == 1)
check("next_actions 已记录", d["next_actions"][0]["mcp"] == "other")
try:
    Result(ok=True, mcp="t", tool="t", verdict="bad")
    check("非法 verdict 被拒绝", False)
except ToolError:
    check("非法 verdict 被拒绝", True)
try:
    Result(ok=True, mcp="t", tool="t", tier="tier9")
    check("非法 tier 被拒绝", False)
except ToolError:
    check("非法 tier 被拒绝", True)
check("错误码表完整", len(ERROR_CODES) >= 10, str(len(ERROR_CODES)))
check("严重度映射", severity_to_verdict("critical") == "fail")
check("verdict 取最坏", worst_verdict(["pass", "warn", "fail"]) == "fail")
check("correlation_id 递增", new_correlation_id() != new_correlation_id())

# ==================================================================


# ==================================================================
print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\n失败明细：")
    for _f in FAILED:
        print("  ✗", _f)
    raise SystemExit(1)
print("全部通过")
