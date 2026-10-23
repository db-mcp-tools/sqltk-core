"""回滚语句生成：把单条 DML 变成可移植的反向语句。

评测对标：Yearning / SQLE / Bytebase 把"自动生成反向下发语句"列为变更审核必考点。
本模块在事务内捕获前镜像、生成精确反向 DML，最后 ROLLBACK 保证**不修改任何数据**。

  - DELETE  -> 逐行 INSERT（前镜像全列回写）
  - UPDATE  -> 逐行 UPDATE（被改列还原为前镜像旧值，全列等值定位）
  - INSERT  -> DELETE WHERE（基于字面量 VALUES 静态构造；含子查询/函数时暂不支持）

设计边界：
  - 永远不连库执行 DDL；DDL 回滚需走影子表 / 备份方案（guard.unsafe_in_transaction 已拒绝）。
  - INSERT 反向为静态生成（不连库执行原语句）；DELETE/UPDATE 必须连库抓前镜像以保证精确。
  - 生成的语句是"如果已经执行了这条写语句，该如何还原"——plan_rollback 自身绝不留下痕迹。
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from sqlglot import exp

from dbcore.connectors import ConnectionSpec, _connect
from dbcore.dialect import parse_statements, table_of
from dbcore.guard import ensure_single_statement, unsafe_in_transaction
from dbcore.protocol import ToolError


@dataclass
class RollbackPlan:
    method: str               # "image-capture" | "static"
    op: str                   # INSERT | UPDATE | DELETE
    table: str
    captured_rows: int
    statements: list[str]
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "op": self.op,
            "table": self.table,
            "captured_rows": self.captured_rows,
            "statements": self.statements,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 字面量渲染
# ---------------------------------------------------------------------------

def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(v) -> str:
    """Python 原生值 -> SQL 字面量。"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return "'" + str(v).replace("'", "''") + "'"
    return "'" + str(v).replace("'", "''") + "'"


def _where_eq(cols, vals) -> str:
    parts = []
    for c, v in zip(cols, vals):
        if v is None:
            parts.append(f"{_quote_ident(c)} IS NULL")
        else:
            parts.append(f"{_quote_ident(c)} = {_lit(v)}")
    return " AND ".join(parts) if parts else "1=1"


def _where_sql(where_node, d: str) -> str:
    if where_node is None:
        return ""
    s = where_node.sql(dialect=d)
    return re.sub(r"^\s*WHERE\s+", "", s, flags=re.I)


def _table_name(ast, d: str) -> str:
    node = ast.this if isinstance(ast.this, exp.Table) else ast.find(exp.Table)
    if node is None:
        node = ast.this if isinstance(ast.this, exp.Schema) else None
    if node is not None:
        return node.sql(dialect=d)
    return table_of(ast) or ""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def plan_rollback(url: str, sql: str, dialect: str | None = None, *, max_rows: int = 5000) -> RollbackPlan:
    """生成单条 DML 的回滚语句。

    在事务内捕获前镜像并执行原语句以确认可解析，最后 ROLLBACK，绝不修改目标库。

    Args:
        url: 连接串（sqlite:///path、mysql://...、postgres://...）。INSERT 静态分支不连库执行。
        sql: 单条 INSERT / UPDATE / DELETE。
        dialect: 可选方言覆盖。
        max_rows: 前镜像行数上限，超过则拒绝生成以防巨型回滚脚本。

    Returns:
        RollbackPlan（含 statements 列表，按序执行即可还原）。
    """
    spec = ConnectionSpec(url)
    d = dialect or spec.driver
    ensure_single_statement(sql, d)
    why = unsafe_in_transaction(sql, d)
    if why:
        raise ToolError(
            "E_UNSUPPORTED",
            f"无法生成回滚：{why}（DDL / 管理类语句请走影子表或备份方案）",
        )
    stmts = parse_statements(sql, d)
    if not stmts or not stmts[0].parsable:
        raise ToolError("E_PARSE", "无法解析该语句，不能生成回滚")
    ast = stmts[0].ast
    if isinstance(ast, exp.Insert):
        return _plan_insert(ast, d)
    if isinstance(ast, (exp.Update, exp.Delete)):
        return _plan_with_image(spec, ast, d, max_rows, sql)
    raise ToolError(
        "E_UNSUPPORTED",
        f"回滚生成仅支持单条 INSERT/UPDATE/DELETE，收到 {type(ast).__name__}",
    )


# ---------------------------------------------------------------------------
# INSERT -> DELETE WHERE（静态，不连库执行原语句）
# ---------------------------------------------------------------------------

def _render_literal(node, d: str) -> str:
    if isinstance(node, exp.Null):
        return "NULL"
    if isinstance(node, exp.Boolean):
        return "1" if node.this else "0"
    if isinstance(node, exp.Literal):
        if node.is_string:
            return _lit(node.this)
        return node.this  # 数字字面量
    if isinstance(node, exp.Neg):
        return "-" + _render_literal(node.this, d)
    if isinstance(node, exp.Paren):
        return "(" + _render_literal(node.this, d) + ")"
    raise ToolError("E_UNSUPPORTED", "非字面量值")


def _plan_insert(ast, d: str) -> RollbackPlan:
    this = ast.this
    if isinstance(this, exp.Schema):
        cols = [c.name for c in this.expressions]
        table = this.this.sql(dialect=d)
    else:
        cols = []
        table = this.sql(dialect=d) if isinstance(this, exp.Table) else _table_name(ast, d)
    if not table:
        raise ToolError("E_PARSE", "无法解析 INSERT 的目标表")
    if not cols:
        raise ToolError(
            "E_UNSUPPORTED",
            "INSERT 未显式列出列名，无法静态生成 DELETE WHERE（需连库取表结构）",
        )
    expr = ast.args.get("expression")
    if not isinstance(expr, exp.Values):
        raise ToolError(
            "E_UNSUPPORTED",
            "INSERT...SELECT / 含子查询的插入暂不支持静态回滚（需连库取行）",
        )
    statements: list[str] = []
    for tup in expr.expressions:
        vals = tup.expressions if isinstance(tup, exp.Tuple) else [tup]
        if len(cols) != len(vals):
            raise ToolError("E_PARSE", "INSERT 列数与值数不一致，无法生成回滚")
        parts = []
        for col, val in zip(cols, vals):
            try:
                rendered = _render_literal(val, d)
            except ToolError:
                raise ToolError(
                    "E_UNSUPPORTED",
                    "INSERT 含非字面量值（函数 / 子查询等），无法静态生成精确回滚",
                )
            parts.append(f"{_quote_ident(col)} = {rendered}")
        where = " AND ".join(parts)
        statements.append(f"DELETE FROM {table} WHERE {where}")
    return RollbackPlan(
        method="static",
        op="INSERT",
        table=table,
        captured_rows=len(statements),
        statements=statements,
        note="基于字面量 VALUES 静态生成 DELETE 反向；未连库执行原语句",
    )


# ---------------------------------------------------------------------------
# DELETE / UPDATE -> 连库捕获前镜像
# ---------------------------------------------------------------------------

def _insert_sql(table: str, cols: list[str], row: tuple) -> str:
    col_list = ", ".join(_quote_ident(c) for c in cols)
    val_list = ", ".join(_lit(v) for v in row)
    return f"INSERT INTO {table} ({col_list}) VALUES ({val_list})"


def _plan_with_image(spec, ast, d: str, max_rows: int, original_sql: str) -> RollbackPlan:
    conn = _connect(spec)
    try:
        cur = conn.cursor()
        table = _table_name(ast, d)
        if not table:
            raise ToolError("E_PARSE", "无法解析目标表")
        wsql = _where_sql(ast.args.get("where"), d)
        # 1) 执行前就捕获前镜像（此时写事务尚未开始，读到的是旧值）
        sel = f"SELECT * FROM {table}" + (f" WHERE {wsql}" if wsql else "")
        cur.execute(sel)
        col_names = [c[0] for c in cur.description]
        rows = cur.fetchall()
        if len(rows) > max_rows:
            raise ToolError(
                "E_TOO_MANY_ROWS",
                f"受影响行数 {len(rows)} 超过上限 {max_rows}，拒绝生成回滚以防巨型脚本",
            )
        # 2) 在事务内真正执行原语句，确认语法/语义可解析（之后整体回滚）
        cur.execute(original_sql)

        if isinstance(ast, exp.Delete):
            statements = [_insert_sql(table, col_names, row) for row in rows]
            op = "DELETE"
        else:  # Update
            set_cols = [eq.this.name for eq in ast.expressions if isinstance(eq, exp.EQ)]
            statements = []
            for row in rows:
                old = dict(zip(col_names, row))
                set_parts = [f"{_quote_ident(c)} = {_lit(old[c])}" for c in set_cols]
                loc = _where_eq(col_names, row)
                statements.append(
                    f"UPDATE {table} SET {', '.join(set_parts)} WHERE {loc}"
                )
            op = "UPDATE"

        # 3) 永远回滚：目标库不得留下任何痕迹
        conn.rollback()
        note = ""
        if len(rows) == 0:
            note = "前镜像为空：原语句未命中任何行，无回滚数据可生成"
        return RollbackPlan(
            method="image-capture",
            op=op,
            table=table,
            captured_rows=len(rows),
            statements=statements,
            note=note,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass
