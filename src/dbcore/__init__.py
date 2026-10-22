"""dbcore —— SQL 工具链运行时内核（协议 / 方言 / 指纹 / 只读连库 / AST / 计划）。

只放**跨 MCP 通用**的能力，不放任何 MCP 的业务判断：
  协议信封      protocol     （dbt.result/v1）
  方言与解析    dialect
  标识与指纹    ids
  只读防护      guard
  连库执行      connectors
  AST 结构化    extract
  执行计划归一  plan
  结果渲染      report
  日志          logging_setup

约束：
  1. 除 sqlglot 外不引入重依赖；数据库驱动（pymysql / psycopg）为可选依赖，
     缺失时给清晰报错而不是崩溃；
  2. 任何对目标库的连接必须是只读会话 + 语句超时 + 单语句；
  3. 本包**不知道**任何具体 MCP 的业务，业务判断一律留在各自 MCP 仓库。
"""

from dbcore.dialect import normalize_dialect, parse_statements, split_sql
from dbcore.guard import GuardError, assert_readonly, ensure_single_statement
from dbcore.ids import (
    instance_id_from_dsn,
    normalize_sql_for_fingerprint,
    redact_dsn,
    sql_fingerprint,
)
from dbcore.protocol import (
    PROTOCOL_VERSION,
    EVIDENCE_KINDS,
    Result,
    ToolError,
    fail,
    new_correlation_id,
    ok,
    severity_to_verdict,
    worst_verdict,
)

__all__ = [
    "PROTOCOL_VERSION", "Result", "ToolError", "ok", "fail", "new_correlation_id",
    "severity_to_verdict", "worst_verdict", "EVIDENCE_KINDS",
    "normalize_dialect", "parse_statements", "split_sql",
    "GuardError", "assert_readonly", "ensure_single_statement",
    "instance_id_from_dsn", "normalize_sql_for_fingerprint", "redact_dsn", "sql_fingerprint",
]

__version__ = "0.2.0"
