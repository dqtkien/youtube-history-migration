"""Isolated browser sign-in and cookie extraction for destination YouTube accounts."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import http.cookiejar
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import TextIO
import urllib.request

import websockets
import yt_dlp.cookies
from yt_dlp.cookies import YoutubeDLCookieJar

YOUTUBE_LOGIN_URL = (
    "https://accounts.google.com/ServiceLogin?service=youtube&continue=https://www.youtube.com"
)
SUPPORTED_BROWSERS = ("chrome", "firefox", "brave")
_CHROMIUM_BROWSERS = frozenset({"chrome", "brave"})
_YOUTUBE_AUTH_COOKIE_NAMES = frozenset({"SAPISID", "__Secure-1PSID", "__Secure-3PSID", "SID"})
_MAC_BROWSER_BINARIES: dict[str, str] = {
    "firefox": "/Applications/Firefox.app/Contents/MacOS/firefox",
    "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "brave": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
}
_ACCOUNT_NAME_RE = re.compile(r'"accountName":\{"simpleText":"([^"]+)"\}')
_ACCOUNT_EMAIL_RE = re.compile(r'"email":\{"simpleText":"([^"]+)"\}')
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class BrowserLoginSession:
    """Running isolated browser process waiting for the user to sign in."""

    browser_name: str
    profile_dir: Path
    proc: subprocess.Popen[bytes]

    def is_running(self) -> bool:
        """Return True while the browser process is still alive."""
        return self.proc.poll() is None


@dataclass(frozen=True)
class AccountVerification:
    """Result of finalizing an isolated browser login session."""

    cookies_path: Path
    browser_name: str
    account_label: str | None


def _find_browser_binary(browser_name: str) -> str | None:
    mac_path = _MAC_BROWSER_BINARIES.get(browser_name)
    if mac_path is not None and Path(mac_path).is_file():
        return mac_path
    return shutil.which(browser_name)


def list_available_browsers() -> list[str]:
    """Return supported browser names installed on this machine."""
    return [name for name in SUPPORTED_BROWSERS if _find_browser_binary(name) is not None]


def detect_login_browser(preferred: str | None = None) -> tuple[str, str]:
    """Select an installed browser (preferring Chrome via CDP, then Firefox/Brave)."""
    if preferred is not None:
        normalized = preferred.strip().lower()
        if normalized not in SUPPORTED_BROWSERS:
            raise ValueError(
                f"Unsupported browser '{preferred}'. Choose from: {', '.join(SUPPORTED_BROWSERS)}"
            )
        binary = _find_browser_binary(normalized)
        if binary is None:
            raise FileNotFoundError(f"Browser '{normalized}' executable not found on this system.")
        return (normalized, binary)

    for candidate in SUPPORTED_BROWSERS:
        binary = _find_browser_binary(candidate)
        if binary is not None:
            return (candidate, binary)

    raise FileNotFoundError(
        "No supported browser found (checked Google Chrome, Firefox, Brave). "
        "Install one or pass --cookies <cookies.txt>."
    )


def _build_browser_command(browser_name: str, binary_path: str, profile_dir: Path) -> list[str]:
    abs_profile = str(profile_dir.resolve())
    if browser_name == "firefox":
        return [
            binary_path,
            "--new-instance",
            "--profile",
            abs_profile,
            YOUTUBE_LOGIN_URL,
        ]
    return [
        binary_path,
        f"--user-data-dir={abs_profile}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        YOUTUBE_LOGIN_URL,
    ]


def _close_browser_process(
    proc: subprocess.Popen[bytes],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    # Give SQLite WAL files a moment to flush cleanly after browser exit.
    sleep_fn(0.5)


def has_youtube_auth_cookies(jar: YoutubeDLCookieJar) -> bool:
    """Return True when the cookie jar contains an authenticated YouTube session cookie."""
    for cookie in jar:
        domain = (cookie.domain or "").lower()
        if "youtube.com" not in domain:
            continue
        if (
            cookie.name in _YOUTUBE_AUTH_COOKIE_NAMES
            and isinstance(cookie.value, str)
            and bool(cookie.value)
        ):
            return True
    return False


def _sanitize_jar_strings(jar: YoutubeDLCookieJar) -> YoutubeDLCookieJar:
    """Drop any undecrypted bytes cookie values so MozillaCookieJar.save never crashes."""
    cleaned = YoutubeDLCookieJar()
    for cookie in jar:
        if isinstance(cookie.name, str) and isinstance(cookie.value, str):
            cleaned.set_cookie(cookie)
    return cleaned


def _read_devtools_ws_url(profile_dir: Path, timeout: float = 6.0) -> str:
    dt_file = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dt_file.is_file():
            lines = [line.strip() for line in dt_file.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
            if len(lines) >= 2 and lines[0].isdigit():
                return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for DevToolsActivePort in {profile_dir}")


async def _cdp_fetch_cookies(ws_url: str) -> list[dict[str, object]]:
    async with websockets.connect(
        ws_url,
        open_timeout=4.0,
        close_timeout=2.0,
        max_size=8 * 1024 * 1024,
    ) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        raw_reply = await ws.recv()

    reply_text = raw_reply.decode("utf-8") if isinstance(raw_reply, bytes) else raw_reply
    decoded: object = json.loads(reply_text)
    if not isinstance(decoded, dict):
        raise ValueError("Unexpected CDP response payload")
    result = decoded.get("result")
    if not isinstance(result, dict):
        raise ValueError("Missing result object in CDP Storage.getCookies response")
    cookies_obj = result.get("cookies")
    if not isinstance(cookies_obj, list):
        raise ValueError("Missing cookies list in CDP Storage.getCookies response")
    return [item for item in cookies_obj if isinstance(item, dict)]


def _cdp_cookies_to_jar(raw_cookies: list[dict[str, object]]) -> YoutubeDLCookieJar:
    jar = YoutubeDLCookieJar()
    for item in raw_cookies:
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        path = item.get("path")
        secure = item.get("secure")
        exp = item.get("expires")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if not isinstance(domain, str) or not domain:
            continue
        cookie_path = path if isinstance(path, str) and path else "/"
        is_secure = bool(secure) if isinstance(secure, bool) else False
        expires = int(exp) if isinstance(exp, (int, float)) and exp > 0 else None
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=cookie_path,
                path_specified=True,
                secure=is_secure,
                expires=expires,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    return jar


def extract_chromium_cookies_cdp(
    browser_name: str,
    profile_dir: Path,
    *,
    live_proc: subprocess.Popen[bytes] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> YoutubeDLCookieJar:
    """Extract decrypted cookies (including Chrome v11+) natively via CDP Storage.getCookies."""
    resolved_dir = profile_dir.resolve()
    dt_file = resolved_dir / "DevToolsActivePort"

    if live_proc is not None and live_proc.poll() is None and dt_file.is_file():
        try:
            ws_url = _read_devtools_ws_url(resolved_dir, timeout=2.0)
            raw_cookies = asyncio.run(_cdp_fetch_cookies(ws_url))
            return _cdp_cookies_to_jar(raw_cookies)
        except Exception:
            pass

    if live_proc is not None:
        _close_browser_process(live_proc, sleep_fn=sleep_fn)

    _, binary_path = detect_login_browser(browser_name)
    dt_file.unlink(missing_ok=True)
    helper_cmd = [
        binary_path,
        f"--user-data-dir={resolved_dir}",
        "--headless=new",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    helper_proc = subprocess.Popen(helper_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = _read_devtools_ws_url(resolved_dir, timeout=6.0)
        raw_cookies = asyncio.run(_cdp_fetch_cookies(ws_url))
        return _cdp_cookies_to_jar(raw_cookies)
    finally:
        _close_browser_process(helper_proc, sleep_fn=sleep_fn)


def _decode_json_snippet(raw: str) -> str:
    try:
        decoded: object = json.loads(f'"{raw}"')
        return decoded if isinstance(decoded, str) else raw
    except json.JSONDecodeError:
        return raw


def probe_youtube_account_label(jar: YoutubeDLCookieJar) -> str | None:
    """Best-effort fetch of signed-in YouTube channel name / email from /account."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        "https://www.youtube.com/account",
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with opener.open(request, timeout=4.0) as response:
            body = response.read(512_000).decode("utf-8", errors="ignore")
    except Exception:
        return None

    name_match = _ACCOUNT_NAME_RE.search(body)
    email_match = _ACCOUNT_EMAIL_RE.search(body)
    name = _decode_json_snippet(name_match.group(1)).strip() if name_match else None
    email = _decode_json_snippet(email_match.group(1)).strip() if email_match else None

    if name and email and name != email:
        return f"{name} ({email})"
    return name or email


def write_account_meta(state_dir: Path, *, account_label: str | None, browser_name: str) -> None:
    """Persist verified account label so UI refreshes remember the connected email/channel."""
    state_dir.mkdir(parents=True, exist_ok=True)
    meta_path = state_dir / "account-meta.json"
    payload = {
        "accountLabel": account_label,
        "browserName": browser_name,
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def start_browser_login_session(
    *,
    state_dir: Path,
    preferred_browser: str | None = None,
    fresh_profile: bool = False,
) -> BrowserLoginSession:
    """Launch an isolated browser window pointing to Google/YouTube Sign-In."""
    browser_name, binary_path = detect_login_browser(preferred_browser)
    profile_dir = state_dir / f"{browser_name}-auth-profile"
    if fresh_profile and profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    command = _build_browser_command(browser_name, binary_path, profile_dir)
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return BrowserLoginSession(browser_name=browser_name, profile_dir=profile_dir, proc=proc)


def finalize_browser_login_session(
    session: BrowserLoginSession,
    *,
    cookies_out: Path,
    sleep_fn: Callable[[float], None] = time.sleep,
    probe_label: bool = True,
) -> AccountVerification:
    """Close browser window, extract decrypted YouTube cookies, and save cookies.txt."""
    if session.browser_name in _CHROMIUM_BROWSERS:
        try:
            raw_jar = extract_chromium_cookies_cdp(
                session.browser_name,
                session.profile_dir,
                live_proc=session.proc,
                sleep_fn=sleep_fn,
            )
        finally:
            _close_browser_process(session.proc, sleep_fn=sleep_fn)
    else:
        _close_browser_process(session.proc, sleep_fn=sleep_fn)
        raw_jar = yt_dlp.cookies.extract_cookies_from_browser(
            session.browser_name,
            profile=str(session.profile_dir.resolve()),
        )

    jar = _sanitize_jar_strings(raw_jar)
    if not has_youtube_auth_cookies(jar):
        raise ValueError(
            "No YouTube login session found. Please sign in to your new Google account "
            "on youtube.com before confirming."
        )

    cookies_out.parent.mkdir(parents=True, exist_ok=True)
    jar.save(str(cookies_out), ignore_discard=True, ignore_expires=True)
    label = probe_youtube_account_label(jar) if probe_label else None
    write_account_meta(
        session.profile_dir.parent,
        account_label=label,
        browser_name=session.browser_name,
    )
    return AccountVerification(
        cookies_path=cookies_out,
        browser_name=session.browser_name,
        account_label=label,
    )


def interactive_browser_login(
    *,
    cookies_out: Path,
    state_dir: Path,
    preferred_browser: str | None = None,
    fresh_profile: bool = False,
    input_fn: Callable[[str], str] = input,
    sleep_fn: Callable[[float], None] = time.sleep,
    out: TextIO | None = None,
) -> Path:
    """Open an isolated browser window for destination Gmail login and save cookies.txt."""
    output = out if out is not None else sys.stdout
    session = start_browser_login_session(
        state_dir=state_dir,
        preferred_browser=preferred_browser,
        fresh_profile=fresh_profile,
    )
    print(
        f"1. Launching an isolated {session.browser_name.capitalize()} window for YouTube sign-in...",
        file=output,
    )
    print(
        "2. Sign in with your NEW Gmail / YouTube account (and pick the target channel if asked).",
        file=output,
    )
    print(
        "3. Wait until youtube.com finishes loading as your new account, then return here and press Enter.\n"
        "   (The isolated browser window will close automatically so YouTube cannot rotate cookies.)",
        file=output,
        flush=True,
    )

    try:
        input_fn("\nPress Enter here AFTER signing in to your NEW YouTube account... ")
    finally:
        _close_browser_process(session.proc, sleep_fn=sleep_fn)

    verification = finalize_browser_login_session(
        session,
        cookies_out=cookies_out,
        sleep_fn=sleep_fn,
        probe_label=False,
    )
    print(f"Saved authenticated YouTube cookies to {verification.cookies_path}", file=output)
    return verification.cookies_path
