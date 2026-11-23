"""失败重采服务：解析、识别与抽取失败环节的失败原因归档与按范围重采恢复。

从解析失败任务中创建重采任务，按失败环节恢复，支持断点续采。

重跑入口统一先做产物反查：只有产物反查绑定记录能确定到唯一且已核实的
来源时，才把源数据选入重跑；旧引用出现歧义时保留待核材料，不按列表
顺序随意挑一个来源。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.artifact_refs import canonical_reference, normalize_reference
from app.services.base import BaseModuleService
from app.store import store


class ParseRetryService(BaseModuleService):
    MODULE = "parse_retry"
    ENTITY = "重采任务"
    OPERATION_TERM = "重采"
    LIST_FIELDS = ["重采编号", "失败环节", "原始批次", "失败原因", "重试次数", "重采范围", "恢复游标", "重采结果"]
    REQUIRED_FIELDS = ["重采编号", "失败环节", "原始批次"]
    STATUS_ORDER = ["草稿", "待重采", "重采中", "已完成", "已放弃"]
    ACTION_RULES = {"启动重采": "待重采", "标记完成": "已完成", "标记放弃": "已放弃"}
    NEGATIVE_ACTIONS = ["标记放弃"]

    # 反查状态 → 能否直接重跑
    RERUNNABLE_STATUS = "已确定"

    # ------------------------------------------------------------------
    # 重跑入口：产物反查绑定记录 → 选入重跑的源数据
    # ------------------------------------------------------------------
    def _lineage_service(self):
        cls = self._safe_import("app.services.extract_lineage", "ExtractLineageService")
        return cls()

    def resolve_rerun_source(self, artifact_reference: str) -> dict[str, Any]:
        """重跑入口第一步：按产物引用反查绑定记录，判断能否确定源数据。"""
        resolution = self._lineage_service().resolve_source_candidates(artifact_reference)
        runnable = resolution.get("status") == self.RERUNNABLE_STATUS and bool(resolution.get("resolved"))
        return {
            "产物引用": resolution.get("artifact", normalize_reference(artifact_reference)),
            "状态": resolution.get("status"),
            "可直接重跑": runnable,
            "源数据": resolution.get("resolved") if runnable else None,
            "候选": resolution.get("candidates", []),
            "待核项": resolution.get("pending_items", []),
            "说明": resolution.get("message", ""),
        }

    def prepare_rerun(
        self,
        retry_id: str,
        artifact_reference: str = "",
        *,
        create_parse_task: bool = True,
    ) -> dict[str, Any]:
        """把已核实的产物绑定选入重跑，并在新解析任务上登记沿用的上游身份。

        引用存在歧义时不创建解析任务，只返回待核项清单，交由操作员核实。
        """
        retry = self.find_retry(retry_id)
        if not retry:
            return {"error": f"重采任务 {retry_id} 不存在", "retry_id": retry_id}

        reference = normalize_reference(artifact_reference or retry.get("产物引用", ""))
        if not reference:
            return {
                "error": f"重采任务 {retry_id} 未指定产物引用，无法反查源数据",
                "retry_id": retry_id,
                "可直接重跑": False,
            }

        resolution = self.resolve_rerun_source(reference)
        if not resolution["可直接重跑"]:
            base = {
                "retry_id": retry_id,
                "产物引用": reference,
                "状态": resolution["状态"],
                "可直接重跑": False,
                "待核项": resolution["待核项"],
                "候选": resolution["候选"],
                "message": resolution["说明"],
            }
            if resolution["待核项"]:
                # 保留可调查材料，写入重采任务方便操作员接着处理
                self._update_retry(retry_id, {
                    "产物引用": reference,
                    "重采结果": "待核实：产物引用存在多个来源候选",
                    "待核项": resolution["待核项"],
                })
            return base

        source = resolution["源数据"]
        self._update_retry(retry_id, {"产物引用": reference})
        result: dict[str, Any] = {
            "retry_id": retry_id,
            "产物引用": reference,
            "状态": resolution["状态"],
            "可直接重跑": True,
            "源数据": source,
            "上游身份": {
                "来源文件": source.get("来源文件", ""),
                "文档身份": source.get("文档身份", ""),
                "绑定编号": source.get("绑定编号", ""),
                "解析任务编号": source.get("解析任务编号", ""),
                "产物引用": reference,
                "重采编号": retry_id,
            },
            "message": resolution["说明"],
        }
        if create_parse_task:
            result["解析任务"] = self._create_rerun_parse_task(retry, source, reference, retry_id)
        return result

    def _create_rerun_parse_task(
        self,
        retry: dict[str, Any],
        source: dict[str, Any],
        artifact_reference: str,
        retry_id: str,
    ) -> dict[str, Any]:
        """为重跑创建解析任务，并把沿用的上游身份挂在任务上。

        同一重采任务重复调用只复用既有解析任务，不重复登记。
        """
        doc_id = source.get("文档编号") or source.get("文档身份") or ""
        if not doc_id:
            return {"error": "反查结果缺少文档编号，无法创建解析任务"}
        parse_cls = self._safe_import("app.services.format_parse", "FormatParseService")
        parse_svc = parse_cls()
        task_id_value = f"PARSE-RERUN-{retry_id}"
        for row in store.rows("format_parse"):
            if str(row.get("任务编号")) == task_id_value:
                return {
                    "task_id": row["id"],
                    "任务编号": row.get("任务编号"),
                    "文档编号": row.get("文档编号"),
                    "reused": True,
                }
        upstream = {
            "来源文件": source.get("来源文件", ""),
            "文档身份": source.get("文档身份", ""),
            "绑定编号": source.get("绑定编号", ""),
            "解析任务编号": source.get("解析任务编号", ""),
            "产物引用": artifact_reference,
            "重采编号": retry_id,
        }
        task, missing = parse_svc.create_entry({
            "任务编号": task_id_value,
            "文档编号": doc_id,
            "解析器类型": retry.get("解析器类型") or "PDF",
            "页码范围": "",
            "解析模式": "full",
            "输出格式": "jsonl",
            "超时秒数": "300",
            "解析器版本": "v2.1.0",
        })
        if task is None:
            return {"error": f"解析任务缺少必填字段：{'、'.join(missing)}"}
        # 上游身份不一定属于列表字段，直接挂在任务记录上随解析结果沿用
        task["重采编号"] = retry_id
        task["上游身份"] = upstream
        return {"task_id": task["id"], "任务编号": task["任务编号"], "文档编号": task["文档编号"]}

    # ------------------------------------------------------------------
    # 重采任务读写
    # ------------------------------------------------------------------
    def find_retry(self, retry_id: str) -> dict[str, Any]:
        """按编号在持久化文件与内存登记表中查找重采任务。"""
        for rf in sorted(self._data_dir("retries").glob("*.jsonl")):
            for task in self._read_jsonl(rf):
                if str(task.get("重采编号")) == str(retry_id):
                    return task
        for row in store.rows(self.MODULE):
            if str(row.get("重采编号")) == str(retry_id):
                return row
        return {}

    def _update_retry(self, retry_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """更新重采任务的字段，同时回写文件记录。"""
        import json

        for rf in sorted(self._data_dir("retries").glob("*.jsonl")):
            tasks = self._read_jsonl(rf)
            changed = False
            for task in tasks:
                if str(task.get("重采编号")) == str(retry_id):
                    task.update(values)
                    changed = True
            if changed:
                with open(rf, "w", encoding="utf-8") as f:
                    for t in tasks:
                        f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")
                return next(t for t in tasks if str(t.get("重采编号")) == str(retry_id))
        for row in store.rows(self.MODULE):
            if str(row.get("重采编号")) == str(retry_id):
                row.update(values)
                return row
        return {}

    # ------------------------------------------------------------------
    # 从失败任务创建重采任务
    # ------------------------------------------------------------------
    def create_retry_from_failures(self, batch_key: str) -> dict[str, Any]:
        """从解析失败的批次中创建重采任务，按失败环节归档。

        重采范围优先取自失败解析任务能确定到的源文件；若产物引用存在歧义，
        保留原引用并标记待核，不在多个来源里挑一个。
        """
        retry_tasks = []
        retry_counter = 0

        # 检查格式解析失败
        parse_rows = [r for r in store.rows("format_parse") if r.get("status") == "解析失败" and r.get("abnormal")]
        for pr in parse_rows:
            retry_counter += 1
            scope, reference, pending = self._retry_scope_for_parse(pr)
            retry_tasks.append({
                "重采编号": f"RETRY-{retry_counter:05d}",
                "失败环节": "格式解析",
                "原始批次": batch_key,
                "失败原因": f"解析任务 {pr.get('任务编号', '?')} 异常失败",
                "重试次数": "0",
                "重采范围": scope,
# 数据操作：重采失败任务数据按环节归档创建重采任务
                "恢复游标": "0",
                "重采结果": "",
                "产物引用": reference,
                "待核项": pending,
                "status": "待重采",
            })

        # 检查OCR失败
        ocr_rows = [r for r in store.rows("ocr_engine") if r.get("status") == "识别失败" and r.get("abnormal")]
        for ocr in ocr_rows:
            retry_counter += 1
            retry_tasks.append({
                "重采编号": f"RETRY-{retry_counter:05d}",
                "失败环节": "OCR识别",
                "原始批次": batch_key,
                "失败原因": f"识别任务 {ocr.get('任务编号', '?')} 异常失败",
                "重试次数": "0",
                "重采范围": ocr.get("图片区域", "全量"),
                "恢复游标": "0",
                "重采结果": "",
                "产物引用": "",
                "待核项": [],
                "status": "待重采",
            })

        if not retry_tasks:
            return {"batch_key": batch_key, "retry_tasks": 0, "message": "未发现需重试的失败任务"}

        output_path = self._data_dir("retries") / f"{batch_key}_retries.jsonl"
        written = self._write_jsonl(output_path, retry_tasks)

        self._write_checkpoint(f"retry_{batch_key}", {
            "batch_key": batch_key,
            "tasks_created": len(retry_tasks),
# 数据产物：重采任务清单写入retries目录持久化文件
            "by_stage": {
                "格式解析": sum(1 for t in retry_tasks if t["失败环节"] == "格式解析"),
                "OCR识别": sum(1 for t in retry_tasks if t["失败环节"] == "OCR识别"),
            },
            "pending_review": sum(1 for t in retry_tasks if t.get("待核项")),
        })

        return {
            "batch_key": batch_key,
            "retry_tasks": len(retry_tasks),
            "records_written": written,
            "pending_review": sum(1 for t in retry_tasks if t.get("待核项")),
            "output": str(output_path),
        }

    def _retry_scope_for_parse(self, parse_row: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
        """确定失败解析任务的重采范围：能用绑定记录定位就定位，否则保留待核项。"""
        doc_id = normalize_reference(parse_row.get("文档编号", ""))
        if not doc_id:
            return "全量", "", []
        reference = f"parsed/{doc_id}_parsed.jsonl"
        try:
            resolution = self.resolve_rerun_source(reference)
        except Exception:
            return doc_id, reference, []
        if resolution["可直接重跑"]:
            source = resolution["源数据"] or {}
            return (
                source.get("来源文件") or doc_id,
                reference,
                resolution["待核项"],
            )
        return doc_id, reference, resolution["待核项"]

    def execute_retry(self, retry_id: str) -> dict[str, Any]:
        """执行重采任务，从上次失败位点恢复。

        解析环节的重跑要求本任务已通过产物反查确定唯一来源；旧引用存在
        歧义时返回待核项，不推进游标也不消耗重试次数。
        """
        task = self.find_retry(retry_id)
        if not task:
            return {"error": f"重采任务 {retry_id} 不存在"}

        if task.get("失败环节") == "格式解析":
            reference = normalize_reference(task.get("产物引用", ""))
            resolution = self.resolve_rerun_source(reference) if reference else {
                "可直接重跑": False, "待核项": [], "状态": "未指定",
                "说明": "重采任务缺少产物引用，请先从产物反查绑定记录选入源数据",
            }
            has_pending = bool(resolution.get("待核项"))
            if has_pending:
                self._update_retry(retry_id, {
                    "重采结果": "待核实：产物引用存在多个来源候选",
                    "待核项": resolution["待核项"],
                })
            if not resolution.get("可直接重跑"):
                return {
                    "retry_id": retry_id,
                    "status": task.get("status", "待重采"),
                    "可直接重跑": False,
                    "状态": resolution.get("状态"),
                    "待核项": resolution.get("待核项", []),
                    "message": resolution.get("说明") or "无法确定重跑来源",
                    "ambiguous": has_pending,
                }

        # 从检查点恢复游标
        cp_name = f"retry_{retry_id}"
        cp = self._read_checkpoint(cp_name) or {"retry_id": retry_id, "attempts": 0, "offset": 0}

        cp["attempts"] = cp.get("attempts", 0) + 1
        cp["offset"] = cp.get("offset", 0)
        cp["last_attempt_at"] = time.time()

        max_retries = 3
        if cp["attempts"] > max_retries:
            self._update_retry(retry_id, {
                "重采结果": f"放弃：已达最大重试次数({max_retries})",
                "status": "已放弃",
                "重试次数": str(cp["attempts"]),
            })
            return {"retry_id": retry_id, "status": "已放弃", "reason": f"超过{max_retries}次重试上限"}

        result: dict[str, Any] = {
            "retry_id": retry_id,
            "status": "已完成",
            "attempts": cp["attempts"],
            "recovered_offset": cp["offset"] + 100,
        }

        if task.get("失败环节") == "格式解析":
            # 解析环节的重跑真正走重跑入口：选入源数据并在新结果记录上游身份
            prepared = self.prepare_rerun(retry_id)
            parse_task = prepared.get("解析任务") or {}
            if parse_task.get("task_id"):
                parse_cls = self._safe_import("app.services.format_parse", "FormatParseService")
                parse_svc = parse_cls()
                existing = store.find("format_parse", parse_task["task_id"]) or {}
                if existing.get("status") in ("已完成", "解析中"):
                    # 同一重采再次执行时沿用已完成的解析结果，不重复解析
                    result["解析任务"] = parse_task
                    result["upstream"] = dict(existing.get("上游身份") or {})
                else:
                    parsed = parse_svc.execute_parse(parse_task["task_id"])
                    result["解析任务"] = parse_task
                    result["upstream"] = parsed.get("upstream", {})
                    if parsed.get("error"):
                        result["status"] = "解析失败"
                        self._update_retry(retry_id, {
                            "重采结果": f"失败：{parsed['error']}",
                            "status": "待重采",
                            "重试次数": str(cp["attempts"]),
                        })
                        self._write_checkpoint(cp_name, cp)
                        result["error"] = parsed["error"]
                        return result

        # 模拟重采成功
        self._update_retry(retry_id, {
            "重采结果": "成功",
            "status": "已完成",
            "重试次数": str(cp["attempts"]),
            "恢复游标": str(cp["offset"] + 100),
        })
        self._write_checkpoint(cp_name, cp)

        return result

    def get_failure_summary(self, batch_key: str) -> dict[str, Any]:
        """获取指定批次的失败汇总，按环节分组统计失败数量和原因。"""
        # 收集各环节的失败记录
        parse_failures = [r for r in store.rows("format_parse") if r.get("status") == "解析失败"]
        ocr_failures = [r for r in store.rows("ocr_engine") if r.get("status") == "识别失败"]
        entity_failures = [r for r in store.rows("entity_recognize") if r.get("status") == "已驳回"]

        summary = {
            "batch_key": batch_key,
            "total_failures": len(parse_failures) + len(ocr_failures) + len(entity_failures),
            "by_stage": {
                "格式解析": len(parse_failures),
                "OCR识别": len(ocr_failures),
                "实体识别": len(entity_failures),
            },
            "retryable": len(parse_failures) + len(ocr_failures),
        }
        return summary
