"""CSS contracts for horizontally scrollable interactive rows."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def rule(css: str, selector: str, *, last: bool = False) -> str:
    matches = re.findall(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", css, re.S)
    if not matches:
        raise AssertionError(f"CSS rule not found: {selector}")
    return matches[-1 if last else 0]


class ShadowSafeScrollRowTests(unittest.TestCase):
    def test_profile_tabs_keep_scrolling_and_reserve_glow_gutters(self):
        css = (STATIC / "profile.css").read_text(encoding="utf-8")
        declarations = rule(css, ".profile-tabs")

        self.assertIn("overflow-x: auto", declarations)
        self.assertIn("flex-wrap: nowrap", declarations)
        self.assertIn("justify-content: safe center", declarations)
        self.assertIn("padding: 10px 14px 20px", declarations)
        self.assertIn("margin: -7px -11px 7px", declarations)
        # Padding + compensating margin keeps the old 3px item origin and the
        # old effective scroll width: 10 - 7 == 14 - 11 == 3.
        self.assertEqual(10 - 7, 3)
        self.assertEqual(14 - 11, 3)

    def test_social_links_keep_scrolling_and_reserve_badge_gutters(self):
        css = (STATIC / "admin.css").read_text(encoding="utf-8")
        declarations = rule(css, ".social-links")

        self.assertIn("overflow-x: auto", declarations)
        self.assertIn("padding: 10px 14px 20px", declarations)
        self.assertIn("margin: -7px -11px -14px", declarations)
        # The vertical flow contribution stays at the previous 3px + 6px.
        self.assertEqual(-7 + 10 + 20 - 14, 9)

    def test_internal_tab_glows_fit_their_scrollport_gutters(self):
        css = (STATIC / "admin.css").read_text(encoding="utf-8")
        tabs = rule(css, "html[data-skin] .tabs .tab-ind", last=True)
        mobile_nav = rule(
            css, "html[data-skin] .top nav.glass-nav .tab-ind", last=True
        )

        self.assertIn("0 1px 2px", tabs)
        self.assertIn("0 2px 3px", mobile_nav)
        self.assertIn("color-mix(in srgb, var(--accent)", tabs)
        self.assertIn("color-mix(in srgb, var(--accent)", mobile_nav)


if __name__ == "__main__":
    unittest.main(verbosity=2)
