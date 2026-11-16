"""SQL模板接口：模板定义维护、输出列校验、发布校验与模板文件载入/下载。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import columnsnapshot
from app.services.sqltpl import SqltplService

router = APIRouter(prefix="/api/sqltpl", tags=["SQL模板"])

service = SqltplService()

LIST_FIELDS = ["模板编号", "模板名称", "模板类型", "SQL片段", "参数占位符", "预期输出列", "参数默认值", "模板版本"]
STATUSES = ["草稿", "模板评审中", "已发布", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、模板评审中、已发布、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤SQL模板列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条模板定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="模板定义已登记", entry=service.decorate(entry))


# 注意：静态路径必须声明在 /{entry_id} 之前，否则会被路径参数当成 id 解析。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出SQL模板清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "sqltpl", "total": total, "items": items}


@router.get("/snapshots", response_model=PageResult[dict])
def list_snapshots(
    keyword: str | None = Query(default=None, description="按模板编号、内容指纹或目标表检索"),
    status: str | None = Query(default=None, description="通过、失败"),
    version: int | None = Query(default=None, description="只看指定校验版本"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """查看输出列校验数据快照的全部版本（重复载入不会新增版本）。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = columnsnapshot.list_snapshots(
        keyword=keyword, status=status, version=version, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/validate-file", response_model=dict)
def validate_file(payload: dict[str, Any]) -> dict[str, Any]:
    """只读预检模板文件：解析输出列并与元数据存储比对，不落快照、不改模板。"""
    values, error = service.parse_package(payload)
    if error:
        return {"ok": False, "message": error, "validation": None}
    validation = service.validate_entry(dict(values))
    return {
        "ok": bool(validation.get("passed")),
        "message": "输出列校验通过" if validation.get("passed") else "输出列校验未通过",
        "validation": validation,
    }


@router.post("/import", response_model=dict)
def import_template(payload: dict[str, Any]) -> dict[str, Any]:
    """载入模板文件（JSON）：同一文件同一目标表口径只生成一个校验版本。"""
    result = service.import_template(payload)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(status_code=status_code, content=result)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条模板定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"模板定义 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/validate", response_model=dict)
def validate_entry(entry_id: int) -> dict[str, Any]:
    """按当前模板定义重新做一次输出列校验（只读，不生成新版本）。"""
    entry, validation, message = service.validate_template(entry_id)
    if entry is None:
        return {"ok": False, "message": message, "validation": None}
    return {
        "ok": bool(validation.get("passed")),
        "message": "输出列校验通过" if validation.get("passed") else "输出列校验未通过",
        "validation": validation,
    }


@router.get("/{entry_id}/snapshot", response_model=dict)
def get_snapshot(entry_id: int) -> dict[str, Any]:
    """读取该模板当前生效的输出列校验数据快照。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"模板定义 {entry_id} 不存在或已归档")
    payload = columnsnapshot.snapshot_payload(str(entry.get("模板编号") or ""))
    payload["模板状态"] = entry.get("status")
    return payload


@router.get("/{entry_id}/download")
def download_template(entry_id: int) -> JSONResponse:
    """下载模板文件；同内容再次载入时会回用同一个校验版本。"""
    package, message = service.export_template(entry_id)
    if package is None:
        raise HTTPException(status_code=404, detail=message)
    filename = f"{package['template'].get('模板编号') or entry_id}-template.json"
    return JSONResponse(
        content=package,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条模板定义执行提交模板评审、发布模板、废弃模板；发布前先做输出列校验。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message, snapshot = service.run_action(entry_id, action)
    if entry is None:
        response = ActionResult(ok=False, message=message, entry=None)
        if snapshot is not None:
            response.entry = {"校验快照": snapshot}
        return response
    return ActionResult(ok=True, message=message, entry=service.decorate(entry))
