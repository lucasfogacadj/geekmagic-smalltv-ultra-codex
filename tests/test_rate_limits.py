from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageFont

from app import smalltv_dashboard as dashboard
from scripts import codex_status_refresh


class DefaultFonts:
    def __init__(self) -> None:
        font = ImageFont.load_default()
        self.tiny = font
        self.small = font
        self.label = font
        self.body = font
        self.body_bold = font
        self.medium = font
        self.big = font
        self.hero = font
        self.mono = font


class StatusParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)

    def test_parses_current_weekly_only_status_as_primary(self) -> None:
        payload = codex_status_refresh.parse_status(
            """
            Account: user@example.com
            Weekly limit: 82% left
            (resets 16:30 on 21 Jul)
            """,
            self.now,
        )

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["primary"]["window_minutes"], 10080)
        self.assertEqual(payload["primary"]["remaining_percent"], 82.0)
        self.assertIsNone(payload["secondary"])

    def test_keeps_legacy_five_hour_and_weekly_windows(self) -> None:
        payload = codex_status_refresh.parse_status(
            """
            5h limit: 75% left (resets 18:00)
            Weekly limit: 40% left (resets 12:00 on 20 Jul)
            """,
            self.now,
        )

        self.assertEqual(payload["primary"]["window_minutes"], 300)
        self.assertEqual(payload["secondary"]["window_minutes"], 10080)


class DashboardWindowTests(unittest.TestCase):
    def test_log_database_size_limit_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "logs.sqlite"
            log_path.write_bytes(b"SQLite format 3\x00")
            data = dashboard.CodexUsageData(
                log_path,
                Path(temp_dir) / "sessions",
                Path(temp_dir) / "status.json",
                420,
                "UTC",
                7,
            )

            self.assertEqual(data.log_max_bytes, 0)

    def test_closes_the_log_database_after_collecting_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "logs.sqlite"
            connection = sqlite3.connect(log_path)
            connection.execute(
                "create table logs (id integer primary key, ts integer, target text, feedback_log_body text)"
            )
            connection.commit()
            connection.close()

            data = dashboard.CodexUsageData(
                log_path,
                Path(temp_dir) / "sessions",
                Path(temp_dir) / "status.json",
                420,
                "UTC",
                7,
            )
            with data._connect() as opened:
                self.assertEqual(opened.execute("select count(*) from logs").fetchone()[0], 0)

            with self.assertRaises(sqlite3.ProgrammingError):
                opened.execute("select 1")

    def test_rejects_log_database_larger_than_safety_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "logs.sqlite"
            log_path.write_bytes(b"SQLite format 3\\x00")
            data = dashboard.CodexUsageData(
                log_path,
                Path(temp_dir) / "sessions",
                Path(temp_dir) / "status.json",
                420,
                "UTC",
                7,
                log_max_bytes=8,
            )

            with self.assertRaisesRegex(RuntimeError, "safety limit"):
                with data._connect():
                    pass

    def test_uses_duration_instead_of_primary_secondary_slot(self) -> None:
        limits = dashboard.CodexRateLimits(
            primary=dashboard.RateWindow(used_percent=18, window_minutes=10080),
            secondary=None,
        )

        windows = dashboard.rate_limit_windows(limits)

        self.assertEqual(len(windows), 1)
        self.assertEqual(dashboard.rate_window_title(windows[0]), "JANELA SEMANA")

    def test_sorts_windows_from_shorter_to_longer(self) -> None:
        limits = dashboard.CodexRateLimits(
            primary=dashboard.RateWindow(used_percent=18, window_minutes=10080),
            secondary=dashboard.RateWindow(used_percent=25, window_minutes=300),
        )

        self.assertEqual(
            [window.window_minutes for window in dashboard.rate_limit_windows(limits)],
            [300, 10080],
        )

    def test_single_weekly_window_renders_one_page(self) -> None:
        usage = dashboard.CodexUsage(
            source_path=Path("test.jsonl"),
            rate_limits=dashboard.CodexRateLimits(
                primary=dashboard.RateWindow(
                    used_percent=18,
                    window_minutes=10080,
                    resets_at=int(self.self_reset_time().timestamp()),
                )
            ),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "dashboard.gif"
            config = dashboard.Config(
                output_path=output,
                upload_enabled=False,
                frames_per_page=3,
                frame_ms=50,
            )
            with (
                patch.object(dashboard, "Fonts", DefaultFonts),
                patch.object(dashboard, "safe_zoneinfo", return_value=timezone.utc),
            ):
                dashboard.render_gif(usage, output, config)

            with Image.open(output) as image:
                self.assertEqual(image.size, (240, 240))
                self.assertEqual(image.n_frames, 3)

    @staticmethod
    def self_reset_time() -> datetime:
        return datetime(2026, 7, 21, 16, 30, tzinfo=timezone.utc)


if __name__ == "__main__":
    unittest.main()
