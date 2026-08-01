#!/usr/bin/env python3
"""Контракт общего внутрисайтового подтверждения действий."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class ConfirmDialogContractTests(unittest.TestCase):
    def test_dialog_is_safe_accessible_and_preserves_submitter(self):
        source = (APP / "static" / "confirm.js").read_text("utf-8")

        self.assertIn("window.d4yConfirm", source)
        self.assertIn('makeElement("dialog", "d4y-confirm")', source)
        self.assertIn('setAttribute("role", "alertdialog")', source)
        self.assertIn('setAttribute("aria-modal", "true")', source)
        self.assertIn('event.key === "Escape"', source)
        self.assertIn("event.target === dialog", source)
        self.assertIn("restoreFocus", source)
        self.assertIn("new WeakSet()", source)
        self.assertIn("event.submitter", source)
        self.assertIn("form.requestSubmit(submitter)", source)
        self.assertIn('addEventListener("submit"', source)
        self.assertNotIn("innerHTML", source)
        self.assertGreaterEqual(source.count(".textContent"), 5)

    def test_native_confirms_are_removed_from_guest_and_operator_scripts(self):
        guest = (APP / "static" / "guest.js").read_text("utf-8")
        operator = (APP / "static" / "operator.js").read_text("utf-8")
        native_confirm = re.compile(r"\b(?:window\.)?confirm\s*\(")

        self.assertIsNone(native_confirm.search(guest))
        self.assertIsNone(native_confirm.search(operator))
        self.assertEqual(guest.count("await window.d4yConfirm"), 2)

    def test_script_loads_before_the_code_that_uses_or_submits_forms(self):
        pairs = {
            "templates/admin/base.html": "vendor/turbo.min.js",
            "templates/operator/base.html": "operator.js",
            "templates/public/category.html": "guest.js",
            "templates/public/share.html": "guest.js",
            "templates/public/profile.html": "profile.js",
        }
        for relative, later_script in pairs.items():
            with self.subTest(template=relative):
                source = (APP / relative).read_text("utf-8")
                self.assertIn("confirm.js", source)
                self.assertLess(source.index("confirm.js"), source.index(later_script))

        review = (APP / "templates/public/profile_review.html").read_text("utf-8")
        self.assertIn("confirm.js", review)


if __name__ == "__main__":
    unittest.main(verbosity=2)
