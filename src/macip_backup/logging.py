"""Logs JSON para encaminhamento futuro à central."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any, TextIO


class StructuredLogger:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def emit(self, level: str, event: str, **fields: Any) -> None:
        record = {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "level": level,
            "event": event,
            **fields,
        }
        self._stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._stream.flush()
