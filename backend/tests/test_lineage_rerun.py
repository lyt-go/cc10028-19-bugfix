"""抽取血缘与重跑链路的回归测试：引用精确匹配、产物反查与上游身份沿用。"""
import os
import sys
import uuid as _uuid

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.services.artifact_refs import canonical_reference
from app.services.extract_lineage import ExtractLineageService
from app.services.format_parse import FormatParseService
from app.services.parse_retry import ParseRetryService


def _uid(prefix="DOC"):
    return f"{prefix}-{_uuid.uuid4().hex[:8]}"


def _build_doc_artifacts(doc_id: str) -> None:
    """跑一遍解析并登记血缘，产出解析产物与绑定记录。"""
    parse_svc = FormatParseService()
    task, _ = parse_svc.create_entry({
        "任务编号": f"PARSE-{doc_id}", "文档编号": doc_id, "解析器类型": "PDF",
    })
    result = parse_svc.execute_parse(task["id"])
    assert "error" not in result, result.get("error", "")
    ExtractLineageService().record_from_parse(doc_id)


def _write_retry(retry_id: str, reference: str, batch_key: str = "BT") -> None:
    ParseRetryService()._write_jsonl(
        ParseRetryService()._data_dir("retries") / f"{batch_key}_retries.jsonl",
        [{
            "重采编号": retry_id,
            "失败环节": "格式解析",
            "原始批次": batch_key,
            "失败原因": "测试",
            "重试次数": "0",
            "重采范围": reference,
            "恢复游标": "0",
            "重采结果": "",
            "产物引用": reference,
            "status": "待重采",
        }],
    )


class TestArtifactReferenceIdentity:
    """相似编号不能互相串档"""

    def test_similar_numbers_are_distinct(self):
        doc_a, doc_b = _uid("doc-1"), _uid("doc-1") + "0"
        _build_doc_artifacts(doc_a)
        _build_doc_artifacts(doc_b)
        service = ExtractLineageService()
        rows, total = service.list_entries(filters={"源文件": doc_a})
        assert total == 1
        assert rows[0]["源文件"] == doc_a
        assert all(doc_b not in str(row.get("源文件", "")) for row in rows)

    def test_canonical_reference_strips_artifact_suffix(self):
        assert canonical_reference("parsed/doc-12_parsed.jsonl") == "doc-12"
        assert canonical_reference("doc-12_parsed_chunks") == "doc-12"
        assert canonical_reference("format_parse::doc-12") == "doc-12"
        assert canonical_reference("doc-120_parsed.jsonl") == "doc-120"

    def test_artifact_query_marks_ownership(self):
        doc_id = _uid("doc")
        _build_doc_artifacts(doc_id)
        service = ExtractLineageService()
        rows, total = service.list_entries(filters={"分块节点": doc_id})
        assert total == 1
        assert rows[0]["归属状态"] == "已核实"
        assert rows[0]["可直接重跑"] is True


class TestArtifactBinding:
    """产物反查绑定记录"""

    def test_record_from_parse_writes_bindings(self):
        doc_id = _uid("doc")
        _build_doc_artifacts(doc_id)
        service = ExtractLineageService()
        bindings = [b for b in service.list_bindings() if b["来源文件"] == doc_id]
        assert bindings, "解析后应登记产物反查绑定记录"
        assert {b["产物种类"] for b in bindings} >= {"解析节点"}
        resolved = service.resolve_source_candidates(f"parsed/{doc_id}_parsed.jsonl")
        assert resolved["status"] == "已确定"
        assert resolved["resolved"]["文档身份"] == canonical_reference(doc_id)

    def test_short_history_reference_resolves(self):
        doc_id = _uid("doc")
        _build_doc_artifacts(doc_id)
        service = ExtractLineageService()
        short = service.resolve_source_candidates(f"{doc_id}_parsed")
        assert short["status"] == "已确定"
        assert canonical_reference(short["source_file"]) == canonical_reference(doc_id)


class TestAmbiguousReference:
    """旧引用出现多个来源候选时只保留调查材料"""

    def test_ambiguous_reference_lists_pending_items(self):
        doc_id = _uid("doc")
        service = ExtractLineageService()
        service.record_artifact_binding(
            source_file=f"a/{doc_id}", artifact_reference=f"parsed/{doc_id}_parsed.jsonl")
        service.record_artifact_binding(
            source_file=f"b/{doc_id}", artifact_reference=f"parsed/{doc_id}_parsed.jsonl")
        resolution = service.resolve_source_candidates(f"{doc_id}_parsed")
        assert resolution["status"] == "存在歧义"
        assert resolution["ambiguous"] is True
        assert resolution["resolved"] is None
        assert len(resolution["pending_items"]) == 2

    def test_ambiguous_source_is_not_rerun(self):
        doc_id = _uid("doc")
        service = ExtractLineageService()
        service.record_artifact_binding(
            source_file=f"a/{doc_id}", artifact_reference=f"parsed/{doc_id}_parsed.jsonl")
        service.record_artifact_binding(
            source_file=f"b/{doc_id}", artifact_reference=f"parsed/{doc_id}_parsed.jsonl")
        retry_id = f"RETRY-{_uuid.uuid4().hex[:6]}"
        _write_retry(retry_id, f"{doc_id}_parsed")
        retry_svc = ParseRetryService()
        prepared = retry_svc.prepare_rerun(retry_id)
        assert prepared["可直接重跑"] is False
        assert prepared["待核项"]
        assert "解析任务" not in prepared
        executed = retry_svc.execute_retry(retry_id)
        assert executed["可直接重跑"] is False


class TestRerunUpstreamIdentity:
    """重跑入口从产物反查选源，并在新解析结果记录沿用身份"""

    def test_rerun_records_upstream_identity(self):
        doc_id = _uid("doc")
        _build_doc_artifacts(doc_id)
        lineage_svc = ExtractLineageService()
        binding = next(
            b for b in lineage_svc.list_bindings()
            if b["来源文件"] == doc_id and b["产物种类"] == "解析节点"
        )
        retry_id = f"RETRY-{_uuid.uuid4().hex[:6]}"
        _write_retry(retry_id, binding["产物引用"])

        retry_svc = ParseRetryService()
        prepared = retry_svc.prepare_rerun(retry_id)
        assert prepared["可直接重跑"] is True
        assert prepared["源数据"]["绑定编号"] == binding["绑定编号"]

        parse_task = prepared["解析任务"]
        parse_svc = FormatParseService()
        result = parse_svc.execute_parse(parse_task["task_id"])
        assert result["upstream"]["绑定编号"] == binding["绑定编号"]
        assert canonical_reference(result["upstream"]["来源文件"]) == canonical_reference(doc_id)

        records = parse_svc._read_jsonl(
            parse_svc._data_dir("parsed") / f"{doc_id}_parsed.jsonl")
        assert any(r.get("upstream_binding") == binding["绑定编号"] for r in records)
        assert any(r.get("rerun_from") == retry_id for r in records)
