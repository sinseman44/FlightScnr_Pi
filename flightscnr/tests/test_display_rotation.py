"""Tests for display rotation + touch inverse mapping."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DATA_DIR = tempfile.mkdtemp(prefix="flightscnr-rotation-")
os.environ["FLIGHTSCNR_DATA_DIR"] = _DATA_DIR
os.environ.setdefault("HOME_LAT", "51.5")
os.environ.setdefault("HOME_LON", "-0.1")


class TestDisplayRotation(unittest.TestCase):
    def test_normalize_degrees(self):
        from display.round_touch.rotation import normalize_degrees

        self.assertEqual(normalize_degrees(90), 90)
        self.assertEqual(normalize_degrees(450), 90)
        self.assertEqual(normalize_degrees(95), 90)

    def test_to_logical_corners(self):
        from display.round_touch import rotation, theme

        side = theme.SIZE
        cases = {
            0: ((10, 20), (10, 20)),
            90: ((10, 20), (20, side - 1 - 10)),
            180: ((10, 20), (side - 1 - 10, side - 1 - 20)),
            270: ((10, 20), (side - 1 - 20, 10)),
        }
        for deg, (phys, expected) in cases.items():
            with self.subTest(deg=deg):
                with mock.patch.object(rotation, "rotation_degrees", return_value=deg):
                    self.assertEqual(rotation.to_logical(*phys), expected)

    def test_rectangular_viewport_offset_and_mapping(self):
        from display.round_touch import rotation, theme

        display_size = (480, 320)
        with mock.patch.object(theme, "SIZE", 320):
            self.assertEqual(rotation.viewport_offset(display_size), (80, 0))
            self.assertTrue(rotation.in_viewport(80, 0, display_size))
            self.assertTrue(rotation.in_viewport(399, 319, display_size))
            self.assertFalse(rotation.in_viewport(79, 160, display_size))
            self.assertFalse(rotation.in_viewport(400, 160, display_size))

            with mock.patch.object(rotation, "rotation_degrees", return_value=0):
                self.assertEqual(
                    rotation.to_logical(90, 20, display_size),
                    (10, 20),
                )
            with mock.patch.object(rotation, "rotation_degrees", return_value=90):
                self.assertEqual(
                    rotation.to_logical(90, 20, display_size),
                    (20, 309),
                )

    def test_present_radar_keeps_extended_map_outside_dial(self):
        import pygame

        from display.round_touch import rotation, theme

        display = pygame.Surface((160, 100))
        background = pygame.Surface((160, 100))
        background.fill((20, 40, 80))
        frame = pygame.Surface((100, 100))
        frame.fill((200, 30, 20))

        rotation._round_mask_cache.clear()
        with (
            mock.patch.object(theme, "SIZE", 100),
            mock.patch.object(theme, "VISIBLE_RADIUS", 48),
            mock.patch.object(rotation, "rotation_degrees", return_value=0),
        ):
            rotation.present_radar(display, frame, background)

        self.assertEqual(display.get_at((0, 50))[:3], (20, 40, 80))
        self.assertEqual(display.get_at((30, 0))[:3], (20, 40, 80))
        self.assertEqual(display.get_at((80, 50))[:3], (200, 30, 20))

    def test_rectangular_map_canvas_covers_any_rotation(self):
        import math

        from display.round_touch import map_bg, theme

        with (
            mock.patch.object(theme, "SIZE", 320),
            mock.patch.object(theme, "VISIBLE_RADIUS", 158),
        ):
            side = map_bg._background_canvas_side((480, 320))
            self.assertGreaterEqual(side, math.ceil(math.hypot(480, 320)) + 8)
            self.assertEqual(side % 2, 0)
            self.assertEqual(
                map_bg._background_canvas_side((320, 320)),
                158 * 2 + map_bg.TILE_SIZE,
            )

    def test_cycle_display_rotation(self):
        from display.round_touch import settings

        settings.set_display_rotation(0)
        self.assertEqual(settings.cycle_display_rotation(), 90)
        self.assertEqual(settings.cycle_display_rotation(), 180)
        self.assertEqual(settings.cycle_display_rotation(), 270)
        self.assertEqual(settings.cycle_display_rotation(), 0)


if __name__ == "__main__":
    unittest.main()
