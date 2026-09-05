#!/usr/bin/env python3
"""Регрессии визуальной полировки списков, заголовков и редактора подборки."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app"


def source(relative: str) -> str:
    return (APP / relative).read_text("utf-8")


class UiPolishSourceTests(unittest.TestCase):
    def test_event_tabs_show_proposals_before_archive(self):
        template = source("templates/admin/dates.html")
        tabs = re.search(
            r'<div class="tabs dates-status-tabs".*?</div>', template, re.S,
        ).group(0)

        self.assertLess(tabs.index("Предложенные"), tabs.index("Архив"))

    def test_search_control_has_one_shared_icon_stacking_contract(self):
        css = source("static/admin.css")
        dates = source("templates/admin/dates.html")
        categories = source("templates/admin/categories.html")

        self.assertRegex(
            css,
            r"\.admin-search-input\s*>\s*svg\s*\{[^}]*z-index:\s*1",
        )
        self.assertIn("@media (forced-colors: active)", css)
        self.assertNotIn(".admin-search-input{", dates)
        self.assertNotIn(".admin-search-input{", categories)

    def test_category_sections_follow_the_task_sequence(self):
        css = source("static/admin.css")
        expected = {
            ".category-events-card": "1",
            ".category-editor-sections > .voting-settings": "2",
            ".category-privacy-card": "3",
            ".category-share-card": "4",
            ".category-appearance-card": "5",
        }
        for selector, order in expected.items():
            with self.subTest(selector=selector):
                self.assertRegex(
                    css,
                    rf"{re.escape(selector)}\s*\{{[^}}]*order:\s*{order}\s*;",
                )

        template = source("templates/admin/category_detail.html")
        self.assertIn(".category-privacy-form{display:grid;gap:16px}", template)

    def test_gradient_display_text_uses_inline_ink_fragments(self):
        contracts = {
            "static/admin.css": r"h1\s*>\s*\.display-ink\s*\{[^}]*display:\s*inline",
            "static/public.css": r"\.home-card h1\)\s*>\s*\.display-ink\s*\{[^}]*display:\s*inline",
            "static/auth.css": r"\.auth-module__brand\s*>\s*\.display-ink\s*\{[^}]*display:\s*inline",
        }
        for relative, pattern in contracts.items():
            with self.subTest(stylesheet=relative):
                self.assertRegex(source(relative), pattern)

        for relative in (
            "templates/admin/categories.html",
            "templates/admin/category_detail.html",
            "templates/admin/category_new.html",
            "templates/admin/dashboard.html",
            "templates/admin/date_form.html",
            "templates/admin/dates.html",
            "templates/admin/profile.html",
            "templates/admin/questions.html",
            "templates/public/category.html",
            "templates/public/share.html",
            "templates/public/profile_review.html",
            "templates/public/gone.html",
            "templates/auth/_login_module.html",
        ):
            with self.subTest(template=relative):
                self.assertIn('class="display-ink"', source(relative))


if __name__ == "__main__":
    unittest.main(verbosity=2)
