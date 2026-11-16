"""SQL模板业务规则：状态流转、输出列校验、模板载入与筛选口径都收在这里。

发布模板前不再只看字段是否为空，而是把「SQL片段解析出的输出列」与元数据存储登记的目标表
字段逐列比对（字段数量、列顺序、类型范围与标准）。比对结论写入「输出列校验数据快照」，
元数据存储与SQL模板后续都只读这份快照，保证两侧看到的是同一列顺序、同一类型。
"""
from __future__ import annotations

import json
from typing import Any

from app.services import columnsnapshot
from app.store import store

MODULE = "sqltpl"
REQUIRED_FIELDS = ["模板编号", "模板名称", "模板类型"]
STATUS_ORDER = ["草稿", "模板评审中", "已发布", "已废弃"]
ACTION_RULES = {"提交模板评审": "模板评审中", "发布模板": "已发布", "废弃模板": "已废弃"}
NEGATIVE_ACTIONS = []

# 载入时允许被模板文件覆盖的字段；版本号/状态/编号始终由服务端决定。
LOADABLE_FIELDS = [
    "模板名称", "模板类型", "SQL片段", "参数占位符", "参数默认值", "预期输出列",
    "输出列", "输出列类型", "输出列顺序", "关联元数据表", "目标数仓层", "模板版本",
]

PACKAGE_FORMAT = "sqltpl-template"
PACKAGE_VERSION = 1


class SqltplService:
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
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self.decorate(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self.decorate(entry) if entry is not None else None

    def decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(entry)
        template_code = str(entry.get("模板编号") or "")
        latest = columnsnapshot.latest_for_template(template_code)
        usable = columnsnapshot.usable_for_template(template_code)
        enriched["校验版本"] = latest.get("校验版本") if latest else None
        enriched["上一可用校验版本"] = usable.get("校验版本") if usable else None
        enriched["校验状态"] = latest.get("校验状态") if latest else "未校验"
        enriched["字段一致性"] = entry.get("字段一致性") or (
            latest.get("字段一致性") if latest else "未校验"
        )
        enriched["可发布"] = bool(
            usable is not None
            and latest is not None
            and int(usable.get("校验版本") or 0) == int(latest.get("校验版本") or 0)
        )
        return enriched

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in LOADABLE_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["字段一致性"] = "未校验"
        rows.append(entry)
        return entry, []

    # -- 校验与发布 -------------------------------------------------------

    def target_table(self, entry: dict[str, Any]) -> columnsnapshot.TargetTable:
        table_name = str(entry.get("关联元数据表") or "").strip()
        if not table_name:
            return columnsnapshot.TargetTable(
                table_name="", error="模板未绑定目标表（关联元数据表为空）"
            )
        meta_rows = {
            str(item.get("id") or index): item
            for index, item in enumerate(store.rows("metastore"))
        }
        by_code = {str(row.get("表编号") or ""): row for row in meta_rows.values()}
        if table_name in by_code:
            row = by_code[table_name]
            table_name = str(row.get("表名") or table_name)
            meta_rows["__resolved__"] = row
        resolved = columnsnapshot.resolve_target_table(
            meta_rows, table_name, str(entry.get("目标数仓层") or "")
        )
        if not resolved.error and not resolved.fields:
            resolved.error = f"目标表「{table_name}」未登记目标表字段"
        return resolved

    def validate_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """按字段数量、顺序、类型范围与标准判定一条模板定义是否可发布。"""
        columns, reason, conflicts = columnsnapshot.declared_columns(entry)
        target = self.target_table(entry)
        if columns is None:
            declared = columnsnapshot.split_list(entry.get("预期输出列"))
            if declared:
                # 有预期输出列但 SQL 无法解析：仍按声明列做数量/顺序/类型比对。
                columns = [
                    columnsnapshot.ColumnOutput(name=name, data_type="未知") for name in declared
                ]
                validation = columnsnapshot.compare_columns(columns, target, warnings=conflicts)
                validation["附加问题"].insert(0, reason)
                validation["passed"] = False
                validation["问题"].append("SQL片段无法解析输出列")
                return validation
            validation = columnsnapshot.compare_columns(None, target, warnings=conflicts)
            validation["附加问题"].insert(0, reason)
            validation["问题"].append("模板定义未提供可判定的输出列")
            return validation
        return columnsnapshot.compare_columns(columns, target, warnings=conflicts)

    def validate_template(self, entry_id: int) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
        """只读校验：生成并返回当前判定结果，不写入快照。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, {}, f"模板定义 {entry_id} 不存在或已归档"
        return entry, self.validate_entry(entry), ""

    def record_validation(self, entry: dict[str, Any], *, load_mode: str = "") -> tuple[dict[str, Any], bool]:
        """把当前模板定义的校验结论落成快照版本；同内容重复校验回用已有版本。"""
        columns, _reason, _conflicts = columnsnapshot.declared_columns(entry)
        target = self.target_table(entry)
        validation = self.validate_entry(entry)
        content_hash = self.content_hash(entry)
        with store.lock:
            snapshot, reused = columnsnapshot.record_snapshot(
                content_hash=content_hash,
                columns=columns or [],
                target=target,
                validation=validation,
                source="sqltpl",
                template_code=str(entry.get("模板编号") or ""),
                template_name=str(entry.get("模板名称") or ""),
                metadata_table=str(entry.get("关联元数据表") or ""),
                load_mode=load_mode,
                template_id=int(entry.get("id") or 0),
            )
            entry["字段一致性"] = snapshot.get("字段一致性")
            entry["snapshot_version"] = snapshot.get("校验版本")
            entry["snapshot_passed"] = bool(snapshot.get("passed"))
            return snapshot, reused

    def content_hash(self, values: dict[str, Any]) -> str:
        """模板内容指纹：只包含真正影响输出列判定的字段，元数据/顺序变化不会误判为新版本。"""
        payload = {
            "模板编号": values.get("模板编号"),
            "SQL片段": str(values.get("SQL片段") or "").strip(),
            "参数占位符": str(values.get("参数占位符") or "").strip(),
            "参数默认值": str(values.get("参数默认值") or "").strip(),
            "预期输出列": str(values.get("预期输出列") or "").strip(),
            "输出列": str(values.get("输出列") or "").strip(),
            "输出列类型": str(values.get("输出列类型") or "").strip(),
            "关联元数据表": str(values.get("关联元数据表") or "").strip(),
            "目标数仓层": str(values.get("目标数仓层") or "").strip(),
        }
        return columnsnapshot.stable_hash(payload)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"模板定义 {entry_id} 不存在或已归档", None
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于SQL模板可执行范围", None
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里", None
        snapshot: dict[str, Any] | None = None
        with store.lock:
            if action == "发布模板":
                snapshot, _reused = self.record_validation(entry, load_mode="发布校验")
                if not snapshot.get("passed"):
                    reasons = snapshot.get("问题") or []
                    details = snapshot.get("附加问题") or []
                    message = "输出列校验未通过，不能保存为发布版本"
                    if reasons:
                        message += f"：{'、'.join(reasons)}"
                    if details:
                        message += f"；{'；'.join(details[:3])}"
                    entry["pending"] = True
                    entry["abnormal"] = True
                    entry["字段一致性"] = snapshot.get("字段一致性")
                    return None, message, snapshot
                entry["字段一致性"] = snapshot.get("字段一致性")
                entry["snapshot_version"] = snapshot.get("校验版本")
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            if action == "发布模板":
                entry["字段一致性"] = "一致"
            elif action == "废弃模板":
                entry["字段一致性"] = "未校验"
                entry["snapshot_version"] = None
        return entry, f"模板定义已{action}", snapshot

    # -- 模板文件载入 / 下载 ----------------------------------------------

    def parse_package(self, payload: Any) -> tuple[dict[str, Any] | None, str]:
        raw: Any = payload
        if isinstance(payload, dict) and set(payload) <= {"values", "remark"}:
            raw = payload.get("values")
        try:
            if isinstance(raw, str):
                raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, "模板文件不是合法 JSON，载入失败"
        if not isinstance(raw, dict):
            return None, "模板文件结构不完整，载入失败"
        if "template" in raw and isinstance(raw.get("template"), dict):
            raw = raw["template"]
        values = dict(raw)
        if not str(values.get("SQL片段") or "").strip():
            return None, "模板文件缺少 SQL片段，载入失败"
        return values, ""

    def _reuse_report(self, existing: dict[str, Any]) -> dict[str, Any]:
        passed = bool(existing.get("passed"))
        return {
            "ok": passed,
            "message": (
                f"模板内容未变化，回用已有校验版本 V{existing.get('校验版本')}"
                if passed else
                f"模板内容与已失败文件相同，沿用 V{existing.get('校验版本')} 的失败结论，未生成新版本"
            ),
            "reused": True,
            "snapshot": existing,
        }

    def import_template(self, payload: Any) -> dict[str, Any]:
        """载入模板文件：同一文件同一目标表口径只生成一个校验版本。

        失败文件本身仍会留下一条“失败”快照作为审计记录，但不会被当作可用版本，
        也不会顶替上一可用快照；目标表字段修正后，同一文件可重新判定。
        """
        values, error = self.parse_package(payload)
        if error:
            return {"ok": False, "message": error, "reused": False, "snapshot": None}

        content_hash = self.content_hash(values)
        template_code = str(values.get("模板编号") or "").strip()
        target = self.target_table(values)
        validation = self.validate_entry(values)
        verdict_hash = columnsnapshot.verdict_fingerprint(content_hash, validation, target)

        with store.lock:
            existing = columnsnapshot.latest_by_verdict(verdict_hash)
            if existing is not None:
                # 同一文件、同一目标表口径：只回用一个校验版本。
                target_entry = store.find(MODULE, int(existing.get("关联模板id") or 0))
                if target_entry is None and existing.get("关联模板"):
                    target_entry = next(
                        (row for row in store.rows(MODULE)
                         if row.get("模板编号") == existing.get("关联模板")),
                        None,
                    )
                if target_entry is not None:
                    self._apply_snapshot_carried_metadata(target_entry, existing)
                result = self._reuse_report(existing)
                result["entry"] = self.decorate(target_entry) if target_entry else None
                return result
            if not template_code:
                return {"ok": False, "message": "模板文件缺少 模板编号，载入失败",
                        "reused": False, "snapshot": None}

            entry = next(
                (row for row in store.rows(MODULE) if row.get("模板编号") == template_code), None
            )
            if entry is not None:
                entry["模板次版本"] = int(entry.get("模板次版本") or 0) + 1
                for field in LOADABLE_FIELDS:
                    if field in values:
                        entry[field] = values[field]
                entry["字段一致性"] = "待重新校验"
                entry["field_on_mark"] = ""
                # 内容变了就作废原来的发布结论，必须重新走发布校验。
                if entry.get("status") == "已发布":
                    entry["status"] = "模板评审中"
                    entry["pending"] = True
                    entry["abnormal"] = True
            else:
                missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
                if missing:
                    return {
                        "ok": False, "message": f"缺少必填字段：{'、'.join(missing)}，载入失败",
                        "reused": False, "snapshot": None,
                    }
                rows = store.rows(MODULE)
                entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
                entry.update(values)
                entry["status"] = STATUS_ORDER[0]
                entry["pending"] = True
                entry["abnormal"] = False
                entry["模板次版本"] = 1
                rows.append(entry)

            # 重新载入可能改变列顺序或别名：以新内容重新生成校验版本。
            snapshot, _reused = self.record_validation(entry, load_mode="模板文件载入")
            entry["field_on_mark"] = ""
            ok = bool(snapshot.get("passed"))
            message = (
                f"模板已载入并生成输出列校验版本 V{snapshot.get('校验版本')}"
                if ok else
                f"模板已载入，输出列校验未通过，未生成可用快照："
                f"{'；'.join((snapshot.get('问题') or []) + (snapshot.get('附加问题') or [])[:2])}"
            )
            return {
                "ok": ok,
                "message": message,
                "reused": False,
                "snapshot": snapshot,
                "entry": self.decorate(entry),
            }

    def _apply_snapshot_carried_metadata(self, entry: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """承接既存元数据字段：回用版本时把快照里记录的元数据关联补回模板。"""
        if not entry.get("关联元数据表") and snapshot.get("目标表"):
            entry["关联元数据表"] = snapshot.get("目标表")
        entry["字段一致性"] = snapshot.get("字段一致性")
        entry["snapshot_version"] = snapshot.get("校验版本")
        entry["snapshot_passed"] = bool(snapshot.get("passed"))

    def export_template(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """下载模板文件：连同当前生效的校验版本一起导出，便于再载入时复核。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"模板定义 {entry_id} 不存在或已归档"
        snapshot = columnsnapshot.active_for_template(str(entry.get("模板编号") or ""))
        template = {field: entry.get(field) for field in LOADABLE_FIELDS if entry.get(field) is not None}
        template.setdefault("模板编号", entry.get("模板编号"))
        template["模板内容指纹"] = self.content_hash(template)
        template["校验版本"] = snapshot.get("校验版本") if snapshot else None
        template["输出列校验快照"] = snapshot
        return {
            "format": PACKAGE_FORMAT,
            "format_version": PACKAGE_VERSION,
            "template": template,
        }, ""

