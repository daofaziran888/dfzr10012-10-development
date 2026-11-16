"""状态快照清单接口：盘点结果查询、清单确认与清理校验。

清单只能由状态后端的快照盘点生成（POST /api/state/{id}/inventory），
这里提供列表、明细、确认清单、清理校验与导出。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.snapshot import snapshot_service

router = APIRouter(prefix="/api/snapshot", tags=["状态快照清单"])

service = snapshot_service

LIST_FIELDS = ["清单编号", "配置编号", "存储路径", "快照频率", "状态大小", "受保护文件", "可清理文件", "引用来源", "清单状态"]
STATUSES = ["待确认", "已确认", "已校验"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按清单编号或存储路径检索"),
    status: str | None = Query(default=None, description="待确认、已确认、已校验"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按清单编号/存储路径与状态过滤状态快照清单；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出状态快照清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "snapshot", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条快照清单明细（含清理待办与处理结论）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"状态快照清单 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条快照清单执行确认清单、清理校验；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
