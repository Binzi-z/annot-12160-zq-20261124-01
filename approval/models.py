"""Value objects shared by the service, CLI and reference machine."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Result:
    """Outcome of one business operation.

    ``code`` is the machine-readable business decision, ``status`` is the
    resulting business state of the subject (for example ``ACTIVE`` or
    ``CONSUMED``).  ``audit_no`` identifies the audit row that records the
    decision and is stable across idempotent retries.
    """

    ok: bool
    code: str
    status: str
    audit_no: str | None
    data: dict[str, Any] = field(default_factory=dict)
    replayed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "status": self.status,
            "audit_no": self.audit_no,
            "data": self.data,
        }

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any], *, replayed: bool = False
    ) -> "Result":
        return cls(
            ok=bool(payload["ok"]),
            code=str(payload["code"]),
            status=str(payload["status"]),
            audit_no=payload.get("audit_no"),
            data=dict(payload.get("data") or {}),
            replayed=replayed,
        )

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False, sort_keys=True)
