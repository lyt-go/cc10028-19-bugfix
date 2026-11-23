"""抽取血缘服务：源文件到解析、分块、抽取产物与下游消费方的全链路血缘记录。

记录文档从接入到抽取全过程的血缘链路，提供数据产品的追溯能力。

产物引用一律按「文档身份」精确比对（见 ``artifact_refs``），不再用字符串
包含判断，因此 ``doc-12`` 不会命中 ``doc-120``。解析重跑通过产物反查绑定
记录确定源数据；历史引用出现多个来源候选时只保留可调查材料，交由操作员
确定到具体待核项，绝不按列表顺序挑一个来源重跑。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.artifact_refs import (
    artifact_references_match,
    canonical_reference,
    is_placeholder,
    looks_like_reference,
    normalize_reference,
    reference_artifact_detail,
    reference_directory,
    references_match,
    source_path_matches,
    source_reference_key,
)
from app.services.base import DATA_ROOT, BaseModuleService
from app.store import store


# 可以携带“文档身份”的引用字段
REFERENCE_FIELDS = ("源文件", "解析节点", "分块节点", "抽取产物")

# 产物目录 → 产物种类，用于把文件路径回溯成血缘节点
ARTIFACT_KINDS: dict[str, str] = {
    "parsed": "解析节点",
    "chunks": "分块节点",
    "entities": "实体抽取",
    "relations": "关系抽取",
    "graph": "图谱节点",
    "vectors": "向量索引",
    "layout": "版式节点",
}


class ExtractLineageService(BaseModuleService):
    MODULE = "extract_lineage"
    ENTITY = "血缘链路"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["链路编号", "源文件", "解析节点", "分块节点", "抽取产物", "下游消费方", "血缘版本", "采集时间"]
    REQUIRED_FIELDS = ["链路编号", "源文件", "解析节点"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "确认生效": "已生效", "标记停用": "已停用"}
    NEGATIVE_ACTIONS = ["标记停用"]

    BINDING_FIELDS = [
        "绑定编号",
        "产物引用",
        "来源文件",
        "文档编号",
        "解析任务编号",
        "产物种类",
        "匹配方式",
        "绑定状态",
        "绑定时间",
    ]

    # ------------------------------------------------------------------
    # 列表查询：产物引用走身份精确匹配
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """查询血缘列表。

        引用型条件（``doc-12``、``parsed/doc-12_parsed.jsonl`` 等）按文档身份
        精确匹配，并给每条结果标注归属状态；普通中文检索词仍保留包含匹配。
        """
        rows = self._all_lineage_rows()
        for field, expected in (filters or {}).items():
            if field not in self.LIST_FIELDS or not str(expected).strip():
                continue
            if field in REFERENCE_FIELDS and looks_like_reference(expected):
                query_identity = canonical_reference(expected)
                matched: list[tuple[dict[str, Any], str]] = []
                for row in rows:
                    verdict = self._classify_match(row, field, query_identity)
                    if verdict:
                        matched.append((row, verdict))
                rows = [self._annotate(row, verdict, query_identity) for row, verdict in matched]
            else:
                needle = str(expected).casefold()
                rows = [row for row in rows if needle in str(row.get(field, "")).casefold()]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            needle = keyword.casefold()
            rows = [row for row in rows if needle in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def _all_lineage_rows(self) -> list[dict[str, Any]]:
        """合并内存登记与 lineage 目录中的链路记录，并把文件记录同步进仓库。

        血缘记录以持久化文件为准；内存里只有示例数据或旧登记时，列表和
        查询都必须能看到实际采集过的链路。同步按链路编号幂等：新链路补登记，
        已登记的只刷新链路字段，保留仓库里的状态流转结果。
        """
        repository_rows = store.rows(self.MODULE)
        by_id = {row.get("链路编号"): row for row in repository_rows}
        for lineage_file in sorted(self._data_dir("lineage").glob("*.jsonl")):
            if lineage_file.name == "bindings.jsonl":
                continue
            for record in self._read_jsonl(lineage_file):
                lineage_id = record.get("链路编号")
                if not lineage_id:
                    continue
                existing = by_id.get(lineage_id)
                if existing is None:
                    entry = dict(record)
                    entry.setdefault("status", "待校验")
                    entry.setdefault("pending", True)
                    entry.setdefault("abnormal", False)
                    entry["id"] = max(
                        (int(r.get("id", 0)) for r in repository_rows), default=0) + 1
                    repository_rows.append(entry)
                    by_id[lineage_id] = entry
                    continue
                for field in self.LIST_FIELDS:
                    if field in record:
                        existing[field] = record[field]
        return [dict(row) for row in repository_rows]

    def _classify_match(self, row: dict[str, Any], field: str, identity: str) -> str | None:
        """判断一行记录与查询身份的关系：确定归属 / 归属待核 / 不匹配。"""
        if not identity:
            return None
        if canonical_reference(row.get(field, "")) != identity:
            return None
        row_identity = canonical_reference(row.get("源文件", ""))
        if row_identity == identity:
            # 再检查下游节点是否引用了另一份文档（断链/串档）
            conflicts = [
                name for name in ("解析节点", "分块节点", "抽取产物")
                if not is_placeholder(row.get(name, ""))
                and canonical_reference(row.get(name, "")) != identity
            ]
            if not conflicts:
                return "已核实"
            return "待核"
        # 仅在下游节点上命中，但源文件指向别的文档：只作为调查材料返回
        return "待核"

    def _annotate(self, row: dict[str, Any], verdict: str, identity: str) -> dict[str, Any]:
        """给查询结果附加归属说明，不修改仓库中的原始记录。

        待核结果同时给出具体待核字段，操作员不必再回查整条链路就能定位
        到是哪一段引用和文档身份对不上。
        """
        annotated = dict(row)
        annotated["归属状态"] = verdict
        annotated["归属文档"] = identity
        if row.get("status") is None:
            # 查询结果用于前端列表时补齐状态字段，不影响仓库记录
            annotated["status"] = "待校验"
        pending: list[dict[str, str]] = []
        if canonical_reference(row.get("源文件", "")) != identity:
            pending.append({
                "字段": "源文件",
                "引用": normalize_reference(row.get("源文件", "")),
                "原因": "源文件指向另一份文档",
            })
        for name in ("解析节点", "分块节点", "抽取产物"):
            raw = row.get(name, "")
            if is_placeholder(raw):
                continue
            if canonical_reference(raw) != identity:
                pending.append({
                    "字段": name,
                    "引用": normalize_reference(raw),
                    "原因": f"{name}引用了另一份文档",
                })
        annotated["待核项"] = pending if verdict == "待核" else []
        if verdict == "已核实":
            annotated["归属依据"] = "源文件与产物引用身份一致"
        elif pending:
            annotated["归属依据"] = "，".join(item["原因"] for item in pending) + "，需人工核实"
        else:
            annotated["归属依据"] = "无法与文档身份相互印证，需人工核实"
        annotated["可直接重跑"] = verdict == "已核实"
        return annotated

    # ------------------------------------------------------------------
    # 产物反查绑定记录
    # ------------------------------------------------------------------
    def _binding_path(self):
        return self._data_dir("lineage") / "bindings.jsonl"

    def list_bindings(self) -> list[dict[str, Any]]:
        """读取全部产物反查绑定记录。"""
        return self._read_jsonl(self._binding_path())

    def record_artifact_binding(
        self,
        *,
        source_file: str,
        artifact_reference: str,
        artifact_kind: str = "",
        document_id: str = "",
        parse_task_key: str = "",
        match_type: str = "解析产物登记",
    ) -> dict[str, Any]:
        """登记一条产物反查绑定记录：产物引用 → 来源文件身份。

        同一 (产物引用身份, 来源文件身份) 只登记一次，重复执行不会漂移。
        """
        source_identity = canonical_reference(source_file)
        artifact_identity = canonical_reference(artifact_reference)
        record = {
            "绑定编号": f"BIND-{artifact_identity or 'UNKNOWN'}-{source_identity or 'UNKNOWN'}",
            "产物引用": normalize_reference(artifact_reference),
            "来源文件": normalize_reference(source_file),
            "文档编号": normalize_reference(document_id) or normalize_reference(source_file),
            "解析任务编号": normalize_reference(parse_task_key),
            "产物种类": artifact_kind,
            "匹配方式": match_type,
            "绑定状态": "已核实" if source_identity and artifact_identity else "待核实",
            "绑定时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        records = self.list_bindings()
        for existing in records:
            if (
                canonical_reference(existing.get("产物引用", "")) == artifact_identity
                and source_reference_key(existing.get("来源文件", "")) == source_reference_key(source_file)
            ):
                return existing
        records.append(record)
        self._overwrite_jsonl(self._binding_path(), records)
        return record

    def _overwrite_jsonl(self, filepath, records: list[dict[str, Any]]) -> int:
        """整体重写 JSONL（用于去重后的绑定记录），返回条数。"""
        import json

        with open(filepath, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return len(records)

    # ------------------------------------------------------------------
    # 正向记录血缘
    # ------------------------------------------------------------------
    def record_from_parse(self, parse_task_key: str) -> dict[str, Any]:
        """从解析任务记录全链路血缘，并登记各级产物的反查绑定记录。

        ``parse_task_key`` 可以是解析任务编号，也可以是文档编号；两种写法最终
        归一到同一个文档身份，产物文件按该身份命名。
        """
        parse_task = self._find_parse_task(parse_task_key, canonical_reference(parse_task_key))
        document_id = normalize_reference(parse_task.get("文档编号", "")) if parse_task else ""
        task_key = str(parse_task.get("任务编号", "")) if parse_task else str(parse_task_key)
        # 源文件保留登记时的原始写法（大小写、扩展名），身份比较时再归一。
        # 没有对应解析任务时，传入的键本身就是产物身份，先剥离产物后缀。
        source_file = (
            normalize_reference(document_id)
            or canonical_reference(parse_task_key)
            or normalize_reference(parse_task_key)
        )

        # 产物文件：身份一律以解析产物文件名为准
        artifact_files = {
            "解析节点": self._data_dir("parsed") / f"{source_file}_parsed.jsonl",
            "分块节点": self._data_dir("chunks") / f"{source_file}_parsed_chunks.jsonl",
            "实体抽取": self._data_dir("entities") / f"{source_file}_parsed_entities.jsonl",
            "关系抽取": self._data_dir("relations") / f"{source_file}_parsed_relations.jsonl",
            "图谱节点": self._data_dir("graph") / f"{source_file}_parsed_graph.jsonl",
            "向量索引": self._data_dir("vectors") / f"{source_file}_parsed_vectors.jsonl",
        }
        exists = {kind: path.exists() for kind, path in artifact_files.items()}

        nodes = []
        if source_file:
            nodes.append(f"源文件: {source_file}")
        for kind, path in artifact_files.items():
            if exists[kind]:
                nodes.append(f"{kind}: {path.stem}")

        downstream = []
        if exists["图谱节点"]:
            downstream.append("知识图谱系统")
        if exists["向量索引"]:
            downstream.append("向量检索引擎")
        if exists["实体抽取"] and exists["关系抽取"]:
            downstream.append("数据可视化平台")
        if not downstream:
            downstream.append("待确认")

        lineage = {
            "链路编号": f"LINEAGE-{source_file}",
            "源文件": source_file or parse_task_key,
            "解析节点": f"format_parse::{source_file}" if exists["解析节点"] else "未解析",
            "分块节点": f"chunk_split::{source_file}" if exists["分块节点"] else "未切分",
            "抽取产物": f"entity+relation::{source_file}" if (exists["实体抽取"] or exists["关系抽取"]) else "未抽取",
            "下游消费方": ", ".join(downstream),
            "血缘版本": "v1.0",
            "采集时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        output_path = self._data_dir("lineage") / f"{task_key}_lineage.jsonl"
        self._overwrite_jsonl(output_path, [lineage])

        # 产物反查绑定记录：只有产物确实存在时才登记
        bindings = []
        for kind, path in artifact_files.items():
            if not exists[kind]:
                continue
            relative = f"{path.parent.name}/{path.name}"
            bindings.append(self.record_artifact_binding(
                source_file=source_file,
                artifact_reference=relative,
                artifact_kind=kind,
                document_id=document_id,
                parse_task_key=str(task_key),
            ))

        self._write_checkpoint(f"lineage_{source_file}", {
            "parse_task_key": parse_task_key,
            "document_id": document_id,
            "nodes_count": len(nodes),
            "downstream_systems": downstream,
            "coverage": {
                "parsed": exists["解析节点"],
                "chunks": exists["分块节点"],
                "entities": exists["实体抽取"],
                "relations": exists["关系抽取"],
                "graph": exists["图谱节点"],
                "vectors": exists["向量索引"],
            },
            "bindings": len(bindings),
        })

        return {
            "parse_task_key": parse_task_key,
            "source_file": source_file,
            "document_id": document_id,
            "nodes": len(nodes),
            "downstream": downstream,
            "bindings": len(bindings),
            "output": str(output_path),
        }

    def _find_parse_task(self, parse_task_key: str, source_identity: str) -> dict[str, Any] | None:
        """按任务编号精确匹配，或按文档身份关联解析任务。"""
        rows = list(store.rows("format_parse"))
        for row in rows:
            if str(row.get("任务编号", "")) == str(parse_task_key):
                return row
        for row in rows:
            if canonical_reference(row.get("文档编号", "")) == source_identity:
                return row
        return None

    # ------------------------------------------------------------------
    # 反向追溯：产物引用 → 来源文件
    # ------------------------------------------------------------------
    def _artifact_candidates(self, artifact_path: str) -> list[dict[str, Any]]:
        """从产物目录中找出与引用相符的产物文件及其来源身份。

        产物文件名本身就是 ``<文档身份>_<产物后缀>``，因此来源身份由文件
        名推导；带目录的引用只接受同目录产物，避免同名短路径串档。
        """
        normalized = normalize_reference(artifact_path)
        identity = canonical_reference(normalized)
        if not identity:
            return []
        directory_hint = reference_directory(normalized)
        candidates: list[dict[str, Any]] = []
        for sub, kind in ARTIFACT_KINDS.items():
            folder = DATA_ROOT / sub
            for path in sorted(folder.glob("*.jsonl")):
                if not artifact_references_match(normalized, f"{sub}/{path.name}"):
                    continue
                if directory_hint and directory_hint not in str(path.parent).casefold():
                    # 引用带目录时只接受同一目录下的产物
                    continue
                file_identity = canonical_reference(path.name)
                candidates.append({
                    "来源文件": file_identity,
                    "文档身份": file_identity,
                    "产物路径": str(path),
                    "产物种类": kind,
                    "匹配方式": "产物文件身份精确匹配",
                    "是否核实": file_identity == identity
                    or not reference_artifact_detail(normalized),
                })
        return candidates

    def _binding_candidates(self, artifact_path: str) -> list[dict[str, Any]]:
        """绑定记录中与引用精确匹配的条目。

        绑定记录既可能按产物路径命中，也可能被查询里的目录点名了来源
        （``a/doc-77_parsed.jsonl`` 指向来源 ``a/doc-77``）。
        """
        candidates = []
        for record in self.list_bindings():
            if not (
                artifact_references_match(artifact_path, record.get("产物引用", ""))
                or source_path_matches(artifact_path, record.get("来源文件", ""))
            ):
                continue
            candidates.append({
                "来源文件": normalize_reference(record.get("来源文件", "")),
                "文档身份": canonical_reference(record.get("来源文件", "")),
                "产物路径": str(record.get("产物引用", "")),
                "产物种类": record.get("产物种类", ""),
                "解析任务编号": record.get("解析任务编号", ""),
                "绑定编号": record.get("绑定编号", ""),
                "匹配方式": "绑定记录精确匹配",
                "是否核实": True,
            })
        return candidates

    def trace_upstream(self, artifact_path: str) -> dict[str, Any]:
        """反向追溯：给定产物引用，确定它唯一指向的来源文件。

        只有来源唯一且身份可关联时才返回确定归属；出现多个来源时返回
        待核项清单（可调查材料），不会替操作员挑一个来源。
        """
        return self.resolve_source_candidates(artifact_path)

    def resolve_source_candidates(self, artifact_reference: str) -> dict[str, Any]:
        """解析产物引用对应的来源候选，并判定能否直接确定到唯一来源。"""
        normalized = normalize_reference(artifact_reference)
        identity = canonical_reference(normalized)
        base = {
            "artifact": normalized,
            "artifact_identity": identity,
        }
        if not identity:
            return {
                **base,
                "status": "未找到",
                "ambiguous": False,
                "resolved": None,
                "source_file": "",
                "candidates": [],
                "pending_items": [],
                "message": "产物引用为空，无法定位来源",
            }

        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add(candidate: dict[str, Any]) -> None:
            key = (source_reference_key(candidate.get("来源文件", "")), str(candidate.get("产物路径", "")))
            if key in seen:
                return
            seen.add(key)
            candidates.append(candidate)

        binding_candidates = self._binding_candidates(normalized)
        for candidate in binding_candidates:
            add(candidate)
        # 有绑定记录时以绑定记录为准；没有绑定时才用产物文件名兜底
        if not binding_candidates:
            for candidate in self._artifact_candidates(normalized):
                add(candidate)

        # 历史血缘文件里的引用：只有与文档身份能够相互关联时才算已核实，
        # 其余仅作为可调查材料返回，绝不被当作可直接重跑的依据。
        for lineage_file in sorted(self._data_dir("lineage").glob("*.jsonl")):
            if lineage_file.name == "bindings.jsonl":
                continue
            for record in self._read_jsonl(lineage_file):
                matched_fields = [
                    name for name in REFERENCE_FIELDS
                    if references_match(normalized, record.get(name, ""))
                ]
                if not matched_fields:
                    continue
                source_raw = normalize_reference(record.get("源文件", ""))
                source_identity = canonical_reference(source_raw)
                # 引用写到了下游节点时，历史记录必须本身能自洽：
                # 所有引用字段指向同一文档身份，才可视为已核实。
                matched_downstream = [name for name in matched_fields if name != "源文件"]
                consistent = bool(source_identity) and bool(matched_downstream) and all(
                    is_placeholder(record.get(name, ""))
                    or canonical_reference(record.get(name, "")) == source_identity
                    for name in REFERENCE_FIELDS
                )
                add({
                    "来源文件": source_raw,
                    "文档身份": source_identity,
                    "产物路径": f"lineage/{lineage_file.name}",
                    "产物种类": "血缘记录",
                    "链路编号": record.get("链路编号", ""),
                    "匹配方式": "历史血缘引用" + ("（身份一致）" if consistent else "（身份不一致，仅作调查材料）"),
                    "是否核实": consistent,
                    "字段": matched_fields,
                    "命中下游节点": matched_downstream,
                })

        # 按来源分组：带了目录的引用按完整路径区分（同名不同目录是两份来源）
        by_source: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            key = source_reference_key(candidate.get("来源文件", ""))
            if not key:
                continue
            by_source.setdefault(key, []).append(candidate)

        confirmed = sorted(
            key for key, items in by_source.items()
            if any(item.get("是否核实") for item in items)
        )

        if not candidates:
            return {
                **base,
                "status": "未找到",
                "ambiguous": False,
                "resolved": None,
                "source_file": "",
                "candidates": [],
                "pending_items": [],
                "message": f"未找到引用 {normalized} 的任何产物或绑定记录",
            }

        if len(confirmed) == 1:
            source_key = confirmed[0]
            items = by_source[source_key]
            pending_items = [
                self._pending_item(key, group)
                for key, group in sorted(by_source.items())
                if key != source_key
            ]
            display = self._display_source(source_key, items)
            return {
                **base,
                "status": "已确定",
                "ambiguous": False,
                "resolved": self._build_resolved_source(source_key, items),
                "source_file": display,
                "candidates": candidates,
                "pending_items": pending_items,
                "message": (
                    f"产物引用可确定到来源 {display}"
                    + (f"，另有 {len(pending_items)} 个未核实候选需调查" if pending_items else "")
                ),
            }

        pending_items = [self._pending_item(key, group) for key, group in sorted(by_source.items())]
        reason = (
            f"命中 {len(confirmed)} 个已核实来源" if len(confirmed) > 1
            else "命中的引用均无法与文档身份相互印证"
        )
        return {
            **base,
            "status": "存在歧义",
            "ambiguous": True,
            "resolved": None,
            "source_file": "",
            "candidates": candidates,
            "pending_items": pending_items,
            "message": f"引用 {normalized} {reason}，不能按列表顺序选定来源，请先核实待核项",
        }

    def _pending_item(self, source_key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """把一个来源候选整理成可调查的待核项。"""
        return {
            "来源文件": self._display_source(source_key, items),
            "文档身份": source_key,
            "候选数": len(items),
            "产物路径": [item.get("产物路径", "") for item in items],
            "核实依据": [item.get("匹配方式", "") for item in items],
            "是否核实": any(item.get("是否核实") for item in items),
            "处理建议": "先核对产物与文档身份，再决定是否重跑",
        }

    def _display_source(self, source_key: str, items: list[dict[str, Any]]) -> str:
        """展示来源时优先使用记录中的原始写法，便于操作员核对。"""
        for item in items:
            raw = normalize_reference(item.get("来源文件", ""))
            if source_reference_key(raw) == source_key and raw and raw != source_key:
                return raw
        for item in items:
            raw = normalize_reference(item.get("来源文件", ""))
            if raw:
                return raw
        return source_key

    def _build_resolved_source(self, source_key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """把唯一来源整理成可直接交给重跑入口的源数据描述。"""
        display = self._display_source(source_key, items)
        identity = canonical_reference(display) or source_key
        parse_task = self._find_parse_task("", identity) or {}
        artifact_paths = [
            item.get("产物路径", "") for item in items
            if item.get("产物种类") != "血缘记录"
        ]
        return {
            "文档身份": identity,
            "来源文件": display,
            "文档编号": str(parse_task.get("文档编号", "") or identity),
            "解析任务编号": str(parse_task.get("任务编号", "") or ""),
            "绑定编号": next((item.get("绑定编号") for item in items if item.get("绑定编号")), ""),
            "产物路径": artifact_paths,
            "依据": [item.get("匹配方式", "") for item in items],
        }

    # ------------------------------------------------------------------
    # 完整性校验
    # ------------------------------------------------------------------
    def validate_lineage_integrity(self, parse_task_key: str) -> dict[str, Any]:
        """校验血缘链路的完整性，检查各级节点是否都存在有效产物文件。"""
        source_file = canonical_reference(parse_task_key) or parse_task_key
        parsed_file = self._data_dir("parsed") / f"{source_file}_parsed.jsonl"
        chunks_file = self._data_dir("chunks") / f"{source_file}_parsed_chunks.jsonl"
        entities_file = self._data_dir("entities") / f"{source_file}_parsed_entities.jsonl"
        relations_file = self._data_dir("relations") / f"{source_file}_parsed_relations.jsonl"

        checks = {
            "parsed": parsed_file.exists(),
            "chunks": chunks_file.exists(),
            "entities": entities_file.exists(),
            "relations": relations_file.exists(),
        }

        all_ok = all(checks.values())
        return {
            "parse_task_key": parse_task_key,
            "checks": checks,
            "integrity": "完整" if all_ok else "存在断链",
            "missing_nodes": [k for k, v in checks.items() if not v],
        }
