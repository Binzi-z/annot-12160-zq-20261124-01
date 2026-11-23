"""Canonical representations for request content and parameters.

The approval is bound to a request *content hash*, not to the raw JSON text.
Canonical JSON guarantees that key order and insignificant whitespace do not
change the binding, while any semantic change does.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def request_hash(request: Any) -> str:
    return digest(request)


def params_hash(params: dict[str, Any]) -> str:
    return digest(params)


def parse_json_object(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("request content must be a JSON object")
    return value
