#!/usr/bin/env python3
"""sqltk-core 回归测试：协议信封与 SQL 基础设施

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
for _sib in ("sqltk-store", "sqltk-know"):
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
# 事务可回滚性判定（AST 而非字符串前缀）
# ==================================================================
from dbcore.guard import is_ddl, unsafe_in_transaction  # noqa: E402

# 回归：`/* 注记 */ ALTER TABLE` 曾让 startswith 判断失效，导致 MySQL 上
# DDL 隐式提交后仍报告"已 ROLLBACK、数据未变更"。
check("注释开头的 DDL 被识别为不可回滚",
      unsafe_in_transaction("/* 运维注记 */ ALTER TABLE t ADD COLUMN c INT", "mysql") is not None)
check("注释开头的 DDL 被 is_ddl 判定为真",
      is_ddl("/* x */ CREATE INDEX i ON t(a)", "mysql"))
check("行内注释的 DDL 被识别",
      unsafe_in_transaction("# note\nALTER TABLE t ADD c INT", "mysql") is not None)
check("纯 DML 允许进入事务",
      unsafe_in_transaction("UPDATE t SET a=1 WHERE id=1", "mysql") is None)
check("INSERT 允许进入事务",
      unsafe_in_transaction("INSERT INTO t(a) VALUES(1)", "mysql") is None)
check("DELETE 允许进入事务",
      unsafe_in_transaction("DELETE FROM t WHERE id=1", "mysql") is None)
for _s in ("TRUNCATE TABLE t", "OPTIMIZE TABLE t", "GRANT SELECT ON d.* TO u",
           "CALL p()", "SET autocommit=1"):
    check(f"非事务安全语句被拒：{_s[:20]}", unsafe_in_transaction(_s, "mysql") is not None)
check("多条语句拒绝整体回滚承诺",
      unsafe_in_transaction("UPDATE t SET a=1; DELETE FROM t", "mysql") is not None)
check("普通 SELECT 不是 DDL", not is_ddl("SELECT 1 FROM t", "mysql"))

# ==================================================================
# 协议层脱敏：next_actions 会进 LLM 上下文，连接串必须在这里就抹掉
# ==================================================================
_r = Result(ok=True, mcp="m", tool="t")
_r.next("m", "dry_run_change", url="mysql://root:S3cretPwd@10.0.0.1:3306/prod", dialect="mysql")
_next_url = _r.to_dict()["next_actions"][0]["args"]["url"]
check("next_actions 的 url 已脱敏", "S3cretPwd" not in _next_url, _next_url)
check("脱敏后仍保留主机与库名", "10.0.0.1" in _next_url and "prod" in _next_url, _next_url)

# 回归：密码含未转义 @ 时，只掩第一段会漏掉后半截
_r2 = Result(ok=True, mcp="m", tool="t")
_r2.next("m", "x", url="mysql://root:p@ss@10.0.0.1:3306/prod")
_next_url2 = _r2.to_dict()["next_actions"][0]["args"]["url"]
check("密码含 @ 时也不残留", "ss@" not in _next_url2 and "p@" not in _next_url2, _next_url2)

_r3 = Result(ok=True, mcp="m", tool="t")
_r3.next("m", "x", instance_id="i:mysql:abc", dialect="mysql")
check("非敏感参数不被改写", _r3.to_dict()["next_actions"][0]["args"]["instance_id"] == "i:mysql:abc")
check("took_ms 仍在信封里", "took_ms" in Result(ok=True, mcp="m", tool="t").to_dict())

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
