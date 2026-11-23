"""格式解析服务：PDF/Word/HTML等格式文档的解析器选择、页码范围切分与输出格式控制。

解析完成后写入已解析文件，自动触发分块切分和版式还原。
"""
from __future__ import annotations

import time
import re
from typing import Any

from app.services.artifact_refs import canonical_reference
from app.services.base import BaseModuleService, _ensure_dir
from app.store import store


class FormatParseService(BaseModuleService):
    MODULE = "format_parse"
    ENTITY = "解析任务"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["任务编号", "文档编号", "解析器类型", "页码范围", "解析模式", "输出格式", "超时秒数", "解析器版本"]
    REQUIRED_FIELDS = ["任务编号", "文档编号", "解析器类型"]
    STATUS_ORDER = ["草稿", "待解析", "解析中", "已完成", "解析失败"]
    ACTION_RULES = {"启动解析": "解析中", "标记完成": "已完成", "标记失败": "解析失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    # 支持的解析器类型与对应正则
    PARSER_REGISTRY = {
        "PDF": {"description": "PDF文档解析器，提取文本段落与页面布局"},
        "DOCX": {"description": "Word文档解析器，提取段落样式与层级结构"},
        "HTML": {"description": "HTML文档解析器，提取标签语义与DOM树"},
# 数据产物：从接入批次中批量创建解析任务数据集
        "TXT": {"description": "纯文本文档解析器，按字符集编码读取"},
        "MARKDOWN": {"description": "Markdown解析器，提取标题层级与代码块"},
    }

    def create_from_batch(self, batch_key: str) -> dict[str, Any]:
        """从接入批次创建解析任务，读取批次中的文档清单逐个生成解析任务。"""
        # 读取已校验批次文件
        batch_file = self._data_dir("batches") / "validated.jsonl"
        batches = self._read_jsonl(batch_file)
        matched = [b for b in batches if b.get("batch_key") == batch_key]
        if not matched:
            return {"error": f"批次 {batch_key} 未找到校验记录，可能未通过校验"}
        batch = matched[0]

        batch_rows = [r for r in store.rows("file_intake") if r.get("批次编号") == batch_key]
        if not batch_rows:
            return {"error": f"批次 {batch_key} 未在file_intake登记"}

        batch_row = batch_rows[0]
        file_list = batch_row.get("文件清单", "").split(",")
        file_type = batch_row.get("文件类型", "PDF")
        parser_type = self.PARSER_REGISTRY.get(file_type, self.PARSER_REGISTRY["PDF"])

        created = []
        for i, fname in enumerate(file_list):
            fname = fname.strip()
            if not fname:
                continue
            task_values = {
                "任务编号": f"PARSE-{batch_key}-{i+1:03d}",
                "文档编号": fname,
                "解析器类型": file_type,
                "页码范围": "",
                "解析模式": "full",
                "输出格式": "jsonl",
                "超时秒数": "300",
                "解析器版本": "v2.1.0",
            }
            result, missing = self.create_entry(task_values)
            if result:
                created.append(result["任务编号"])
        return {"batch_key": batch_key, "tasks_created": len(created), "task_ids": created}

    def execute_parse(self, task_id: int) -> dict[str, Any]:
        """执行文档解析任务，将解析产物持久化为JSONL文件，同时触发下游分块切分。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
# 数据操作：解析文档生成已解析页面产物与区域检测
            return {"error": f"解析任务 {task_id} 不存在"}
        if task.get("status") not in ("草稿", "待解析", "解析中"):
            return {"error": f"解析任务 {task_id} 状态不允许执行解析，当前状态: {task.get('status')}"}

        task["status"] = "解析中"
        start_time = time.time()
        doc_id = task["文档编号"]
        parser = task["解析器类型"]
        upstream = dict(task.get("上游身份") or {})
        if not upstream:
            upstream = self._resolve_upstream_identity(task)

        try:
            # 模拟文档解析
            content = self._simulate_doc_content(doc_id, paragraphs=8)
            pages = self._simulate_pages(content)
            table_regions = self._detect_table_regions(pages)
            image_regions = self._detect_image_regions(pages)

            # 写入解析产物；被重跑沿用的上游身份一并写入结果记录
            output_path = self._data_dir("parsed") / f"{doc_id}_parsed.jsonl"
            parsed_records = []
            for p in pages:
                record = {
                    "doc_id": doc_id,
# 数据产物：解析page产物数据写入jsonl文件
                    "page_num": p["page"],
                    "paragraphs": len(p["paragraphs"]),
                    "text_length": p["text_length"],
                    "has_tables": p["has_tables"],
                    "has_images": p["has_images"],
                    "parser": parser,
                    "content_hash": self._compute_checksum("\n".join(p["paragraphs"])),
                }
                if upstream:
                    record.update({
                        "upstream_source": upstream.get("来源文件", ""),
                        "upstream_document": upstream.get("文档身份", ""),
                        "upstream_artifact": upstream.get("产物引用", ""),
                        "upstream_binding": upstream.get("绑定编号", ""),
                        "upstream_task": upstream.get("解析任务编号", ""),
                        "rerun_from": upstream.get("重采编号", ""),
                    })
                parsed_records.append(record)
            written = self._write_jsonl(output_path, parsed_records)

            # 写入表格区域到共享产物，供table_extract读取
            if table_regions:
                table_path = self._data_dir("layout") / f"{doc_id}_tables.jsonl"
                self._write_jsonl(table_path, table_regions)

            # 写入图片区域到共享产物，供ocr_engine读取
            if image_regions:
                image_path = self._data_dir("layout") / f"{doc_id}_images.jsonl"
                self._write_jsonl(image_path, image_regions)

            elapsed = time.time() - start_time
# 数据操作：解析完毕写入检查点，存储数据集至持久化文件
            task["status"] = "已完成"
            task["pending"] = False
            if elapsed > float(task.get("超时秒数") or 300):
                task["abnormal"] = True

            if upstream:
                # 新解析结果记录的沿用身份同时停留在任务上，便于再次核查
                task["上游身份"] = upstream
                task["重采编号"] = upstream.get("重采编号", "")
            # 写入检查点
            self._write_checkpoint(f"parse_{doc_id}", {
                "task_id": task_id,
                "doc_id": doc_id,
                "pages": len(pages),
                "records_written": written,
                "elapsed": elapsed,
                "table_count": len(table_regions),
                "image_count": len(image_regions),
                "upstream": upstream,
            })

            # 跨模块自动触发
            triggers = {}
            trig_chunk = self._trigger_chunk_split(f"{doc_id}_parsed")
            triggers["chunk_split"] = trig_chunk
            trig_layout = self._trigger_layout_restore(doc_id)
            triggers["layout_restore"] = trig_layout
            trig_quality = self._trigger_quality_check(doc_id)
            triggers["extract_quality"] = trig_quality
            trig_lineage = self._trigger_lineage_record(doc_id)
            triggers["extract_lineage"] = trig_lineage

            return {
                "task_id": task_id,
                "doc_id": doc_id,
                "pages": len(pages),
                "records_written": written,
                "upstream": upstream,
# 【数据工程】解析操作完成后触发下游分块数据集消费
                "elapsed": round(elapsed, 2),
                "output": str(output_path),
                "triggers": triggers,
            }
        except Exception as e:
            task["status"] = "解析失败"
            task["abnormal"] = True
            return {"task_id": task_id, "error": str(e), "doc_id": doc_id}

    def _resolve_upstream_identity(self, task: dict[str, Any]) -> dict[str, Any]:
        """重跑任务通过产物反查绑定记录确定源数据身份，供新解析结果沿用。

        只有反查结果唯一且已核实时才返回可沿用的上游身份；存在歧义时
        返回空字典，让本次解析按自身文档编号独立记录，不臆测上游。
        """
        retry_id = str(task.get("重采编号") or "")
        if not retry_id:
            return {}
        retry = self._find_retry_task(retry_id)
        # 重跑入口通常从产物反查绑定记录选入源数据，引用就在重采任务的产物引用上。
        # 反查不到唯一来源时，不把不相关的文档身份写成这次解析的上游。
        resolution = self._lineage_resolution(retry, task)
        if not resolution:
            return {}
        source, artifact_reference = resolution
        return {
            "来源文件": source.get("来源文件", ""),
            "文档身份": source.get("文档身份", ""),
            "产物引用": artifact_reference,
            "绑定编号": source.get("绑定编号", ""),
            "解析任务编号": source.get("解析任务编号", ""),
            "重采编号": retry_id,
            "依据": source.get("依据", []),
        }

    def _lineage_resolution(
        self, retry: dict[str, Any], task: dict[str, Any]
    ) -> tuple[dict[str, Any], str] | None:
        """反查重采任务选入的源数据；只有唯一且已核实返回 (源数据, 产物引用)。"""
        artifact_reference = str(retry.get("产物引用") or "")
        if not artifact_reference:
            return None
        try:
            lineage_cls = self._safe_import(
                "app.services.extract_lineage", "ExtractLineageService")
            resolved = lineage_cls().resolve_source_candidates(artifact_reference)
        except Exception:
            return None
        if resolved.get("status") != "已确定" or not resolved.get("resolved"):
            return None
        source = resolved["resolved"]
        # 反查出的源数据必须就是本次要重跑的文档，避免把别的文档身份挂上来
        source_identity = canonical_reference(source.get("文档身份", ""))
        task_identity = canonical_reference(task.get("文档编号", ""))
        if source_identity and task_identity and source_identity != task_identity:
            return None
        return source, artifact_reference

    def _find_retry_task(self, retry_id: str) -> dict[str, Any]:
        """在重采任务文件里按编号查找任务，供重跑入口读取源数据引用。"""
        for rf in sorted(self._data_dir("retries").glob("*.jsonl")):
            for task in self._read_jsonl(rf):
                if str(task.get("重采编号")) == retry_id:
                    return task
        return {}

    def _simulate_pages(self, content: list[str]) -> list[dict[str, Any]]:
        """模拟将文本内容切分为多页文档，每页含段落、表格和图片区域标记。"""
        pages = []
        for page_num in range(1, 4):
            start = (page_num - 1) * (len(content) // 3 + 1)
            end = min(page_num * (len(content) // 3 + 1), len(content))
            page_lines = content[start:end]
            pages.append({
                "page": page_num,
                "paragraphs": page_lines,
                "text_length": sum(len(l) for l in page_lines),
                "has_tables": page_num % 2 == 1,
                "has_images": page_num == 2,
            })
        return pages

    def _detect_table_regions(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """检测页面中的表格区域边界并生成结构化输出标记。"""
        regions = []
        for p in pages:
            if p["has_tables"]:
                regions.append({
                    "page": p["page"],
                    "doc_id": "from_parse",
                    "bbox": f"({p['page']*10}, {p['page']*8}, {p['page']*10+200}, {p['page']*8+80})",
                    "rows": 4,
                    "cols": 5,
                    "header_row": 1,
                    "merged_cells": 2,
                })
        return regions

    def _detect_image_regions(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """检测页面中的图片区域边界并标记识别语言和精度要求。"""
        regions = []
        for p in pages:
            if p["has_images"]:
                regions.append({
                    "page": p["page"],
                    "doc_id": "from_parse",
                    "bbox": f"({p['page']*5}, {p['page']*4+1}, {p['page']*5+150}, {p['page']*4+151})",
                    "language": "chinese",
                    "precision": "high",
                })
        return regions