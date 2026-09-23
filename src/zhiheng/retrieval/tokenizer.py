from __future__ import annotations

import re
from typing import Protocol

import jieba  # type: ignore[import-untyped]


class Tokenizer(Protocol):
    def segment(self, value: str) -> str: ...


class JiebaChineseTokenizer:
    def segment(self, value: str) -> str:
        normalized = _normalize_query(value)
        tokens = [
            token.strip().lower()
            for token in jieba.cut_for_search(normalized)
            if token.strip() and _is_indexable(token)
        ]
        if not tokens and value.strip():
            raise ValueError("query contains no indexable tokens")
        # Preserve exact phrases as individual FTS terms while avoiding
        # punctuation/operators being interpreted as SQLite MATCH syntax.
        return " ".join(dict.fromkeys(tokens))


DEFAULT_TOKENIZER = JiebaChineseTokenizer()


def segment_for_fts(value: str) -> str:
    return DEFAULT_TOKENIZER.segment(value)


_QUERY_SEPARATOR_RE = re.compile(r"[\u0000-\u001f]+")
_PUNCTUATION_RE = re.compile(r"[^\w\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", re.UNICODE)
_FTS_OPERATORS = {"and", "or", "not", "near"}


def _normalize_query(value: str) -> str:
    return _PUNCTUATION_RE.sub(" ", _QUERY_SEPARATOR_RE.sub(" ", value)).strip()


def _is_indexable(token: str) -> bool:
    return token.lower() not in _FTS_OPERATORS and bool(
        re.search(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", token)
    )
