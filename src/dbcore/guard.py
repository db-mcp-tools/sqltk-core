"""只读护栏。

任何"要打到目标库上执行"的 SQL 必须先过这里。
原则：不认意图，只认语法；不放行的(nolist)就是不放行。
"""

from __future__ import annotations

from sqlglot import exp

from dbcore.dialect import normalize_dialect, parse_statements

READONLY_KINDS = {"select", "other"}  # other = SHOW/EXPLAIN/DESC 等
READONLY_PREFIX = ("select", "show", "explain", "desc", "describe", "with", "analyze")

# 只有这几类语句放进事务里回滚是可靠的。
# 用白名单而不是黑名单：漏掉一种黑名单的代价是"假回滚"（dry-run 声称没改库但实际改了），
# 比多拒一种语句严重得多。DDL 在 MySQL/Oracle 隐式提交；OPTIMIZE/GRANT/ANALYZE/CALL/
# SET autocommit 等管理类语句同样不是事务安全的。
TRANSACTIONAL_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge)


class GuardError(Exception):
    """语句不允许在只读会话中执行。"""


def ensure_single_statement(sql: str, dialect: str | None = None) -> None:
    stmts = parse_statements(sql, dialect)
    if len(stmts) == 0:
        raise GuardError("empty statement")
    if len(stmts) > 1:
        raise GuardError(f"只允许单条语句，检测到 {len(stmts)} 条")


def assert_readonly(sql: str, dialect: str | None = None) -> None:
    """确认语句只读。写操作、DDL、多行注入都会被拦下。"""
    d = normalize_dialect(dialect)
    stmts = parse_statements(sql, d)
    if not stmts:
        raise GuardError("empty statement")
    if len(stmts) > 1:
        raise GuardError(f"只允许单条语句，检测到 {len(stmts)} 条（疑似注入堆叠）")

    stmt = stmts[0]
    text = stmt.sql.strip().lower().rstrip(";").strip()

    if stmt.parsable:
        if stmt.kind not in READONLY_KINDS:
            raise GuardError(
                f"只读护栏拦截：{stmt.kind.upper()} 语句不允许在巡检会话执行 -> {stmt.sql[:120]}"
            )
        for node in stmt.ast.walk():
            if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Alter, exp.Drop, exp.Command)):
                cmd = node.key.upper() if isinstance(node, exp.Command) else type(node).__name__.upper()
                # Command 里 explain/show 是允许的
                if isinstance(node, exp.Command) and text.startswith(READONLY_PREFIX):
                    continue
                raise GuardError(f"只读护栏拦截：语句内检测到 {cmd}")
        return

    # 解析失败时回退到前缀判断，宁可漏也不放行明显写操作
    if not text.startswith(READONLY_PREFIX):
        raise GuardError(f"该方言无法解析且不以只读关键字开头，拒绝执行 -> {text[:80]}")


def unsafe_in_transaction(sql: str, dialect: str | None = None) -> str | None:
    """判断语句能否放进事务里安全回滚：安全返回 None，不安全返回原因。

    为什么需要它（而不是让调用方自己看字符串前缀）：
      dry-run 的实现基础是"真执行一遍再回滚"。一旦语句隐式提交，dry-run
      就变成了真实的破坏性操作，却还返回"数据未发生任何变更"。
      `/* 注记 */ ALTER TABLE ...` 这类写法能让任何 startswith 判断失效，
      而 AST 判定不会。

    判定按 AST 节点而非 kind 字符串，避免 sqlglot 各方言 fallback 到 Command 的差异。
    """
    d = normalize_dialect(dialect)
    stmts = parse_statements(sql, d)
    if not stmts:
        return "空语句"
    if len(stmts) > 1:
        return f"一次传入 {len(stmts)} 条语句，无法保证整体可回滚（应逐条执行）"
    stmt = stmts[0]
    if not stmt.parsable:
        return f"无法解析（疑似 {stmt.kind}），不能保证可回滚"
    if isinstance(stmt.ast, TRANSACTIONAL_NODES):
        return None
    return f"{stmt.kind.upper()} 语句在事务内不可回滚（DDL/管理类语句会隐式提交）"


def is_ddl(sql: str, dialect: str | None = None) -> bool:
    """语句是否包含 DDL。用于"DDL 不支持事务内 dry-run，请走影子表"这类分流。"""
    d = normalize_dialect(dialect)
    for stmt in parse_statements(sql, d):
        if stmt.parsable and isinstance(stmt.ast, (exp.Create, exp.Alter, exp.Drop, exp.TruncateTable)):
            return True
    return False
