#!/usr/bin/env python3
"""Регрессия desktop-позиции кнопки возврата создания категории."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EditorBackPositionTests(unittest.TestCase):
    def test_category_new_back_uses_lower_offset_on_1280_and_1366(self):
        css = (ROOT / "app" / "static" / "admin.css").read_text("utf-8")

        desktop = css.split("@media (min-width: 901px)", 1)[1]
        desktop = desktop.split("@media", 1)[0]
        self.assertIn(".editor-back.category-new-back { top: 112px; }", desktop)

        sticky = css.split("@media (max-width: 1380px)", 1)[1]
        sticky = sticky.split("@media", 1)[0]
        self.assertIn("position: sticky", sticky)
        self.assertIn("top: 76px", sticky)  # mobile/default editor-back remains unchanged


if __name__ == "__main__":
    unittest.main(verbosity=2)
