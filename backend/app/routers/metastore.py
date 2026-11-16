"""元数据存储接口：元数据表维护、目标表字段变更与输出列校验快照读取。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.metastore import MetastoreService

router = APIRouter(prefix="/api/metastore", tags=["元数据存储"])

service = MetastoreService()

LIST_FIELDS = ["表编号", "表名", "所属数仓层", "字段数量", "分区字段", "主键字段", "数据量级", "最近更新时间"]
STATUSES = ["草稿", "元数据评审中", "已确认", "已过期"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按表编号检索"),
    status: str | None = Query(default=None, description="草稿、元数据评审中、已确认、已过期"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按表编号与状态过滤元数据存储列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条元数据表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="元数据表已登记", entry=service.decorate(entry))


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出元数据存储清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "metastore", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条元数据表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"元数据表 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/output-column-snapshots", response_model=dict)
def output_column_snapshots(entry_id: int) -> dict[str, Any]:
    """读取引用该表的SQL模板输出列校验数据快照，并回写字段一致性标记。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"元数据表 {entry_id} 不存在或已归档")
    snapshots, refreshed = service.snapshots_for_entry(entry_id)
    return {
        "表编号": entry.get("表编号"),
        "表名": entry.get("表名"),
        "字段一致性": (refreshed or entry).get("字段一致性"),
        "snapshots": snapshots,
    }


@router.put("/{entry_id}/columns", response_model=ActionResult)
def update_columns(entry_id: int, payload: EntryPayload) -> ActionResult:
    """更新目标表字段；字段口径变化会作废关联模板已生效的校验结论。"""
    entry, message, feed = service.update_columns(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    text = "目标表字段已更新"
    if feed:
        changed = feed.get("模板状态") or []
        text += f"，字段一致性：{feed.get('字段一致性')}"
        if changed:
            text += "；模板状态：" + "、".join(
                f"{item['模板编号']} {item['原状态']}→{item['现状态']}" for item in changed
            )
        if feed.get("字段一致性") == "待重新校验":
            text += "；请让引用模板重新校验后再发布"
    else:
        text += "，暂无引用模板需要联动"
    return ActionResult(ok=True, message=text, entry=service.decorate(entry))


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条元数据表执行提交元数据评审、确认元数据、标记过期；字段不一致时不能确认。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=service.decorate(entry))
