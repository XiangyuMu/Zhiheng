from __future__ import annotations

import re

_SOURCE_TITLE_PATTERN = re.compile(r"《([^》]{1,512})》")


def explicit_source_titles(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_SOURCE_TITLE_PATTERN.findall(text)))


def unambiguous_source_title(text: str) -> str | None:
    titles = explicit_source_titles(text)
    if len(titles) != 1:
        return None
    return titles[0]
