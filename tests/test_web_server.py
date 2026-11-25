"""Integration tests for the 3-step local Web UI server and REST API."""

from collections.abc import Iterator
import http.client
import io
import json
from pathlib import Path
import threading
import time
from types import TracebackType
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

import pytest

from yt_history_migrator.auth import AccountVerification, BrowserLoginSession
from yt_history_migrator.importer import YtDlpWatchLogger
from yt_history_migrator.web_server import WebAppController, create_ui_server


class _DummyProc:
    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        return None


class _FakeYoutubeDL:
    def __init__(self, ydl_opts: dict[str, object]) -> None:
        logger_obj = ydl_opts.get("logger")
        assert isinstance(logger_obj, YtDlpWatchLogger)
        self._logger = logger_obj

    def __enter__(self) -> "_FakeYoutubeDL":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None

    def extract_info(self, url: str, *, download: bool = False) -> dict[str, str]:
        assert download is False
        vid = url.split("v=")[-1]
        self._logger.debug(f"[youtube] {vid}: Marking watched")
        self._logger.debug(f"[youtube] {vid}: Marking fully watched")
        return {"id": vid}


def _build_sample_takeout_zip() -> bytes:
    payload = [
        {
            "header": "YouTube",
            "title": "Watched Older Video",
            "titleUrl": "https://www.youtube.com/watch?v=older123456",
            "subtitles": [{"name": "Chan A"}],
            "time": "2025-01-01T10:00:00Z",
        },
        {
            "header": "YouTube",
            "title": "Watched Newer Video",
            "titleUrl": "https://www.youtube.com/watch?v=newer123456",
            "subtitles": [{"name": "Chan B"}],
            "time": "2026-02-01T10:00:00Z",
        },
        {
            "header": "YouTube",
            "title": "Watched Promo Ad",
            "titleUrl": "https://www.youtube.com/watch?v=ad000000003",
            "details": [{"name": "From Google Ads"}],
            "time": "2026-02-02T10:00:00Z",
        },
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Takeout/YouTube and YouTube Music/history/watch-history.json",
            json.dumps(payload),
        )
    return buf.getvalue()


@pytest.fixture()
def running_ui(tmp_path: Path) -> Iterator[tuple[str, WebAppController]]:
    server, controller = create_ui_server(tmp_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield (base_url, controller)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            decoded: object = json.loads(resp.read().decode("utf-8"))
            assert isinstance(decoded, dict)
            return (resp.status, decoded)
    except urllib.error.HTTPError as err:
        decoded_err: object = json.loads(err.read().decode("utf-8"))
        assert isinstance(decoded_err, dict)
        return (err.code, decoded_err)


def test_ui_serves_html_and_bootstrap(running_ui: tuple[str, WebAppController]) -> None:
    base_url, _controller = running_ui

    with urllib.request.urlopen(f"{base_url}/", timeout=5) as resp:
        assert resp.status == 200
        html = resp.read().decode("utf-8")
        assert "<!DOCTYPE html>" in html
        assert '<meta name="color-scheme" content="light dark">' in html
        assert "<dialog" in html
        assert "<progress" in html

    status, boot = _request_json(f"{base_url}/api/bootstrap")
    assert status == 200
    assert isinstance(boot["csrfToken"], str)
    assert isinstance(boot["currentYear"], int)
    assert isinstance(boot["checkpoint"], dict)
    takeout = boot["takeout"]
    assert isinstance(takeout, dict)
    assert takeout["loaded"] is False


def test_ui_upload_auth_and_migration_flow(running_ui: tuple[str, WebAppController], tmp_path: Path) -> None:
    base_url, controller = running_ui
    _, boot = _request_json(f"{base_url}/api/bootstrap")
    csrf = str(boot["csrfToken"])
    zip_bytes = _build_sample_takeout_zip()

    # Reject upload without CSRF token.
    bad_status, _ = _request_json(
        f"{base_url}/api/takeout/upload",
        method="POST",
        headers={"X-Filename": "takeout.zip"},
        body=zip_bytes,
    )
    assert bad_status == http.client.FORBIDDEN

    # Upload valid .zip with CSRF token.
    up_status, up_data = _request_json(
        f"{base_url}/api/takeout/upload",
        method="POST",
        headers={"X-CSRF-Token": csrf, "X-Filename": "takeout.zip"},
        body=zip_bytes,
    )
    assert up_status == 200
    takeout = up_data["takeout"]
    assert isinstance(takeout, dict)
    assert takeout["loaded"] is True
    assert takeout["uniqueVideos"] == 2

    fake_session = BrowserLoginSession(
        browser_name="firefox",
        profile_dir=tmp_path / "state" / "firefox-auth-profile",
        proc=_DummyProc(),  # type: ignore[arg-type]
    )

    def fake_finalize(
        session: BrowserLoginSession,
        *,
        cookies_out: Path,
        probe_label: bool = True,
    ) -> AccountVerification:
        cookies_out.parent.mkdir(parents=True, exist_ok=True)
        cookies_out.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        return AccountVerification(
            cookies_path=cookies_out,
            browser_name=session.browser_name,
            account_label="New User (new@gmail.com)",
        )

    with (
        patch("yt_history_migrator.web_server.start_browser_login_session", return_value=fake_session),
        patch("yt_history_migrator.web_server.finalize_browser_login_session", side_effect=fake_finalize),
    ):
        s_code, s_body = _request_json(
            f"{base_url}/api/auth/start",
            method="POST",
            headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
            body=json.dumps({"browser": "firefox", "fresh": False}).encode("utf-8"),
        )
        assert s_code == 200
        assert s_body["loginWindowOpen"] is True

        c_code, c_body = _request_json(
            f"{base_url}/api/auth/complete",
            method="POST",
            headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
            body=b"{}",
        )
        assert c_code == 200
        assert c_body["authenticated"] is True
        assert c_body["accountLabel"] == "New User (new@gmail.com)"

    with (
        patch("yt_history_migrator.importer.yt_dlp.YoutubeDL", side_effect=lambda opts: _FakeYoutubeDL(opts)),
        patch("yt_history_migrator.importer.time.sleep", return_value=None),
        patch("yt_history_migrator.importer.random.uniform", return_value=0.0),
    ):
        m_code, _ = _request_json(
            f"{base_url}/api/migration/start",
            method="POST",
            headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
            body=json.dumps({"scope": "test5", "includeMusic": False, "excludeShorts": False}).encode("utf-8"),
        )
        assert m_code == 200

        deadline = time.monotonic() + 5.0
        final_status: dict[str, object] = {}
        while time.monotonic() < deadline:
            _, final_status = _request_json(f"{base_url}/api/migration/status")
            if final_status.get("finished"):
                break
            time.sleep(0.05)

    assert final_status.get("finished") is True
    assert final_status.get("phase") == "completed"
    assert final_status.get("watched") == 2
    assert final_status.get("remaining") == 0
    assert controller.progress_path.exists()


def test_ui_summary_custom_preview_reset_and_shutdown(
    running_ui: tuple[str, WebAppController],
    tmp_path: Path,
) -> None:
    base_url, controller = running_ui
    _, boot = _request_json(f"{base_url}/api/bootstrap")
    csrf = str(boot["csrfToken"])

    payload = [
        {
            "header": "YouTube",
            "title": "Watched Older Video",
            "titleUrl": "https://www.youtube.com/watch?v=older123456",
            "subtitles": [{"name": "Chan A"}],
            "time": "2023-05-01T10:00:00Z",
        },
        {
            "header": "YouTube Music",
            "title": "Watched Music Track",
            "titleUrl": "https://music.youtube.com/watch?v=music123456",
            "subtitles": [{"name": "Artist"}],
            "time": "2025-03-01T10:00:00Z",
        },
        {
            "header": "YouTube",
            "title": "Watched Recent Short",
            "titleUrl": "https://www.youtube.com/shorts/short123456",
            "subtitles": [{"name": "Chan B"}],
            "time": "2026-04-01T10:00:00Z",
        },
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "Takeout/YouTube and YouTube Music/history/watch-history.json",
            json.dumps(payload),
        )

    up_status, _ = _request_json(
        f"{base_url}/api/takeout/upload",
        method="POST",
        headers={"X-CSRF-Token": csrf, "X-Filename": "takeout.zip"},
        body=buf.getvalue(),
    )
    assert up_status == 200

    # Dynamic summary recalculates when includeMusic / excludeShorts toggle.
    sum_code, sum_body = _request_json(
        f"{base_url}/api/takeout/summary",
        method="POST",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        body=json.dumps({"includeMusic": True, "excludeShorts": True}).encode("utf-8"),
    )
    assert sum_code == 200
    takeout_summary = sum_body["takeout"]
    assert isinstance(takeout_summary, dict)
    assert takeout_summary["uniqueVideos"] == 2

    # Custom date preview filters accurately and returns sampleFirst.
    prev_code, prev_body = _request_json(
        f"{base_url}/api/takeout/preview",
        method="POST",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        body=json.dumps({
            "scope": "custom",
            "afterDate": "2025-01-01",
            "beforeDate": "2026-12-31",
            "includeMusic": True,
            "excludeShorts": False,
        }).encode("utf-8"),
    )
    assert prev_code == 200
    assert prev_body["targetCount"] == 2
    sample_first = prev_body["sampleFirst"]
    assert isinstance(sample_first, list) and len(sample_first) == 2

    # Invalid custom date range returns 400.
    bad_prev_code, _ = _request_json(
        f"{base_url}/api/takeout/preview",
        method="POST",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        body=json.dumps({
            "scope": "custom",
            "afterDate": "2026-05-01",
            "beforeDate": "2026-01-01",
        }).encode("utf-8"),
    )
    assert bad_prev_code == http.client.BAD_REQUEST

    # Write a checkpoint record and verify POST /api/progress/reset clears it.
    controller.progress_path.parent.mkdir(parents=True, exist_ok=True)
    controller.progress_path.write_text(
        json.dumps({
            "video_id": "older123456",
            "status": "watched",
            "title": "Older Video",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "reason": None,
        }) + "\n",
        encoding="utf-8",
    )
    reset_code, reset_body = _request_json(
        f"{base_url}/api/progress/reset",
        method="POST",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        body=b"{}",
    )
    assert reset_code == 200
    checkpoint = reset_body["checkpoint"]
    assert isinstance(checkpoint, dict)
    assert checkpoint["totalRecorded"] == 0
    assert not controller.progress_path.exists()

    # Auth start with resetProgress=True clears checkpoint before launching browser.
    controller.progress_path.write_text(
        json.dumps({
            "video_id": "older123456",
            "status": "watched",
            "title": "Older Video",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "reason": None,
        }) + "\n",
        encoding="utf-8",
    )
    fake_session = BrowserLoginSession(
        browser_name="firefox",
        profile_dir=tmp_path / "state" / "firefox-auth-profile",
        proc=_DummyProc(),  # type: ignore[arg-type]
    )
    with patch("yt_history_migrator.web_server.start_browser_login_session", return_value=fake_session):
        auth_code, _ = _request_json(
            f"{base_url}/api/auth/start",
            method="POST",
            headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
            body=json.dumps({"browser": "firefox", "fresh": True, "resetProgress": True}).encode("utf-8"),
        )
        assert auth_code == 200
    assert not controller.progress_path.exists()

    # POST /api/server/shutdown invokes the registered shutdown callback.
    called = threading.Event()
    controller.set_shutdown_callback(called.set)
    shut_code, shut_body = _request_json(
        f"{base_url}/api/server/shutdown",
        method="POST",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        body=b"{}",
    )
    assert shut_code == 200
    assert shut_body["stopping"] is True
    assert called.wait(timeout=2.0)


def test_server_close_does_not_hang_on_keepalive_socket(tmp_path: Path) -> None:
    server, _controller = create_ui_server(tmp_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request("GET", "/api/bootstrap")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()

        # Keep HTTP/1.1 Keep-Alive connection open while shutting down server.
        t0 = time.monotonic()
        server.shutdown()
        server.server_close()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
    finally:
        conn.close()
        thread.join(timeout=2)
