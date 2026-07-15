from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


SESSION_PREFIX = "codex-status-refresh"
DEFAULT_OUTPUT = Path.home() / ".codex" / "codex-status.json"
DEFAULT_CODEX_BIN = "codex"
DEFAULT_ATTEMPTS = 3
RATE_LIMIT_LABELS = (("5h limit:", 300), ("Weekly limit:", 10080))


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def command_path(env_name: str, default: str) -> str:
    configured = os.getenv(env_name)
    if configured:
        return configured
    return shutil.which(default) or default


def run(command: list[str], timeout: int = 10, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def capture_pane(tmux: str, session: str) -> str:
    try:
        result = run([tmux, "capture-pane", "-t", session, "-p", "-S", "-200"], timeout=5)
        return result.stdout
    except subprocess.CalledProcessError:
        return ""


def pane_state(tmux: str, session: str) -> str:
    try:
        result = run(
            [
                tmux,
                "display-message",
                "-p",
                "-t",
                session,
                "dead=#{pane_dead} status=#{pane_dead_status} command=#{pane_current_command}",
            ],
            timeout=5,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as err:
        return (err.stderr or str(err)).strip()


def wait_for_pane_activity(tmux: str, session: str, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = capture_pane(tmux, session)
        if normalize_lines(text):
            return text
        time.sleep(1)
    return text


def wait_for_text(tmux: str, session: str, patterns: tuple[str, ...], timeout: int) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = capture_pane(tmux, session)
        if all(pattern in text for pattern in patterns):
            return text
        time.sleep(1)
    return text


def wait_for_rate_limits(tmux: str, session: str, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = capture_pane(tmux, session)
        try:
            parse_status(text, datetime.now(timezone.utc))
            return text
        except RuntimeError:
            time.sleep(1)
    return text


def send_completed_status(tmux: str, session: str) -> None:
    run([tmux, "send-keys", "-t", session, "/"], timeout=5)
    time.sleep(0.2)
    run([tmux, "send-keys", "-t", session, "status"], timeout=5)
    time.sleep(0.2)
    run([tmux, "send-keys", "-t", session, "Tab"], timeout=5)
    time.sleep(0.2)
    run([tmux, "send-keys", "-t", session, "Enter"], timeout=5)


def assert_rate_limits(text: str, tmux: str, session: str) -> str:
    try:
        parse_status(text, datetime.now(timezone.utc))
        return text
    except RuntimeError:
        pass
    tail = "\n".join(normalize_lines(text)[-12:])
    state = pane_state(tmux, session)
    raise RuntimeError(
        "Codex /status output did not include rate limits"
        f" (pane {state}):\n{tail}"
    )


def is_blocked_by_update_prompt(text: str) -> bool:
    return (
        "Update available!" in text
        and "Skip until next version" in text
        and ">_ OpenAI Codex" not in text
    )


def dismiss_update_prompt(tmux: str, session: str) -> None:
    run([tmux, "send-keys", "-t", session, "3"], timeout=5)
    time.sleep(0.2)
    run([tmux, "send-keys", "-t", session, "Enter"], timeout=5)


def run_status_attempt(tmux: str, codex: str, cwd: str, timeout: int, attempt: int) -> str:
    session = f"{SESSION_PREFIX}-{os.getpid()}-{attempt}"
    run([tmux, "kill-session", "-t", session], check=False)
    try:
        run([tmux, "new-session", "-d", "-s", session, "-c", cwd, f"{codex} --no-alt-screen"], timeout=10)
        initial_text = wait_for_pane_activity(tmux, session, min(timeout, 20))
        if is_blocked_by_update_prompt(initial_text):
            dismiss_update_prompt(tmux, session)
            wait_for_text(tmux, session, ("OpenAI Codex",), timeout=min(timeout, 20))
        time.sleep(3)

        # Slash commands are accepted reliably in the TUI when completed first.
        send_completed_status(tmux, session)
        text = wait_for_rate_limits(tmux, session, timeout=timeout)
        return assert_rate_limits(text, tmux, session)
    finally:
        run([tmux, "kill-session", "-t", session], check=False)


def run_status(tmux: str, codex: str, cwd: str, timeout: int, attempts: int) -> str:
    errors: list[str] = []
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return run_status_attempt(tmux, codex, cwd, timeout, attempt)
        except Exception as err:
            errors.append(f"attempt {attempt}: {err}")
            if attempt < attempts:
                time.sleep(3)
    raise RuntimeError("Codex /status refresh failed after retries:\n" + "\n".join(errors))


def normalize_lines(text: str) -> list[str]:
    box_chars = {
        "│": " ",
        "╭": " ",
        "╮": " ",
        "╯": " ",
        "╰": " ",
        "─": " ",
        "┌": " ",
        "┐": " ",
        "┘": " ",
        "└": " ",
        "├": " ",
        "┤": " ",
        "┬": " ",
        "┴": " ",
    }

    lines: list[str] = []
    for line in text.splitlines():
        cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        cleaned = cleaned.replace("|", " ")
        cleaned = "".join(ch if ch not in box_chars else " " for ch in cleaned)
        cleaned = "".join(ch if ch.isprintable() else " " for ch in cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def parse_reset(text: str | None, now: datetime) -> int | None:
    if not text:
        return None
    text = text.strip()

    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if match:
        reset = now.replace(
            hour=int(match.group(1)),
            minute=int(match.group(2)),
            second=0,
            microsecond=0,
        )
        if reset <= now - timedelta(minutes=1):
            reset += timedelta(days=1)
        return int(reset.timestamp())

    match = re.fullmatch(r"(\d{1,2}):(\d{2}) on (\d{1,2}) ([A-Za-z]{3})", text)
    if match:
        month_names = {
            "Jan": 1,
            "Feb": 2,
            "Mar": 3,
            "Apr": 4,
            "May": 5,
            "Jun": 6,
            "Jul": 7,
            "Aug": 8,
            "Sep": 9,
            "Oct": 10,
            "Nov": 11,
            "Dec": 12,
        }
        hour, minute, day, month_name = match.groups()
        month = month_names.get(month_name)
        if month is None:
            return None
        reset = now.replace(
            month=month,
            day=int(day),
            hour=int(hour),
            minute=int(minute),
            second=0,
            microsecond=0,
        )
        if reset <= now - timedelta(days=1):
            reset = reset.replace(year=reset.year + 1)
        return int(reset.timestamp())

    return None


def parse_limit_window(
    lines: list[str],
    index: int,
    marker: str,
    window_minutes: int,
    now: datetime,
) -> dict | None:
    line = lines[index]
    if marker.lower() not in line.lower():
        return None

    match = re.search(
        r"(\d+(?:\.\d+)?)%\s+left(?:\s+\(resets ([^)]+)\))?",
        line,
        re.IGNORECASE,
    )
    if not match:
        return None

    remaining = max(0.0, min(100.0, float(match.group(1))))
    reset_text = match.group(2)
    if reset_text is None and index + 1 < len(lines):
        reset_match = re.search(r"\(resets ([^)]+)\)", lines[index + 1], re.IGNORECASE)
        if reset_match:
            reset_text = reset_match.group(1)

    return {
        "used_percent": 100.0 - remaining,
        "remaining_percent": remaining,
        "window_minutes": window_minutes,
        "resets_at": parse_reset(reset_text, now),
    }


def parse_status(text: str, now: datetime) -> dict:
    lines = normalize_lines(text)
    windows_by_minutes: dict[int, dict] = {}
    account: str | None = None

    for index, line in enumerate(lines):
        if "Account:" in line:
            account = line.split("Account:", 1)[1].strip()

        for marker, window_minutes in RATE_LIMIT_LABELS:
            window = parse_limit_window(lines, index, marker, window_minutes, now)
            if window is not None:
                windows_by_minutes[window_minutes] = window

    windows = [windows_by_minutes[key] for key in sorted(windows_by_minutes)]
    if not windows:
        raise RuntimeError("Could not parse any Codex usage limit from /status")

    return {
        "schema_version": 2,
        "source": "codex_status",
        "updated_at": int(now.timestamp()),
        "updated_at_iso": now.isoformat(),
        "account": account,
        "primary": windows[0],
        "secondary": windows[1] if len(windows) > 1 else None,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def main() -> int:
    tmux = command_path("CODEX_STATUS_TMUX_BIN", "tmux")
    codex = command_path("CODEX_STATUS_CODEX_BIN", DEFAULT_CODEX_BIN)
    cwd = os.getenv("CODEX_STATUS_CWD", str(Path.home()))
    output = Path(os.getenv("CODEX_STATUS_OUTPUT", str(DEFAULT_OUTPUT)))
    timeout = env_int("CODEX_STATUS_TIMEOUT", 30)
    attempts = env_int("CODEX_STATUS_ATTEMPTS", DEFAULT_ATTEMPTS)

    text = run_status(tmux, codex, cwd, timeout, attempts)
    payload = parse_status(text, datetime.now(timezone.utc))
    write_json(output, payload)
    windows = [window for window in (payload["primary"], payload["secondary"]) if window]
    summary = " ".join(
        f'{window["window_minutes"]}m={window["remaining_percent"]}%'
        for window in windows
    )
    print(f"wrote {output}: {summary}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as err:
        print(f"codex status refresh failed: {err}", file=sys.stderr)
        raise SystemExit(1)
