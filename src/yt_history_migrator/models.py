"""Typed data models for Google Takeout parsing, filtering, and migration state."""

from dataclasses import dataclass, field
from enum import Enum


class SkipReason(str, Enum):
    """Reason a raw Google Takeout item was excluded before migration."""

    MISSING_URL = "missing_url"
    GOOGLE_AD = "google_ad"
    YOUTUBE_MUSIC = "youtube_music"
    COMMUNITY_POST = "community_post"
    SHORT_VIDEO = "short_video"
    NON_WATCH_ACTIVITY = "non_watch_activity"
    REMOVED_VIDEO = "removed_video"
    INVALID_VIDEO_ID = "invalid_video_id"
    BEFORE_DATE_CUTOFF = "before_date_cutoff"
    AFTER_DATE_CUTOFF = "after_date_cutoff"
    CHANNEL_EXCLUDED = "channel_excluded"


class MigrationStatus(str, Enum):
    """Outcome status recorded for a single video in the progress store."""

    WATCHED = "watched"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True)
class RawHistoryItem:
    """Single unparsed-filtered activity record from a Google Takeout export."""

    record_index: int
    header: str
    title: str
    title_url: str | None
    channel_name: str | None
    watched_at_raw: str | None
    watched_at_epoch: float | None
    is_ad: bool


@dataclass(frozen=True)
class WatchEntry:
    """Normalized, deduplicated watch entry ready for chronological replay."""

    video_id: str
    canonical_url: str
    title: str
    channel_name: str | None
    watched_at_raw: str | None
    watched_at_epoch: float | None
    record_index: int
    watch_count: int = 1


@dataclass(frozen=True)
class FilterOptions:
    """User-configurable filter criteria for selecting Takeout history entries."""

    include_music: bool = False
    include_removed: bool = False
    exclude_shorts: bool = False
    after_date: str | None = None
    before_date: str | None = None
    channels: frozenset[str] = field(default_factory=frozenset)


@dataclass
class FilterReport:
    """Summary and chronologically sorted entries produced by filtering & dedup."""

    total_raw: int
    skipped_counts: dict[SkipReason, int]
    duplicate_watches_collapsed: int
    entries: list[WatchEntry]


@dataclass(frozen=True)
class ProgressRecord:
    """Checkpoint record persisted in JSONL for resume support."""

    video_id: str
    status: MigrationStatus
    title: str
    updated_at: str
    reason: str | None = None
