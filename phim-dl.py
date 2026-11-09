#!/usr/bin/env python3
"""phim-dl - download movies and series from phimkey.com and its clone sites (phimsea.com, ...).

How it works
------------
0. Site            set_site()             the clones run the same code on other domains: the domain
                                          is taken from the link (or --site).
1. Account         login(), import_cookies()   the site only works with an account: the session
                                               cookies (one set per domain) are kept in session.txt.
2. Movie info      load_target()          title, year, seasons and episodes (SvelteKit __data.json
                                          and the getEpisodes remote function).
3. Stream link     open_stream()          the getVideoSrc remote function hands out an HLS playlist.
4. Clean playlist  parse_playlist()       segment and key names are obfuscated (ROT6 + base64), the
                                          real names are restored; segments are AES-128 encrypted.
5. Size            estimate_size()        measures a few segments spread over the episode;
                   probe_resolution()     ffmpeg reads the real resolution from one segment.
6. Subtitles       save_subtitles()       the subtitle list comes from getSubtitles, every file is
                                          AES-GCM encrypted and saved next to the video as .srt.
7. Download        download_segments()    aria2c, many segments at once, broken segments are fetched
                                          again (without aria2c: Python threads).
8. Mux             make_mp4()             the segments are decrypted and piped into ffmpeg, which
                                          writes the MP4 without re-encoding.

Code map
--------
Console output   log(), colors, class Board (live progress bars)   (all user-facing text goes through t())
HTTP / account   http_get(), class Session, login(), import_cookies(), ensure_login()
Site data        devalue_loads(), devalue_dumps(), Session.page_data(), Session.remote()
Movie info       parse_target(), load_target(), list_episodes()
Stream           class Stream, open_stream(), parse_playlist(), deobfuscate(), decrypt_segment()
Size / quality   estimate_size(), probe_resolution()
Subtitles        pick_subtitles(), decrypt_subtitle(), save_subtitles()
Names            build_filename(), episode_label(), parse_selection(), parse_numbers()
Download / mux   download_segments(), _run_aria2(), _run_builtin(), make_mp4(), class Meter
Main flow        run() -> account_commands() / download_command(): pick_episodes(), resolve_jobs(),
                 download_jobs(), show_sizes(); errors stop with class Abort

Messages are bilingual: t("English", "Tiếng Việt"). Please add both when you add a message.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.cookiejar
import importlib.util
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

__version__ = "1.0.0"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
# phimkey.com and its clones run the same site on other domains, each with its own accounts.
# The domain is taken from the link (or --site); any other domain with the same site works too.
KNOWN_SITES = ("phimkey.com", "phimsea.com", "phimfix.com", "xemphim.in")
HOST = KNOWN_SITES[0]
SITE = f"https://{HOST}"
REFERER = SITE + "/"
SCRIPT_DIR = Path(__file__).resolve().parent
SESSION_FILE = SCRIPT_DIR / "session.txt"
_SSL_CTX = ssl.create_default_context()

# The player offers these servers. Server 1 is used by default; when it fails the others are tried.
SERVERS = ("1", "2", "3")

# Message language: "en" (default) or "vi". Set with --lang or the PHIM_DL_LANG environment variable.
LANG = "en"


def normalize_host(text: str) -> str:
    """'https://www.phimsea.com/title/...' -> 'phimsea.com'."""
    text = text.strip().lower()
    if "//" not in text:
        text = "https://" + text
    host = urllib.parse.urlparse(text).hostname or ""
    return host[4:] if host.startswith("www.") else host


def set_site(host: str) -> None:
    """Point every request at one domain (phimkey.com or a clone)."""
    global HOST, SITE, REFERER
    HOST = normalize_host(host) or KNOWN_SITES[0]
    SITE = f"https://{HOST}"
    REFERER = SITE + "/"


def is_site_cookie(domain: str) -> bool:
    domain = domain.lstrip(".").lower()
    return domain == HOST or domain.endswith("." + HOST)


def t(en: str, vi: str) -> str:
    """Pick the message for the current language."""
    return vi if LANG == "vi" else en


def detect_lang(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1].lower()
        if arg == "--lang" and i + 1 < len(argv):
            return argv[i + 1].lower()
    env = os.environ.get("PHIM_DL_LANG", "").lower()
    if env in ("en", "vi"):
        return env
    try:
        import locale
        name = (locale.getlocale()[0] or "").lower()
    except Exception:
        name = ""
    return "vi" if name.startswith(("vi", "vietnam")) else "en"


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Console output
# --------------------------------------------------------------------------- #

_print_lock = threading.RLock()
_COLOR = False      # turned on in main() when running in a terminal
_VT = False         # the terminal understands cursor movement (needed for the live board)
_TTY = sys.stdout.isatty()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _style(code: str):
    return lambda s: f"\x1b[{code}m{s}\x1b[0m" if _COLOR else str(s)


bold = _style("1")
dim = _style("2")
gray = _style("90")
red = _style("31")
green = _style("32")
yellow = _style("33")
cyan = _style("36")
bold_red = _style("1;31")
bold_green = _style("1;32")
bold_cyan = _style("1;36")


def _enable_vt() -> bool:
    """Switch on ANSI escape codes (Windows 10+ needs VT mode turned on by hand)."""
    if not _TTY or os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ok = False
        for handle_id in (-11, -12):   # stdout, stderr
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                ok = bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004)) or ok
        return ok
    except Exception:
        return False


def init_console(color: bool) -> None:
    global _COLOR, _VT
    _VT = _enable_vt()
    forced = bool(os.environ.get("FORCE_COLOR"))
    _COLOR = color and not os.environ.get("NO_COLOR") and (_VT or forced)


def visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def term_width() -> int:
    # A line exactly as wide as the window wraps, which would break erasing the board.
    return max(40, shutil.get_terminal_size((100, 24)).columns - 1)


def log(msg: str = "", err: bool = False) -> None:
    """Print a line above the progress board, then draw the board again under it."""
    with _print_lock:
        sys.stdout.write(board.erase())
        sys.stdout.flush()
        print(msg, file=sys.stderr if err else sys.stdout, flush=True)
        sys.stdout.write(board.render())
        sys.stdout.flush()


def warn(msg: str) -> None:
    log(f"  {yellow('!')} {yellow(msg)}", err=True)


def fmt_bytes(n: Optional[float]) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_eta(sec: Optional[float]) -> str:
    if sec is None or sec < 0 or sec > 99 * 3600:
        return "--:--"
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def _bar(frac: float, width: int) -> str:
    frac = max(0.0, min(1.0, frac))
    if _COLOR:
        # "━━━━━╸━━━━" style: the done part is colored, with half-cell precision.
        halves = int(frac * width * 2)
        full, half = divmod(halves, 2)
        done = "━" * full + ("╸" if half else "")
        rest = "━" * (width - full - (1 if half else 0))
        return (green if frac >= 1 else cyan)(done) + gray(rest)
    # No colors: solid / shaded blocks so the done and remaining parts still look different.
    eighths = int(frac * width * 8)
    full, part = divmod(eighths, 8)
    partial = " ▏▎▍▌▋▊▉"[part] if part else ""
    return "█" * full + partial + "░" * (width - full - (1 if partial else 0))


class Board:
    """Live progress board, redrawn in place 8 times per second.

        ⠹ S01E01   ━━━━━━━━━━━━━━╸━━━━━━━━━━   58.3%   412.0 MB / 706.5 MB   18.2 MB/s   ETA 00:16
        ⠼ S01E02   ━━━━━╸━━━━━━━━━━━━━━━━━━━   21.0%   148.6 MB / 707.9 MB   17.9 MB/s   ETA 00:31
        ───────────────────────────────────────────────────────────────────────────────────────
        Σ Total    ━━━━━━━╸━━━━━━━━━━━━━━━━━   32.1%   3/12 done  │  2.3 GB / 8.4 GB  │  36.1 MB/s  │  ETA 02:53

    One row per episode being downloaded, plus a total row when there is more than one episode.
    Outside a terminal (output redirected to a file) a plain line is printed every few seconds instead.
    """

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    PLAIN_EVERY = 5.0

    def __init__(self) -> None:
        self.enabled = False
        self.rows: dict[str, dict] = {}
        self.drawn = 0
        self.total = 0
        self.ok = self.failed = 0
        self.expected: dict[str, float] = {}   # label -> estimated bytes
        self.done_bytes = 0.0                  # bytes of finished episodes
        self.frame = 0
        self.started = time.monotonic()
        self._plain_last: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- life cycle ------------------------------------------------------- #

    def start(self, jobs: list[tuple[str, Optional[float]]]) -> None:
        self.enabled = _VT
        self.total = len(jobs)
        self.expected = {label: size or 0.0 for label, size in jobs}
        self.ok = self.failed = 0
        self.done_bytes = 0.0
        self.started = time.monotonic()
        if self.enabled:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
        with _print_lock:
            sys.stdout.write(self.erase())
            sys.stdout.flush()
            self.enabled = False
            self.rows.clear()

    def _loop(self) -> None:
        while not self._stop.wait(0.125):
            with _print_lock:
                self.frame = (self.frame + 1) % len(self.SPINNER)
                sys.stdout.write(self.erase() + self.render())
                sys.stdout.flush()

    # -- state ------------------------------------------------------------ #

    def update(self, label: str, **state) -> None:
        """state: pct, done, total, speed, eta, or note="..." for a text-only row."""
        with _print_lock:
            if state.get("note"):
                # Keep the numbers of a row that turns into a note, so the total does not jump back.
                state = {**self.rows.get(label, {}), "speed": None, "eta": None, **state}
            self.rows[label] = state
        if not self.enabled and not state.get("note"):
            now = time.monotonic()
            if now - self._plain_last.get(label, 0) >= self.PLAIN_EVERY:
                self._plain_last[label] = now
                log(f"  {label}: {state.get('pct') or 0:5.1f}%  {fmt_bytes(state.get('done'))}"
                    f" / {fmt_bytes(state.get('total'))}  {fmt_bytes(state.get('speed') or 0)}/s"
                    f"  ETA {fmt_eta(state.get('eta'))}")

    def finish(self, label: str, ok: bool, size: float = 0) -> None:
        """Take an episode off the board (call before printing its done / failed line)."""
        with _print_lock:
            self.rows.pop(label, None)
            if ok:
                self.ok += 1
                self.done_bytes += size
            else:
                self.failed += 1
                self.expected.pop(label, None)

    # -- drawing ---------------------------------------------------------- #

    def erase(self) -> str:
        if not self.drawn:
            return ""
        # Move up to the first board line and clear to the end of the screen.
        seq, self.drawn = f"\x1b[{self.drawn}F\x1b[J", 0
        return seq

    # Columns after the bar: (name, width, importance). When the terminal is too narrow the
    # least important column is dropped first; every row uses the same columns so they line up.
    COLUMNS = (("pct", 6, 9), ("bytes", 21, 2), ("speed", 10, 4), ("eta", 11, 3))
    GAP = "   "

    def render(self) -> str:
        if not self.enabled or not self.rows:
            return ""
        width = term_width()
        rows = [(label, st) for label, st in self.rows.items()]
        total_label = self._total_label()
        labels = [label for label, _ in rows] + ([total_label] if self.total > 1 else [])
        label_w = min(max(len(label) for label in labels), width // 4)
        head_w = 4 + label_w + len(self.GAP)

        cols = list(self.COLUMNS)
        while True:
            bar_w = min(30, width - head_w - sum(w + len(self.GAP) for _, w, _ in cols))
            if bar_w >= 10 or len(cols) <= 1:
                break
            cols.remove(min(cols, key=lambda c: c[2]))
        bar_w = max(0, bar_w)

        spin = cyan(self.SPINNER[self.frame])
        lines = []
        for label, st in rows:
            head = f"  {spin} {bold(self._fit(label, label_w))}{self.GAP}"
            if st.get("note"):
                lines.append(self._clip(head + cyan(st["note"]), width))
            else:
                lines.append(self._clip(head + self._cells(st, bar_w, cols), width))
        if self.total > 1:
            total_row = self._clip(self._total_row(total_label, label_w, bar_w, cols), width)
            rule_w = max(visible_len(line) for line in lines + [total_row]) - 2
            lines += ["  " + gray("─" * rule_w), total_row]
        self.drawn = len(lines)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _fit(label: str, w: int) -> str:
        return (label if len(label) <= w else label[:w - 1] + "…").ljust(w)

    @staticmethod
    def _clip(line: str, width: int) -> str:
        return line if visible_len(line) <= width else _ANSI_RE.sub("", line)[:width]

    def _cells(self, st: dict, bar_w: int, cols: list) -> str:
        frac = max(0.0, min(1.0, (st.get("pct") or 0.0) / 100))
        done, total, speed = st.get("done") or 0, st.get("total"), st.get("speed")
        out = _bar(frac, bar_w) if bar_w else ""
        for name, w, _ in cols:
            if name == "pct":
                cell = bold(f"{frac * 100:5.1f}%".rjust(w))
            elif name == "bytes":
                cell = (f"{fmt_bytes(done):>9} / {fmt_bytes(total):<9}" if total else f"{fmt_bytes(done):>9}").ljust(w)
            elif name == "speed":
                cell = cyan(f"{(fmt_bytes(speed) + '/s') if speed else '--/s':>{w}}")
            else:
                eta = fmt_eta(st.get("eta"))
                cell = "ETA " + yellow(f"{eta:>{w - 4}}")
            out += (self.GAP if out else "") + cell
        return out

    def _total_label(self) -> str:
        label = f"{t('Total', 'Tổng')} {self.ok + self.failed}/{self.total}"
        return label + (f" ✗{self.failed}" if self.failed else "")

    def _total_row(self, label: str, label_w: int, bar_w: int, cols: list) -> str:
        active = list(self.rows.values())
        got = self.done_bytes + sum((st.get("done") or 0) for st in active)
        speed = sum((st.get("speed") or 0) for st in active)
        finished = self.ok + self.failed
        # Each running episode counts by its own percentage, so the total bar moves smoothly.
        frac = (finished + sum((st.get("pct") or 0) / 100 for st in self.rows.values())) / max(1, self.total)
        expected = sum(self.expected.values())
        if expected and all(self.expected.values()):
            total = max(expected, got)
            eta = (total - got) / speed if speed else None
        else:
            total = None
            elapsed = time.monotonic() - self.started
            eta = elapsed / frac * (1 - frac) if frac > 0.01 else None
        fitted = self._fit(label, label_w)
        name = bold(fitted)
        if "✗" in fitted:
            before, after = fitted.split("✗", 1)
            name = bold(before) + bold_red("✗" + after)
        st = {"pct": frac * 100, "done": got, "total": total, "speed": speed, "eta": eta}
        return f"  {bold_cyan('Σ')} {name}{self.GAP}" + self._cells(st, bar_w, cols)


board = Board()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _is_transient(error: Exception) -> bool:
    """Short network problems worth retrying (dropped connection, timeout, busy server)."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (429, 500, 502, 503, 504)
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


def http_get(url: str, binary: bool = False, tries: int = 3):
    """GET a public file (playlist, key) from the video servers. No cookies are sent there."""
    headers = {"User-Agent": USER_AGENT, "Referer": REFERER}
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
                data = resp.read()
                return data if binary else data.decode("utf-8", "replace")
        except Exception as ex:
            if attempt == tries - 1 or not _is_transient(ex):
                raise
            time.sleep(1.5 * (attempt + 1))
    raise AssertionError("unreachable")


def describe_error(url: str, error: Exception) -> str:
    host = urllib.parse.urlparse(url).hostname or t("the server", "máy chủ")
    if isinstance(error, urllib.error.HTTPError):
        cf = " (Cloudflare)" if error.headers.get("CF-RAY") else ""
        return t(f"HTTP {error.code} from {host}{cf}", f"HTTP {error.code} từ {host}{cf}")
    if isinstance(error, urllib.error.URLError):
        return t(f"network error reaching {host}: {error.reason}", f"lỗi mạng tới {host}: {error.reason}")
    return str(error).strip() or type(error).__name__


# --------------------------------------------------------------------------- #
# Site data: SvelteKit's devalue format
# --------------------------------------------------------------------------- #

def devalue_loads(data):
    """Rebuild a value stored in devalue's flat format: a list where objects point to other
    entries by index. Negative numbers are special values (-1 = undefined, ...)."""
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, int):
        return {-1: None, -2: None, -3: float("nan"), -4: float("inf"), -5: float("-inf"), -6: 0.0}.get(data)
    memo: dict[int, object] = {}

    def get(i):
        if not isinstance(i, int) or i < 0:
            return devalue_loads(i) if isinstance(i, int) else i
        if i in memo:
            return memo[i]
        v = data[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str):
                kind = v[0]
                if kind == "Date":
                    out = v[1]
                elif kind == "Set":
                    out = [get(x) for x in v[1:]]
                elif kind in ("Map", "null"):
                    pairs = v[1:]
                    out = {}
                    memo[i] = out
                    for j in range(0, len(pairs) - 1, 2):
                        key = pairs[j] if kind == "null" else get(pairs[j])
                        out[str(key)] = get(pairs[j + 1])
                    return out
                elif kind == "BigInt":
                    out = int(v[1])
                else:
                    out = get(v[1]) if len(v) > 1 and isinstance(v[1], int) else (v[1] if len(v) > 1 else None)
                memo[i] = out
                return out
            out = []
            memo[i] = out
            out.extend(get(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            memo[i] = out
            for key, x in v.items():
                out[key] = get(x)
            return out
        memo[i] = v
        return v

    return get(0)


def devalue_dumps(value) -> str:
    """The opposite of devalue_loads(), for plain values (the arguments of remote functions)."""
    out: list = []

    def add(v) -> int:
        i = len(out)
        out.append(None)
        if isinstance(v, dict):
            out[i] = {k: add(x) for k, x in v.items()}
        elif isinstance(v, (list, tuple)):
            out[i] = [add(x) for x in v]
        else:
            out[i] = v
        return i

    add(value)
    return json.dumps(out, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #

class NotLoggedIn(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# Remote function groups and the ids the site used when this was written. They change when the
# site is updated; then the real ids are read from the watch page's code (find_remote_ids()).
DEFAULT_REMOTES = {"getVideoSrc": "3w1j30", "getEpisodes": "3w1j30", "getSubtitles": "1odrich"}


class Session:
    """Requests to the site with the account's cookies (kept in session.txt, Netscape format,
    one set of cookies per domain)."""

    def __init__(self, path: Path):
        self.path = path
        self.jar = http.cookiejar.MozillaCookieJar(str(path))
        if path.is_file():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                pass
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=_SSL_CTX),
            _NoRedirect(),
        )
        self.remotes = dict(DEFAULT_REMOTES)
        self.watch_fid = "Vk8k"            # any watch page works to read the remote ids
        self._remotes_checked = False
        self._lock = threading.Lock()

    def has_cookies(self) -> bool:
        return any(is_site_cookie(c.domain) for c in self.jar)

    def forget(self) -> int:
        """Remove the cookies of the current domain only. Returns how many were removed."""
        mine = [c for c in self.jar if is_site_cookie(c.domain)]
        for c in mine:
            self.jar.clear(c.domain, c.path, c.name)
        return len(mine)

    def save(self) -> None:
        try:
            self.jar.save(ignore_discard=True, ignore_expires=True)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except OSError as ex:
            warn(t(f"Could not save the session: {ex}", f"Không lưu được phiên đăng nhập: {ex}"))

    def request(self, path: str, data: Optional[bytes] = None, headers: Optional[dict] = None,
                tries: int = 3) -> tuple[int, dict, bytes]:
        """(status, headers, body). Redirects are returned as they are, never followed."""
        url = path if path.startswith("http") else SITE + path
        all_headers = {"User-Agent": USER_AGENT, "Referer": REFERER, **(headers or {})}
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, headers=all_headers)
            try:
                with self.opener.open(req, timeout=30) as resp:
                    return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
            except urllib.error.HTTPError as ex:
                if attempt < tries - 1 and _is_transient(ex):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return ex.code, {k.lower(): v for k, v in ex.headers.items()}, ex.read()
            except Exception as ex:
                if attempt == tries - 1 or not _is_transient(ex):
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise AssertionError("unreachable")

    @staticmethod
    def _check_login(status: int, headers: dict, body: bytes) -> None:
        location = headers.get("location", "")
        if status in (301, 302, 303, 307, 308) and "/auth/login" in location:
            raise NotLoggedIn()
        if status == 401:
            raise NotLoggedIn()
        if body[:1] == b"{":
            try:
                obj = json.loads(body)
            except ValueError:
                return
            if obj.get("type") == "redirect" and "/auth/login" in str(obj.get("location", "")):
                raise NotLoggedIn()

    def page_data(self, path: str, query: str = "") -> list:
        """The data of one page (SvelteKit __data.json), one item per layout / page node."""
        for _ in range(3):
            status, headers, body = self.request(path.rstrip("/") + "/__data.json" + query)
            self._check_login(status, headers, body)
            if status != 200:
                raise ValueError(t(f"HTTP {status} from {HOST}", f"HTTP {status} từ {HOST}"))
            obj = json.loads(body)
            if obj.get("type") == "redirect":
                # e.g. /title/_~Vk8k -> /title/the-odyssey~Vk8k
                path = urllib.parse.urlparse(obj.get("location", "")).path
                continue
            if obj.get("type") == "error":
                raise ValueError(t("the site returned an error: ", "trang báo lỗi: ")
                                 + str((obj.get("error") or {}).get("message", "")))
            nodes = []
            for node in obj.get("nodes") or []:
                if node and node.get("type") == "data":
                    nodes.append(devalue_loads(node.get("data")))
                else:
                    nodes.append(None)
            return nodes
        raise ValueError(t("too many redirects", "chuyển hướng quá nhiều lần"))

    def remote(self, name: str, arg):
        """Call one of the site's remote functions (what the player calls in the browser)."""
        payload = base64.urlsafe_b64encode(devalue_dumps(arg).encode("utf-8")).decode().rstrip("=")
        for attempt in range(2):
            rid = self.remotes.get(name, "")
            status, headers, body = self.request(f"/_app/remote/{rid}/{name}?payload={payload}",
                                                 headers={"Accept": "application/json"})
            self._check_login(status, headers, body)
            obj = None
            if status == 200:
                try:
                    obj = json.loads(body)
                except ValueError:
                    obj = None
            if obj and obj.get("type") == "result":
                value = devalue_loads(obj.get("data"))
                if isinstance(value, dict) and "_" in value:
                    value = value["_"]
                return value
            if obj and obj.get("type") == "error":
                err = obj.get("error") or {}
                raise ValueError(t("the site returned an error: ", "trang báo lỗi: ")
                                 + str(err.get("message", err) if isinstance(err, dict) else err))
            # Unknown id: the site was updated. Read the new ids once and try again.
            if attempt == 0 and self.find_remote_ids():
                continue
            raise ValueError(t(f"HTTP {status} from {name}", f"HTTP {status} từ {name}"))
        raise AssertionError("unreachable")

    def find_remote_ids(self) -> bool:
        """Read the remote function ids from the code of the watch page. True when they changed."""
        with self._lock:
            if self._remotes_checked:
                return False
            self._remotes_checked = True
            status, headers, body = self.request(f"/watch/{self.watch_fid}")
            self._check_login(status, headers, body)
            page = body.decode("utf-8", "replace")
            scripts = sorted(set(re.findall(r'(_app/immutable/nodes/[\w.-]+\.js)', page)),
                             key=lambda s: -int(re.search(r"nodes/(\d+)", s).group(1)))
            found = {}
            for script in scripts:
                try:
                    code = http_get(f"{SITE}/{script}")
                except Exception:
                    continue
                for rid, name in re.findall(r"[`'\"]([0-9a-z]+)/(getVideoSrc|getEpisodes|getSubtitles)[`'\"]", code):
                    found[name] = rid
                if len(found) == 3:
                    break
            changed = any(self.remotes.get(k) != v for k, v in found.items())
            self.remotes.update(found)
            return changed


def whoami(sess: Session) -> Optional[dict]:
    """The account behind the session, or None when it is not logged in."""
    if not sess.has_cookies():
        return None
    try:
        nodes = sess.page_data("/", "?x-sveltekit-invalidated=01")
    except NotLoggedIn:
        return None
    for node in nodes:
        if isinstance(node, dict) and node.get("userId"):
            return node
    return None


def _failure_message(obj: dict, secrets: tuple[str, ...] = ()) -> str:
    """The error text of a failed form (e.g. wrong password), if the site sent one.
    Values that look like the form fields sent back (email, password) are never shown."""
    try:
        data = devalue_loads(obj.get("data")) if obj.get("data") is not None else None
    except Exception:
        data = None
    texts = []

    def walk(v):
        if (isinstance(v, str) and v.strip() and "@" not in v and not v.startswith("/")
                and not any(sec and sec in v for sec in secrets)):
            texts.append(v.strip())
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(data)
    return "; ".join(dict.fromkeys(texts))[:200]


def login(sess: Session, email: str, password: str) -> dict:
    """Log in with email and password (the site's login form). Returns the account."""
    body = urllib.parse.urlencode({"currentUrl": "/", "email": email, "password": password}).encode()
    status, headers, resp = sess.request("/auth/login", data=body, tries=1, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": SITE,
        "Referer": SITE + "/auth/login",
        "Accept": "application/json",
        "x-sveltekit-action": "true",
    })
    message = ""
    if resp[:1] == b"{":
        try:
            obj = json.loads(resp)
            if obj.get("type") == "failure":
                message = _failure_message(obj, (email, password))
            elif obj.get("type") == "error":
                message = str((obj.get("error") or {}).get("message", ""))
        except ValueError:
            pass
    user = whoami(sess)
    if not user:
        raise ValueError(message or t(f"login failed (HTTP {status}), check the email and password",
                                      f"đăng nhập thất bại (HTTP {status}), kiểm tra email và mật khẩu"))
    sess.save()
    return user


def import_cookies(sess: Session, path: Path) -> int:
    """Copy the cookies of the current domain from a cookies.txt file exported from a browser."""
    count = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, cpath, secure, expires, name, value = parts[:7]
            if not is_site_cookie(domain):
                continue
            try:
                expiry = int(float(expires)) or None
            except ValueError:
                expiry = None
            sess.jar.set_cookie(http.cookiejar.Cookie(
                0, name, value, None, False, domain, domain.startswith("."), domain.startswith("."),
                cpath or "/", True, secure.upper() == "TRUE", expiry, expiry is None, None, None,
                {"HttpOnly": None} if http_only else {}))
            count += 1
    return count


def prompt_login(sess: Session, email: str = "") -> Optional[dict]:
    """Ask for the email and password in the terminal and log in."""
    log("\n" + bold(t(f"Log in to {HOST}", f"Đăng nhập {HOST}")))
    log(gray(t(f"The password is only sent to {HOST}; it is not saved (only the session cookie is, "
               "in session.txt).",
               f"Mật khẩu chỉ được gửi tới {HOST} và không được lưu lại (chỉ lưu cookie phiên "
               "vào session.txt).")))
    try:
        email = email or input("  Email: ").strip()
        password = getpass.getpass(t("  Password: ", "  Mật khẩu: "))
    except (EOFError, KeyboardInterrupt):
        log("")
        return None
    if not email or not password:
        log(red(t("Email and password are needed.", "Cần nhập email và mật khẩu.")), err=True)
        return None
    try:
        user = login(sess, email, password)
    except Exception as ex:
        log(red(t("Login failed: ", "Đăng nhập thất bại: ") + describe_error(SITE, ex)), err=True)
        return None
    log(green(t("Logged in as ", "Đã đăng nhập: ") + str(user.get("userName") or "?")))
    return user


def ensure_login(sess: Session, args) -> Optional[dict]:
    """The logged-in account; asks to log in when there is no valid session."""
    user = whoami(sess)
    if user:
        return user
    had = sess.has_cookies()
    log(yellow(t(f"The {HOST} session has expired." if had else f"You are not logged in to {HOST}.",
                 f"Phiên đăng nhập {HOST} đã hết hạn." if had else f"Bạn chưa đăng nhập {HOST}.")))
    if sys.stdin.isatty() and not args.yes:
        return prompt_login(sess)
    log(red(t(f"{HOST} needs an account. Run: {login_hint()} "
              "(or --cookies cookies.txt exported from your browser)",
              f"{HOST} cần tài khoản. Chạy: {login_hint()} "
              "(hoặc --cookies cookies.txt xuất từ trình duyệt)")), err=True)
    return None


# --------------------------------------------------------------------------- #
# Movie info
# --------------------------------------------------------------------------- #

def parse_target(text: str) -> tuple[str, str]:
    """('title' | 'watch', fid) from a link, 'slug~FID' or a bare FID."""
    text = text.strip()
    m = re.search(r"/watch/([A-Za-z0-9_-]+)", text)
    if m:
        return "watch", m.group(1)
    m = re.search(r"(?:^|/title/)[^/?#~]*~([A-Za-z0-9_-]+)", text)
    if m:
        return "title", m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{2,12}", text):
        return "title", text
    raise ValueError(t("not a movie link (/title/... or /watch/...)",
                       "không phải link phim (/title/... hoặc /watch/...)"))


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _year(date) -> str:
    m = re.match(r"(\d{4})", str(date or ""))
    return m.group(1) if m else ""


def _page_node(nodes: list, key: str) -> dict:
    for node in reversed(nodes):
        if isinstance(node, dict) and key in node:
            return node
    raise ValueError(t("could not read the movie page", "không đọc được trang phim"))


def load_target(sess: Session, kind: str, fid: str) -> dict:
    """Title, year and what to download. 'type' is movie, show or episode (one episode of a show)."""
    if kind == "watch":
        page = _page_node(sess.page_data(f"/watch/{fid}"), "title")
        title = page["title"]
        if title.get("type") == "episode":
            return {
                "type": "episode",
                "fid": title.get("showFid") or fid,
                "title": title.get("showNameEn") or title.get("showNameVi") or fid,
                "title_vi": title.get("showNameVi") or "",
                "year": _year(title.get("showPublishDate")),
                "seasons": page.get("seasons") or [],
                "episode": {"fid": title["fid"], "season": _int(title.get("seasonNumber"), 1),
                            "number": _int(title.get("number"), 1), "name": "",
                            "media": title.get("mediainfoParsed") or {}},
            }
    page = _page_node(sess.page_data(f"/title/_~{fid}"), "title")
    title = page["title"]
    info = {
        "type": title.get("type") or "movie",
        "fid": title.get("fid") or fid,
        "title": title.get("nameEn") or title.get("nameVi") or fid,
        "title_vi": title.get("nameVi") or "",
        "year": _year(title.get("publishDate")),
        "seasons": page.get("seasons") or [],
        "can_watch": page.get("canWatch", True),
        "media": title.get("mediainfoParsed") or {},
    }
    if info["type"] == "episode":
        # A /title/ link of a single episode: treat it like its watch page.
        return load_target(sess, "watch", info["fid"])
    return info


def list_episodes(sess: Session, season: dict) -> list[dict]:
    """The released episodes of one season, with their names."""
    result = sess.remote("getEpisodes", season["fid"]) or {}
    names = {r.get("number"): r.get("name") or "" for r in result.get("releases") or []}
    number = _int(season.get("number"), 1)
    return [{"fid": e["fid"], "season": number, "number": _int(e.get("number"), i + 1),
             "name": names.get(e.get("number"), ""), "media": {}}
            for i, e in enumerate(result.get("episodes") or []) if e.get("fid")]


# --------------------------------------------------------------------------- #
# Stream
# --------------------------------------------------------------------------- #

def _rot(text: str, shift: int) -> str:
    out = []
    for ch in text:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - 97 + shift) % 26 + 97))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - 65 + shift) % 26 + 65))
        else:
            out.append(ch)
    return "".join(out)


def deobfuscate(url: str) -> str:
    """The player hides the file names of the key and the segments: name = ROT6(base64(real name)),
    with a fake extension (.jpg, .css, ...). This restores the real link; others stay as they are."""
    head, sep, name = url.rpartition("/")
    if not sep:
        return url
    query = ""
    if "?" in name:
        name, query = name.split("?", 1)
        query = "?" + query
    stem, dot, ext = name.partition(".")
    encoded = _rot(stem, 6)
    try:
        real = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True).decode("ascii")
    except Exception:
        return url
    if not real or not re.fullmatch(r"[\w.~-]+", real):
        return url
    return f"{head}/{real}{dot}{ext}{query}"


@dataclass
class Segment:
    url: str
    duration: float
    key: Optional[str] = None      # key URL (AES-128), None = not encrypted
    iv: bytes = b""
    byte_range: Optional[tuple[int, int]] = None   # (offset, length) inside the file at url

    @property
    def range_header(self) -> str:
        """'start-end' for an HTTP Range header, or '' for a whole file."""
        if not self.byte_range:
            return ""
        offset, length = self.byte_range
        return f"{offset}-{offset + length - 1}"


@dataclass
class Stream:
    """The media playlist of one episode, with real links and the AES keys already fetched."""
    url: str
    server: str
    segments: list[Segment]
    keys: dict[str, bytes] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)


def parse_variants(text: str, base_url: str) -> list[dict]:
    """The qualities listed in a master playlist, highest first."""
    variants, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        nxt = next((l.strip() for l in lines[i + 1:] if l.strip() and not l.startswith("#")), None)
        if not nxt:
            continue
        bw = re.search(r"[:,]BANDWIDTH=(\d+)", line)
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        variants.append({
            "url": urllib.parse.urljoin(base_url, nxt),
            "bandwidth": int(bw.group(1)) if bw else 0,
            "height": int(res.group(2)) if res else 0,
        })
    variants.sort(key=lambda v: (v["height"], v["bandwidth"]), reverse=True)
    return variants


def parse_playlist(text: str, base_url: str) -> list[Segment]:
    """Segments of a media playlist with their real links, durations and keys."""
    segments: list[Segment] = []
    key_url: Optional[str] = None
    key_iv: Optional[bytes] = None
    sequence = 0
    duration = 0.0
    byte_range: Optional[tuple[int, Optional[int]]] = None   # (length, offset) of the next segment
    range_end: dict[str, int] = {}                           # where the last range of each file ended
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-KEY"):
            method = re.search(r"METHOD=([\w-]+)", line)
            if not method or method.group(1) == "NONE":
                key_url, key_iv = None, None
            elif method.group(1) != "AES-128":
                raise ValueError(t(f"unsupported encryption {method.group(1)}",
                                   f"kiểu mã hóa {method.group(1)} chưa hỗ trợ"))
            else:
                uri = re.search(r'URI="([^"]+)"', line)
                if not uri:
                    raise ValueError(t("the playlist has no key link", "playlist không có link khóa"))
                key_url = deobfuscate(urllib.parse.urljoin(base_url, uri.group(1)))
                iv = re.search(r"IV=0[xX]([0-9a-fA-F]+)", line)
                key_iv = bytes.fromhex(iv.group(1).zfill(32)) if iv else None
        elif line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:([\d.]+)", line)
            duration = float(m.group(1)) if m else 0.0
        elif line.startswith("#EXT-X-BYTERANGE:"):
            # Some servers keep many segments in one big file: LENGTH[@OFFSET].
            m = re.match(r"#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?", line)
            if not m:
                raise ValueError(t("broken byte range in the playlist", "byte range trong playlist bị lỗi"))
            byte_range = (int(m.group(1)), int(m.group(2)) if m.group(2) else None)
        elif line.startswith("#EXT-X-MAP"):
            raise ValueError(t("unsupported playlist (fMP4)", "playlist chưa hỗ trợ (fMP4)"))
        elif not line.startswith("#"):
            url = deobfuscate(urllib.parse.urljoin(base_url, line))
            iv = key_iv if key_iv is not None else (sequence + len(segments)).to_bytes(16, "big")
            seg = Segment(url, duration, key_url, iv if key_url else b"")
            if byte_range:
                length, offset = byte_range
                offset = range_end.get(url, 0) if offset is None else offset
                seg.byte_range = (offset, length)
                range_end[url] = offset + length
                byte_range = None
            segments.append(seg)
            duration = 0.0
    return segments


def open_stream(sess: Session, fid: str, server: str) -> Stream:
    """The playlist of one episode on one server, ready to download."""
    result = sess.remote("getVideoSrc", {"fid": fid, "server": server}) or {}
    src = result.get("src") if isinstance(result, dict) else None
    if not src:
        error = result.get("error") if isinstance(result, dict) else None
        raise ValueError(t("no video link", "không có link video") + (f" ({error})" if error else ""))
    text = http_get(src)
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError(t("the video link is not a playlist", "link video không phải playlist"))
    variants = parse_variants(text, src)
    if variants:
        src = variants[0]["url"]
        text = http_get(src)
    segments = parse_playlist(text, src)
    if not segments:
        raise ValueError(t("the playlist is empty", "playlist trống"))
    stream = Stream(src, server, segments)
    for key_url in dict.fromkeys(s.key for s in segments if s.key):
        key = http_get(key_url, binary=True)
        if len(key) != 16:
            raise ValueError(t("the decryption key is broken", "khóa giải mã bị lỗi"))
        stream.keys[key_url] = key
    return stream


def open_stream_any(sess: Session, fid: str, first: str) -> Stream:
    """Try the chosen server first, then the others."""
    order = [first] + [s for s in SERVERS if s != first]
    errors = []
    for server in order:
        try:
            return open_stream(sess, fid, server)
        except NotLoggedIn:
            raise
        except Exception as ex:
            errors.append(f"server {server}: {describe_error(REFERER, ex)}")
    raise ValueError("; ".join(errors))


def decrypt_segment(data: bytes, key: Optional[bytes], iv: bytes, final: bool = True) -> bytes:
    """AES-128-CBC like HLS players do. With final=False only a prefix is decrypted (for checks)."""
    if key is None:
        return data
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if final and len(data) % 16:
        raise ValueError(t("a segment is broken (bad length)", "một đoạn bị lỗi (sai độ dài)"))
    data = data[:len(data) - len(data) % 16]
    plain = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(data)
    if final and plain:
        pad = plain[-1]
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
    return plain


# --------------------------------------------------------------------------- #
# Segments, size and resolution
# --------------------------------------------------------------------------- #

TS_PACKET = 188


def ts_start(data: bytes) -> int:
    """Where the MPEG-TS stream starts in a segment, or -1 if it is not one."""
    if len(data) > 2 * TS_PACKET and data[0] == 0x47 and data[TS_PACKET] == 0x47:
        return 0
    for i in range(min(len(data) - 3 * TS_PACKET, 65536)):
        if data[i] == 0x47 and data[i + TS_PACKET] == 0x47 and data[i + 2 * TS_PACKET] == 0x47:
            return i
    return -1


def ts_payload(data: bytes) -> bytes:
    start = ts_start(data)
    if start < 0:
        raise ValueError(t("a segment is not a video file", "một đoạn tải về không phải video"))
    return data[start:] if start else data


def segment_video(stream: Stream, seg: Segment, data: bytes) -> bytes:
    """The decrypted MPEG-TS data of one downloaded segment."""
    return ts_payload(decrypt_segment(data, stream.keys.get(seg.key) if seg.key else None, seg.iv))


def _get(url: str, method: str = "GET", byte_range: str = "", timeout: int = 15):
    headers = {"User-Agent": USER_AGENT, "Referer": REFERER}
    if byte_range:
        headers["Range"] = f"bytes={byte_range}"
    req = urllib.request.Request(url, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)


def remote_size(url: str) -> Optional[int]:
    """File size on the server (HEAD, or a 1-byte GET), without downloading it."""
    for method, byte_range in (("HEAD", ""), ("GET", "0-0")):
        try:
            with _get(url, method, byte_range) as resp:
                m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
                if m:
                    return int(m.group(1))
                length = resp.headers.get("Content-Length")
                if length and (method == "HEAD" or resp.status == 200):
                    return int(length)
        except Exception:
            continue
    return None


def estimate_size(stream: Stream, samples: int = 40) -> Optional[float]:
    """Estimate the size of one episode from a few segments spread over it.
    With byte ranges the playlist already has the exact size of every segment."""
    segs = stream.segments
    if segs and all(s.byte_range for s in segs):
        return float(sum(s.byte_range[1] for s in segs))
    n = min(samples, len(segs))
    picks = sorted({round(i * (len(segs) - 1) / max(1, n - 1)) for i in range(n)})
    sizes: dict[int, int] = {}

    def probe(i):
        size = remote_size(segs[i].url)
        if size:
            sizes[i] = size

    run_parallel(probe, picks, 8)
    if not sizes:
        return None
    sampled = sum(segs[i].duration for i in sizes)
    total = stream.duration
    if total > 0 and sampled > 0:
        return sum(sizes.values()) / sampled * total
    return sum(sizes.values()) / len(sizes) * len(segs)


def resolution_label(width: int, height: int) -> str:
    """'1080p' for 1920x1080, but also for 1920x800 (wide movies) or 1440x1080 (4:3)."""
    for min_w, min_h, name in ((3800, 2100, "2160p"), (2500, 1400, "1440p"), (1900, 1000, "1080p"),
                               (1260, 700, "720p"), (840, 470, "480p"), (630, 350, "360p")):
        if width >= min_w or height >= min_h:
            return name
    return f"{height}p"


def probe_resolution(stream: Stream, ffmpeg: Optional[str]) -> Optional[tuple[int, int]]:
    """Width and height of the video, read by ffmpeg from one decrypted segment."""
    if not ffmpeg or not stream.segments:
        return None
    seg = stream.segments[len(stream.segments) // 2]
    try:
        with _get(seg.url, byte_range=seg.range_header, timeout=30) as resp:
            data = resp.read()
        data = ts_payload(decrypt_segment(data, stream.keys.get(seg.key) if seg.key else None, seg.iv, final=False))
        proc = subprocess.run([ffmpeg, "-hide_banner", "-f", "mpegts", "-i", "pipe:0"],
                              input=data, capture_output=True, timeout=30)
    except Exception:
        return None
    m = re.search(r"Video: .*?, (\d{2,5})x(\d{2,5})", proc.stderr.decode("utf-8", "replace"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def describe_quality(media: dict, stream: Stream, ffmpeg: Optional[str]) -> str:
    """e.g. '1080p (1920x1080)', from the site's media info or else read by ffmpeg."""
    w, h = (media or {}).get("width"), (media or {}).get("height")
    if not (w and h):
        res = probe_resolution(stream, ffmpeg)
        if res:
            w, h = res
    return f"{resolution_label(int(w), int(h))} ({w}x{h})" if w and h else t("source", "gốc")


# --------------------------------------------------------------------------- #
# Subtitles
# --------------------------------------------------------------------------- #

def pick_subtitles(subs: list[dict], wanted: str) -> list[dict]:
    """The best subtitle of each wanted language ('vi,en', 'all' or 'none').
    Best = the one the site plays by default, then the most liked."""
    wanted = (wanted or "").strip().lower()
    if wanted in ("", "none", "no", "0"):
        return []
    langs = None if wanted == "all" else [x.strip() for x in wanted.split(",") if x.strip()]
    best: dict[str, tuple] = {}
    for sub in subs:
        lang = str(sub.get("language") or "").lower()
        base = lang.split("-")[0]
        key = lang if langs is None or lang in langs else base if base in langs else None
        if key is None:
            continue
        score = (bool(sub.get("isDefault")), (sub.get("likes") or 0) - (sub.get("dislikes") or 0))
        cur = best.get(key)
        if cur is None or score > cur[0]:
            best[key] = (score, sub)
    order = langs or sorted(best)
    return [best[k][1] | {"_lang": k} for k in order if k in best]


def decrypt_subtitle(file_name: str, data: str) -> str:
    """Subtitles are AES-GCM encrypted: key = SHA-256('/watch/' + ROT19(file name)), base64(iv + data)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = hashlib.sha256(("/watch/" + _rot(file_name, 19)).encode("utf-8")).digest()
    raw = base64.b64decode(data.strip())
    text = AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8", "replace")
    return text.lstrip("\ufeff")


def save_subtitles(sess: Session, fid: str, video_path: str, wanted: str) -> list[str]:
    """Download the chosen subtitles of one episode next to the video. Returns the language codes."""
    subs = sess.remote("getSubtitles", fid) or []
    saved = []
    stem = os.path.splitext(video_path)[0]
    for sub in pick_subtitles(subs if isinstance(subs, list) else [], wanted):
        file_name = str(sub.get("fileName") or "")
        ext = os.path.splitext(file_name)[1].lower()
        ext = ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ".srt"
        lang = re.sub(r"[^A-Za-z0-9-]", "", sub["_lang"]) or "sub"
        target = f"{stem}.{lang}{ext}"
        if os.path.exists(target) and os.path.getsize(target) > 0:
            saved.append(lang)
            continue
        sub_id = f"{sub.get('subsceneId')}" + (f"~{sub['rand']}" if sub.get("rand") else "")
        path = f"/api/subtitle/{urllib.parse.quote(sub_id)}/{urllib.parse.quote(file_name)}"
        status, headers, body = sess.request(path)
        Session._check_login(status, headers, body)
        if status != 200:
            raise ValueError(t(f"HTTP {status} for subtitle {file_name}", f"HTTP {status} khi tải phụ đề {file_name}"))
        text = decrypt_subtitle(file_name, body.decode("ascii", "replace"))
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        saved.append(lang)
    return saved


# --------------------------------------------------------------------------- #
# File names, episode selection
# --------------------------------------------------------------------------- #

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", name).strip().rstrip(". ")
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name += "_"
    return name if name not in ("", ".", "..") else "phim"


def build_filename(title: str, year: str, ep: Optional[dict]) -> str:
    """Movie.Name.2026.mp4 for a movie, Series.Name.S01E01.mp4 for an episode."""
    base = re.sub(r"\s+", ".", safe_name(title))
    if ep is None:
        return f"{base}.{year}.mp4" if year else f"{base}.mp4"
    return f"{base}.S{ep['season']:02d}E{ep['number']:02d}.mp4"


def episode_label(ep: Optional[dict]) -> str:
    return t("Movie", "Phim") if ep is None else f"S{ep['season']:02d}E{ep['number']:02d}"


def parse_selection(text: str, count: int, strict: bool = True) -> list[int]:
    """'all', '1-5,8', '10-' -> list of 0-based indexes.
    strict=False leaves out numbers above count instead of failing (for several seasons)."""
    text = (text or "").strip().lower()
    if text in ("", "all", "*", "tatca", "tất cả"):
        return list(range(count))
    picked = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)", part)
        if m:
            start = int(m.group(1) or 1)
            end = int(m.group(2) or count)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ValueError(t(f"cannot read '{part}'", f"không hiểu '{part}'"))
        if not strict:
            end = min(end, count)
            if start > end:
                continue
        if start < 1 or end > count or start > end:
            raise ValueError(t(f"'{part}' is outside 1-{count}", f"'{part}' nằm ngoài khoảng 1-{count}"))
        picked.extend(range(start - 1, end))
    return sorted(set(picked))


# --------------------------------------------------------------------------- #
# Download / mux
# --------------------------------------------------------------------------- #

def find_tool(name: str) -> Optional[str]:
    """Prefer an executable next to phim-dl.py (aria2c.exe, ffmpeg.exe), then PATH."""
    for candidate in (SCRIPT_DIR / f"{name}.exe", SCRIPT_DIR / name):
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate)
    return shutil.which(name)


def find_ffmpeg() -> Optional[str]:
    """ffmpeg next to the script or on PATH, else the copy shipped with imageio-ffmpeg."""
    path = find_tool("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


# Running aria2c / ffmpeg processes, so Ctrl+C stops them too.
_children: set = set()


def kill_children() -> None:
    for proc in list(_children):
        try:
            proc.terminate()
        except Exception:
            pass


def _remove(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def make_mp4(ffmpeg: str, out_path: str, chunks: Callable[[], Iterable[bytes]]) -> None:
    """Join the decrypted segments into a real MP4 without re-encoding. They are piped into ffmpeg,
    so no big temporary .ts is written. chunks() gives the segment data from the start
    (it is called again on a retry)."""
    tmp = out_path + ".part"
    err = ""
    # aac_adtstoasc is needed for AAC audio in TS; retry without it if the audio is not AAC.
    for extra in (["-bsf:a", "aac_adtstoasc"], []):
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", "pipe:0",
               "-map", "0:v?", "-map", "0:a?", "-c", "copy", *extra, "-movflags", "+faststart", "-f", "mp4", tmp]
        with tempfile.TemporaryFile() as err_file:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=err_file)
            _children.add(proc)
            try:
                try:
                    for chunk in chunks():
                        proc.stdin.write(chunk)
                except OSError:
                    pass   # ffmpeg stopped early; its error message says why
                finally:
                    proc.stdin.close()
                code = proc.wait()
            finally:
                _children.discard(proc)
            err_file.seek(0)
            err = err_file.read().decode("utf-8", "replace").strip()
        if code == 0:
            os.replace(tmp, out_path)
            return
    _remove(tmp)
    raise RuntimeError(t("ffmpeg could not make the MP4", "ffmpeg không ghép được MP4") + f": {err[-300:]}")


def _note_joining(label: str) -> None:
    if board.enabled:
        board.update(label, note=t("joining into MP4...", "đang ghép MP4..."))
    else:
        log(f"  {label}: " + t("joining into MP4...", "đang ghép MP4..."))


class Meter:
    """Speed (5-second moving average), total and ETA of one episode, shown on the board."""

    def __init__(self, label: str, count: int, expected: Optional[float]):
        self.label, self.count, self.expected = label, count, expected
        self.samples: list[tuple[float, int]] = []

    def report(self, done_count: int, done_bytes: int, all_bytes: int) -> None:
        now = time.monotonic()
        self.samples = [s for s in self.samples if now - s[0] <= 5] + [(now, all_bytes)]
        t0, b0 = self.samples[0]
        speed = (all_bytes - b0) / (now - t0) if now - t0 >= 0.5 else None
        # At the start a few segments are not enough to guess the total: use the earlier estimate.
        if self.expected and done_count < max(5, self.count // 20):
            total = max(self.expected, all_bytes)
        else:
            total = done_bytes / done_count * self.count if done_count else None
        eta = (total - all_bytes) / speed if total and speed else None
        board.update(self.label, pct=100.0 * done_count / self.count, done=all_bytes,
                     total=total, speed=speed, eta=eta)


# aria2c runs again for the segments that failed or came back broken, before giving up.
ARIA2_PASSES = 3
# Segments downloaded at once per episode (-n). The CDN is slow per connection, so many at once
# helps; measured best: 32 with aria2c, 16 with the built-in downloader (Python threads).
DEFAULT_FRAGMENTS = {"aria2": 32, "builtin": 16}
# Seconds without data before the built-in downloader drops a connection and tries again.
STALL_TIMEOUT = 12


def _check_parts(parts_dir: Path, stream: Stream, names: list[str], good: set[str]) -> None:
    """Add the finished segments that really are video to `good`; delete broken ones
    (empty files, error pages) and half-downloaded ones so they are downloaded again.
    Half files cannot be continued: the video servers do not support resuming whole files."""
    with os.scandir(parts_dir) as it:
        listing = {e.name: e.stat().st_size for e in it}
    for name in listing:
        if name.endswith((".aria2", ".part")):
            _remove(parts_dir / name)
            _remove(parts_dir / name.rsplit(".", 1)[0])
    for seg, name in zip(stream.segments, names):
        if name in good or name not in listing or name + ".aria2" in listing:
            continue
        path = parts_dir / name
        size = listing[name]
        key = stream.keys.get(seg.key) if seg.key else None
        try:
            with open(path, "rb") as fh:
                head = fh.read(65536 + 4 * TS_PACKET)
            ok = size > 0 and (key is None or size % 16 == 0)
            ok = ok and ts_start(decrypt_segment(head, key, seg.iv, final=False)) >= 0
        except (OSError, ValueError):
            ok = False
        if ok:
            good.add(name)
        else:
            _remove(path)


def _run_aria2(aria2: str, parts_dir: Path, pending: list[tuple[str, str]],
               connections: int, meter: Meter, names: set[str]) -> None:
    input_path = parts_dir / "input.txt"
    input_path.write_text("".join(f"{url}\n  out={name}\n" for url, name in pending), encoding="utf-8")
    cmd = [
        aria2, "--no-conf",
        f"--input-file={input_path}", f"--dir={parts_dir}",
        # The CDN is slow per connection: download many segments at once, one connection each.
        # Splitting a segment over several connections does not work: the video servers do not
        # support byte ranges on whole files, so aria2c aborts those parts (error 8).
        f"--max-concurrent-downloads={connections}",
        "--split=1", "--max-connection-per-server=1",
        f"--user-agent={USER_AGENT}", f"--referer={REFERER}",
        # A retry after a dropped connection starts the segment again instead of resuming it.
        "--always-resume=false", "--max-resume-failure-tries=0",
        "--auto-file-renaming=false", "--allow-overwrite=true",
        "--file-allocation=none", "--max-tries=10", "--retry-wait=2",
        # A stalled connection holds up the whole episode: drop it and try again.
        "--connect-timeout=10", "--timeout=20", "--lowest-speed-limit=20K",
        "--console-log-level=error", "--summary-interval=0",
        "--download-result=hide", "--show-console-readout=false",
        f"--log={parts_dir / 'aria2.log'}", "--log-level=warn",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _children.add(proc)
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                finished = True
            except subprocess.TimeoutExpired:
                finished = False
            with os.scandir(parts_dir) as it:
                listing = {e.name: e.stat().st_size for e in it}
            done = [n for n in names if n in listing and n + ".aria2" not in listing]
            meter.report(len(done), sum(listing[n] for n in done),
                         sum(size for name, size in listing.items() if name in names))
            if finished:
                return
    except BaseException:
        proc.terminate()
        raise
    finally:
        _children.discard(proc)


def _run_builtin(parts_dir: Path, pending: list[tuple[str, str, str]], connections: int,
                 meter: Meter, names: set[str]) -> None:
    """Download segments with Python threads: when aria2c is not available, or for byte ranges
    (aria2c cannot download one part of a file per entry)."""
    lock = threading.Lock()
    stop = threading.Event()

    def fetch(item):
        url, name, byte_range = item
        tmp = parts_dir / (name + ".part")
        for attempt in range(8):
            if stop.is_set():
                return
            try:
                # A short timeout: a stalled connection is dropped and tried again quickly.
                with _get(url, byte_range=byte_range, timeout=STALL_TIMEOUT) as resp, open(tmp, "wb") as out:
                    if byte_range and resp.status != 206:
                        raise ValueError(t("the server ignored the byte range", "máy chủ bỏ qua byte range"))
                    while True:
                        block = resp.read(262144)
                        if not block:
                            break
                        out.write(block)
                os.replace(tmp, parts_dir / name)
                return
            except Exception as ex:
                if attempt == 7 or (isinstance(ex, urllib.error.HTTPError) and not _is_transient(ex)):
                    with lock:
                        with open(parts_dir / "aria2.log", "a", encoding="utf-8") as log_fh:
                            log_fh.write(f"ERROR {name}: {describe_error(url, ex)}\n")
                    _remove(tmp)
                    return
                time.sleep(min(1 + attempt, 5))

    pool = ThreadPoolExecutor(max_workers=max(1, connections))
    futures = [pool.submit(fetch, item) for item in pending]
    try:
        while True:
            finished = not wait(futures, timeout=0.5).not_done
            with os.scandir(parts_dir) as it:
                listing = {e.name: e.stat().st_size for e in it}
            done = [n for n in names if n in listing]
            partial = sum(size for name, size in listing.items() if name.endswith(".part"))
            meter.report(len(done), sum(listing[n] for n in done), sum(listing[n] for n in done) + partial)
            if finished:
                break
    except BaseException:
        stop.set()
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def download_segments(aria2: Optional[str], stream: Stream, out_path: str, label: str, ffmpeg: Optional[str],
                      connections: Optional[int] = None, expected: Optional[float] = None) -> str:
    """Download the segments (aria2c, or Python threads without it), check them, then decrypt
    and join them into the MP4.

    Unfinished segments live in a hidden .<name>.parts folder next to the target file,
    so Ctrl+C and running the command again continues instead of starting over.
    """
    stem = os.path.splitext(out_path)[0]
    parts_dir = Path(out_path).parent / f".{os.path.basename(stem)}.parts"
    parts_dir.mkdir(exist_ok=True)
    names = [f"{i:05d}.ts" for i in range(len(stream.segments))]
    good: set[str] = set()
    meter = Meter(label, len(names), expected)

    for _ in range(ARIA2_PASSES):
        _check_parts(parts_dir, stream, names, good)
        pending = [(s.url, n, s.range_header) for s, n in zip(stream.segments, names) if n not in good]
        if not pending:
            break
        if aria2 and not any(rng for _, _, rng in pending):
            _run_aria2(aria2, parts_dir, [(u, n) for u, n, _ in pending],
                       connections or DEFAULT_FRAGMENTS["aria2"], meter, set(names))
        else:
            _run_builtin(parts_dir, pending, connections or DEFAULT_FRAGMENTS["builtin"], meter, set(names))
    _check_parts(parts_dir, stream, names, good)

    missing = len(names) - len(good)
    if missing:
        detail = ""
        try:
            with open(parts_dir / "aria2.log", encoding="utf-8", errors="replace") as fh:
                errors = [l.strip() for l in fh if "ERROR" in l or "error" in l]
            if errors:
                detail = f": {errors[-1][-200:]}"
        except OSError:
            pass
        raise RuntimeError(t(f"{missing}/{len(names)} segments could not be downloaded{detail} "
                             "(run the same command again to continue)",
                             f"còn {missing}/{len(names)} đoạn chưa tải được{detail} "
                             "(chạy lại lệnh để tải tiếp)"))

    def chunks():
        for seg, name in zip(stream.segments, names):
            yield segment_video(stream, seg, (parts_dir / name).read_bytes())

    if ffmpeg:
        _note_joining(label)
        make_mp4(ffmpeg, out_path, chunks)
        final = out_path
    else:
        # Plain concatenation of MPEG-TS segments is a valid stream.
        final = stem + ".ts"
        with open(final + ".part", "wb") as out:
            for chunk in chunks():
                out.write(chunk)
        os.replace(final + ".part", final)
    shutil.rmtree(parts_dir, ignore_errors=True)
    return final


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #

def run_parallel(fn, items: list, workers: int) -> None:
    """Run fn(item) in parallel while Ctrl+C still works.

    On Windows, pool.map() / `with ThreadPoolExecutor` blocks the main thread in a lock that
    cannot be interrupted, so Ctrl+C is ignored until everything is done. Waiting in short steps
    lets KeyboardInterrupt through.
    """
    if not items:
        return
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(items))))
    futures = [pool.submit(fn, item) for item in items]
    try:
        while wait(futures, timeout=0.3).not_done:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for fut in futures:
        fut.result()


@dataclass
class Job:
    """One movie or episode to download."""
    label: str
    fid: str
    out: str
    media: dict = field(default_factory=dict)
    stream: Optional[Stream] = None
    quality: str = ""
    size: Optional[float] = None     # estimated bytes
    subs: list = field(default_factory=list)
    status: str = ""                 # "", "ok", "skip" or "fail"
    error: str = ""

    def fail(self, error: str) -> None:
        self.status, self.error = "fail", error


def aria2_tip() -> str:
    return t("Tip: aria2c downloads several times faster. Get aria2c.exe from "
             "https://github.com/aria2/aria2/releases and put it next to phim-dl.py "
             "(macOS: brew install aria2, Linux: sudo apt install aria2).",
             "Mẹo: aria2c tải nhanh hơn nhiều lần. Tải aria2c.exe tại "
             "https://github.com/aria2/aria2/releases rồi đặt cạnh phim-dl.py "
             "(macOS: brew install aria2, Linux: sudo apt install aria2).")


def fmt_bitrate(size: Optional[float], duration: float) -> str:
    return f"{size * 8 / duration / 1e6:.1f} Mbps" if size and duration else "?"


def watch_url(fid: str) -> str:
    return f"{SITE}/watch/{fid}"


def show_sizes(sess: Session, jobs: list[Job], server: str, ffmpeg: Optional[str]) -> int:
    """Print the quality, size and bitrate on every server and exit, without downloading.
    For a series only the first selected episode is measured and multiplied."""
    job = jobs[0]
    what = job.label if len(jobs) == 1 else t(f"{job.label} (first of {len(jobs)})",
                                               f"{job.label} (đầu tiên trong {len(jobs)})")
    log("\n" + cyan(t(f"Measuring {what} on every server (nothing is downloaded)...",
                      f"Đang đo {what} trên mọi server (không tải phim)...")))
    order = [server] + [s for s in SERVERS if s != server]
    rows: dict[str, dict] = {}

    def probe(srv):
        try:
            stream = open_stream(sess, job.fid, srv)
            rows[srv] = {"quality": describe_quality(job.media, stream, ffmpeg),
                         "size": estimate_size(stream), "duration": stream.duration,
                         "host": urllib.parse.urlparse(stream.segments[0].url).hostname or "?"}
        except NotLoggedIn:
            raise
        except Exception as ex:
            rows[srv] = {"error": describe_error(REFERER, ex)}

    run_parallel(probe, order, 3)
    scale = len(jobs)
    h_size = t(f"All {scale} episodes", f"Cả {scale} tập") if scale > 1 else t("Size", "Dung lượng")
    head = f"{'Server':<9}{t('Quality', 'Chất lượng'):<20}"
    if scale > 1:
        head += f"{t('Per episode', 'Mỗi tập'):<14}"
    head += f"{h_size:<16}{'Bitrate':<12}{t('Length', 'Thời lượng'):<12}{t('Host', 'Máy chủ')}"
    log("\n  " + bold(head))
    log("  " + gray("─" * visible_len(head)))
    for srv in order:
        r = rows.get(srv, {})
        if "error" in r:
            log("  " + bold(f"{srv:<9}") + red(r["error"]))
            continue
        size = r["size"]
        row = bold(f"{srv:<9}") + f"{r['quality']:<20}"
        if scale > 1:
            row += f"{(f'~{fmt_bytes(size)}' if size else '?'):<14}"
        total = size * scale if size else None
        row += green(f"{(f'~{fmt_bytes(total)}' if total else t('unknown', 'không đo được')):<16}")
        row += f"{fmt_bitrate(size, r['duration']):<12}{fmt_eta(r['duration'] * scale):<12}{gray(r['host'])}"
        if srv == server:
            row += "  " + cyan(t("← used by default", "← mặc định dùng server này"))
        log("  " + row)
    if scale > 1:
        log(gray(t("\n  (Estimated from the first episode.)", "\n  (Ước tính từ tập đầu.)")))
    log(gray(t("  Pick another server with -s, for example: -s 2",
               "  Chọn server khác bằng -s, ví dụ: -s 2")))
    if not find_tool("aria2c"):
        log("\n" + yellow(aria2_tip()))
    return 0 if any("error" not in r for r in rows.values()) else 1


def check_dependencies() -> bool:
    missing = [name for name in ("cryptography",) if importlib.util.find_spec(name) is None]
    if missing:
        log(red(t(f"Missing library ({', '.join(missing)}). Run: python -m pip install -r requirements.txt",
                  f"Thiếu thư viện ({', '.join(missing)}). Chạy: python -m pip install -r requirements.txt")),
            err=True)
        return False
    return True


def resolve_jobs(sess: Session, todo: list[Job], server: str,
                 subs: str, ffmpeg: Optional[str]) -> None:
    """Get the stream, the size and the subtitles of every episode, in parallel."""
    log("\n" + cyan(t(f"Getting links for {len(todo)} file(s)...", f"Đang lấy link {len(todo)} file...")))

    def resolve(job: Job):
        try:
            job.stream = open_stream_any(sess, job.fid, server)
        except NotLoggedIn:
            job.fail(t(f"the session has expired, run: {login_hint()}",
                       f"phiên đăng nhập hết hạn, chạy: {login_hint()}"))
            log(f"  {red('✗')} {job.label}: {red(job.error)}", err=True)
            return
        except Exception as ex:
            job.fail(describe_error(REFERER, ex))
            log(f"  {red('✗')} {job.label}: {red(job.error)}", err=True)
            return
        job.quality = describe_quality(job.media, job.stream, ffmpeg)
        try:
            job.size = estimate_size(job.stream)
        except Exception:
            job.size = None
        try:
            job.subs = save_subtitles(sess, job.fid, job.out, subs)
        except Exception as ex:
            warn(f"{job.label}: " + t("subtitles: ", "phụ đề: ") + describe_error(REFERER, ex))
        duration = job.stream.duration
        details = [
            bold(job.quality),
            f"server {job.stream.server}",
            f"~{fmt_bytes(job.size)}" if job.size else None,
            fmt_bitrate(job.size, duration) if job.size else None,
            fmt_eta(duration) if duration else None,
            t("subtitles: ", "phụ đề: ") + ", ".join(job.subs) if job.subs else None,
        ]
        log(f"  {gray('•')} {job.label}: " + gray(" · ").join(d for d in details if d))

    run_parallel(resolve, todo, 3)


def download_jobs(ready: list[Job], aria2: Optional[str], ffmpeg: Optional[str],
                  jobs: int, fragments: Optional[int]) -> None:
    board.start([(j.label, j.size) for j in ready])

    def download(job: Job):
        if board.enabled:
            board.update(job.label, note=t("starting...", "đang chuẩn bị..."))
        else:
            log(f"> {job.label}: " + t("start", "bắt đầu") + f" -> {os.path.basename(job.out)}")
        try:
            final = download_segments(aria2, job.stream, job.out, job.label, ffmpeg,
                                      connections=fragments, expected=job.size)
            size = os.path.getsize(final)
            job.status = "ok"
            board.finish(job.label, True, size)
            log(f"  {bold_green('✓')} {bold(job.label)}  {green(os.path.basename(final))}  {gray(fmt_bytes(size))}")
        except Exception as ex:
            job.fail(str(ex))
            board.finish(job.label, False)
            log(f"  {bold_red('✗')} {bold(job.label)}  {red(job.error)}", err=True)

    try:
        run_parallel(download, ready, jobs)
    finally:
        board.stop()


def write_failed(out_dir: Path, url: str, failed: list[Job]) -> Optional[Path]:
    """failed.txt in the output folder: each failed file with its link and reason.
    Removed again when nothing fails."""
    path = out_dir / "failed.txt"
    if not failed:
        _remove(path)
        return None
    lines = [url, ""]
    for job in failed:
        lines.append(f"{job.label}  {watch_url(job.fid)}")
        lines.append(f"    {job.error}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def account_commands(sess: Session, args) -> Optional[int]:
    """--logout, --cookies and --login. Returns an exit code when there is nothing else to do."""
    if args.logout:
        sess.forget()
        sess.save()
        log(green(t(f"Logged out of {HOST} (its cookies were removed from session.txt).",
                    f"Đã đăng xuất {HOST} (đã xóa cookie của trang khỏi session.txt).")))
        return 0 if not args.url else None
    if args.cookies:
        try:
            count = import_cookies(sess, Path(args.cookies).expanduser())
        except OSError as ex:
            log(red(t(f"Cannot read {args.cookies}: {ex}", f"Không đọc được {args.cookies}: {ex}")), err=True)
            return 1
        if not count:
            log(red(t(f"No {HOST} cookies in that file (use --site for another domain).",
                      f"File đó không có cookie của {HOST} (dùng --site cho tên miền khác).")), err=True)
            return 1
        user = whoami(sess)
        if not user:
            log(red(t(f"These cookies are not logged in (expired?). Log in on {HOST} and export them again.",
                      f"Cookie này chưa đăng nhập (hết hạn?). Đăng nhập {HOST} rồi xuất lại.")), err=True)
            return 1
        sess.save()
        log(green(t("Cookies imported, logged in as ", "Đã nhập cookie, đăng nhập: ") + str(user.get("userName") or "?")))
    if args.login:
        if not sys.stdin.isatty():
            log(red(t("--login needs a terminal to type the password.",
                      "--login cần chạy trong terminal để nhập mật khẩu.")), err=True)
            return 1
        if not prompt_login(sess, args.email or ""):
            return 1
    if not args.url:
        if args.login or args.cookies:
            return 0
        user = whoami(sess)
        log(t(f"{HOST}: logged in as ", f"{HOST}: đang đăng nhập ") + bold(str(user.get("userName"))) if user
            else yellow(t(f"Not logged in to {HOST}. Run: {login_hint()}",
                          f"Chưa đăng nhập {HOST}. Chạy: {login_hint()}")))
        return 0 if user else 1
    return None


class Abort(Exception):
    """Stop with a red message and an exit code (1 = error, 2 = wrong command line)."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def login_hint() -> str:
    """The command that logs in to the current site."""
    site = "" if HOST == KNOWN_SITES[0] else f" --site {HOST}"
    return f"python phim-dl.py --login{site}"


def parse_numbers(text: str, available: list[int]) -> list[int]:
    """Season numbers from '1', '2-3', '2-' (only the ones that exist)."""
    wanted: set[int] = set()
    for part in re.split(r"[,\s]+", (text or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)", part)
        if m:
            lo, hi = int(m.group(1) or min(available)), int(m.group(2) or max(available))
            wanted |= {n for n in available if lo <= n <= hi}
        elif part.isdigit():
            wanted.add(int(part))
        else:
            raise ValueError(t(f"cannot read '{part}'", f"không hiểu '{part}'"))
    return [n for n in available if n in wanted]


def pick_episodes(sess: Session, info: dict, args) -> list[Optional[dict]]:
    """What to download: [None] for a movie, else the chosen episodes. With --list, prints them."""
    if info["type"] == "movie":
        return [None]
    if info["type"] == "episode":
        return [info["episode"]]

    seasons = sorted(info["seasons"], key=lambda s: int(s.get("number") or 0))
    if not seasons:
        raise Abort(t("This series has no seasons yet.", "Phim bộ này chưa có mùa nào."))
    numbers = [int(s.get("number") or i + 1) for i, s in enumerate(seasons)]
    log("  " + gray(t(f"Series · {len(seasons)} season(s): ", f"Phim bộ · {len(seasons)} mùa: ")
                    + ", ".join(f"S{n:02d} ({s.get('episodeCount') or '?'} {t('eps', 'tập')})"
                                for n, s in zip(numbers, seasons))))
    if args.season:
        try:
            keep = parse_numbers(args.season, numbers)
        except ValueError as ex:
            raise Abort(t("Season selection: ", "Lỗi chọn mùa: ") + str(ex), 2)
        seasons = [s for n, s in zip(numbers, seasons) if n in keep]
        if not seasons:
            raise Abort(t("No such season.", "Không có mùa đó."), 2)

    picked: list[Optional[dict]] = []
    for season in seasons:
        try:
            eps = list_episodes(sess, season)
        except NotLoggedIn:
            raise
        except Exception as ex:
            raise Abort(t("Could not read the episode list: ", "Không đọc được danh sách tập: ")
                        + describe_error(SITE, ex))
        if args.list:
            log("\n" + bold(t(f"Season {season.get('number')}", f"Mùa {season.get('number')}")))
            for ep in eps:
                num = f"{ep['number']:>3}."
                log(f"  {gray(num)} {episode_label(ep)}  {ep['name']}")
            continue
        try:
            # With several seasons, "-e 1-12" takes what exists in each one (a season may be shorter).
            chosen = parse_selection(args.eps or "all", len(eps), strict=len(seasons) == 1)
        except ValueError as ex:
            raise Abort(t(f"Episode selection (season {season.get('number')}): ",
                          f"Lỗi chọn tập (mùa {season.get('number')}): ") + str(ex), 2)
        picked.extend(eps[i] for i in chosen)
    if not args.list and not picked:
        raise Abort(t("No episode selected.", "Không có tập nào được chọn."), 2)
    return picked


def choose_downloader(args, ffmpeg: Optional[str]) -> Optional[str]:
    """aria2c (path) or None for the built-in downloader."""
    if not ffmpeg:
        warn(t("ffmpeg not found, files will be saved as .ts "
               "(put ffmpeg.exe next to phim-dl.py or run `python -m pip install imageio-ffmpeg`).",
               "Không tìm thấy ffmpeg, file sẽ được lưu dạng .ts "
               "(đặt ffmpeg.exe cạnh phim-dl.py hoặc chạy `python -m pip install imageio-ffmpeg`)."))
    aria2 = find_tool("aria2c") if args.downloader in ("auto", "aria2") else None
    if args.downloader == "aria2" and not aria2:
        raise Abort(t("aria2c not found: put aria2c.exe next to phim-dl.py or install it on PATH.",
                      "Không tìm thấy aria2c: đặt aria2c.exe cạnh phim-dl.py hoặc cài vào PATH."))
    log(gray(t("Downloader: ", "Bộ tải: ") + ("aria2c" if aria2 else t("built-in", "có sẵn"))
             + "  ·  ffmpeg: " + (t("yes", "có") if ffmpeg else t("no", "không"))))
    if not aria2 and args.downloader == "auto":
        log(yellow(aria2_tip()))
    return aria2


def skip_finished(all_jobs: list[Job], ffmpeg: Optional[str]) -> list[Job]:
    """The jobs whose file is not there yet; finished ones are marked 'skip'."""
    todo = []
    for job in all_jobs:
        done_files = [job.out] + ([] if ffmpeg else [os.path.splitext(job.out)[0] + ".ts"])
        existing = next((f for f in done_files if os.path.exists(f) and os.path.getsize(f) > 0), None)
        if existing:
            log(f"  {gray('=')} {job.label}: " + gray(t("already there, skipped", "đã có, bỏ qua")
                                                      + f" ({os.path.basename(existing)})"))
            job.status = "skip"
        else:
            todo.append(job)
    return todo


def report_size(ready: list[Job], out_dir: Path) -> None:
    total_size = sum(j.size or 0 for j in ready)
    if not total_size:
        return
    unknown = sum(1 for j in ready if not j.size)
    log("\n" + t("Estimated size: ", "Dung lượng ước tính: ") + bold(f"~{fmt_bytes(total_size)}")
        + (gray(t(f" ({unknown} unknown)", f" (chưa tính {unknown} file không đo được)")) if unknown else ""))
    free = shutil.disk_usage(out_dir).free
    if free < total_size * 1.05:
        warn(t(f"Only {fmt_bytes(free)} free on this drive, it may not be enough.",
               f"Ổ đĩa chỉ còn trống {fmt_bytes(free)}, có thể không đủ chỗ."))


def run(args) -> int:
    log(bold_cyan("phim-dl") + gray(f" v{__version__}"))
    if args.site:
        set_site(args.site)
    elif args.url and "://" in args.url:
        set_site(args.url)
    if HOST not in KNOWN_SITES:
        warn(t(f"{HOST} is not a known site; trying it anyway.",
               f"{HOST} không nằm trong danh sách trang đã biết; vẫn thử."))
    sess = Session(SESSION_FILE)
    try:
        code = account_commands(sess, args)
        if code is not None:
            return code
        return download_command(sess, args)
    except Abort as ex:
        log(red(str(ex)), err=True)
        return ex.code
    except NotLoggedIn:
        log(red(t(f"The {HOST} session has expired. Run: {login_hint()}",
                  f"Phiên đăng nhập {HOST} đã hết hạn. Chạy: {login_hint()}")), err=True)
        return 1


def download_command(sess: Session, args) -> int:
    """Everything after the account part: read the movie, then measure or download it."""
    try:
        kind, fid = parse_target(args.url)
    except ValueError as ex:
        raise Abort(t("Error: ", "Lỗi: ") + str(ex), 2)
    server = str(args.server or 1)
    if server not in SERVERS:
        raise Abort(t(f"The server must be 1 to {len(SERVERS)}.", f"Server phải từ 1 đến {len(SERVERS)}."), 2)
    if not check_dependencies():
        return 1
    sess.watch_fid = fid
    user = ensure_login(sess, args)
    if not user:
        return 1
    if user.get("canWatch") is False:
        warn(t(f"This account cannot watch movies right now (check its plan on {HOST}).",
               f"Tài khoản này hiện không xem phim được (kiểm tra gói trên {HOST})."))

    url = args.url.strip()
    log(gray(t("Reading movie page: ", "Đang đọc trang phim: ") + (url if url.startswith("http") else fid)))
    try:
        info = load_target(sess, kind, fid)
    except NotLoggedIn:
        raise
    except Exception as ex:
        raise Abort(t("Error: ", "Lỗi: ") + describe_error(SITE, ex))

    title, year = info["title"], info["year"]
    vi = f" {gray('·')} {info['title_vi']}" if info.get("title_vi") and info["title_vi"] != title else ""
    log("\n  " + bold(title) + (gray(f" ({year})") if year else "") + vi)

    picked = pick_episodes(sess, info, args)
    if args.list:
        if info["type"] != "show":
            for ep in picked:
                log(f"  {episode_label(ep)}  {watch_url(ep['fid'] if ep else info['fid'])}")
        return 0

    out_dir = Path(args.output).expanduser() if args.output else SCRIPT_DIR / "downloads"
    if not args.no_subfolder:
        out_dir = out_dir / safe_name(f"{title} ({year})" if year else title)
    all_jobs = [Job(episode_label(ep), ep["fid"] if ep else info["fid"],
                    str(out_dir / build_filename(title, year, ep)),
                    (ep.get("media") if ep else info.get("media")) or {})
                for ep in picked]

    ffmpeg = find_ffmpeg()
    if args.info:
        return show_sizes(sess, all_jobs, server, ffmpeg)

    aria2 = choose_downloader(args, ffmpeg)
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = max(1, min(args.jobs, 8, len(all_jobs)))
    fragments = None if args.fragments is None else max(1, min(args.fragments, 64))
    shown = fragments or DEFAULT_FRAGMENTS["aria2" if aria2 else "builtin"]

    todo = skip_finished(all_jobs, ffmpeg)
    try:
        resolve_jobs(sess, todo, server, args.subs, ffmpeg)
        ready = [j for j in todo if j.stream]
        report_size(ready, out_dir)
        if ready:
            log(t(f"Downloading {len(ready)} file(s) to ", f"Tải {len(ready)} file vào ") + bold(str(out_dir))
                + gray(t(f"  ({min(jobs, len(ready))} at a time × {shown} segments)",
                         f"  ({min(jobs, len(ready))} file cùng lúc × {shown} đoạn)")) + "\n")
            download_jobs(ready, aria2, ffmpeg, jobs, fragments)
    finally:
        sess.save()

    ok = sum(1 for j in all_jobs if j.status in ("ok", "skip"))
    failed = [j for j in all_jobs if j.status == "fail"]
    failed_file = write_failed(out_dir, url, failed)
    done_txt = t(f"Done: {ok}/{len(all_jobs)} file(s).", f"Hoàn tất: {ok}/{len(all_jobs)} file.")
    log("\n" + (bold_green(done_txt) if not failed else bold_red(done_txt)) + " " + gray(str(out_dir)))
    if failed_file:
        log(yellow(t(f"Failed files and reasons: {failed_file}", f"File lỗi và lý do: {failed_file}")))
        log(gray(t("Run the same command again to retry them.", "Chạy lại đúng lệnh cũ để thử lại.")))
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phim-dl",
        description=t("phim-dl: download movies and series from phimkey.com and its clones "
                      "(phimsea.com, phimfix.com, xemphim.in, ...). An account is needed.",
                      "phim-dl: tải phim lẻ và phim bộ từ phimkey.com và các trang clone "
                      "(phimsea.com, phimfix.com, xemphim.in, ...). Cần tài khoản."),
        epilog=t("First log in once:   python phim-dl.py --login   (another site: --login --site phimsea.com)\n"
                 'Then:                python phim-dl.py "https://phimkey.com/title/the-odyssey~Vk8k"\n\n'
                 "aria2c is recommended for much faster downloads: put aria2c.exe next to phim-dl.py\n"
                 "(https://github.com/aria2/aria2/releases).",
                 "Đăng nhập một lần:   python phim-dl.py --login   (trang khác: --login --site phimsea.com)\n"
                 'Sau đó:              python phim-dl.py "https://phimkey.com/title/the-odyssey~Vk8k"\n\n'
                 "Khuyên dùng aria2c để tải nhanh hơn nhiều: đặt aria2c.exe cạnh phim-dl.py\n"
                 "(https://github.com/aria2/aria2/releases)."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", nargs="?", help=t("movie / series / episode link on phimkey.com or a clone site",
                                            "link phim lẻ / phim bộ / tập phim trên phimkey.com hoặc trang clone"))
    p.add_argument("--site", metavar="DOMAIN",
                   help=t("the site for --login / --cookies / --logout without a link, "
                          "e.g. phimsea.com (default: the link's domain, else phimkey.com)",
                          "trang dùng cho --login / --cookies / --logout khi không có link, "
                          "vd phimsea.com (mặc định: tên miền của link, không thì phimkey.com)"))
    p.add_argument("--login", action="store_true",
                   help=t("log in with email and password (asked in the terminal)",
                          "đăng nhập bằng email và mật khẩu (nhập trong terminal)"))
    p.add_argument("--email", help=t("email for --login", "email cho --login"))
    p.add_argument("--cookies", metavar="FILE",
                   help=t("use the session of your browser: a cookies.txt file exported from the site",
                          "dùng phiên của trình duyệt: file cookies.txt xuất từ trang phim"))
    p.add_argument("--logout", action="store_true",
                   help=t("forget the saved session of the site", "xóa phiên đã lưu của trang"))
    p.add_argument("-S", "--season", help=t('only some seasons of a series: "1", "2-3" (default: all)',
                                            'chỉ tải một số mùa: "1", "2-3" (mặc định: tất cả)'))
    p.add_argument("-e", "--eps", help=t('only some episodes of each season: "1-5,8", "10-" (default: all)',
                                         'chỉ tải một số tập của mỗi mùa: "1-5,8", "10-" (mặc định: tải hết)'))
    p.add_argument("-s", "--server", type=int, help=t("server 1-3 (default: 1, the others are tried when it fails)",
                                                      "server 1-3 (mặc định: 1, lỗi thì tự thử server khác)"))
    p.add_argument("--subs", default="vi,en",
                   help=t('subtitle languages: "vi,en", "all" or "none" (default: vi,en)',
                          'ngôn ngữ phụ đề: "vi,en", "all" hoặc "none" (mặc định: vi,en)'))
    p.add_argument("-i", "--info", action="store_true",
                   help=t("only show the quality and estimated size on each server, do not download",
                          "chỉ xem chất lượng và dung lượng ước tính trên từng server, không tải"))
    p.add_argument("-o", "--output", help=t("output folder (default: downloads/ next to phim-dl.py)",
                                            "thư mục lưu (mặc định: downloads/ cạnh phim-dl.py)"))
    p.add_argument("-j", "--jobs", type=int, default=2, help=t("episodes downloaded at once, 1-8 (default: 2)",
                                                               "số tập tải cùng lúc, 1-8 (mặc định: 2)"))
    p.add_argument("-n", "--fragments", type=int,
                   help=t("segments downloaded at once per episode, 1-64 (default: 32 with aria2c, 16 without)",
                          "số đoạn video tải cùng lúc trong mỗi tập, 1-64 (mặc định: 32 với aria2c, 16 nếu không có)"))
    p.add_argument("-d", "--downloader", choices=("auto", "aria2", "builtin"), default="auto",
                   help=t("auto = aria2c if found (next to the script or on PATH), else the built-in one",
                          "auto = aria2c nếu có (cạnh script hoặc trong PATH), không thì bộ tải có sẵn"))
    p.add_argument("--no-subfolder", action="store_true",
                   help=t("do not make a subfolder named after the movie", "không tạo thư mục con theo tên phim"))
    p.add_argument("-l", "--list", action="store_true",
                   help=t("only list seasons and episodes", "chỉ liệt kê mùa và tập"))
    p.add_argument("-y", "--yes", action="store_true", help=t("never ask anything", "không hỏi gì"))
    p.add_argument("--lang", choices=("en", "vi"), default=LANG,
                   help=t("message language (default: auto)", "ngôn ngữ thông báo (mặc định: tự nhận)"))
    p.add_argument("--no-color", action="store_true", help=t("plain text, no colors", "chữ trơn, không màu"))
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None) -> int:
    global LANG
    argv = sys.argv[1:] if argv is None else argv
    LANG = detect_lang(argv)
    if LANG not in ("en", "vi"):
        LANG = "en"
    parser = build_parser()
    args = parser.parse_args(argv)
    LANG = args.lang
    init_console(not args.no_color)
    if not (args.url or args.login or args.cookies or args.logout or args.site):
        parser.print_help()
        return 2

    try:
        return run(args)
    except KeyboardInterrupt:
        kill_children()
        board.stop()
        log("\n" + yellow(t("Stopped. Run the same command again to continue.",
                            "Đã dừng. Chạy lại đúng lệnh cũ để tải tiếp.")), err=True)
        # Download threads cannot be stopped by Ctrl+C; end the whole process.
        os._exit(130)


if __name__ == "__main__":
    sys.exit(main())
