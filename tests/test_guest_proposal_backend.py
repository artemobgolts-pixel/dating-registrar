#!/usr/bin/env python3
"""Изолированные backend-контракты гостевого proposal media-протокола."""

from __future__ import annotations

import ast
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROUTES = ROOT / "app" / "public_routes.py"


def load_function(name: str, namespace: dict | None = None):
    """Загружает чистую функцию без импорта FastAPI-приложения и его env."""
    tree = ast.parse(PUBLIC_ROUTES.read_text("utf-8"), filename=str(PUBLIC_ROUTES))
    node = next(
        item for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = dict(namespace or {})
    exec(compile(module, str(PUBLIC_ROUTES), "exec"), namespace)
    return namespace[name]


class GuestProposalBackendContractTests(unittest.TestCase):
    def test_order_accepts_legacy_and_new_tokens(self):
        ordered_media_refs = load_function("ordered_media_refs")

        self.assertEqual(
            ordered_media_refs("22", [11, 22], 1),
            [("saved", 22), ("saved", 11), ("new", 0)],
        )
        self.assertEqual(
            ordered_media_refs("s22,n1,s11,n0", [11, 22], 2),
            [("saved", 22), ("new", 1), ("saved", 11), ("new", 0)],
        )

    def test_order_rejects_foreign_ids_bad_indexes_and_duplicates(self):
        ordered_media_refs = load_function("ordered_media_refs")

        self.assertEqual(
            ordered_media_refs("s999,n8,s22,s22,n1,n1", [11, 22], 2),
            [("saved", 22), ("new", 1), ("saved", 11), ("new", 0)],
        )

    def test_order_ignores_unicode_digits_and_pathologically_long_numbers(self):
        ordered_media_refs = load_function("ordered_media_refs")
        huge = "9" * 5000

        self.assertEqual(
            ordered_media_refs(
                f"²,s²,n²,{huge},s{huge},n{huge},s22,n1",
                [11, 22],
                2,
            ),
            [("saved", 22), ("new", 1), ("saved", 11), ("new", 0)],
        )

    def test_proposal_actions_close_at_deadline_before_background_tick(self):
        voting = SimpleNamespace(
            CategoryState=object,
            STATUS_UNCONFIGURED="unconfigured",
            STATUS_OPEN="open",
        )
        proposal_changes_open = load_function("proposal_changes_open", {
            "voting": voting,
            "datetime": datetime,
            "MSK": ZoneInfo("Europe/Moscow"),
            "now_naive": lambda: datetime(2030, 1, 1, 12, 0),
        })

        self.assertTrue(proposal_changes_open(
            SimpleNamespace(status="unconfigured", voting_deadline=None),
            now=datetime(2030, 1, 1, 12, 0),
        ))
        state = SimpleNamespace(status="open", voting_deadline="2030-01-01T12:01")
        self.assertTrue(proposal_changes_open(state, now=datetime(2030, 1, 1, 12, 0)))
        self.assertFalse(proposal_changes_open(state, now=datetime(2030, 1, 1, 12, 1)))
        self.assertFalse(proposal_changes_open(
            SimpleNamespace(status="resolved", voting_deadline="2030-01-01T13:00"),
            now=datetime(2030, 1, 1, 12, 0),
        ))

    def test_routes_use_batch_video_limit_and_deadline_aware_ui(self):
        source = PUBLIC_ROUTES.read_text("utf-8")

        self.assertIn('videos: list[UploadFile] = File(default=[], alias="videos")', source)
        self.assertIn("len(remaining_vids) + len(new_video_files) > images.MAX_VIDEOS", source)
        self.assertIn("images.save_videos_batch(new_video_files)", source)
        self.assertIn("proposals_editable = proposal_changes_open(vote_state)", source)
        self.assertIn('d["editable"] = (proposals_editable', source)

        # Недоверенный order-протокол разбирается до любых записей upload-файлов:
        # даже будущая ошибка парсера не сможет оставить файлы-сироты.
        edit_source = source.split("def public_propose_edit(", 1)[1]
        self.assertLess(
            edit_source.index("photo_order = ordered_media_refs("),
            edit_source.index("saved = images.save_batch(new_files)"),
        )
        self.assertLess(
            edit_source.index("video_order = ordered_media_refs("),
            edit_source.index("saved_videos = images.save_videos_batch(new_video_files)"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
