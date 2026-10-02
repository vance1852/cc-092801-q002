"""确定性的 JSON 规范化与内容摘要。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def canonical_json(value: object) -> str:
    """生成跨平台一致、可复现的紧凑 JSON 文本。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def redacted(value: Any) -> Any:
    """把需要遮蔽的字段替换为稳定占位符，保持结构可解释。"""

    return "***REDACTED***" if value is not None else None
