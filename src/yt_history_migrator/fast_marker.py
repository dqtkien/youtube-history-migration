"""Fast Keep-Alive InnerTube player + watchtime marker with ordered prefetch support."""

from dataclasses import dataclass
from pathlib import Path
import random
import threading
from types import TracebackType
from typing import TypedDict
import urllib.parse

import requests
from requests.adapters import HTTPAdapter
import yt_dlp

from yt_history_migrator.models import MigrationStatus

_CPN_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
_UNAVAILABLE_STATUSES = frozenset({"ERROR", "UNPLAYABLE", "CONTENT_CHECK_REQUIRED"})
_UNAVAILABLE_LOGIN_REASONS = ("private", "age", "inappropriate", "members-only")
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class SpeedPreset:
    """Throughput and pacing configuration for watch history replay."""

    name: str
    min_delay: float
    max_delay: float
    cooldown_every: int
    cooldown_seconds: float
    prefetch_workers: int
    estimated_seconds_per_video: float


SPEED_PRESETS: dict[str, SpeedPreset] = {
    "fast": SpeedPreset("fast", 0.15, 0.35, 250, 5.0, 4, 0.40),
    "turbo": SpeedPreset("turbo", 0.03, 0.10, 400, 5.0, 6, 0.20),
    "safe": SpeedPreset("safe", 0.80, 1.40, 150, 10.0, 2, 1.30),
}


@dataclass(frozen=True)
class PrefetchedPlayer:
    """Result of prefetching YouTube InnerTube /player metadata for one video."""

    video_id: str
    outcome: MigrationStatus | None
    reason: str | None = None
    playback_url: str | None = None
    watchtime_url: str | None = None
    rate_limited: bool = False


class _MarkerYdlOptions(TypedDict, total=False):
    quiet: bool
    no_warnings: bool
    cookiefile: str
    cookiesfrombrowser: tuple[str, ...]


def _extract_tracking_urls(payload: dict[str, object]) -> tuple[str | None, str | None]:
    tracking = payload.get("playbackTracking")
    if not isinstance(tracking, dict):
        return (None, None)
    pb_obj = tracking.get("videostatsPlaybackUrl")
    wt_obj = tracking.get("videostatsWatchtimeUrl")
    pb_url = pb_obj.get("baseUrl") if isinstance(pb_obj, dict) else None
    wt_url = wt_obj.get("baseUrl") if isinstance(wt_obj, dict) else None
    pb_str = pb_url if isinstance(pb_url, str) and pb_url else None
    wt_str = wt_url if isinstance(wt_url, str) and wt_url else None
    return (pb_str, wt_str)


def _extract_playability_info(payload: dict[str, object]) -> tuple[str, str]:
    ps = payload.get("playabilityStatus")
    if not isinstance(ps, dict):
        return ("", "")
    status_raw = ps.get("status")
    status_str = status_raw if isinstance(status_raw, str) else ""
    reason_raw = ps.get("reason")
    reason_str = reason_raw if isinstance(reason_raw, str) else ""

    error_screen = ps.get("errorScreen")
    if not isinstance(error_screen, dict):
        return (status_str, reason_str)
    interstitial = error_screen.get("playerInterstitialRenderer")
    if not isinstance(interstitial, dict):
        return (status_str, reason_str)
    content = interstitial.get("content")
    if not isinstance(content, dict):
        return (status_str, reason_str)
    vm = content.get("interstitialViewModel")
    if not isinstance(vm, dict):
        return (status_str, reason_str)
    desc = vm.get("description")
    desc_text = desc.get("content") if isinstance(desc, dict) else None
    if isinstance(desc_text, str) and desc_text:
        return (status_str, desc_text)
    return (status_str, reason_str)


def _classify_missing_tracking(status_str: str, reason_str: str) -> tuple[MigrationStatus, str]:
    if status_str in _UNAVAILABLE_STATUSES:
        return (MigrationStatus.UNAVAILABLE, reason_str or f"Playability status: {status_str}")
    lowered_reason = reason_str.lower()
    if status_str == "LOGIN_REQUIRED" and any(p in lowered_reason for p in _UNAVAILABLE_LOGIN_REASONS):
        return (MigrationStatus.UNAVAILABLE, reason_str or "Private or age-restricted video")
    if status_str == "LOGIN_REQUIRED":
        return (MigrationStatus.AUTH_FAILED, reason_str or "YouTube requested sign-in")
    return (
        MigrationStatus.AUTH_FAILED,
        reason_str or "Missing playbackTracking URLs (session may be logged out)",
    )


def _build_ping_url(raw_url: str, cpn: str, *, is_full: bool) -> str:
    parsed_url = urllib.parse.urlparse(raw_url)
    qs = urllib.parse.parse_qs(parsed_url.query)
    raw_len = (qs.get("len") or ["1.5"])[0]
    try:
        length_val = max(0.5, float(raw_len) - 1.0)
    except ValueError:
        length_val = 0.5
    video_length = [str(length_val)]
    qs.update({
        "ver": ["2"],
        "cpn": [cpn],
        "cmt": video_length,
        "el": ["detailpage"],
    })
    if is_full:
        qs.update({"st": ["0"], "et": video_length})
    return urllib.parse.urlunparse(
        parsed_url._replace(query=urllib.parse.urlencode(qs, True))
    )


class FastYoutubeMarker:
    """Bootstrap InnerTube session once and issue lightweight player + watchtime pings."""

    def __init__(
        self,
        *,
        session_cookiefile: Path | None,
        cookies_from_browser: tuple[str, ...] | None,
    ) -> None:
        self._session_cookiefile = session_cookiefile
        self._cookies_from_browser = cookies_from_browser
        self._ydl: yt_dlp.YoutubeDL | None = None
        self._ie: object | None = None
        self._ytcfg: dict[str, object] = {}
        self._context: dict[str, object] = {}
        self._http: requests.Session | None = None
        self._user_agent = _DEFAULT_USER_AGENT
        self._header_lock = threading.Lock()

    def __enter__(self) -> "FastYoutubeMarker":
        opts: _MarkerYdlOptions = {"quiet": True, "no_warnings": True}
        if self._session_cookiefile is not None:
            opts["cookiefile"] = str(self._session_cookiefile)
        if self._cookies_from_browser is not None:
            opts["cookiesfrombrowser"] = self._cookies_from_browser

        ydl = yt_dlp.YoutubeDL(opts)
        ydl.__enter__()
        self._ydl = ydl
        try:
            ie = ydl.get_info_extractor("Youtube")
            webpage = ie._download_webpage("https://www.youtube.com/", None, fatal=False) or ""
            ytcfg = ie.extract_ytcfg(None, webpage) or {}
            context = ie._extract_context(ytcfg=ytcfg, default_client="web")
            self._ie = ie
            self._ytcfg = ytcfg
            self._context = context

            headers = ie.generate_api_headers(ytcfg=ytcfg, default_client="web")
            ua = headers.get("User-Agent")
            if isinstance(ua, str) and ua:
                self._user_agent = ua

            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=12, pool_maxsize=12)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.cookies = ydl.cookiejar
            self._http = session
        except Exception:
            ydl.__exit__(None, None, None)
            self._ydl = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._ydl is not None:
            self._ydl.__exit__(exc_type, exc_val, exc_tb)
            self._ydl = None

    def _build_api_headers(self) -> dict[str, str]:
        if self._ie is None:
            raise RuntimeError("FastYoutubeMarker must be entered before calling APIs")
        with self._header_lock:
            raw = self._ie.generate_api_headers(ytcfg=self._ytcfg, default_client="web")  # type: ignore[attr-defined]
        headers = {str(k): str(v) for k, v in raw.items()}
        headers["Content-Type"] = "application/json"
        return headers

    def prefetch_player(self, video_id: str) -> PrefetchedPlayer:
        """Fetch /youtubei/v1/player metadata and tracking URLs (safe to run concurrently)."""
        if self._http is None:
            raise RuntimeError("FastYoutubeMarker HTTP session is not open")

        headers = self._build_api_headers()
        try:
            resp = self._http.post(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                json={"context": self._context, "videoId": video_id},
                headers=headers,
                timeout=15,
            )
        except requests.RequestException as exc:
            return PrefetchedPlayer(video_id=video_id, outcome=MigrationStatus.FAILED, reason=str(exc)[:300])

        if resp.status_code == 429:
            return PrefetchedPlayer(
                video_id=video_id,
                outcome=None,
                rate_limited=True,
                reason="HTTP 429 Too Many Requests",
            )
        if resp.status_code in (401, 403):
            return PrefetchedPlayer(
                video_id=video_id,
                outcome=MigrationStatus.AUTH_FAILED,
                reason=f"Player API HTTP {resp.status_code}",
            )
        if resp.status_code >= 400:
            return PrefetchedPlayer(
                video_id=video_id,
                outcome=MigrationStatus.FAILED,
                reason=f"Player API HTTP {resp.status_code}",
            )

        try:
            decoded: object = resp.json()
        except ValueError as exc:
            return PrefetchedPlayer(
                video_id=video_id,
                outcome=MigrationStatus.FAILED,
                reason=f"Invalid player JSON: {exc}",
            )

        if not isinstance(decoded, dict):
            return PrefetchedPlayer(
                video_id=video_id,
                outcome=MigrationStatus.FAILED,
                reason="Unexpected non-object player JSON response",
            )

        pb_url, wt_url = _extract_tracking_urls(decoded)
        if pb_url is not None and wt_url is not None:
            return PrefetchedPlayer(
                video_id=video_id,
                outcome=None,
                playback_url=pb_url,
                watchtime_url=wt_url,
            )

        status_str, reason_str = _extract_playability_info(decoded)
        outcome, detail = _classify_missing_tracking(status_str, reason_str)
        return PrefetchedPlayer(video_id=video_id, outcome=outcome, reason=detail[:400])

    def commit_watchtime(self, prefetched: PrefetchedPlayer) -> tuple[MigrationStatus, str | None]:
        """Fire playback & watchtime tracking pings sequentially to record Watch History."""
        if prefetched.outcome is not None:
            return (prefetched.outcome, prefetched.reason)
        if prefetched.playback_url is None or prefetched.watchtime_url is None:
            return (MigrationStatus.AUTH_FAILED, "Missing tracking URLs")
        if self._http is None:
            raise RuntimeError("FastYoutubeMarker HTTP session is not open")

        cpn = "".join(_CPN_ALPHABET[random.randint(0, 256) & 63] for _ in range(16))
        ping_urls = (
            _build_ping_url(prefetched.playback_url, cpn, is_full=False),
            _build_ping_url(prefetched.watchtime_url, cpn, is_full=True),
        )
        req_headers = {"User-Agent": self._user_agent}
        for url in ping_urls:
            try:
                resp = self._http.get(url, headers=req_headers, timeout=15)
            except requests.RequestException as exc:
                return (MigrationStatus.FAILED, str(exc)[:300])
            if resp.status_code == 429:
                return (MigrationStatus.FAILED, "Watchtime HTTP 429")
            if resp.status_code >= 400:
                return (MigrationStatus.AUTH_FAILED, f"Watchtime HTTP {resp.status_code}")
        return (MigrationStatus.WATCHED, None)
