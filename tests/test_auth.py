"""Tests for isolated browser login and YouTube auth cookie extraction."""

import http.cookiejar
import io
from pathlib import Path
from unittest.mock import patch
import pytest
from yt_dlp.cookies import YoutubeDLCookieJar

from yt_history_migrator.auth import (
    YOUTUBE_LOGIN_URL,
    _cdp_cookies_to_jar,
    _sanitize_jar_strings,
    has_youtube_auth_cookies,
    interactive_browser_login,
)
from yt_history_migrator.cli import build_parser, main
from yt_history_migrator.web_server import create_ui_server


def _make_cookie(name: str, value: str, domain: str = ".youtube.com") -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path="/",
        path_specified=True,
        secure=True,
        expires=1893456000,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
    )


class _FakePopen:
    """Minimal typed stand-in for subprocess.Popen during unit tests."""

    def __init__(self, command: list[str], *, stdout: int | None = None, stderr: int | None = None) -> None:
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.terminated = False
        self.waited = False
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        self._returncode = 0
        return 0

    def kill(self) -> None:
        self._returncode = -9


def test_has_youtube_auth_cookies_requires_auth_cookie_on_youtube_domain() -> None:
    anon_jar = YoutubeDLCookieJar()
    anon_jar.set_cookie(_make_cookie("PREF", "tz=Asia.Bangkok"))
    anon_jar.set_cookie(_make_cookie("VISITOR_INFO1_LIVE", "xyz"))
    anon_jar.set_cookie(_make_cookie("SAPISID", "val", domain=".google.com"))
    assert has_youtube_auth_cookies(anon_jar) is False

    auth_jar = YoutubeDLCookieJar()
    auth_jar.set_cookie(_make_cookie("PREF", "tz=Asia.Bangkok"))
    auth_jar.set_cookie(_make_cookie("SAPISID", "secret-sapisid-token", domain=".youtube.com"))
    assert has_youtube_auth_cookies(auth_jar) is True


def test_interactive_browser_login_launches_firefox_closes_and_saves_cookies(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    cookies_out = tmp_path / "data" / "cookies.txt"

    auth_jar = YoutubeDLCookieJar()
    auth_jar.set_cookie(_make_cookie("__Secure-1PSID", "auth-token-123", domain=".youtube.com"))

    spawned: list[_FakePopen] = []

    def fake_popen(command: list[str], stdout: int | None = None, stderr: int | None = None) -> _FakePopen:
        proc = _FakePopen(command, stdout=stdout, stderr=stderr)
        spawned.append(proc)
        return proc

    out = io.StringIO()
    with (
        patch("yt_history_migrator.auth.detect_login_browser", return_value=("firefox", "/usr/bin/firefox")),
        patch("yt_history_migrator.auth.subprocess.Popen", side_effect=fake_popen),
        patch("yt_history_migrator.auth.yt_dlp.cookies.extract_cookies_from_browser", return_value=auth_jar) as extract_mock,
    ):
        saved_path = interactive_browser_login(
            cookies_out=cookies_out,
            state_dir=state_dir,
            input_fn=lambda _prompt: "",
            sleep_fn=lambda _secs: None,
            out=out,
        )

    assert saved_path == cookies_out
    assert cookies_out.exists()
    content = cookies_out.read_text(encoding="utf-8")
    assert "__Secure-1PSID" in content
    assert "auth-token-123" in content

    assert len(spawned) == 1
    cmd = spawned[0].command
    assert cmd[0] == "/usr/bin/firefox"
    assert "--new-instance" in cmd
    assert "--profile" in cmd
    assert cmd[-1] == YOUTUBE_LOGIN_URL
    assert spawned[0].terminated is True
    assert spawned[0].waited is True

    extract_mock.assert_called_once_with(
        "firefox",
        profile=str((state_dir / "firefox-auth-profile").resolve()),
    )


def test_interactive_browser_login_rejects_unauthenticated_session(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    cookies_out = tmp_path / "data" / "cookies.txt"

    empty_jar = YoutubeDLCookieJar()
    empty_jar.set_cookie(_make_cookie("VISITOR_INFO1_LIVE", "anon-only", domain=".youtube.com"))

    spawned: list[_FakePopen] = []

    def fake_popen(command: list[str], stdout: int | None = None, stderr: int | None = None) -> _FakePopen:
        proc = _FakePopen(command, stdout=stdout, stderr=stderr)
        spawned.append(proc)
        return proc

    out = io.StringIO()
    with (
        patch("yt_history_migrator.auth.detect_login_browser", return_value=("firefox", "/usr/bin/firefox")),
        patch("yt_history_migrator.auth.subprocess.Popen", side_effect=fake_popen),
        patch("yt_history_migrator.auth.yt_dlp.cookies.extract_cookies_from_browser", return_value=empty_jar),
        pytest.raises(ValueError, match="No YouTube login session found"),
    ):
        interactive_browser_login(
            cookies_out=cookies_out,
            state_dir=state_dir,
            input_fn=lambda _prompt: "",
            sleep_fn=lambda _secs: None,
            out=out,
        )

    assert not cookies_out.exists()
    assert spawned[0].terminated is True


def test_cli_parser_supports_login_and_cookie_free_migrate() -> None:
    parser = build_parser()
    login_args = parser.parse_args(["login", "--browser", "firefox", "--fresh"])
    assert login_args.command == "login"
    assert login_args.browser == "firefox"
    assert login_args.fresh is True

    migrate_args = parser.parse_args(["migrate", "--takeout", "data/watch-history.html", "--limit", "5"])
    assert migrate_args.command == "migrate"
    assert migrate_args.cookies is None
    assert migrate_args.cookies_from_browser is None
    assert migrate_args.dry_run is False
    assert migrate_args.limit == 5


def test_cdp_cookies_to_jar_and_sanitize_drops_bytes_values(tmp_path: Path) -> None:
    raw_cookies: list[dict[str, object]] = [
        {
            "name": "SAPISID",
            "value": "valid-token",
            "domain": ".youtube.com",
            "path": "/",
            "secure": True,
            "expires": 1893456000.0,
        },
        {
            "name": "BROKEN",
            "value": b"v11-undecrypted",
            "domain": ".youtube.com",
        },
    ]
    jar = _cdp_cookies_to_jar(raw_cookies)
    bad_cookie = _make_cookie("RAWBYTES", "temp", domain=".youtube.com")
    bad_cookie.value = b"v11=bytes"  # type: ignore[assignment]
    jar.set_cookie(bad_cookie)

    assert has_youtube_auth_cookies(jar) is True
    sanitized = _sanitize_jar_strings(jar)
    assert len(sanitized) == 1

    out_path = tmp_path / "cookies.txt"
    sanitized.save(str(out_path), ignore_discard=True, ignore_expires=True)
    text = out_path.read_text(encoding="utf-8")
    assert "SAPISID" in text
    assert "RAWBYTES" not in text


def test_ui_command_returns_cleanly_when_existing_ui_already_running(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import threading

    server, _controller = create_ui_server(tmp_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bound_port = server.server_address[1]
    try:
        exit_code = main([
            "ui",
            "--host",
            "127.0.0.1",
            "--port",
            str(bound_port),
            "--project-dir",
            str(tmp_path),
            "--no-open",
        ])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"already running at http://127.0.0.1:{bound_port}" in captured.out


def test_stop_command_terminates_verified_ui_listener(capsys: pytest.CaptureFixture[str]) -> None:
    import signal

    killed: list[tuple[int, int]] = []
    poll_calls = 0

    def fake_find_pids(_port: int) -> list[int]:
        nonlocal poll_calls
        poll_calls += 1
        return [99999] if poll_calls == 1 else []

    with (
        patch("yt_history_migrator.cli._find_listening_pids", side_effect=fake_find_pids),
        patch("yt_history_migrator.cli._is_existing_ui_running", return_value=True),
        patch("yt_history_migrator.cli.os.kill", side_effect=lambda pid, sig: killed.append((pid, sig))),
    ):
        exit_code = main(["stop", "--port", "8765"])

    assert exit_code == 0
    assert killed == [(99999, signal.SIGTERM)]
    captured = capsys.readouterr()
    assert "Stopped YouTube History Migrator UI on port 8765 (PID 99999)" in captured.out
