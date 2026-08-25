#!/usr/bin/env python3
"""Allowlist возвратов из редакторов подборок и событий."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode


APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="date4you-return-import-")
os.environ.update({
    "DATA_DIR": _IMPORT_DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "editor-return.test",
    "SECRET_KEY": "editor-return-test-secret",
    "TG_BOT_TOKEN": "",
})

import admin_routes  # noqa: E402


class EditorReturnSafetyTests(unittest.TestCase):
    def test_operator_lists_keep_only_supported_filters(self):
        category_target = "/operator/categories?" + urlencode({
            "q": "лето",
            "page": "3",
        })
        self.assertEqual(
            admin_routes._safe_category_editor_return(
                "/operator/categories?q=лето&page=03&unexpected=1",
                42,
                allow_operator=True,
            ),
            category_target,
        )

        date_target = "/operator/dates?" + urlencode({
            "flt": "reported",
            "q": "ужин",
            "page": "4",
        })
        self.assertEqual(
            admin_routes._safe_date_editor_return(
                "/operator/dates?flt=reported&q=ужин&page=4&unexpected=1",
                42,
                allow_operator=True,
            ),
            date_target,
        )

    def test_owner_card_is_allowed_only_for_matching_operator_owner(self):
        owner_card = "/operator/users/42"
        self.assertEqual(
            admin_routes._safe_category_editor_return(
                owner_card, 42, allow_operator=True,
            ),
            owner_card,
        )
        self.assertEqual(
            admin_routes._safe_date_editor_return(
                owner_card, 42, allow_operator=True,
            ),
            owner_card,
        )
        self.assertEqual(
            admin_routes._safe_category_editor_return(
                "/operator/users/43", 42, allow_operator=True,
            ),
            "/admin/categories",
        )
        self.assertEqual(
            admin_routes._safe_date_editor_return(
                "/operator/users/43", 42, allow_operator=True,
            ),
            "/admin/dates",
        )

    def test_external_and_protocol_relative_urls_are_rejected(self):
        for unsafe in (
            "https://evil.example/operator/categories?page=2",
            "//evil.example/operator/dates?flt=reported",
        ):
            with self.subTest(unsafe=unsafe, editor="category"):
                self.assertEqual(
                    admin_routes._safe_category_editor_return(
                        unsafe, 42, allow_operator=True,
                    ),
                    "/admin/categories",
                )
            with self.subTest(unsafe=unsafe, editor="date"):
                self.assertEqual(
                    admin_routes._safe_date_editor_return(
                        unsafe, 42, allow_operator=True,
                    ),
                    "/admin/dates",
                )

    def test_regular_user_cannot_return_to_operator_surface(self):
        self.assertEqual(
            admin_routes._safe_category_editor_return(
                "/operator/categories?q=лето&page=3", 42,
            ),
            "/admin/categories",
        )
        self.assertEqual(
            admin_routes._safe_date_editor_return(
                "/operator/dates?flt=reported&page=4", 42,
            ),
            "/admin/dates",
        )

    def test_date_editor_preserves_safe_operator_return_through_category(self):
        nested = "/operator/categories?q=лето&page=2"
        raw = "/admin/categories/7?" + urlencode({"return_to": nested}) \
              + "#categoryDates"
        self.assertEqual(
            admin_routes._safe_date_editor_return(
                raw, 42, allow_operator=True,
            ),
            "/admin/categories/7?" + urlencode({
                "return_to": "/operator/categories?" + urlencode({
                    "q": "лето", "page": "2",
                }),
            })
            + "#categoryDates",
        )
        self.assertEqual(
            admin_routes._safe_date_editor_return(raw, 42),
            "/admin/categories/7#categoryDates",
        )

    def test_owner_can_return_to_local_public_page_but_not_cross_kind(self):
        self.assertEqual(
            admin_routes._safe_category_editor_return("/c/category_token-1", 42),
            "/c/category_token-1",
        )
        self.assertEqual(
            admin_routes._safe_date_editor_return("/d/date_token-1", 42),
            "/d/date_token-1",
        )
        self.assertEqual(
            admin_routes._safe_category_editor_return("/d/date_token-1", 42),
            "/admin/categories",
        )
        self.assertEqual(
            admin_routes._safe_date_editor_return("/c/category_token-1", 42),
            "/admin/dates",
        )


if __name__ == "__main__":
    unittest.main()
