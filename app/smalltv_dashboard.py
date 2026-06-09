from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFont


WIDTH = 240
HEIGHT = 240
APP_DIR = Path(__file__).resolve().parent
CODEX_ICON_FRAMES = APP_DIR / "assets" / "codex-icon-frames"
BG = (0, 0, 0)
PANEL = (0, 0, 0)
TRACK = (8, 8, 8)
TEXT = (246, 250, 255)
MUTED = (166, 176, 196)
DIM = (44, 48, 58)
GREEN = (58, 255, 137)
CYAN = (31, 235, 255)
YELLOW = (255, 220, 65)
ORANGE = (255, 129, 45)
RED = (255, 52, 90)
BLUE = (102, 130, 255)
PINK = (255, 45, 218)
VIOLET = (159, 87, 255)


def build_gif_palette() -> Image.Image:
    colors = [
        BG,
        TRACK,
        PANEL,
        TEXT,
        MUTED,
        DIM,
        GREEN,
        CYAN,
        YELLOW,
        ORANGE,
        RED,
        BLUE,
        PINK,
        VIOLET,
    ]
    levels = (0, 51, 102, 153, 204, 255)
    colors.extend((r, g, b) for r in levels for g in levels for b in levels)
    colors.extend((v, v, v) for v in range(0, 256, 17))

    palette: list[int] = []
    seen: set[tuple[int, int, int]] = set()
    for color in colors:
        if color in seen:
            continue
        seen.add(color)
        palette.extend(color)
        if len(palette) >= 256 * 3:
            break

    palette.extend([0] * (256 * 3 - len(palette)))
    image = Image.new("P", (1, 1))
    image.putpalette(palette)
    return image


GIF_PALETTE = build_gif_palette()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Config:
    smalltv_host: str = os.getenv("SMALLTV_HOST", "192.168.1.100")
    codex_log_path: Path = Path(
        os.getenv("CODEX_LOG_PATH", "/var/lib/codex/logs_2.sqlite")
    )
    codex_sessions_path: Path = Path(
        os.getenv("CODEX_SESSIONS_PATH", "/var/lib/codex/sessions")
    )
    codex_status_path: Path = Path(
        os.getenv("CODEX_STATUS_PATH", "/var/lib/codex/codex-status.json")
    )
    codex_status_max_age_seconds: int = env_int("CODEX_STATUS_MAX_AGE_SECONDS", 420)
    refresh_seconds: int = env_int("REFRESH_SECONDS", 30)
    gif_filename: str = os.getenv("GIF_FILENAME", "dashboard.gif")
    output_path: Path = Path(
        os.getenv("OUTPUT_PATH", "/var/cache/smalltv-dashboard/dashboard.gif")
    )
    upload_enabled: bool = env_bool("UPLOAD_ENABLED", True)
    run_once: bool = env_bool("RUN_ONCE", False)
    timeout: float = float(os.getenv("HTTP_TIMEOUT", "10"))
    display_tz: str = os.getenv("DISPLAY_TZ", os.getenv("TZ", "America/Sao_Paulo"))
    frames_per_page: int = env_int("FRAMES_PER_PAGE", 8)
    frame_ms: int = env_int("FRAME_MS", 700)
    max_gif_bytes: int = env_int("MAX_GIF_BYTES", 450_000)
    codex_lookback_days: int = env_int("CODEX_LOOKBACK_DAYS", 7)


class SmallTV:
    def __init__(self, host: str, timeout: float) -> None:
        self.base_url = f"http://{host.strip('/')}"
        self.timeout = timeout

    def get_json(self, path: str) -> dict:
        response = requests.get(urljoin(self.base_url, path), timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def upload_and_display(self, path: Path, filename: str) -> None:
        with path.open("rb") as fh:
            try:
                response = requests.post(
                    urljoin(self.base_url, "/doUpload?dir=/image/"),
                    files={"file": (filename, fh, "image/gif")},
                    timeout=self.timeout,
                )
                response.raise_for_status()
            except requests.exceptions.InvalidHeader as err:
                # Stock SmallTV Ultra firmware often returns duplicate Content-Length
                # headers after a successful upload. Treat that response quirk as
                # non-fatal and continue with the explicit image selection.
                logging.warning("ignoring malformed upload response from firmware: %s", err)

        requests.get(
            urljoin(self.base_url, "/set"),
            params={"i_i": "5", "autoplay": "0"},
            timeout=self.timeout,
        ).raise_for_status()
        requests.get(
            urljoin(self.base_url, "/set"),
            params={"img": f"/image/{filename}"},
            timeout=self.timeout,
        ).raise_for_status()
        requests.get(
            urljoin(self.base_url, "/set"),
            params={"theme": "3"},
            timeout=self.timeout,
        ).raise_for_status()


@dataclass
class UsageEvent:
    response_id: str
    ts: int
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    total_tokens: int


@dataclass
class RateWindow:
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: int | None = None
    inferred_reset: bool = False

    @property
    def remaining_percent(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, min(100.0, 100.0 - self.used_percent))


@dataclass
class CodexRateLimits:
    source_path: Path | None = None
    updated_at: int | None = None
    plan_type: str | None = None
    primary: RateWindow | None = None
    secondary: RateWindow | None = None
    reached_type: str | None = None


@dataclass
class CodexUsage:
    source_path: Path
    sessions_path: Path | None = None
    total_tokens: int = 0
    total_calls: int = 0
    today_tokens: int = 0
    today_input: int = 0
    today_output: int = 0
    today_cached: int = 0
    today_reasoning: int = 0
    today_calls: int = 0
    week_tokens: int = 0
    week_calls: int = 0
    latest: UsageEvent | None = None
    daily_tokens: list[tuple[str, int]] | None = None
    model_tokens: list[tuple[str, int]] | None = None
    rate_limits: CodexRateLimits | None = None
    error: str | None = None

    @property
    def cache_ratio(self) -> float | None:
        if self.today_input <= 0:
            return None
        return self.today_cached / self.today_input * 100


class CodexUsageData:
    COMPLETED_MARKER = '{"type":"response.completed"'
    TURN_USAGE_RE = re.compile(r"(total_usage_tokens|estimated_token_count)=([^\s,}]+)")
    MODEL_RE = re.compile(r"\bmodel=([A-Za-z0-9_.:-]+)")

    def __init__(
        self,
        log_path: Path,
        sessions_path: Path,
        status_path: Path,
        status_max_age_seconds: int,
        display_tz: str,
        lookback_days: int,
    ) -> None:
        self.log_path = log_path
        self.sessions_path = sessions_path
        self.status_path = status_path
        self.status_max_age_seconds = max(0, status_max_age_seconds)
        self.display_tz = display_tz
        self.lookback_days = max(1, lookback_days)

    def collect(self) -> CodexUsage:
        usage = CodexUsage(source_path=self.log_path, sessions_path=self.sessions_path)
        events: list[UsageEvent] = []
        try:
            with self._connect() as conn:
                events = self._completed_events(conn)
                if not events:
                    events = self._turn_usage_events(conn)
        except Exception as err:
            logging.exception("failed to read Codex usage from %s", self.log_path)
            usage.error = str(err)

        try:
            usage.rate_limits = self._latest_rate_limits()
        except Exception as err:
            logging.exception("failed to read Codex rate limits from %s", self.sessions_path)
            usage.error = f"{usage.error}; {err}" if usage.error else str(err)

        if not events:
            return usage

        tz = safe_zoneinfo(self.display_tz)
        now = datetime.now(tz)
        today = now.date()
        start_day = today - timedelta(days=self.lookback_days - 1)

        day_totals = {start_day + timedelta(days=i): 0 for i in range(self.lookback_days)}
        model_totals: dict[str, int] = {}
        latest = max(events, key=lambda event: event.ts)

        for event in events:
            event_date = datetime.fromtimestamp(event.ts, tz).date()
            usage.total_calls += 1
            usage.total_tokens += event.total_tokens
            model_totals[event.model] = model_totals.get(event.model, 0) + event.total_tokens

            if event_date == today:
                usage.today_calls += 1
                usage.today_tokens += event.total_tokens
                usage.today_input += event.input_tokens
                usage.today_output += event.output_tokens
                usage.today_cached += event.cached_tokens
                usage.today_reasoning += event.reasoning_tokens

            if start_day <= event_date <= today:
                usage.week_calls += 1
                usage.week_tokens += event.total_tokens
                day_totals[event_date] = day_totals.get(event_date, 0) + event.total_tokens

        usage.latest = latest
        usage.daily_tokens = [
            (day.strftime("%d/%m"), day_totals.get(day, 0))
            for day in sorted(day_totals)
        ]
        usage.model_tokens = sorted(
            model_totals.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:4]
        return usage

    def _connect(self) -> sqlite3.Connection:
        candidates = [self.log_path]
        home_default = Path.home() / ".codex" / "logs_2.sqlite"
        if self.log_path != home_default:
            candidates.append(home_default)

        for path in candidates:
            if path.exists():
                return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        raise FileNotFoundError(f"Codex log database not found: {self.log_path}")

    def _latest_rate_limits(self) -> CodexRateLimits | None:
        status_limits = self._status_rate_limits()
        if status_limits is not None:
            return status_limits

        sessions_root = self._sessions_root()
        if sessions_root is None:
            return None

        latest: tuple[int, Path, dict] | None = None
        files = sorted(
            sessions_root.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:40]

        for path in files:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = event.get("payload") or {}
                        if payload.get("type") != "token_count":
                            continue
                        rate_limits = payload.get("rate_limits")
                        if not isinstance(rate_limits, dict):
                            continue
                        ts = parse_timestamp(event.get("timestamp"))
                        if latest is None or ts >= latest[0]:
                            latest = (ts, path, rate_limits)
            except OSError:
                continue

        if latest is None:
            return None

        updated_at, source_path, rate_limits = latest
        return refresh_expired_rate_limits(
            CodexRateLimits(
                source_path=source_path,
                updated_at=updated_at,
                plan_type=optional_str(rate_limits.get("plan_type")),
                primary=parse_rate_window(rate_limits.get("primary")),
                secondary=parse_rate_window(rate_limits.get("secondary")),
                reached_type=optional_str(rate_limits.get("rate_limit_reached_type")),
            ),
            int(time.time()),
        )

    def _status_rate_limits(self) -> CodexRateLimits | None:
        if self.status_max_age_seconds <= 0 or not self.status_path.exists():
            return None

        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        updated_at = to_int_value(payload.get("updated_at"))
        now_ts = int(time.time())
        if updated_at <= 0 or now_ts - updated_at > self.status_max_age_seconds:
            return None

        return refresh_expired_rate_limits(
            CodexRateLimits(
                source_path=self.status_path,
                updated_at=updated_at,
                plan_type=optional_str(payload.get("account")),
                primary=parse_rate_window(payload.get("primary")),
                secondary=parse_rate_window(payload.get("secondary")),
                reached_type=None,
            ),
            now_ts,
        )

    def _sessions_root(self) -> Path | None:
        candidates = [self.sessions_path]
        home_default = Path.home() / ".codex" / "sessions"
        if self.sessions_path != home_default:
            candidates.append(home_default)

        for path in candidates:
            if path.exists() and path.is_dir():
                return path
        return None

    def _completed_events(self, conn: sqlite3.Connection) -> list[UsageEvent]:
        rows = conn.execute(
            """
            select id, ts, feedback_log_body
            from logs
            where target = 'codex_api::endpoint::responses_websocket'
              and feedback_log_body like ?
            order by id
            """,
            ("%response.completed%",),
        )
        by_response: dict[str, UsageEvent] = {}
        decoder = json.JSONDecoder()

        for log_id, ts, body in rows:
            if not body:
                continue
            marker_index = body.find(self.COMPLETED_MARKER)
            if marker_index < 0:
                continue
            try:
                event_payload, _ = decoder.raw_decode(body[marker_index:])
            except json.JSONDecodeError:
                continue

            response = event_payload.get("response") or {}
            usage = response.get("usage") or {}
            response_id = str(response.get("id") or log_id)
            input_details = usage.get("input_tokens_details") or {}
            output_details = usage.get("output_tokens_details") or {}
            input_tokens = to_int_value(usage.get("input_tokens"))
            output_tokens = to_int_value(usage.get("output_tokens"))
            total_tokens = to_int_value(usage.get("total_tokens")) or input_tokens + output_tokens

            by_response[response_id] = UsageEvent(
                response_id=response_id,
                ts=int(ts),
                model=str(response.get("model") or "unknown"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=to_int_value(input_details.get("cached_tokens")),
                reasoning_tokens=to_int_value(output_details.get("reasoning_tokens")),
                total_tokens=total_tokens,
            )

        return list(by_response.values())

    def _turn_usage_events(self, conn: sqlite3.Connection) -> list[UsageEvent]:
        rows = conn.execute(
            """
            select id, ts, feedback_log_body
            from logs
            where target = 'codex_core::session::turn'
              and feedback_log_body like '%post sampling token usage%'
            order by id
            """
        )
        events: list[UsageEvent] = []
        for log_id, ts, body in rows:
            fields = dict(self.TURN_USAGE_RE.findall(body or ""))
            total_tokens = to_int_value(fields.get("total_usage_tokens"))
            if total_tokens <= 0:
                continue
            estimated = parse_optional_int(fields.get("estimated_token_count"))
            model_match = self.MODEL_RE.search(body or "")
            events.append(
                UsageEvent(
                    response_id=f"turn-{log_id}",
                    ts=int(ts),
                    model=model_match.group(1) if model_match else "unknown",
                    input_tokens=estimated,
                    output_tokens=max(0, total_tokens - estimated),
                    cached_tokens=0,
                    reasoning_tokens=0,
                    total_tokens=total_tokens,
                )
            )
        return events


def refresh_expired_rate_limits(rate_limits: CodexRateLimits, now_ts: int) -> CodexRateLimits:
    return CodexRateLimits(
        source_path=rate_limits.source_path,
        updated_at=rate_limits.updated_at,
        plan_type=rate_limits.plan_type,
        primary=refresh_expired_window(rate_limits.primary, rate_limits.updated_at, now_ts),
        secondary=refresh_expired_window(rate_limits.secondary, rate_limits.updated_at, now_ts),
        reached_type=rate_limits.reached_type,
    )


def refresh_expired_window(
    window: RateWindow | None,
    updated_at: int | None,
    now_ts: int,
) -> RateWindow | None:
    if (
        window is None
        or updated_at is None
        or window.resets_at is None
        or window.window_minutes is None
        or now_ts < window.resets_at
        or updated_at >= window.resets_at
    ):
        return window

    period_seconds = max(60, window.window_minutes * 60)
    next_reset = window.resets_at
    while next_reset <= now_ts:
        next_reset += period_seconds

    return RateWindow(
        used_percent=0.0,
        window_minutes=window.window_minutes,
        resets_at=next_reset,
        inferred_reset=True,
    )


class Fonts:
    def __init__(self) -> None:
        regular = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        mono = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
        self.tiny = ImageFont.truetype(regular, 9)
        self.small = ImageFont.truetype(regular, 12)
        self.label = ImageFont.truetype(bold, 14)
        self.body = ImageFont.truetype(regular, 15)
        self.body_bold = ImageFont.truetype(bold, 16)
        self.medium = ImageFont.truetype(bold, 19)
        self.big = ImageFont.truetype(bold, 30)
        self.hero = ImageFont.truetype(bold, 42)
        self.mono = ImageFont.truetype(mono, 11)


def safe_zoneinfo(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logging.warning("unknown DISPLAY_TZ/TZ '%s', falling back to UTC", timezone_name)
        return ZoneInfo("UTC")


def to_int_value(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def parse_optional_int(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else 0


def optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def parse_timestamp(value: object) -> int:
    if not value:
        return 0
    text = str(value)
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def parse_rate_window(value: object) -> RateWindow | None:
    if not isinstance(value, dict):
        return None
    used_percent = to_float_value(value.get("used_percent"))
    if used_percent is None:
        remaining_percent = to_float_value(value.get("remaining_percent"))
        if remaining_percent is not None:
            used_percent = 100.0 - remaining_percent
    return RateWindow(
        used_percent=used_percent,
        window_minutes=to_int_value(value.get("window_minutes")) or None,
        resets_at=to_int_value(value.get("resets_at")) or None,
    )


def to_float_value(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def clamp(value: float | None, low: float = 0.0, high: float = 100.0) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return max(low, min(high, value))


def token_text(value: int | float | None) -> str:
    if value is None:
        return "--"
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    units = ("", "K", "M", "B")
    unit = units[0]
    for unit in units:
        if value < 1000 or unit == units[-1]:
            break
        value /= 1000
    if unit == "":
        return f"{sign}{value:.0f}"
    if value >= 100:
        return f"{sign}{value:.0f}{unit}"
    if value >= 10:
        return f"{sign}{value:.1f}{unit}"
    return f"{sign}{value:.2f}{unit}"


def ratio_text(value: float | None) -> str:
    if value is None:
        return "--%"
    return f"{value:.1f}%" if value < 10 else f"{value:.0f}%"


def window_text(window: RateWindow | None) -> str:
    if window is None or not window.window_minutes:
        return "--"
    if window.window_minutes >= 1440:
        days = window.window_minutes / 1440
        return f"{days:.0f}d"
    hours = window.window_minutes / 60
    if hours >= 1:
        return f"{hours:.0f}h"
    return f"{window.window_minutes}m"


def reset_text(window: RateWindow | None, tz: ZoneInfo) -> str:
    if window is None or not window.resets_at:
        return "--"
    reset_at = datetime.fromtimestamp(window.resets_at, tz)
    now = datetime.now(tz)
    if reset_at.date() == now.date():
        return reset_at.strftime("%H:%M")
    return reset_at.strftime("%d/%m %H:%M")


def dim_color(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(channel * factor))) for channel in color)


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int] = TEXT,
    anchor: str | None = None,
) -> None:
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    candidate = text
    while candidate and draw.textlength(candidate + "...", font=font) > max_width:
        candidate = candidate[:-1]
    return candidate + "..." if candidate else ""


def rounded_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    outline: tuple[int, int, int] = CYAN,
    fill: tuple[int, int, int] = PANEL,
    radius: int = 8,
) -> None:
    x1, y1, x2, y2 = box
    glow = dim_color(outline, 0.35)
    draw.rounded_rectangle((x1 - 2, y1 - 2, x2 + 2, y2 + 2), radius=radius + 2, outline=glow, width=1)
    draw.rounded_rectangle((x1 - 1, y1 - 1, x2 + 1, y2 + 1), radius=radius + 1, outline=dim_color(outline, 0.55), width=1)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)
    tick = 13
    draw.line((x1 + 8, y1, x1 + 8 + tick, y1), fill=outline, width=2)
    draw.line((x1, y1 + 8, x1, y1 + 8 + tick), fill=outline, width=2)
    draw.line((x2 - 8 - tick, y2, x2 - 8, y2), fill=outline, width=2)
    draw.line((x2, y2 - 8 - tick, x2, y2 - 8), fill=outline, width=2)


def progress_bar(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    value: float | None,
    color: tuple[int, int, int],
    phase: float = 1.0,
) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=(y2 - y1) // 2, fill=TRACK)
    width = max(0, int((x2 - x1) * clamp(value) / 100 * clamp(phase, 0, 1)))
    if width > 0:
        draw.rounded_rectangle((x1, y1 - 2, x1 + width, y2 + 2), radius=(y2 - y1) // 2 + 2, fill=dim_color(color, 0.18))
        draw.rounded_rectangle((x1, y1, x1 + width, y2), radius=(y2 - y1) // 2, fill=color)
        draw.line((x1 + 2, y1 + 1, x1 + width - 2, y1 + 1), fill=(255, 255, 255), width=1)


def header(
    draw: ImageDraw.ImageDraw,
    fonts: Fonts,
    title: str,
    page: int,
    now: datetime,
    total_pages: int = 2,
) -> None:
    draw_text(draw, (10, 6), title.upper(), fonts.label, TEXT)
    draw.line((10, 25, 74, 25), fill=CYAN, width=2)
    draw.line((75, 25, 90, 25), fill=PINK, width=2)
    draw_text(draw, (231, 8), now.strftime("%H:%M"), fonts.mono, CYAN, anchor="ra")
    start_x = 208 - total_pages * 6
    for idx in range(total_pages):
        fill = (72, 70, 102) if idx != page else PINK
        draw.rounded_rectangle((start_x + idx * 12, 22, start_x + 7 + idx * 12, 25), radius=2, fill=fill)


def cyber_background(image: Image.Image, accent: tuple[int, int, int], phase: float) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    draw.line((0, 28, WIDTH, 28), fill=(*accent, 55), width=1)


def draw_scanlines(image: Image.Image, offset: int) -> None:
    return


_CODEX_ICON_CACHE: list[Image.Image] | None = None


def remove_light_background(frame: Image.Image) -> Image.Image:
    frame = frame.convert("RGBA")
    cleaned = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    pixels: list[tuple[int, int, int, int]] = []
    for red, green, blue, alpha in frame.getdata():
        if alpha == 0:
            pixels.append((0, 0, 0, 0))
            continue

        brightest = max(red, green, blue)
        darkest = min(red, green, blue)
        if red >= 232 and green >= 232 and blue >= 232:
            pixels.append((0, 0, 0, 0))
        elif red >= 210 and green >= 210 and blue >= 218 and brightest - darkest <= 58:
            edge_alpha = max(0, min(alpha, (255 - brightest) * 6))
            pixels.append((red, green, blue, edge_alpha))
        else:
            pixels.append((red, green, blue, alpha))

    cleaned.putdata(pixels)
    return cleaned


def load_codex_icon_frames() -> list[Image.Image]:
    global _CODEX_ICON_CACHE
    if _CODEX_ICON_CACHE is not None:
        return _CODEX_ICON_CACHE

    frames: list[Image.Image] = []
    for path in sorted(CODEX_ICON_FRAMES.glob("frame-*.png")):
        try:
            frames.append(remove_light_background(Image.open(path)))
        except OSError:
            continue
    _CODEX_ICON_CACHE = frames
    return frames


def remaining_color(value: float | None) -> tuple[int, int, int]:
    if value is None:
        return MUTED
    if value < 20:
        return RED
    if value < 45:
        return YELLOW
    return GREEN


def draw_codex_icon(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    fonts: Fonts,
    center: tuple[int, int],
    accent: tuple[int, int, int],
    phase: float,
) -> None:
    frames = load_codex_icon_frames()
    if frames:
        idx = min(len(frames) - 1, int(phase * len(frames)))
        icon = frames[idx]
        x = center[0] - icon.width // 2
        y = center[1] - icon.height // 2
        image.alpha_composite(icon, (x, y))
        return

    cx, cy = center
    box = (cx - 22, cy - 22, cx + 22, cy + 22)
    draw.rounded_rectangle((box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2), radius=11, outline=dim_color(accent, 0.28), width=2)
    draw.rounded_rectangle(box, radius=10, fill=BG, outline=accent, width=2)
    draw.line((cx - 13, cy - 10, cx - 4, cy - 2, cx - 13, cy + 6), fill=CYAN, width=3)
    draw.line((cx + 1, cy + 8, cx + 13, cy + 8), fill=PINK, width=3)
    draw_text(draw, (cx + 6, cy - 6), "C", fonts.body_bold, TEXT, "mm")


def draw_rate_screen(
    usage: CodexUsage,
    fonts: Fonts,
    phase: float,
    now: datetime,
    window: RateWindow | None,
    title: str,
    page: int,
    accent: tuple[int, int, int],
) -> Image.Image:
    image = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    draw = ImageDraw.Draw(image)
    cyber_background(image, accent, phase)
    header(draw, fonts, "Restante", page, now, total_pages=2)
    rounded_panel(draw, (8, 31, 232, 228), outline=accent, radius=10)

    if usage.rate_limits is None:
        draw_codex_icon(image, draw, fonts, (120, 64), accent, phase)
        draw_no_data(draw, fonts, usage)
        draw_scanlines(image, int(phase * 8))
        return image.convert("RGB")

    tz = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else safe_zoneinfo("UTC")
    remaining = window.remaining_percent if window else None
    used = window.used_percent if window else None
    color = remaining_color(remaining)

    draw_codex_icon(image, draw, fonts, (120, 58), accent, phase)
    draw_text(draw, (120, 91), title, fonts.label, MUTED, "mm")
    draw_text(draw, (120, 126), ratio_text(remaining), fonts.hero, color, "mm")
    status = "RENOVADO" if window and window.inferred_reset else "DISPONIVEL"
    draw_text(draw, (120, 158), status, fonts.label, TEXT, "mm")
    progress_bar(draw, (22, 179, 218, 191), remaining, color, phase)

    draw_text(draw, (22, 204), "USO", fonts.label, MUTED)
    draw_text(draw, (78, 204), ratio_text(used), fonts.body_bold, ORANGE)
    draw_text(draw, (218, 198), "RESET", fonts.label, MUTED, "ra")
    draw_text(draw, (218, 213), reset_text(window, tz), fonts.small, CYAN, "ra")

    draw_scanlines(image, int(phase * 8))
    return image.convert("RGB")


def page_primary_remaining(usage: CodexUsage, fonts: Fonts, phase: float, now: datetime) -> Image.Image:
    window = usage.rate_limits.primary if usage.rate_limits else None
    return draw_rate_screen(usage, fonts, phase, now, window, "JANELA 05H", 0, GREEN)


def page_week_remaining(usage: CodexUsage, fonts: Fonts, phase: float, now: datetime) -> Image.Image:
    window = usage.rate_limits.secondary if usage.rate_limits else None
    return draw_rate_screen(usage, fonts, phase, now, window, "JANELA SEMANA", 1, BLUE)


def sparkline_tokens(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    rows: list[tuple[str, int]] | None,
    color: tuple[int, int, int],
) -> None:
    x1, y1, x2, y2 = box
    values = [value for _, value in (rows or [])]
    if len(values) < 2 or max(values) <= 0:
        draw.line((x1, (y1 + y2) // 2, x2, (y1 + y2) // 2), fill=DIM, width=1)
        return

    hi = max(values)
    points = []
    for idx, value in enumerate(values):
        x = x1 + int(idx * (x2 - x1) / (len(values) - 1))
        y = y2 - int(value * (y2 - y1) / hi)
        points.append((x, y))
    draw.line(points, fill=dim_color(color, 0.32), width=6, joint="curve")
    draw.line(points, fill=color, width=2, joint="curve")
    for x, y in points[-2:]:
        draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=color)


def draw_no_data(draw: ImageDraw.ImageDraw, fonts: Fonts, usage: CodexUsage) -> None:
    draw_text(draw, (120, 92), "SEM", fonts.hero, ORANGE, "mm")
    draw_text(draw, (120, 128), "DADOS", fonts.big, TEXT, "mm")
    source = fit_text(draw, str(usage.source_path), fonts.tiny, 188)
    draw_text(draw, (120, 176), source, fonts.tiny, MUTED, "mm")
    if usage.error:
        error = fit_text(draw, usage.error, fonts.tiny, 188)
        draw_text(draw, (120, 194), error, fonts.tiny, RED, "mm")


def draw_metric_row(
    draw: ImageDraw.ImageDraw,
    fonts: Fonts,
    label: str,
    value: int,
    denominator: int,
    y: int,
    color: tuple[int, int, int],
    phase: float,
) -> None:
    draw_text(draw, (22, y), label, fonts.label, MUTED)
    draw_text(draw, (218, y - 3), token_text(value), fonts.body_bold, TEXT, "ra")
    percent = value / denominator * 100 if denominator > 0 else 0
    progress_bar(draw, (22, y + 21, 218, y + 29), percent, color, phase)


def page_today(usage: CodexUsage, fonts: Fonts, phase: float, now: datetime) -> Image.Image:
    image = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    draw = ImageDraw.Draw(image)
    cyber_background(image, GREEN, phase)
    header(draw, fonts, "Restante", 0, now)

    rounded_panel(draw, (8, 31, 232, 228), outline=GREEN, radius=10)
    if usage.rate_limits is None:
        if usage.total_calls > 0:
            draw_text(draw, (22, 45), "TOKENS HOJE", fonts.label, PINK)
            draw_text(draw, (120, 92), token_text(usage.today_tokens), fonts.hero, TEXT, "mm")
            draw_text(draw, (120, 126), f"{usage.today_calls} chamadas", fonts.label, MUTED, "mm")
            draw_text(draw, (22, 174), "LIMITE", fonts.label, ORANGE)
            draw_text(draw, (218, 174), "sem dados", fonts.body, MUTED, "ra")
            draw_text(draw, (22, 208), "SESSOES", fonts.label, PINK)
            sessions = fit_text(draw, str(usage.sessions_path or "--"), fonts.tiny, 132)
            draw_text(draw, (218, 211), sessions, fonts.tiny, BLUE, "ra")
            draw_scanlines(image, int(phase * 8))
            return image.convert("RGB")
        draw_no_data(draw, fonts, usage)
        return image.convert("RGB")

    limits = usage.rate_limits
    primary = limits.primary
    secondary = limits.secondary
    primary_remaining = primary.remaining_percent if primary else None
    secondary_remaining = secondary.remaining_percent if secondary else None
    tz = now.tzinfo if isinstance(now.tzinfo, ZoneInfo) else safe_zoneinfo("UTC")

    remaining_color = GREEN
    if primary_remaining is not None and primary_remaining < 20:
        remaining_color = RED
    elif primary_remaining is not None and primary_remaining < 45:
        remaining_color = YELLOW

    draw_text(draw, (22, 45), f"JANELA {window_text(primary)}", fonts.label, MUTED)
    draw_text(draw, (120, 96), ratio_text(primary_remaining), fonts.hero, remaining_color, "mm")
    draw_text(draw, (120, 129), "DISPONIVEL", fonts.label, TEXT, "mm")
    progress_bar(draw, (22, 151, 218, 162), primary_remaining, remaining_color, phase)
    draw_text(draw, (22, 177), "RESET", fonts.label, MUTED)
    draw_text(draw, (218, 177), reset_text(primary, tz), fonts.body_bold, CYAN, "ra")

    draw_text(draw, (22, 205), f"SEMANA {window_text(secondary)}", fonts.label, MUTED)
    draw_text(draw, (218, 204), ratio_text(secondary_remaining), fonts.medium, BLUE, "ra")
    progress_bar(draw, (22, 222, 218, 227), secondary_remaining, BLUE, phase)

    draw_scanlines(image, int(phase * 8))
    return image.convert("RGB")


def page_breakdown(usage: CodexUsage, fonts: Fonts, phase: float, now: datetime) -> Image.Image:
    image = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    draw = ImageDraw.Draw(image)
    cyber_background(image, YELLOW, phase)
    header(draw, fonts, "Tokens", 1, now)

    rounded_panel(draw, (8, 31, 232, 228), outline=YELLOW, radius=10)
    if usage.total_calls <= 0:
        draw_no_data(draw, fonts, usage)
        return image.convert("RGB")

    denominator = max(1, usage.today_tokens)
    draw_metric_row(draw, fonts, "ENTRADA", usage.today_input, denominator, 48, CYAN, phase)
    draw_metric_row(draw, fonts, "CACHE", usage.today_cached, max(1, usage.today_input), 91, GREEN, phase)
    draw_metric_row(draw, fonts, "SAIDA", usage.today_output, denominator, 134, PINK, phase)
    draw_metric_row(draw, fonts, "REASON", usage.today_reasoning, denominator, 177, VIOLET, phase)

    draw_scanlines(image, int(phase * 8))
    return image.convert("RGB")


def page_week(usage: CodexUsage, fonts: Fonts, phase: float, now: datetime) -> Image.Image:
    image = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    draw = ImageDraw.Draw(image)
    cyber_background(image, GREEN, phase)
    header(draw, fonts, "7 Dias", 2, now)

    rounded_panel(draw, (8, 31, 232, 228), outline=GREEN, radius=10)
    if usage.total_calls <= 0:
        draw_no_data(draw, fonts, usage)
        return image.convert("RGB")

    draw_text(draw, (22, 47), "TOTAL", fonts.label, PINK)
    draw_text(draw, (218, 44), token_text(usage.week_tokens), fonts.big, TEXT, "ra")
    sparkline_tokens(draw, (28, 86, 212, 141), usage.daily_tokens, GREEN)

    if usage.daily_tokens:
        first_label = usage.daily_tokens[0][0]
        last_label = usage.daily_tokens[-1][0]
        top_label, top_value = max(usage.daily_tokens, key=lambda item: item[1])
    else:
        first_label = last_label = top_label = "--"
        top_value = 0
    draw_text(draw, (28, 149), first_label, fonts.tiny, MUTED)
    draw_text(draw, (212, 149), last_label, fonts.tiny, MUTED, "ra")

    avg = int(usage.week_tokens / usage.week_calls) if usage.week_calls else 0
    draw_text(draw, (26, 177), "MEDIA", fonts.label, MUTED)
    draw_text(draw, (26, 197), token_text(avg), fonts.medium, CYAN)
    draw_text(draw, (142, 177), "PICO", fonts.label, MUTED)
    draw_text(draw, (142, 197), token_text(top_value), fonts.medium, YELLOW)
    draw_text(draw, (218, 211), top_label, fonts.tiny, MUTED, "ra")

    draw_scanlines(image, int(phase * 8))
    return image.convert("RGB")


def page_models(usage: CodexUsage, fonts: Fonts, phase: float, now: datetime) -> Image.Image:
    image = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    draw = ImageDraw.Draw(image)
    cyber_background(image, PINK, phase)
    header(draw, fonts, "Modelos", 3, now)

    rounded_panel(draw, (8, 31, 232, 228), outline=PINK, radius=10)
    if usage.total_calls <= 0:
        draw_no_data(draw, fonts, usage)
        return image.convert("RGB")

    rows = usage.model_tokens or []
    denominator = max(1, rows[0][1] if rows else usage.total_tokens)
    draw_text(draw, (22, 45), "TOTAL LOCAL", fonts.label, CYAN)
    draw_text(draw, (218, 42), token_text(usage.total_tokens), fonts.medium, TEXT, "ra")
    for idx, (model, tokens) in enumerate(rows[:4]):
        yy = 76 + idx * 32
        draw_text(draw, (22, yy), fit_text(draw, model, fonts.body_bold, 122), fonts.body_bold, TEXT)
        draw_text(draw, (218, yy), token_text(tokens), fonts.body, MUTED, "ra")
        progress_bar(draw, (22, yy + 20, 218, yy + 27), tokens / denominator * 100, PINK, phase)

    if usage.latest is not None:
        latest_at = datetime.fromtimestamp(usage.latest.ts, now.tzinfo).strftime("%d/%m %H:%M")
        draw_text(draw, (22, 211), "ATUALIZADO", fonts.label, CYAN)
        draw_text(draw, (218, 213), latest_at, fonts.small, BLUE, "ra")

    draw_scanlines(image, int(phase * 8))
    return image.convert("RGB")


def render_gif(usage: CodexUsage, output_path: Path, config: Config) -> None:
    fonts = Fonts()
    now = display_now(config.display_tz)
    frames: list[Image.Image] = []
    page_renderers = (page_primary_remaining, page_week_remaining)

    for page_index, renderer in enumerate(page_renderers):
        for frame_index in range(config.frames_per_page):
            raw_phase = frame_index / max(1, config.frames_per_page - 1)
            phase = 1 - (1 - raw_phase) ** 3
            frame = renderer(usage, fonts, phase, now)
            frame = ImageEnhance.Sharpness(frame).enhance(1.2)
            frames.append(frame.convert("RGB").quantize(palette=GIF_PALETTE, dither=Image.Dither.NONE))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    durations = [config.frame_ms] * len(frames)
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        background=0,
        optimize=True,
        disposal=1,
    )

    if output_path.stat().st_size > config.max_gif_bytes:
        logging.warning(
            "GIF is %d bytes, above MAX_GIF_BYTES=%d; regenerating with fewer frames",
            output_path.stat().st_size,
            config.max_gif_bytes,
        )
        reduced = frames[::2] if len(frames) > 6 else frames
        reduced[0].save(
            output_path,
            save_all=True,
            append_images=reduced[1:],
            duration=[config.frame_ms * 2] * len(reduced),
            loop=0,
            background=0,
            optimize=True,
            disposal=1,
        )


def display_now(timezone_name: str) -> datetime:
    return datetime.now(safe_zoneinfo(timezone_name))


def setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime


def run_cycle(config: Config, data: CodexUsageData, device: SmallTV) -> None:
    started = time.monotonic()
    usage = data.collect()
    render_gif(usage, config.output_path, config)
    size = config.output_path.stat().st_size
    primary_remaining = None
    rate_source = None
    if usage.rate_limits and usage.rate_limits.primary:
        primary_remaining = usage.rate_limits.primary.remaining_percent
        rate_source = usage.rate_limits.source_path
    logging.info(
        "rendered %s (%d bytes): remaining=%s today=%s calls=%s week=%s total=%s source=%s rate_source=%s",
        config.output_path,
        size,
        ratio_text(primary_remaining),
        token_text(usage.today_tokens),
        usage.today_calls,
        token_text(usage.week_tokens),
        token_text(usage.total_tokens),
        usage.source_path,
        rate_source or "--",
    )
    if config.upload_enabled:
        device.upload_and_display(config.output_path, config.gif_filename)
        logging.info("uploaded and selected /image/%s", config.gif_filename)
    logging.debug("cycle completed in %.2fs", time.monotonic() - started)


def sleep_until_next(seconds: int, stop: "StopFlag") -> None:
    deadline = time.monotonic() + max(5, seconds)
    while not stop.stopped and time.monotonic() < deadline:
        time.sleep(min(1.0, deadline - time.monotonic()))


class StopFlag:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self, *_args: object) -> None:
        self.stopped = True


def main() -> int:
    setup_logging()
    config = Config()
    device = SmallTV(config.smalltv_host, config.timeout)
    data = CodexUsageData(
        config.codex_log_path,
        config.codex_sessions_path,
        config.codex_status_path,
        config.codex_status_max_age_seconds,
        config.display_tz,
        config.codex_lookback_days,
    )
    stop = StopFlag()
    signal.signal(signal.SIGTERM, stop.stop)
    signal.signal(signal.SIGINT, stop.stop)

    logging.info(
        "starting SmallTV Codex usage dashboard: smalltv=%s codex_log=%s sessions=%s refresh=%ss upload=%s",
        config.smalltv_host,
        config.codex_log_path,
        config.codex_sessions_path,
        config.refresh_seconds,
        config.upload_enabled,
    )

    while not stop.stopped:
        try:
            run_cycle(config, data, device)
        except Exception:
            logging.exception("dashboard cycle failed")
        if config.run_once:
            break
        sleep_until_next(config.refresh_seconds, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
