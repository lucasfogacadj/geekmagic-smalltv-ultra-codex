from __future__ import annotations

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
