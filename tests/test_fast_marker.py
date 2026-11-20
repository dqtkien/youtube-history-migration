"""Tests for FastYoutubeMarker and ordered sliding-window prefetch pipeline."""

import io
from pathlib import Path
import threading
import time
from types import TracebackType
from unittest.mock import patch

from yt_history_migrator.fast_marker import FastYoutubeMarker, PrefetchedPlayer
from yt_history_migrator.importer import run_migration
from yt_history_migrator.models import MigrationStatus, WatchEntry
from yt_history_migrator.progress_store import ProgressStore


class _FakeHttpResponse:
    def __init__(self, status_code: int, json_body: dict[str, object] | None = None) -> None:
        self.status_code = status_code
        self._json_body = json_body

    def json(self) -> object:
        if self._json_body is None:
            raise ValueError("No JSON body")
        return self._json_body


class _FakeSession:
    def __init__(
        self,
        post_handler: dict[str, _FakeHttpResponse],
        get_status: int = 204,
    ) -> None:
        self.post_handler = post_handler
        self.get_status = get_status
        self.get_calls: list[str] = []

    def post(
        self,
        _url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: int,
    ) -> _FakeHttpResponse:
        vid = str(json.get("videoId", ""))
        return self.post_handler[vid]

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: int,
    ) -> _FakeHttpResponse:
        self.get_calls.append(url)
        return _FakeHttpResponse(self.get_status)

    def close(self) -> None:
        return None


def _build_marker_with_session(session: _FakeSession) -> FastYoutubeMarker:
    marker = FastYoutubeMarker(session_cookiefile=None, cookies_from_browser=None)
    marker._http = session  # type: ignore[assignment]
    marker._ytcfg = {"LOGGED_IN": True}
    marker._context = {"client": {"clientName": "WEB", "clientVersion": "2.2026"}}
    marker._build_api_headers = lambda: {"Authorization": "SAPISIDHASH test", "Content-Type": "application/json"}  # type: ignore[method-assign]
    return marker


def test_fast_marker_marks_watched_and_detects_unavailable_and_429() -> None:
    responses = {
        "good0000001": _FakeHttpResponse(
            200,
            {
                "playabilityStatus": {"status": "OK"},
                "playbackTracking": {
                    "videostatsPlaybackUrl": {"baseUrl": "https://www.youtube.com/api/stats/playback?len=120"},
                    "videostatsWatchtimeUrl": {"baseUrl": "https://www.youtube.com/api/stats/watchtime?len=120"},
                },
            },
        ),
        "dead0000002": _FakeHttpResponse(
            200,
            {
                "playabilityStatus": {
                    "status": "ERROR",
                    "reason": "Video unavailable",
                }
            },
        ),
        "rate0000003": _FakeHttpResponse(429),
    }
    session = _FakeSession(responses)
    marker = _build_marker_with_session(session)

    prefetched_good = marker.prefetch_player("good0000001")
    assert prefetched_good.outcome is None
    status_good, reason_good = marker.commit_watchtime(prefetched_good)
    assert status_good == MigrationStatus.WATCHED
    assert reason_good is None
    assert len(session.get_calls) == 2
    assert "el=detailpage" in session.get_calls[0]
    assert "st=0" in session.get_calls[1]
    assert "et=119.0" in session.get_calls[1]

    prefetched_dead = marker.prefetch_player("dead0000002")
    assert prefetched_dead.outcome == MigrationStatus.UNAVAILABLE
    status_dead, reason_dead = marker.commit_watchtime(prefetched_dead)
    assert status_dead == MigrationStatus.UNAVAILABLE
    assert reason_dead == "Video unavailable"
    assert len(session.get_calls) == 2

    prefetched_rate = marker.prefetch_player("rate0000003")
    assert prefetched_rate.rate_limited is True


def test_ordered_prefetch_commits_oldest_to_newest_even_when_worker_2_finishes_first(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    store = ProgressStore(state_dir / "progress.jsonl")
    store.load()

    committed_order: list[str] = []
    vid2_done = threading.Event()

    class _OrderedFakeMarker:
        def __init__(self, *, session_cookiefile: Path | None, cookies_from_browser: tuple[str, ...] | None) -> None:
            return None

        def __enter__(self) -> "_OrderedFakeMarker":
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
        ) -> None:
            return None

        def prefetch_player(self, video_id: str) -> PrefetchedPlayer:
            # Make video #1 wait briefly until video #2 completes to prove commit stays ordered!
            if video_id == "oldest00001":
                vid2_done.wait(timeout=2.0)
            if video_id == "second00002":
                vid2_done.set()
            return PrefetchedPlayer(
                video_id=video_id,
                outcome=None,
                playback_url=f"https://example.com/pb/{video_id}?len=10",
                watchtime_url=f"https://example.com/wt/{video_id}?len=10",
            )

        def commit_watchtime(self, prefetched: PrefetchedPlayer) -> tuple[MigrationStatus, str | None]:
            committed_order.append(prefetched.video_id)
            return (MigrationStatus.WATCHED, None)

    entries = [
        WatchEntry("oldest00001", "https://www.youtube.com/watch?v=oldest00001", "First", "Chan", "2024-01-01", 100.0, 2),
        WatchEntry("second00002", "https://www.youtube.com/watch?v=second00002", "Second", "Chan", "2024-01-02", 200.0, 1),
        WatchEntry("newest00003", "https://www.youtube.com/watch?v=newest00003", "Third", "Chan", "2024-01-03", 300.0, 0),
    ]
    out = io.StringIO()

    with patch("yt_history_migrator.importer.FastYoutubeMarker", _OrderedFakeMarker):
        summary = run_migration(
            entries,
            store,
            state_dir,
            cookies_path=cookies,
            min_delay=0.0,
            max_delay=0.0,
            prefetch_workers=2,
            use_fast_engine=True,
            sleep_fn=lambda _secs: None,
            out=out,
        )

    assert summary.watched == 3
    assert committed_order == ["oldest00001", "second00002", "newest00003"]
    for vid in committed_order:
        assert store.should_skip(vid) is True
