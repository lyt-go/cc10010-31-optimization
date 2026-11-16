"""输出列校验数据快照：SQL模板与元数据存储共用的唯一判定口径。

设计要点（两侧读取同一份快照，不各自重算）：

* 列比对以 **列名（字段别名）** 为准而不是以 SELECT 中的位置为准。模板导出再载入后
  SELECT 顺序可能变化，但“哪一列映射到目标表哪个字段”不应因此改变；真正改变映射的是
  **别名**。因此校验结果里同时保留 ``输出列顺序``（当前观测顺序，仅作快照记录）与
  ``目标字段顺序``（与目标表物理字段顺序逐位对应的输出列名），下游按后者读取。
* 模板文件按 **文件指纹（``模板指纹``）** 判重：同一文件重复载入只回用一个校验版本；
  校验结论另有 ``校验指纹``（文件 + 当时的判定标准 + 目标表字段），目标表字段修好后
  同一文件可以重新判定，不会被旧的失败结论永久钉死。载入失败的文件不会覆盖上一可用快照。
* 版本激活使用仓储级 ``RLock``：并发载入同一文件时，后到者能命中先到者生成的指纹，
  只允许一个版本生效。历史快照通过新增可选字段演进，旧行缺字段时由兼容层补默认值。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from app.store import store

SNAPSHOT_MODULE = "columnsnapshot"
SCHEMA_VERSION = 1

# 默认阈值：字段数量必须与目标表字段数量完全相等；顺序必须逐位一致；类型必须逐列匹配。
DEFAULT_POLICY: dict[str, Any] = {
    "字段数量下限": 1,
    "字段数量上限": 200,
    "字段数量偏差容忍": 0,
    "顺序不一致容忍": 0,
    "类型不一致容忍": 0,
    "类型兼容": True,
    # 模板没声明类型时无法判定，默认只提示；置 True 则未声明类型也算失败。
    "未知类型视为失败": False,
}

# 类型标准：规范类型 -> 可被目标列接受的输出类型集合（含自身）。
# "未知" 不在集合里：任一侧类型未知时不做兼容判定，由「未知类型视为失败」阈值统一处理。
TYPE_STANDARDS: dict[str, set[str]] = {
    "整数": {"整数", "小数", "文本"},
    "小数": {"小数", "文本"},
    "文本": {"文本"},
    "布尔": {"布尔", "文本"},
    "日期": {"日期", "文本"},
    "时间": {"时间", "日期", "文本"},
    "时间戳": {"时间戳", "时间", "日期", "文本"},
    "二进制": {"二进制"},
    "未知": set(),
}

_TYPE_BY_CODE = {"I": "整数", "D": "小数", "S": "文本", "B": "布尔", "T": "时间"}
_CODE_BY_TYPE = {"整数": "I", "小数": "D", "文本": "S", "布尔": "B", "时间": "T"}

_TYPE_ALIASES = {
    "整数": {"int", "integer", "bigint", "smallint", "tinyint", "int2", "int4", "int8",
             "long", "short", "serial", "bigserial", "整数", "整型", "长整型"},
    "小数": {"decimal", "numeric", "number", "float", "double", "real", "float4", "float8",
             "money", "小数", "浮点", "浮点数", "数值"},
    "文本": {"varchar", "varchar2", "nvarchar", "char", "nchar", "text", "string", "clob",
             "nvarchar2", "文本", "字符串", "字符"},
    "布尔": {"bool", "boolean", "bit", "布尔", "布尔值"},
    "日期": {"date", "日期"},
    "时间": {"time", "时间"},
    "时间戳": {"timestamp", "datetime", "timestamptz", "timestamp_tz", "timestamp_ntz",
               "时间戳", "日期时间"},
    "二进制": {"binary", "varbinary", "blob", "bytea", "raw", "bytes", "二进制"},
}
_ALIAS_TO_TYPE = {alias: name for name, aliases in _TYPE_ALIASES.items() for alias in aliases}

_SQL_KEYWORDS = {
    "as", "case", "when", "then", "else", "end", "distinct", "cast", "try_cast", "interval",
    "null", "true", "false", "and", "or", "not", "is", "in", "like", "between", "exists",
    "select", "from", "where", "group", "by", "order", "having", "union", "all", "on",
    "join", "left", "right", "inner", "outer", "full", "cross", "using",
}


# ---------------------------------------------------------------------------
# 类型归一
# ---------------------------------------------------------------------------


def normalize_type(raw: Any) -> str:
    """把 DDL / SQL 里看到的类型写法归一为标准类型；无法识别返回「未知」。"""
    text = str(raw or "").strip().lower()
    if not text:
        return "未知"
    # 去掉参数、数组/结构后缀：decimal(18,2)、varchar(64)、array<int>、struct<...>
    text = re.split(r"[(<\[]", text, maxsplit=1)[0]
    text = re.sub(r"\s+", "", text)
    if not text:
        return "未知"
    if text in _ALIAS_TO_TYPE:
        return _ALIAS_TO_TYPE[text]
    if text.startswith("int") or text.startswith("uint"):
        return "整数"
    if text.startswith(("decimal", "numeric", "number", "float", "double", "real")):
        return "小数"
    if text.startswith(("varchar", "char", "nvarchar", "nchar", "text", "string")):
        return "文本"
    if text.startswith(("timestamp", "datetime")):
        return "时间戳"
    return "未知"


def type_compatible(output_type: Any, target_type: Any, *, policy: Mapping[str, Any] | None = None) -> bool:
    """输出类型能否写入目标列；policy 里关掉类型兼容时退化为严格相等。"""
    cfg = {**DEFAULT_POLICY, **(policy or {})}
    output = normalize_type(output_type)
    target = normalize_type(target_type)
    # 任一侧类型未知时无法判定：交给「未知类型视为失败」阈值统一处理。
    if output == "未知" or target == "未知":
        return True
    if not cfg.get("类型兼容", True):
        return output == target
    return target in TYPE_STANDARDS.get(output, set())


# ---------------------------------------------------------------------------
# 数据类型（历史列类型为紧凑编码；新列同时写出规范类型）


def encode_type(raw: Any) -> str:
    """写入历史兼容的紧凑编码：I/D/S/B/T，无法识别为 U。"""
    return _CODE_BY_TYPE.get(normalize_type(raw), "U")


def decode_type(code: Any) -> str:
    """兼容点：历史行可能写的是代码（I/D/S/T/U）、中文类型名或完整 DDL 类型。"""
    text = str(code or "").strip()
    if not text:
        return "未知"
    if text in _TYPE_BY_CODE:
        return _TYPE_BY_CODE[text]
    return normalize_type(text)


def split_list(raw: Any, *extra_separators: str) -> list[str]:
    """支持列表与 `a|b` / `a,b` / `a、b` 等写法；括号/引号内的分隔符不切分。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    separators = "|,，、;；" + "".join(extra_separators)
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in str(raw):
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([<":
            depth += 1
        elif ch in ")]>":
            depth = max(depth - 1, 0)
        if ch in separators and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if quote or buf:
        parts.append("".join(buf))
    return [part.strip() for part in parts if part.strip()]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是", "一致"}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 列选择解析（SQL模板 -> 输出列）


@dataclass
class SelectColumn:
    name: str
    expression: str
    data_type: str = ""


@dataclass
class SqlSelection:
    columns: list[SelectColumn]
    error: str = ""


_IDENT = r"`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[^\W\d]\w*"
# 显式别名优先：expr AS alias / expr alias（alias 不能是关键字）。
_ALIAS_AS = re.compile(rf"^(?P<expr>.*?)\s+\bas\b\s+(?P<alias>{_IDENT})\s*$", re.IGNORECASE | re.DOTALL)


def _clean_identifier(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] in "`\"[" and text[-1] in "`\"]":
        return text[1:-1].strip()
    return text


def _split_top_level(text: str, separator: str = ",") -> tuple[list[str], str]:
    """按顶层分隔符切分；返回 (片段列表, 括号不平衡时的错误说明)。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return [], "SQL片段括号不匹配"
        if ch == separator and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if depth != 0 or quote:
        return [], "SQL片段括号或引号未闭合"
    parts.append("".join(buf))
    return parts, ""


def _implicit_name(expression: str) -> str:
    expr = expression.strip()
    m = re.search(rf"({_IDENT})\s*$", expr)
    if not m:
        return ""
    name = _clean_identifier(m.group(1))
    return "" if name.lower() in _SQL_KEYWORDS else name


def _top_level_last_space(text: str) -> int:
    """返回顶层（不在括号/引号内）最后一个空白之后的位置；没有则 -1。"""
    depth = 0
    quote: str | None = None
    last = -1
    for index, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch.isspace() and depth == 0:
            last = index + 1
    return last


_TYPE_DECLARATION = re.compile(rf"^(?P<name>{_IDENT})\s*:\s*(?P<type>.+)$", re.DOTALL)


def _apply_alias(expr: str, alias: str) -> tuple[str, str, str]:
    """返回 (列名候选, 表达式, 声明的类型)；支持 `expr AS 别名` 与 `别名:类型`。"""
    declared_type = ""
    bare = _TYPE_DECLARATION.match(expr.strip())
    if bare:
        # `amount:decimal(18,2)`：冒号前是字段名、冒号后是类型声明
        declared_type = bare.group("type").strip()
        expr = bare.group("name").strip()
    if ":" in alias:
        alias, _, declared_type = alias.partition(":")
        alias = alias.strip()
        declared_type = declared_type.strip()
    return alias or _implicit_name(expr), expr, declared_type


def parse_selection(sql: Any) -> SqlSelection:
    """解析 ``SELECT ... FROM`` 的输出列（含别名与 ``别名:类型`` 声明）。"""
    text = str(sql or "")
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    match = re.search(r"\bselect\b", text, re.IGNORECASE)
    if not match:
        return SqlSelection([], "SQL片段中未找到 SELECT 输出列")
    body = text[match.end():]
    from_match = re.search(r"\bfrom\b", body, re.IGNORECASE)
    if from_match:
        body = body[: from_match.start()]
    body = body.strip()
    # 去掉顶层 DISTINCT / ALL
    body = re.sub(r"^(distinct|all)\b", "", body, flags=re.IGNORECASE).strip()
    if not body:
        return SqlSelection([], "SELECT 未声明输出列")

    fragments, split_error = _split_top_level(body)
    if split_error:
        return SqlSelection([], split_error)

    columns: list[SelectColumn] = []
    for fragment in fragments:
        piece = fragment.strip()
        if not piece:
            return SqlSelection([], "SELECT 输出列中存在空项")
        if piece == "*" or piece.endswith(".*"):
            return SqlSelection([], "SELECT * 无法确定输出列，请显式列出字段")
        alias = ""
        expr = piece
        as_match = _ALIAS_AS.match(piece)
        if as_match:
            expr = as_match.group("expr").strip()
            alias = _clean_identifier(as_match.group("alias"))
        else:
            split_at = _top_level_last_space(piece)
            if split_at > 0:
                candidate = piece[split_at:].strip()
                if re.fullmatch(_IDENT, candidate) and candidate.lower() not in _SQL_KEYWORDS:
                    expr = piece[:split_at].strip()
                    alias = _clean_identifier(candidate)
        name, expr, declared_type = _apply_alias(expr, alias)
        if not name:
            return SqlSelection([], f"输出列 `{piece[:40]}` 缺少可识别的字段别名")
        columns.append(SelectColumn(name=name, expression=piece, data_type=declared_type.strip()))
    if not columns:
        return SqlSelection([], "SELECT 未声明输出列")
    return SqlSelection(columns)


def apply_parameter_types(columns: Iterable[SelectColumn], *declarations: Any) -> None:
    """用 ``参数占位符`` / ``参数默认值`` 里的 ``name:type`` 声明补全参数列类型。

    只补全“表达式里直接引用了该占位符”且尚未声明类型的输出列，不猜测无关列。
    """
    declared: dict[str, str] = {}
    for raw in declarations:
        for part in split_list(raw):
            name, _, raw_type = part.partition(":")
            if name.strip() and raw_type.strip():
                declared[name.strip()] = raw_type.strip()
    if not declared:
        return
    for column in columns:
        if column.data_type:
            continue
        for name, raw_type in declared.items():
            if _references(column.expression, name):
                column.data_type = raw_type
                break


def _references(expression: str, name: str) -> bool:
    """表达式是否引用了该占位符：支持 `dt`、`${dt}`、`#{dt}`、`:dt`、`{{dt}}`。"""
    if not name:
        return False
    if re.fullmatch(r"\w+", name):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", expression) is not None
    return name in expression


# ---------------------------------------------------------------------------
# 目标表字段（元数据存储 -> 目标字段）


@dataclass
class TargetField:
    name: str
    data_type: str


@dataclass
class TargetTable:
    table_name: str
    warehouse_layer: str = ""
    fields: list[TargetField] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "表名": self.table_name,
            "所属数仓层": self.warehouse_layer,
            "字段": [asdict(item) for item in self.fields],
        }


def _field_pairs(raw_fields: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in split_list(raw_fields):
        name, _, data_type = item.partition(":")
        pairs.append((name.strip(), data_type.strip()))
    return pairs


def field_entries(raw_fields: Any) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for index, (name, data_type) in enumerate(_field_pairs(raw_fields), start=1):
        data_type = data_type or "未知"
        entries.append({
            "字段名": name,
            "规范类型": normalize_type(data_type),
            "原始类型": data_type,
            "ordinal": str(index),
        })
    return entries


def _legacy_field_entries(row: Mapping[str, Any]) -> list[dict[str, str]]:
    """历史元数据行只给了「字段数量」，按 ordinal 合成占位字段用于数量校验。"""
    try:
        count = int(row.get("字段数量") or 0)
    except (TypeError, ValueError):
        count = 0
    return [
        {"字段名": f"#{index}", "规范类型": "未知", "原始类型": "", "ordinal": str(index)}
        for index in range(1, max(count, 0) + 1)
    ]


def table_fields(row: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """读取元数据行里的目标字段；返回 (字段列表, 是否历史兼容数据)。"""
    entries = field_entries(row.get("目标表字段"))
    if entries:
        return entries, False
    return _legacy_field_entries(row), True


def resolve_target_table(rows: Mapping[str, Mapping[str, Any]], table_name: str, warehouse_layer: str = "") -> TargetTable:
    """按目标表名（同名时用数仓层消歧）从元数据存储解析目标表字段。"""
    candidates = [row for row in rows.values() if str(row.get("表名") or "").strip() == table_name]
    if warehouse_layer:
        narrowed = [row for row in candidates if str(row.get("所属数仓层") or "").strip() == warehouse_layer]
        if narrowed:
            candidates = narrowed
    target = TargetTable(table_name=table_name, warehouse_layer=warehouse_layer)
    if not candidates:
        target.error = f"目标表「{table_name}」在元数据存储中不存在"
        return target
    if len(candidates) > 1:
        target.error = f"目标表「{table_name}」在元数据存储中存在多条记录，请补充所属数仓层"
        return target
    row = candidates[0]
    entries, _legacy = table_fields(row)
    target.warehouse_layer = str(row.get("所属数仓层") or warehouse_layer)
    target.fields = [TargetField(name=item["字段名"], data_type=item["规范类型"]) for item in entries]
    if not target.fields:
        target.error = f"目标表「{table_name}」未登记目标表字段"
    return target


# ---------------------------------------------------------------------------
# 比对判定


@dataclass
class ColumnOutput:
    name: str
    data_type: str
    expression: str = ""


def declared_columns(row: Mapping[str, Any]) -> tuple[list[ColumnOutput] | None, str, list[str]]:
    """解析输出列，返回 (列, 错误, 冲突说明)。

    别名冲突的取舍：**以 SQL 片段里的字段别名为准**。别名决定列映射到目标表哪个字段，
    模板文件里的「预期输出列 / 输出列」只是声明，和别名冲突时不能反过来改写映射；
    冲突会作为告警记录下来，并且声明顺序与别名顺序不一致时同样提示。
    """
    declared = split_list(row.get("输出列"))
    declared_types = split_list(row.get("输出列类型"))
    declared_order = split_list(row.get("输出列顺序")) or split_list(row.get("预期输出列"))
    names = declared or declared_order
    conflicts: list[str] = []
    selection = parse_selection(row.get("SQL片段"))
    if not names:
        if selection.error:
            return None, selection.error, []
        apply_parameter_types(selection.columns, row.get("参数占位符"), row.get("参数默认值"))
        columns = [
            ColumnOutput(name=item.name, data_type=item.data_type, expression=item.expression)
            for item in selection.columns
        ]
        columns = _merge_declared_types(columns, declared_types, conflicts)
        return columns, "", conflicts

    if selection.error:
        # 解析不出别名时只能按声明列判定（例如 SQL 是纯占位符拼接）。
        columns = [
            ColumnOutput(
                name=name,
                data_type=normalize_type(declared_types[index]) if index < len(declared_types) else "",
                expression="",
            )
            for index, name in enumerate(names)
        ]
        return columns, "", conflicts

    apply_parameter_types(selection.columns, row.get("参数占位符"), row.get("参数默认值"))
    actual = [item.name for item in selection.columns]
    if actual != names:
        conflicts.append(
            f"模板声明输出列为 {'、'.join(names)}，SQL 别名为 {'、'.join(actual)}；以 SQL 别名为准"
        )
    columns = [
        ColumnOutput(name=item.name, data_type=item.data_type, expression=item.expression)
        for item in selection.columns
    ]
    return _merge_declared_types(columns, declared_types, conflicts), "", conflicts


def _merge_declared_types(
    columns: list[ColumnOutput],
    declared_types: list[str],
    conflicts: list[str],
) -> list[ColumnOutput]:
    """SQL 通常不带类型：按「同数量按位置、否则按列名」合并模板声明的输出列类型。"""
    if not declared_types:
        return columns
    if len(declared_types) == len(columns):
        for column, raw_type in zip(columns, declared_types):
            if not column.data_type:
                column.data_type = raw_type
        return columns
    for column in columns:
        if column.data_type:
            continue
        for raw_type in declared_types:
            if ":" in raw_type:
                name, _, value = raw_type.partition(":")
                if name.strip() == column.name:
                    column.data_type = value.strip()
                    break
    if len(declared_types) != len(columns):
        conflicts.append(
            f"输出列类型声明 {len(declared_types)} 项与输出列 {len(columns)} 个不匹配，仅按列名补全"
        )
    return columns


def compare_columns(
    columns: list[ColumnOutput] | None,
    target: TargetTable,
    policy: Mapping[str, Any] | None = None,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """按字段数量、顺序、类型范围与标准判定输出列是否可发布。

    ``warnings`` 用来承接“声明与别名冲突”这类不改变判定、但必须留痕的说明。
    """
    cfg = {**DEFAULT_POLICY, **(policy or {})}
    base = {
        "判定标准": dict(cfg),
        "目标字段": [asdict(item) for item in target.fields],
        "目标表": target.as_dict(),
        "兼容历史数据": True,
    }
    if target.error or not target.fields:
        error = target.error or f"目标表「{target.table_name}」未登记目标表字段"
        return {**base, "passed": False, "问题": [error], "附加问题": [], "告警": list(warnings),
                "差异": [], "输出列": [], "输出列类型": [], "输出列顺序": [], "目标字段顺序": [],
                "字段数量": 0, "目标字段数量": len(target.fields)}
    if columns is None:
        return {**base, "passed": False, "问题": [], "附加问题": [], "告警": list(warnings),
                "差异": [], "输出列": [], "输出列类型": [], "输出列顺序": [], "目标字段顺序": [],
                "字段数量": 0, "目标字段数量": len(target.fields)}

    output_names = [column.name for column in columns]
    output_types = [normalize_type(column.data_type) for column in columns]
    target_names = [field.name for field in target.fields]
    target_types = [normalize_type(field.data_type) for field in target.fields]
    target_type_map = dict(zip(target_names, target_types))
    target_position = {name: index for index, name in enumerate(target_names)}

    problems: list[str] = []
    extra: list[str] = []
    differences: list[dict[str, Any]] = []

    # 1) 字段数量与字段集合
    if len(columns) != len(target.fields):
        extra.append(f"输出列 {len(columns)} 个，目标表字段 {len(target.fields)} 个，字段数量不一致")
    missing = [name for name in target_names if name not in set(output_names)]
    unexpected = [name for name in output_names if name not in target_position]
    if missing:
        extra.append(f"目标表字段未在输出列中出现：{'、'.join(missing)}")
    if unexpected:
        extra.append(f"输出列在目标表中不存在：{'、'.join(unexpected)}")
    duplicates = [name for name in dict.fromkeys(output_names) if output_names.count(name) > 1]
    if duplicates:
        extra.append(f"输出列别名重复：{'、'.join(duplicates)}")
    if unexpected:
        differences.append({
            "类型": "字段映射", "输出列": "、".join(unexpected), "目标字段": "", "位置": "",
            "说明": "字段别名与目标表字段不同名，无法产生映射",
        })

    # 2) 列顺序（仅在同名字段集合一致时有意义）
    mismatches: list[int] = []
    if not missing and not unexpected:
        mismatches = [index for index in range(len(columns)) if output_names[index] != target_names[index]]
        if mismatches:
            detail = "、".join(
                f"第{index + 1}列应为「{target_names[index]}」实际为「{output_names[index]}」"
                for index in mismatches
            )
            extra.append(f"输出列顺序与目标表字段顺序不一致：{detail}")
            differences.extend({
                "类型": "顺序", "输出列": output_names[index], "目标字段": target_names[index],
                "位置": str(index + 1), "说明": "位置不一致",
            } for index in mismatches)

    # 3) 类型范围与标准
    type_issues: list[dict[str, Any]] = []
    unknown_columns: list[str] = []
    for index, column in enumerate(columns):
        name = column.name
        if name not in target_type_map:
            continue
        output_type = output_types[index]
        target_type = target_type_map[name]
        if output_type == "未知" or target_type == "未知":
            # 历史元数据/未声明类型：无法判定，默认告警；严格模式才计入失败。
            unknown_columns.append(name)
            continue
        if not type_compatible(output_type, target_type, policy=cfg):
            type_issues.append({
                "类型": "类型", "输出列": name, "目标字段": name, "位置": str(index + 1),
                "说明": f"输出列「{name}」类型 {output_type} 不能写入目标字段类型 {target_type}",
                "输出类型": output_type, "目标类型": target_type,
            })
    warnings: list[str] = list(warnings)
    if unknown_columns:
        warnings.append(
            f"输出列类型未声明，无法参与类型判定：{'、'.join(unknown_columns)}"
        )
    if type_issues:
        given = "、".join(item["说明"] for item in type_issues[:5])
        extra.append(f"输出列类型与目标表字段类型标准不一致：{given}")
    differences.extend(type_issues)

    # 4) 阈值判定
    count_ok = (
        int(cfg["字段数量下限"]) <= len(columns) <= int(cfg["字段数量上限"])
        and abs(len(columns) - len(target.fields)) <= int(cfg["字段数量偏差容忍"])
    )
    order_ok = len(mismatches) <= int(cfg["顺序不一致容忍"]) and not missing and not unexpected
    strict_unknown = bool(cfg.get("未知类型视为失败"))
    type_ok = len(type_issues) <= int(cfg["类型不一致容忍"]) and not (strict_unknown and unknown_columns)
    passed = count_ok and order_ok and type_ok and not target.error
    if not count_ok:
        problems.append("字段数量超出阈值")
    if not order_ok:
        problems.append("字段顺序超出阈值")
    if not type_ok:
        problems.append("字段类型超出阈值")

    return {
        **base,
        "passed": passed,
        "问题": problems,
        "附加问题": extra,
        "告警": warnings,
        "差异": differences,
        "输出列": output_names,
        "输出列类型": output_types,
        "输出列顺序": output_names,
        "目标字段顺序": output_names if not missing and not unexpected else [],
        "字段数量": len(columns),
        "目标字段数量": len(target.fields),
        "顺序不一致数": len(mismatches),
        "类型不一致数": len(type_issues),
    }


# ---------------------------------------------------------------------------
# 快照存储


def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """历史兼容层：老快照可能缺字段、用代码表示类型、用字符串表示差异。"""
    data = copy.deepcopy(dict(row))
    data.setdefault("字段数量", len(split_list(data.get("输出列"))))
    data.setdefault("输出列顺序", split_list(data.get("输出列")))
    data.setdefault("输出列类型", [])
    data.setdefault("目标表字段", split_list(data.get("目标字段")))
    data.setdefault("目标表字段类型", [])
    data.setdefault("目标字段顺序", split_list(data.get("目标表字段")))
    data.setdefault("目标表", "")
    data.setdefault("校验版本", int(data.get("id") or 0))
    data.setdefault("校验状态", "通过" if data.get("passed") else "失败")
    data.setdefault("问题", [])
    data.setdefault("附加问题", [])
    data.setdefault("告警", [])
    if isinstance(data.get("差异"), str):
        data["差异"] = split_list(data.get("差异"))
    data.setdefault("差异", [])
    data.setdefault("阈值", {})
    data.setdefault("字段一致性", "一致" if _as_bool(data.get("passed")) else "不一致")
    data.setdefault("当前生效", False)
    data.setdefault("已回用", False)
    data.setdefault("关联模板", "")
    data.setdefault("关联模板id", 0)
    data.setdefault("关联元数据表", "")
    data.setdefault("模板指纹", "")
    data.setdefault("校验指纹", data.get("模板指纹") or "")
    data.setdefault("载入方式", "")
    data.setdefault("明细完整", True)
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data


def validated(row: Mapping[str, Any]) -> bool:
    """可用快照 = 校验通过 + 明细齐全 + 目标表字段未变更；失败/作废快照不能作为上一可用版本。"""
    return (
        _as_bool(row.get("passed"))
        and _as_bool(row.get("明细完整"), True)
        and str(row.get("字段一致性") or "") != "不一致"
    )


def latest_by_verdict(verdict_hash: str) -> dict[str, Any] | None:
    """同“文件 + 判定标准 + 目标表字段”的最近版本：结论相同的载入直接回用。"""
    if not verdict_hash:
        return None
    candidates = [
        normalize_row(row) for row in store.snapshot_rows()
        if row.get("校验指纹") == verdict_hash
    ]
    return max(candidates, key=lambda item: int(item.get("校验版本") or 0), default=None)


def verdict_fingerprint(content_hash: str, validation: Mapping[str, Any], target: TargetTable) -> str:
    return stable_hash({
        "模板指纹": content_hash,
        "判定标准": validation.get("判定标准"),
        "目标表": target.table_name,
        "目标字段": [[item.name, item.data_type] for item in target.fields],
    })


def record_snapshot(
    *,
    content_hash: str,
    columns: list[ColumnOutput],
    target: TargetTable,
    validation: Mapping[str, Any],
    source: str,
    template_code: str = "",
    template_name: str = "",
    metadata_table: str = "",
    load_mode: str = "",
    template_id: int = 0,
) -> tuple[dict[str, Any], bool]:
    """写入一个校验版本。

    返回 ``(快照, 是否为回用)``。调用方必须已经持有 ``store.lock``，保证
    “按指纹查重 -> 分配版本 -> 激活” 的原子性。
    """
    verdict_hash = verdict_fingerprint(content_hash, validation, target)
    existing = latest_by_verdict(verdict_hash)
    if existing is not None:
        # 同一文件在同一目标表口径下重复载入：不新增版本，
        # 也不让失败文件覆盖上一可用快照。
        if _as_bool(existing.get("passed")) and existing.get("字段一致性") != "一致":
            # 指纹里包含目标表字段与判定标准，命中即说明口径已恢复，可恢复一致性标记。
            for row in store.snapshot_rows():
                if row.get("校验指纹") == verdict_hash:
                    row["字段一致性"] = "一致"
                    row["当前生效"] = True
            existing["字段一致性"] = "一致"
            existing["当前生效"] = True
        return existing, True

    rows = store.snapshot_rows()
    version = max((int(row.get("校验版本") or row.get("id") or 0) for row in rows), default=0) + 1
    passed = bool(validation.get("passed"))
    status = "通过" if passed else "失败"
    snapshot = {
        "id": version,
        "校验版本": version,
        "模板指纹": content_hash,
        "校验指纹": verdict_hash,
        "校验状态": status,
        "passed": passed,
        "问题": list(validation.get("问题") or []),
        "附加问题": list(validation.get("附加问题") or []),
        "告警": list(validation.get("告警") or []),
        "差异": copy.deepcopy(list(validation.get("差异") or [])),
        "输出列": list(validation.get("输出列") or []),
        "输出列类型": list(validation.get("输出列类型") or []),
        "输出列顺序": list(validation.get("输出列顺序") or []),
        "目标字段顺序": list(validation.get("目标字段顺序") or []),
        "字段数量": int(validation.get("字段数量") or len(columns)),
        "目标字段数量": int(validation.get("目标字段数量") or len(target.fields)),
        "目标表": target.table_name,
        "目标表字段": [item.name for item in target.fields],
        "目标表字段类型": [item.data_type for item in target.fields],
        "判定标准": copy.deepcopy(dict(validation.get("判定标准") or DEFAULT_POLICY)),
        "阈值": copy.deepcopy(dict(validation.get("判定标准") or DEFAULT_POLICY)),
        "兼容历史数据": True,
        "关联模板": template_code,
        "关联模板id": template_id,
        "模板名称": template_name,
        "关联元数据表": metadata_table,
        "载入方式": load_mode,
        "来源": source,
        "字段一致性": "一致" if passed else "不一致",
        "明细完整": True,
        "当前生效": True if passed else False,
        "已回用": False,
        "schema_version": SCHEMA_VERSION,
        "生成时间": _now(),
    }
    rows.append(snapshot)
    return normalize_row(snapshot), False


def mark_consistency(content_hash: str, consistent: bool) -> list[dict[str, Any]]:
    """校验结果回流：把同一指纹的全部版本标记为一致/不一致。"""
    touched: list[dict[str, Any]] = []
    for row in store.snapshot_rows():
        if content_hash:
            if row.get("模板指纹") != content_hash:
                continue
        # 一致性是“快照 vs 当前目标表字段”的结论；失败快照本来就是不一致。
        row["字段一致性"] = "一致" if consistent and _as_bool(row.get("passed")) else "不一致"
        touched.append(normalize_row(row))
    return touched


def latest_for_template(template_code: str) -> dict[str, Any] | None:
    """模板最近一次校验尝试（含失败），用于判定“当前结论”。"""
    candidates = all_for_template(template_code)
    return max(candidates, key=lambda item: int(item.get("校验版本") or 0), default=None)


def usable_for_template(template_code: str) -> dict[str, Any] | None:
    """模板上一可用快照：通过校验、明细齐全、且目标表字段未变更。"""
    candidates = [item for item in all_for_template(template_code) if validated(item)]
    return max(candidates, key=lambda item: int(item.get("校验版本") or 0), default=None)


def active_for_template(template_code: str) -> dict[str, Any] | None:
    """两侧统一读取的“当前快照”：最近一次校验尝试；没有尝试时退回上一可用版本。"""
    return latest_for_template(template_code) or usable_for_template(template_code)


def all_for_template(template_code: str) -> list[dict[str, Any]]:
    return [
        normalize_row(row) for row in store.snapshot_rows()
        if row.get("关联模板") == template_code
    ]


def snapshot_payload(template_code: str) -> dict[str, Any]:
    latest = latest_for_template(template_code)
    usable = usable_for_template(template_code)
    if latest is None:
        return {"模板编号": template_code, "校验快照": None, "校验状态": "未校验"}
    return {
        "模板编号": template_code,
        "校验快照": latest,
        "上一可用快照": usable,
        "校验状态": latest["校验状态"],
        "可发布": bool(usable and int(usable.get("校验版本") or 0) == int(latest.get("校验版本") or 0)),
    }


def list_snapshots(
    *,
    keyword: str | None = None,
    status: str | None = None,
    version: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    rows = [normalize_row(row) for row in store.snapshot_rows()]
    if keyword:
        rows = [
            row for row in rows
            if keyword in str(row.get("关联模板", "")) or keyword in str(row.get("模板指纹", ""))
            or keyword in str(row.get("目标表", ""))
        ]
    if status:
        rows = [row for row in rows if row.get("校验状态") == status]
    if version is not None:
        rows = [row for row in rows if int(row.get("校验版本") or 0) == version]
    rows.sort(key=lambda item: int(item.get("校验版本") or 0), reverse=True)
    total = len(rows)
    start = max(page - 1, 0) * size
    return rows[start:start + size], total


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
