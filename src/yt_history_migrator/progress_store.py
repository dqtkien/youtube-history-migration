"""Append-only JSONL progress persistence for resumable history migration."""

import json
from pathlib import Path
from types import TracebackType
from typing import TextIO

from yt_history_migrator.models import MigrationStatus, ProgressRecord


class ProgressStore:
    """In-memory O(1) index backed by an append-only JSONL checkpoint file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: dict[str, ProgressRecord] = {}
        self._append_handle: TextIO | None = None

    def __enter__(self) -> "ProgressStore":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._append_handle = self._path.open("a", encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._append_handle is not None:
            self._append_handle.flush()
            self._append_handle.close()
            self._append_handle = None

    @property
    def path(self) -> Path:
        """Return the checkpoint JSONL path."""
        return self._path

    def load(self) -> None:
        """Load existing checkpoint records from disk if present."""
        self._records.clear()
        if not self._path.exists():
            return

        with self._path.open("r", encoding="utf-8") as handle:
            for line_num, raw_line in enumerate(handle, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                record = _decode_progress_line(stripped, line_num)
                if record is not None:
                    self._records[record.video_id] = record

    def get(self, video_id: str) -> ProgressRecord | None:
        """Return the latest recorded state for a video ID, if any."""
        return self._records.get(video_id)

    def should_skip(self, video_id: str, *, retry_unavailable: bool = False) -> bool:
        """Determine whether a video already completed and should be skipped on resume."""
        existing = self._records.get(video_id)
        if existing is None:
            return False
        if existing.status == MigrationStatus.WATCHED:
            return True
        if existing.status == MigrationStatus.UNAVAILABLE:
            return not retry_unavailable
        # Transient FAILED and AUTH_FAILED records are always retried on resume.
        return False

    def record(self, entry_record: ProgressRecord) -> None:
        """Persist a single migration outcome immediately so interrupts lose zero state."""
        self._records[entry_record.video_id] = entry_record
        line = json.dumps(
            {
                "video_id": entry_record.video_id,
                "status": entry_record.status.value,
                "title": entry_record.title,
                "updated_at": entry_record.updated_at,
                "reason": entry_record.reason,
            },
            ensure_ascii=False,
        ) + "\n"

        if self._append_handle is not None:
            self._append_handle.write(line)
            self._append_handle.flush()
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()

    def total_recorded(self) -> int:
        """Return the count of unique video IDs tracked in the store."""
        return len(self._records)

    def summary_counts(self) -> dict[MigrationStatus, int]:
        """Count current status distribution across all tracked video IDs."""
        counts: dict[MigrationStatus, int] = {status: 0 for status in MigrationStatus}
        for rec in self._records.values():
            counts[rec.status] += 1
        return counts


def _decode_progress_line(line: str, line_num: int) -> ProgressRecord | None:
    try:
        decoded: object = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrupt JSONL at progress line {line_num}: {exc}") from exc

    if not isinstance(decoded, dict):
        return None

    video_id = decoded.get("video_id")
    status_raw = decoded.get("status")
    title = decoded.get("title")
    updated_at = decoded.get("updated_at")
    reason = decoded.get("reason")

    if not isinstance(video_id, str) or not isinstance(status_raw, str):
        return None
    if not isinstance(title, str) or not isinstance(updated_at, str):
        return None
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)

    try:
        status = MigrationStatus(status_raw)
    except ValueError:
        return None

    return ProgressRecord(
        video_id=video_id,
        status=status,
        title=title,
        updated_at=updated_at,
        reason=reason,
    )
