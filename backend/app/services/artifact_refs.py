"""产物引用归一化工具：把路径、短路径和产物文件名还原成稳定的文档身份。

抽取血缘、格式解析重跑和失败重采都需要回答同一个问题：
「这个产物引用到底指向哪一份源文件/文档？」

这里刻意不使用字符串包含判断。``doc-12`` 和 ``doc-120`` 是两个不同的
身份，只有完整身份相等（或仅省略了目录、产物后缀）才能视为同一引用。
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

# 产物文件后缀（按长度从长到短匹配，避免 _parsed 抢先吃掉 _parsed_chunks）
ARTIFACT_SUFFIXES: tuple[str, ...] = (
    "_parsed_chunks",
    "_parsed_entities",
    "_parsed_relations",
    "_parsed_vectors",
    "_parsed_graph",
    "_parsed_pages",
    "_parsed",
    "_chunks",
    "_entities",
    "_relations",
    "_vectors",
    "_graph",
    "_tables",
    "_images",
    "_layout",
    "_lineage",
    "_retries",
    "_quality",
    "_reviews",
    "_delivery",
    "_published",
    "_manifest",
    "_locked",
)

FILE_EXTENSIONS: tuple[str, ...] = (
    ".jsonl",
    ".json",
    ".csv",
    ".parquet",
    ".xlsx",
    ".xls",
    ".pdf",
    ".docx",
    ".doc",
    ".html",
    ".htm",
    ".xml",
    ".txt",
    ".md",
)

# 版本后缀：doc-12_v1.0 / report_v2
_VERSION_SUFFIX = re.compile(r"_v\d+(?:\.\d+)*$", re.IGNORECASE)

# 带数字的 ASCII 编号（doc-12 / DOC-ABC-01 / 2024Q3）按引用精确匹配；
# 纯字母的普通词（Excel / Test）仍当作检索关键字处理。
_IDENTITY_LIKE = re.compile(r"[A-Za-z][A-Za-z0-9._-]*\d[A-Za-z0-9._-]*|\d[A-Za-z0-9._-]*[A-Za-z]")
# 主机名/系统名这类点分写法不一定是文档引用，保留关键字检索
_HOSTNAME_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*(\.[A-Za-z][A-Za-z0-9-]*)+$")


def normalize_reference(value: object) -> str:
    """把引用整理成无引号、统一分隔符的路径文本（保留目录）。

    兼容 ``format_parse::doc-12`` 这类“节点类型::节点键”的写法。
    """
    text = str(value or "").strip().strip("\"'")
    if not text:
        return ""
    if "::" in text:
        # format_parse::doc-12 / chunk_split::doc-12
        text = text.rsplit("::", 1)[-1].strip()
    return text.replace("\\", "/")


def canonical_reference(value: object) -> str:
    """返回引用的文档身份（小写）：去掉目录、文件扩展名和产物后缀。

    ``/data/parsed/doc-12_parsed.jsonl``、``parsed/doc-12_parsed`` 和
    ``doc-12`` 都会得到 ``doc-12``；``doc-120_parsed`` 则得到 ``doc-120``，
    不会与 ``doc-12`` 混淆。
    """
    text = normalize_reference(value)
    if not text:
        return ""
    name = PurePosixPath(text).name or text
    # 去掉可能的版本号再剥后缀，支持 doc_v1.0_parsed.jsonl 这种组合
    guard = 0
    while guard < 8:
        guard += 1
        before = name
        lowered = name.lower()
        for ext in FILE_EXTENSIONS:
            if lowered.endswith(ext) and len(name) > len(ext):
                name = name[: -len(ext)]
                lowered = name.lower()
                break
        for suffix in ARTIFACT_SUFFIXES:
            if lowered.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                lowered = name.lower()
                break
        version = _VERSION_SUFFIX.search(name)
        if version and version.start() > 0:
            name = name[: version.start()]
        if name == before:
            break
    return name.casefold()


def reference_stem(value: object) -> str:
    """返回引用的文件名主干（小写、去扩展名），保留产物后缀。

    ``parsed/doc-12_parsed.jsonl`` → ``doc-12_parsed``；
    ``doc-12`` → ``doc-12``。
    """
    text = normalize_reference(value)
    if not text:
        return ""
    name = PurePosixPath(text).name or text
    lowered = name.casefold()
    for ext in FILE_EXTENSIONS:
        if lowered.endswith(ext) and len(name) > len(ext):
            return lowered[: -len(ext)]
    return lowered


def reference_artifact_detail(value: object) -> str:
    """返回引用中指明的产物后缀（如 ``_parsed``、``_parsed_chunks``）。

    引用只写到文档编号时返回空串，表示“任意产物种类”。
    """
    stem = reference_stem(value)
    lowered = stem.casefold()
    for suffix in ARTIFACT_SUFFIXES:
        if lowered.endswith(suffix) and len(stem) > len(suffix):
            return suffix
    return ""


def _directories_overlap(left: str, right: str) -> bool:
    """两个目录是否同一目录或存在包含关系。"""
    if not left or not right:
        return True
    if left == right:
        return True
    return left.endswith("/" + right) or right.endswith("/" + left)


def artifact_references_match(query: object, candidate: object) -> bool:
    """产物引用匹配：引用写到产物文件时要求文件名主干一致，否则按文档身份匹配。

    目录只用于排除明显不同的位置，不要求完全相等——历史记录常只保存
    短路径（``doc-77_parsed``）或相对目录（``parsed/doc-77_parsed.jsonl``），
    它们仍应指向同一产物；是否有多份同名来源由上层按候选数量判定。
    """
    query_text = normalize_reference(query)
    candidate_text = normalize_reference(candidate)
    if not query_text or not candidate_text:
        return False
    if query_text.casefold() == candidate_text.casefold():
        return True
    if not _directories_overlap(
        reference_directory(query_text), reference_directory(candidate_text)
    ):
        return False
    detail = reference_artifact_detail(query_text)
    if detail:
        # 引用明确到某类产物：主干必须一致，不能把 _parsed 当成 _parsed_chunks
        return reference_stem(query_text) == reference_stem(candidate_text)
    return references_match(query_text, candidate_text)


def reference_directory(value: object) -> str:
    """返回引用所在目录（归一化后的小写路径），无目录时返回空串。"""
    text = normalize_reference(value)
    if "/" not in text:
        return ""
    return str(PurePosixPath(text).parent).casefold()


def looks_like_reference(value: object) -> bool:
    """判断一个筛选值是否更像产物/文档引用而不是普通关键字。

    ``doc-12`` / ``DOC-ABC`` / ``parsed/doc-12_parsed.jsonl`` 按身份精确匹配；
    ``样例`` 这类普通检索词仍然保留包含匹配的易用性。
    """
    text = normalize_reference(value)
    if not text:
        return False
    if "/" in text or "\\" in text or "::" in text:
        return True
    if _HOSTNAME_LIKE.fullmatch(text) and reference_artifact_detail(text) == "" \
            and not text.casefold().endswith(FILE_EXTENSIONS):
        return False
    if "_" in text or "." in text:
        return True
    if _IDENTITY_LIKE.search(text):
        return True
    # 连字符/分隔的编号（DOC-ABC）按引用处理，单个英文单词仍按关键字
    return bool(re.search(r"[A-Za-z0-9]-[A-Za-z0-9]", text))


def source_reference_key(value: object) -> str:
    """来源文件的分组键：带目录时保留目录（同名不同目录是两份来源），否则按文档身份。"""
    text = normalize_reference(value)
    if not text:
        return ""
    if "/" in text:
        return text.casefold()
    return canonical_reference(text)


def source_path_matches(query: object, source: object) -> bool:
    """查询引用是否点名了这份来源：身份一致、产物种类一致、目录不冲突。"""
    query_text = normalize_reference(query)
    source_text = normalize_reference(source)
    if not query_text or not source_text:
        return False
    if canonical_reference(query_text) != canonical_reference(source_text):
        return False
    detail = reference_artifact_detail(query_text)
    if detail and reference_artifact_detail(source_text) not in ("", detail):
        return False
    query_dir = reference_directory(query_text)
    source_dir = reference_directory(source_text)
    return _directories_overlap(query_dir, source_dir)


def references_match(query: object, candidate: object) -> bool:
    """引用精确匹配：完整路径相等，或文档身份相等。"""
    query_text = normalize_reference(query).casefold()
    candidate_text = normalize_reference(candidate).casefold()
    if not query_text or not candidate_text:
        return False
    if query_text == candidate_text:
        return True
    query_identity = canonical_reference(query_text)
    return bool(query_identity) and query_identity == canonical_reference(candidate_text)


# 表示“该节点尚未产生”的占位值，不能当成真实身份
PLACEHOLDER_VALUES: frozenset[str] = frozenset({
    "", "未解析", "未切分", "未抽取", "待确认", "未知", "none", "null", "—", "-",
})


def is_placeholder(value: object) -> bool:
    """判断引用字段是否只是“尚未产生”的占位值。"""
    return normalize_reference(value).casefold() in PLACEHOLDER_VALUES
