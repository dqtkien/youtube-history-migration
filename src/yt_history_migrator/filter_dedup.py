"""Filtering, deduplication, and chronological ordering for Takeout history."""

from datetime import datetime, time, timezone
import re
from urllib.parse import parse_qs, urlparse

from yt_history_migrator.models import (
    FilterOptions,
    FilterReport,
    RawHistoryItem,
    SkipReason,
    WatchEntry,
)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_NON_WATCH_PREFIXES = ("Visited ", "Searched for ", "Subscribed to ", "Answered ", "Viewed ")
_YOUTUBE_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
})


def extract_video_id(url: str) -> tuple[str | None, bool, bool]:
    """Extract (video_id, is_music_url, is_shorts_url) from a YouTube URL."""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    path = parsed.path or ""
    is_music_url = host == "music.youtube.com"
    is_shorts_url = False

    if host == "youtu.be":
        candidate = path.strip("/").split("/")[0]
        return (_validated_video_id(candidate), is_music_url, False)

    if host not in _YOUTUBE_HOSTS:
        return (None, is_music_url, False)

    if path == "/watch":
        v_values = parse_qs(parsed.query).get("v")
        candidate = v_values[0] if v_values else None
        return (_validated_video_id(candidate), is_music_url, False)

    if path.startswith("/shorts/"):
        is_shorts_url = True
        candidate = path.removeprefix("/shorts/").strip("/").split("/")[0]
        return (_validated_video_id(candidate), is_music_url, is_shorts_url)

    if path.startswith("/live/"):
        candidate = path.removeprefix("/live/").strip("/").split("/")[0]
        return (_validated_video_id(candidate), is_music_url, False)

    return (None, is_music_url, is_shorts_url)


def _validated_video_id(candidate: str | None) -> str | None:
    if candidate is None:
        return None
    if _VIDEO_ID_RE.match(candidate):
        return candidate
    return None


def parse_date_bound(date_str: str | None, *, end_of_day: bool) -> float | None:
    """Parse an inclusive YYYY-MM-DD UTC date filter boundary into epoch seconds."""
    if date_str is None:
        return None
    cleaned = date_str.strip()
    if not cleaned:
        return None
    try:
        parsed_date = datetime.strptime(cleaned, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{date_str}'. Expected YYYY-MM-DD.") from exc

    boundary_time = time.max if end_of_day else time.min
    return datetime.combine(parsed_date, boundary_time, tzinfo=timezone.utc).timestamp()


def classify_skip_reason(
    item: RawHistoryItem,
    options: FilterOptions,
    after_epoch: float | None,
    before_epoch: float | None,
) -> tuple[SkipReason | None, str | None]:
    """Return (skip_reason, video_id) for a raw item using flat guard clauses."""
    if item.is_ad:
        return (SkipReason.GOOGLE_AD, None)

    if not item.title_url:
        return (SkipReason.MISSING_URL, None)

    lower_url = item.title_url.lower()
    if "/post/" in lower_url or "/post?" in lower_url or lower_url.endswith("/post"):
        return (SkipReason.COMMUNITY_POST, None)

    if item.title.startswith(_NON_WATCH_PREFIXES):
        return (SkipReason.NON_WATCH_ACTIVITY, None)

    video_id, is_music_url, is_shorts_url = extract_video_id(item.title_url)
    is_music_header = item.header.strip().lower() == "youtube music"
    if not options.include_music and (is_music_url or is_music_header):
        return (SkipReason.YOUTUBE_MUSIC, None)

    if options.exclude_shorts and is_shorts_url:
        return (SkipReason.SHORT_VIDEO, None)

    if video_id is None:
        return (SkipReason.INVALID_VIDEO_ID, None)

    cleaned_title = _clean_title(item.title)
    is_removed_placeholder = item.channel_name is None and cleaned_title.startswith(("http://", "https://"))
    if not options.include_removed and is_removed_placeholder:
        return (SkipReason.REMOVED_VIDEO, None)

    if after_epoch is not None and item.watched_at_epoch is not None and item.watched_at_epoch < after_epoch:
        return (SkipReason.BEFORE_DATE_CUTOFF, None)

    if before_epoch is not None and item.watched_at_epoch is not None and item.watched_at_epoch > before_epoch:
        return (SkipReason.AFTER_DATE_CUTOFF, None)

    if options.channels:
        normalized_channel = item.channel_name.strip().lower() if item.channel_name else None
        if normalized_channel is None or normalized_channel not in options.channels:
            return (SkipReason.CHANNEL_EXCLUDED, None)

    return (None, video_id)


def _clean_title(raw_title: str) -> str:
    cleaned = raw_title.strip()
    if cleaned.startswith("Watched "):
        return cleaned.removeprefix("Watched ").strip()
    return cleaned


def _chronological_sort_key(entry: WatchEntry) -> tuple[int, float, int]:
    """Order oldest -> newest.

    Google Takeout records are ordered newest-first (record_index 0 = most recent).
    Negating record_index ensures smaller record_index sorts later (newer) both as
    a tie-breaker and as a fallback when HTML dates cannot be parsed.
    """
    if entry.watched_at_epoch is not None:
        return (1, entry.watched_at_epoch, -entry.record_index)
    return (0, 0.0, -entry.record_index)


def _merge_duplicate_watch(existing: WatchEntry, candidate: WatchEntry) -> WatchEntry:
    # YouTube sets watch timestamp to "now" on replay, so keep each video at its
    # latest watch occurrence to preserve relative recency in the new Watch History.
    winner = candidate if _chronological_sort_key(candidate) > _chronological_sort_key(existing) else existing
    return WatchEntry(
        video_id=winner.video_id,
        canonical_url=winner.canonical_url,
        title=winner.title,
        channel_name=winner.channel_name,
        watched_at_raw=winner.watched_at_raw,
        watched_at_epoch=winner.watched_at_epoch,
        record_index=winner.record_index,
        watch_count=existing.watch_count + 1,
    )


def prepare_watch_entries(items: list[RawHistoryItem], options: FilterOptions) -> FilterReport:
    """Filter raw Takeout items, deduplicate in O(N), and sort oldest -> newest."""
    after_epoch = parse_date_bound(options.after_date, end_of_day=False)
    before_epoch = parse_date_bound(options.before_date, end_of_day=True)

    skipped_counts: dict[SkipReason, int] = {}
    by_id: dict[str, WatchEntry] = {}
    duplicate_watches_collapsed = 0

    for item in items:
        skip_reason, video_id = classify_skip_reason(item, options, after_epoch, before_epoch)
        if skip_reason is not None:
            skipped_counts[skip_reason] = skipped_counts.get(skip_reason, 0) + 1
            continue
        if video_id is None:
            continue

        candidate = WatchEntry(
            video_id=video_id,
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
            title=_clean_title(item.title),
            channel_name=item.channel_name,
            watched_at_raw=item.watched_at_raw,
            watched_at_epoch=item.watched_at_epoch,
            record_index=item.record_index,
            watch_count=1,
        )

        existing = by_id.get(video_id)
        if existing is None:
            by_id[video_id] = candidate
            continue

        duplicate_watches_collapsed += 1
        by_id[video_id] = _merge_duplicate_watch(existing, candidate)

    entries = sorted(by_id.values(), key=_chronological_sort_key)
    return FilterReport(
        total_raw=len(items),
        skipped_counts=skipped_counts,
        duplicate_watches_collapsed=duplicate_watches_collapsed,
        entries=entries,
    )
