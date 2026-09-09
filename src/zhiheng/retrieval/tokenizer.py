from __future__ import annotations

from typing import Protocol

import jieba  # type: ignore[import-untyped]


class Tokenizer(Protocol):
    def segment(self, value: str) -> str: ...


class JiebaChineseTokenizer:
    def segment(self, value: str) -> str:
        tokens = [token.strip().lower() for token in jieba.cut_for_search(value) if token.strip()]
        if not tokens and value.strip():
            raise ValueError("query contains no indexable tokens")
        return " ".join(tokens)


DEFAULT_TOKENIZER = JiebaChineseTokenizer()


def segment_for_fts(value: str) -> str:
    return DEFAULT_TOKENIZER.segment(value)
