"""血缘与重跑的操作接口：产物反查、绑定记录查询与重跑源数据选择。

通用资源路由只覆盖列表/登记/状态动作；「从产物反查来源」「从结果跳到
重跑」需要按产物引用查询，因此单独提供固定路径接口。这些路径必须先于
``/{module}/{entry_id}`` 注册，避免被动态段抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.artifact_refs import artifact_references_match, source_path_matches
from app.services.extract_lineage import ExtractLineageService
from app.services.parse_retry import ParseRetryService

router = APIRouter(prefix="/api", tags=["血缘与重跑"])


@router.get("/extract_lineage/resolve")
def resolve_artifact(
    artifact: str = Query(..., description="产物引用，支持完整路径、短路径或文档编号"),
) -> dict[str, Any]:
    """按产物引用反查来源：唯一且已核实时给出可直接重跑的源数据，否则返回待核项。"""
    return ExtractLineageService().resolve_source_candidates(artifact)


@router.get("/extract_lineage/bindings")
def list_bindings(
    source: str | None = Query(default=None, description="按来源文件过滤"),
    artifact: str | None = Query(default=None, description="按产物引用过滤"),
) -> dict[str, Any]:
    """查询产物反查绑定记录，支持按来源文件或产物引用过滤。"""
    records = ExtractLineageService().list_bindings()
    if source:
        records = [
            record for record in records
            if source_path_matches(source, record.get("来源文件", ""))
            or artifact_references_match(source, record.get("产物引用", ""))
        ]
    if artifact:
        records = [
            record for record in records
            if artifact_references_match(artifact, record.get("产物引用", ""))
        ]
    return {"items": records, "total": len(records)}


@router.get("/parse_retry/{retry_id}/rerun-source")
def rerun_source(
    retry_id: str,
    artifact: str = Query(default="", description="产物引用；省略时用重采任务上登记的引用"),
) -> dict[str, Any]:
    """重跑入口第一步：只做反查，不创建解析任务。"""
    service = ParseRetryService()
    if not service.find_retry(retry_id):
        raise HTTPException(status_code=404, detail=f"重采任务 {retry_id} 不存在")
    return service.prepare_rerun(retry_id, artifact, create_parse_task=False)


@router.post("/parse_retry/{retry_id}/prepare-rerun")
def prepare_rerun(
    retry_id: str,
    artifact: str = Query(default="", description="产物引用；省略时用重采任务上登记的引用"),
) -> dict[str, Any]:
    """从产物反查绑定记录选入重跑；只有已核实的唯一来源才创建解析任务。"""
    service = ParseRetryService()
    if not service.find_retry(retry_id):
        raise HTTPException(status_code=404, detail=f"重采任务 {retry_id} 不存在")
    return service.prepare_rerun(retry_id, artifact)
