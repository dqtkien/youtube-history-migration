"""Tests for filtering, deduplication, and chronological ordering."""

from yt_history_migrator.filter_dedup import extract_video_id, prepare_watch_entries
from yt_history_migrator.models import FilterOptions, RawHistoryItem, SkipReason


def _make_item(
    idx: int,
    *,
    header: str = "YouTube",
    title: str = "Watched Video",
    url: str | None = "https://www.youtube.com/watch?v=vid00000001",
    channel: str | None = "Channel A",
    watched_raw: str | None = "2025-01-01T00:00:00Z",
    epoch: float | None = 1735689600.0,
    is_ad: bool = False,
) -> RawHistoryItem:
    return RawHistoryItem(
        record_index=idx,
        header=header,
        title=title,
        title_url=url,
        channel_name=channel,
        watched_at_raw=watched_raw,
        watched_at_epoch=epoch,
        is_ad=is_ad,
    )


def test_extract_video_id_supports_watch_shorts_youtu_be_and_music() -> None:
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1") == (
        "dQw4w9WgXcQ",
        False,
        False,
    )
    assert extract_video_id("https://www.youtube.com/shorts/abcDEF123_-") == (
        "abcDEF123_-",
        False,
        True,
    )
    assert extract_video_id("https://music.youtube.com/watch?v=12345678901") == (
        "12345678901",
        True,
        False,
    )
    assert extract_video_id("https://youtu.be/9876543210A?t=12") == (
        "9876543210A",
        False,
        False,
    )
    assert extract_video_id("https://www.youtube.com/post/Ugkx1234") == (None, False, False)


def test_filter_excludes_ads_music_posts_and_non_watch_by_default() -> None:
    items = [
        _make_item(0, url="https://www.youtube.com/watch?v=good0000001", title="Watched Good Video"),
        _make_item(1, url="https://www.youtube.com/watch?v=ad000000002", is_ad=True),
        _make_item(2, header="YouTube Music", url="https://music.youtube.com/watch?v=mus00000003"),
        _make_item(3, url="https://www.youtube.com/post/Ugkx12345"),
        _make_item(4, url=None, title="Watched a video that has been removed"),
        _make_item(5, title="Visited YouTube Music", url="https://music.youtube.com/"),
    ]

    report = prepare_watch_entries(items, FilterOptions())

    assert [e.video_id for e in report.entries] == ["good0000001"]
    assert report.entries[0].title == "Good Video"
    assert report.skipped_counts[SkipReason.GOOGLE_AD] == 1
    assert report.skipped_counts[SkipReason.YOUTUBE_MUSIC] == 1
    assert report.skipped_counts[SkipReason.COMMUNITY_POST] == 1
    assert report.skipped_counts[SkipReason.MISSING_URL] == 1
    assert report.skipped_counts[SkipReason.NON_WATCH_ACTIVITY] == 1


def test_dedup_keeps_latest_watch_and_orders_oldest_to_newest() -> None:
    # Takeout exports newest first: index 0 is 2026, index 1 is 2025, index 2 is 2022 repeat of index 0
    items = [
        _make_item(
            0,
            url="https://www.youtube.com/watch?v=repeat12345",
            title="Watched Repeated Video (Updated Title)",
            watched_raw="2026-05-01T10:00:00Z",
            epoch=1777629600.0,
        ),
        _make_item(
            1,
            url="https://www.youtube.com/watch?v=middle12345",
            title="Watched Middle Video",
            watched_raw="2025-01-15T10:00:00Z",
            epoch=1736935200.0,
        ),
        _make_item(
            2,
            url="https://www.youtube.com/watch?v=repeat12345",
            title="Watched Repeated Video (Old Title)",
            watched_raw="2022-03-01T10:00:00Z",
            epoch=1646128800.0,
        ),
        _make_item(
            3,
            url="https://www.youtube.com/watch?v=oldest12345",
            title="Watched Oldest Video",
            watched_raw="2021-01-01T10:00:00Z",
            epoch=1609495200.0,
        ),
    ]

    report = prepare_watch_entries(items, FilterOptions())

    assert report.duplicate_watches_collapsed == 1
    assert [e.video_id for e in report.entries] == [
        "oldest12345",
        "middle12345",
        "repeat12345",
    ]
    repeated = report.entries[2]
    assert repeated.watch_count == 2
    assert repeated.watched_at_raw == "2026-05-01T10:00:00Z"
    assert repeated.title == "Repeated Video (Updated Title)"


def test_fallback_record_index_ordering_when_timestamps_missing() -> None:
    # Record 0 is newest in Takeout; record 2 is oldest.
    items = [
        _make_item(0, url="https://www.youtube.com/watch?v=newest12345", watched_raw=None, epoch=None),
        _make_item(1, url="https://www.youtube.com/watch?v=middle12345", watched_raw=None, epoch=None),
        _make_item(2, url="https://www.youtube.com/watch?v=oldest12345", watched_raw=None, epoch=None),
    ]

    report = prepare_watch_entries(items, FilterOptions())

    assert [e.video_id for e in report.entries] == [
        "oldest12345",
        "middle12345",
        "newest12345",
    ]


def test_date_channel_music_and_shorts_filters() -> None:
    items = [
        _make_item(
            0,
            url="https://www.youtube.com/shorts/short123456",
            channel="Fireship",
            watched_raw="2025-06-01T12:00:00Z",
            epoch=1748779200.0,
        ),
        _make_item(
            1,
            header="YouTube Music",
            url="https://music.youtube.com/watch?v=music123456",
            channel="Fireship",
            watched_raw="2025-05-15T12:00:00Z",
            epoch=1747310400.0,
        ),
        _make_item(
            2,
            url="https://www.youtube.com/watch?v=keep1234567",
            channel="Fireship",
            watched_raw="2025-05-10T12:00:00Z",
            epoch=1746878400.0,
        ),
        _make_item(
            3,
            url="https://www.youtube.com/watch?v=other123456",
            channel="Other Channel",
            watched_raw="2025-05-05T12:00:00Z",
            epoch=1746446400.0,
        ),
        _make_item(
            4,
            url="https://www.youtube.com/watch?v=early123456",
            channel="Fireship",
            watched_raw="2024-01-01T12:00:00Z",
            epoch=1704110400.0,
        ),
    ]

    options = FilterOptions(
        include_music=True,
        exclude_shorts=True,
        after_date="2025-05-01",
        before_date="2025-05-20",
        channels=frozenset({"fireship"}),
    )
    report = prepare_watch_entries(items, options)

    assert [e.video_id for e in report.entries] == ["keep1234567", "music123456"]
    assert report.skipped_counts[SkipReason.SHORT_VIDEO] == 1
    assert report.skipped_counts[SkipReason.CHANNEL_EXCLUDED] == 1
    assert report.skipped_counts[SkipReason.BEFORE_DATE_CUTOFF] == 1


def test_removed_videos_and_viewed_activity_filtering() -> None:
    items = [
        _make_item(
            0,
            title="Watched https://www.youtube.com/watch?v=dead0000001",
            url="https://www.youtube.com/watch?v=dead0000001",
            channel=None,
        ),
        _make_item(
            1,
            title="Viewed a product",
            url="https://www.youtube.com/watch?v=view0000002",
            channel="Some Channel",
        ),
        _make_item(
            2,
            title="Watched Live Video",
            url="https://www.youtube.com/watch?v=live0000003",
            channel="Live Channel",
        ),
    ]

    default_report = prepare_watch_entries(items, FilterOptions())
    assert [e.video_id for e in default_report.entries] == ["live0000003"]
    assert default_report.skipped_counts[SkipReason.REMOVED_VIDEO] == 1
    assert default_report.skipped_counts[SkipReason.NON_WATCH_ACTIVITY] == 1

    with_removed_report = prepare_watch_entries(items, FilterOptions(include_removed=True))
    assert [e.video_id for e in with_removed_report.entries] == ["live0000003", "dead0000001"]
