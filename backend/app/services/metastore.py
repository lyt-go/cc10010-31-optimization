"""元数据存储业务规则：状态流转、字段校验、快照读取与筛选口径都收在这里。

元数据存储侧不重新比对 SQL，只读取 SQL模板写入的「输出列校验数据快照」：
同一份快照、同一列顺序与类型，避免两个模块各算一套结论。
"""
from __future__ import annotations

from typing import Any

from app.services import columnsnapshot
from app.store import store

MODULE = "metastore"
REQUIRED_FIELDS = ["表编号", "表名", "所属数仓层"]
STATUS_ORDER = ["草稿", "元数据评审中", "已确认", "已过期"]
ACTION_RULES = {"提交元数据评审": "元数据评审中", "确认元数据": "已确认", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []
CONFIRM_ACTION = "确认元数据"

# 决定目标表字段口径的登记字段；变化后已生效的校验结论需要重新生成。
COLUMN_FIELDS = ["目标表字段", "字段口径"]


def field_signature(entry: dict[str, Any]) -> str:
    """目标表字段的稳定签名，用于判断字段结构是否在确认之后发生变化。"""
    return columnsnapshot.stable_hash([
        [item["字段名"], item["原始类型"]] for item in columnsnapshot.table_fields(entry)[0]
    ])


class MetastoreService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("表编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.decorate(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self.decorate(entry) if entry is not None else None

    # -- 快照读取 ---------------------------------------------------------

    def referencing_templates(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """按「表名（+ 所属数仓层）」找出引用该目标表的SQL模板。"""
        table_name = str(entry.get("表名") or "").strip()
        layer = str(entry.get("所属数仓层") or "").strip()
        references: list[dict[str, Any]] = []
        for row in store.rows("sqltpl"):
            if str(row.get("关联元数据表") or "").strip() != table_name:
                continue
            if row.get("目标数仓层") and layer and row["目标数仓层"] != layer:
                continue
            references.append(row)
        return references

    def snapshots_for_template(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        template_code = str(row.get("模板编号") or "")
        return [
            columnsnapshot.normalize_row(snapshot)
            for snapshot in store.snapshot_rows()
            if snapshot.get("关联模板") == template_code
        ]

    def snapshots_for_entry(self, entry_id: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """按表编号反查引用模板的最新输出列校验快照，并刷新字段一致性标记。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return [], None
        snapshots = [
            item for item in (
                columnsnapshot.latest_for_template(str(row.get("模板编号") or ""))
                for row in self.referencing_templates(entry)
            ) if item is not None
        ]
        if not snapshots:
            return [], entry
        if any(item.get("字段一致性") == "待重新校验" for item in snapshots):
            entry["字段一致性"] = "待重新校验"
        elif all(item.get("passed") and item.get("字段一致性") == "一致" for item in snapshots):
            entry["字段一致性"] = "一致"
        else:
            entry["字段一致性"] = "不一致"
        return snapshots, entry

    def decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(entry)
        templates = self.referencing_templates(entry)
        latest = [
            columnsnapshot.latest_for_template(str(row.get("模板编号") or ""))
            for row in templates
        ]
        latest = [item for item in latest if item is not None]
        if latest:
            consistent = all(
                item.get("字段一致性") == "一致" and item.get("passed") for item in latest
            )
            enriched["字段一致性"] = "一致" if consistent else "不一致"
        else:
            enriched["字段一致性"] = (
                "待重新校验" if entry.get("字段一致性") == "待重新校验"
                else entry.get("字段一致性") or "未校验"
            )
        enriched["引用模板"] = [str(row.get("模板编号") or "") for row in templates]
        enriched["输出列校验快照"] = [
            {
                "模板编号": item.get("关联模板"),
                "校验版本": item.get("校验版本"),
                "校验状态": item.get("校验状态"),
                "字段一致性": item.get("字段一致性"),
                "输出列": item.get("输出列"),
                "输出列类型": item.get("输出列类型"),
                "目标字段顺序": item.get("目标字段顺序"),
            }
            for item in latest
        ]
        return enriched

    # -- 登记与字段变更 ---------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        fields = columnsnapshot.field_entries(values.get("目标表字段"))
        names = [item["字段名"] for item in fields]
        if len(set(names)) != len(names):
            return None, ["目标表字段存在重复字段名"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in COLUMN_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        if fields:
            entry["字段数量"] = len(fields)
        entry["field_on_mark"] = ""
        entry["字段一致性"] = "未校验"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_columns(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """更新目标表字段；字段结构变化时作废关联模板的已生效结论。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"元数据表 {entry_id} 不存在或已归档", None
        fields = columnsnapshot.field_entries(values.get("目标表字段"))
        names = [item["字段名"] for item in fields]
        if len(set(names)) != len(names):
            return None, "目标表字段存在重复字段名", None
        if not fields:
            return None, "目标表字段不能为空", None
        with store.lock:
            before = str(entry.get("field_on_mark") or "")
            previous_status = {
                str(row.get("模板编号") or ""): str(row.get("status") or "")
                for row in self.referencing_templates(entry)
            }
            entry["目标表字段"] = values.get("目标表字段")
            entry["字段数量"] = len(fields)
            if values.get("字段口径") is not None:
                entry["字段口径"] = values.get("字段口径")
            signature = field_signature(entry)
            new_names = [item["字段名"] for item in fields]
            new_types = [item["规范类型"] for item in fields]
            # 旧快照是按旧字段口径判定的；字段一变就必须作废，即使该表还没确认过。
            stale = [
                template for template in self.referencing_templates(entry)
                if any(
                    snapshot.get("目标表字段") != new_names
                    or [columnsnapshot.normalize_type(t) for t in columnsnapshot.split_list(snapshot.get("目标表字段类型"))] != new_types
                    for snapshot in [columnsnapshot.latest_for_template(str(template.get("模板编号") or ""))]
                    if snapshot is not None
                )
            ]
            # field_on_mark 为空表示尚未确认过，没有“已生效结论”需要作废。
            changed = (bool(before) and before != signature) or bool(stale)
            if changed:
                entry["field_on_mark"] = ""
                entry["字段一致性"] = "待重新校验"
                for template in self.referencing_templates(entry):
                    template["字段一致性"] = "待重新校验"
                    if template.get("status") == "已发布":
                        template["status"] = "模板评审中"
                        template["pending"] = True
                        template["abnormal"] = True
                    for snapshot in self.snapshots_for_template(template):
                        columnsnapshot.mark_consistency(
                            str(snapshot.get("模板指纹") or ""), False
                        )
            elif not before:
                entry["字段一致性"] = "待重新校验"
            feed = self.feed_state(entry, previous_status)
        return entry, "", feed

    def feed_state(
        self,
        entry: dict[str, Any],
        previous_status: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """把快照结论标记到引用模板上；返回跨模块联动说明。"""
        templates = self.referencing_templates(entry)
        if not templates:
            return None
        changes: list[dict[str, Any]] = []
        for template in templates:
            if template.get("字段一致性") == "待重新校验":
                code = str(template.get("模板编号") or "")
                changes.append({
                    "模板编号": code,
                    "校验版本": template.get("snapshot_version"),
                    "原状态": (previous_status or {}).get(code, template.get("status")),
                    "现状态": template.get("status"),
                    "字段一致性": "待重新校验",
                })
                continue
            snapshot = columnsnapshot.latest_for_template(str(template.get("模板编号") or ""))
            if snapshot is None:
                continue
            consistent = snapshot.get("字段一致性") == "一致" and bool(snapshot.get("passed"))
            template["字段一致性"] = "一致" if consistent else "不一致"
            template["snapshot_version"] = snapshot.get("校验版本")
            previous = str(template.get("status") or "")
            if not consistent and previous == "已发布":
                template["status"] = "模板评审中"
                template["pending"] = True
                template["abnormal"] = True
            changes.append({
                "模板编号": template.get("模板编号"),
                "校验版本": snapshot.get("校验版本"),
                "原状态": previous,
                "现状态": template.get("status"),
                "字段一致性": template["字段一致性"],
            })
        if not changes:
            return None
        verdicts = {item["字段一致性"] for item in changes}
        if verdicts == {"一致"}:
            verdict = "一致"
        elif verdicts == {"待重新校验"}:
            verdict = "待重新校验"
        else:
            verdict = "不一致"
        return {
            "元数据表": entry.get("表名"),
            "字段一致性": verdict,
            "模板状态": changes,
        }

    # -- 状态流转 ---------------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"元数据表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于元数据存储可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with store.lock:
            if action == CONFIRM_ACTION:
                references = self.referencing_templates(entry)
                latest = [
                    columnsnapshot.latest_for_template(str(row.get("模板编号") or ""))
                    for row in references
                ]
                latest = [item for item in latest if item is not None]
                if latest and not all(
                    item.get("passed") and item.get("字段一致性") == "一致" for item in latest
                ):
                    return None, "引用该表的SQL模板存在字段不一致的输出列校验快照，不允许确认元数据"
                if latest:
                    entry["字段一致性"] = "一致"
                entry["field_on_mark"] = field_signature(entry)
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            self.feed_state(entry)
        return entry, f"元数据表已{action}"
