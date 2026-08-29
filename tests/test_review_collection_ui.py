"""Static UI contracts for review editing and the desktop date collection."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def source(relative: str) -> str:
    return (APP / relative).read_text(encoding="utf-8")


def rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", css, re.S)
    if not match:
        raise AssertionError(f"CSS rule not found: {selector}")
    return match.group(1)


class ReviewCollectionUiTests(unittest.TestCase):
    def test_review_wording_uses_otzyv_across_user_surfaces(self):
        user_surfaces = (
            "templates/admin/questions.html",
            "templates/public/_profile_sections.html",
            "templates/public/_profile_review_widget.html",
            "templates/public/profile_review.html",
            "templates/public/share.html",
            "static/profile.js",
            "static/public_review.js",
            "admin_routes.py",
            "public_routes.py",
            "social_events.py",
        )
        legacy_term = re.compile(
            r"\b(?:Обзор|обзор)(?:ы|а|ов|е|ом|у)?\b",
        )
        for relative in user_surfaces:
            with self.subTest(relative=relative):
                self.assertNotRegex(source(relative), legacy_term)

        profile_sections = source("templates/public/_profile_sections.html")
        self.assertIn("Отзывы <b>{{ review_total }}</b>", profile_sections)
        self.assertIn("Пока нет отзывов.", profile_sections)
        self.assertIn("Отзыв о событии", source("templates/public/_profile_review_widget.html"))
        self.assertIn("Оставить отзыв", source("templates/admin/questions.html"))

    def test_review_editor_is_star_only_and_saves_in_place(self):
        widget = source("templates/public/_profile_review_widget.html")
        share = source("templates/public/share.html")
        profile_css = source("static/profile.css")
        profile_js = source("static/profile.js")

        self.assertIn("{% for score in range(5, 0, -1) %}", widget)
        self.assertIn("{{ 'checked' if review['rating'] == score else '' }}", widget)
        self.assertIn('id="profile-rating-{{ review[\'review_id\'] }}-{{ score }}"', widget)
        self.assertIn('<span class="sr-only">{{ score }} из 5</span>', widget)
        self.assertIn('class="profile-review-actions', widget)
        self.assertIn(">Сохранить отзыв</button>", widget)
        self.assertIn(">Поделиться</button>", widget)
        self.assertIn("{% if not is_me %}", widget)
        self.assertNotIn("{{ score }} ★", widget)
        self.assertIn(
            "{{ 'checked' if my_review and my_review['rating'] == score else '' }}",
            share,
        )

        radios = rule(profile_css, ".profile-rating-stars input")
        self.assertIn("position: absolute", radios)
        self.assertIn("width: 1px", radios)
        self.assertIn("font-size: 40px", rule(profile_css, ".profile-rating-stars label"))
        actions = rule(profile_css, ".profile-review-actions")
        self.assertIn("repeat(2, minmax(0, 1fr))", actions)
        self.assertIn("min-height: 52px", rule(profile_css, ".profile-review-actions .btn"))

        self.assertIn('headers: {\n          "Accept": "application/json"', profile_js)
        self.assertIn('event.target.closest(".profile-review-editor")', profile_js)
        self.assertIn("event.preventDefault();\n      saveReview(form);", profile_js)
        self.assertIn('document.getElementById("review-" + form.dataset.reviewId)', profile_js)
        self.assertIn('button.classList.contains("profile-review-share")', profile_js)
        self.assertIn('button.textContent = "Ссылка скопирована ✓"', profile_js)

    def test_profile_counts_avatar_and_editor_back_are_aligned(self):
        profile_css = source("static/profile.css")
        admin_css = source("static/admin.css")
        date_form = source("templates/admin/date_form.html")
        profile = source("templates/admin/profile.html")

        profile_tab = rule(profile_css, ".profile-tabs a")
        self.assertIn("display: inline-flex", profile_tab)
        self.assertIn("align-items: center", profile_tab)
        self.assertIn("line-height: 1", rule(profile_css, ".profile-tabs b"))
        self.assertIn("display: inline-flex", rule(admin_css, ".tabs a"))

        avatar = rule(admin_css, ".avatar-delete")
        self.assertIn("width: 34px", avatar)
        self.assertIn("height: 34px", avatar)
        self.assertIn("top: 2px", avatar)
        self.assertIn("right: 2px", avatar)
        self.assertIn('class="btn ghost editor-back date-editor-back"', date_form)
        self.assertRegex(
            admin_css,
            r"\.editor-back\.date-editor-back,\s*"
            r"\.editor-back\.category-new-back \{ top: 112px; \}",
        )
        self.assertNotIn("Так твои события, планы и отзывы собраны в профиле.", profile)

    def test_event_cards_use_desktop_grid_and_full_width_mobile_cards(self):
        dates = source("templates/admin/dates.html")
        profile_sections = source("templates/public/_profile_sections.html")
        admin_css = source("static/admin.css")
        profile_css = source("static/profile.css")
        admin_js = source("static/admin.js")

        self.assertIn('action="/admin/dates/bulk"', dates)
        self.assertIn('name="date_ids"', dates)
        for action in ("archive", "restore", "make_public", "make_private", "delete"):
            self.assertIn(f'value="{action}"', dates)
        self.assertIn("data-bulk-all", dates)
        self.assertIn("data-bulk-count", dates)
        self.assertIn("Удалить выбранные события безвозвратно?", dates)
        self.assertIn(
            'data-copy="{{ BASE_URL }}/d/{{ r[\'share_token\'] }}">Поделиться</button>',
            dates,
        )

        self.assertIn(
            "grid-template-columns: repeat(auto-fill, minmax(320px, 1fr))",
            rule(admin_css, ".grid"),
        )
        self.assertIn(
            ".dlist {\n"
            "    display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));",
            admin_css,
        )
        self.assertIn(
            ".drow {\n"
            "    display: flex; flex-direction: column; align-items: stretch; gap: 0;",
            admin_css,
        )
        self.assertIn(
            ".drow-cover {\n"
            "    width: 100%; height: auto; aspect-ratio: 16 / 9;",
            admin_css,
        )
        self.assertIn(
            ".drow:not(:has(.drow-cover)) .drow-main {\n"
            "    min-height: 62px; padding-left: 68px;",
            admin_css,
        )
        self.assertIn(".drow:has(.menu.open)", admin_css)
        self.assertIn(
            ".grid { grid-template-columns: minmax(0, 1fr); gap: 14px; }",
            admin_css,
        )
        self.assertNotIn(
            ".grid { grid-template-columns: repeat(2, minmax(0, 1fr))",
            admin_css,
        )
        self.assertIn(
            ".grid .dcard .ph { height: auto; aspect-ratio: 16 / 9; }",
            admin_css,
        )
        self.assertIn(".cfeed { grid-template-columns: minmax(0, 1fr); }", admin_css)
        self.assertIn(".dates-bulk-form, .drow-select { display: none; }", admin_css)
        self.assertIn("profile-tab-{{ tab }}", profile_sections)
        self.assertIn(
            "sizes=\"(min-width: 980px) 360px, (min-width: 641px) 44vw, calc(100vw - 28px)\"",
            profile_sections,
        )
        self.assertIn(".pub-grid { grid-template-columns: 1fr; }", profile_css)
        self.assertIn(
            ".profile-public-page .pub-grid:not(.review-grid) {\n"
            "  grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px;",
            profile_css,
        )
        self.assertIn("@media (min-width: 980px)", profile_css)
        self.assertIn(
            "grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px;",
            profile_css,
        )
        self.assertIn(
            ".profile-public-page .review-grid {\n"
            "    grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px;",
            profile_css,
        )
        self.assertIn(
            ".profile-public-page .profile-event-dlg:has(.cwid.has-media)",
            profile_css,
        )
        self.assertIn("width: min(680px, calc(100vw - 56px))", profile_css)
        self.assertIn(
            "display: flex; flex-direction: column; min-height: 0;",
            profile_css,
        )
        self.assertNotIn(
            "grid-template-columns: minmax(0, 1.45fr) minmax(360px, .85fr)",
            profile_css,
        )
        self.assertIn(
            ".profile-public-page .pub-dates.profile-tab-events .pub-grid {\n"
            "    grid-template-columns: minmax(0, 1fr); gap: 14px;",
            profile_css,
        )
        self.assertNotIn(".pub-dates.profile-tab-want .pub-grid", profile_css)
        self.assertIn('document.getElementById("datesBulkForm")', admin_js)
        self.assertIn("bulkAll.indeterminate", admin_js)
        self.assertIn('row.classList.toggle("is-selected", item.checked)', admin_js)
        self.assertIn('var dlist = document.querySelector(".dlist")', admin_js)
        self.assertNotIn('sessionStorage.getItem("forcedCards")', admin_js)
        self.assertNotIn('sessionStorage.setItem("forcedCards"', admin_js)

        bulk_form = dates.split('<form class="dates-bulk-form"', 1)[1].split(
            "</form>", 1,
        )[0]
        self.assertNotIn('class="btn small', bulk_form)
        self.assertGreaterEqual(bulk_form.count("bulk-action"), 4)
        self.assertGreaterEqual(bulk_form.count(" disabled"), 4)
        self.assertIn("bulk-danger", bulk_form)

        checkbox = rule(admin_css, ".dates-bulk-all input, .drow-check")
        self.assertIn("appearance: none", checkbox)
        self.assertIn("width: 26px", checkbox)
        self.assertIn("height: 26px", checkbox)
        self.assertIn("var(--accent)", checkbox)
        self.assertIn(".dates-bulk-all input:checked, .drow-check:checked", admin_css)
        self.assertIn(".dates-bulk-all input:indeterminate", admin_css)
        bulk_buttons = rule(admin_css, ".dates-bulk-actions .btn")
        self.assertIn("min-height: 42px", bulk_buttons)
        self.assertIn("border-radius: 12px", bulk_buttons)

    def test_public_profile_uses_full_application_visual_shell(self):
        template = source("templates/public/profile.html")
        profile_css = source("static/profile.css")

        self.assertIn('class="profile-public-page"', template)
        self.assertIn('{% include "public/_bg.html" %}', template)
        self.assertIn("asset('ink.js')", template)
        self.assertIn('class="bg-gather"', template)
        self.assertIn('class="bg-hearts"', template)
        self.assertIn('class="profile-public-actions"', template)
        self.assertIn('class="profile-public-shell"', template)
        self.assertIn("{% if me %}", template)
        self.assertIn("/login?next={{ profile_return_url | urlencode }}", template)
        self.assertNotIn("<style>", template)
        self.assertIn("body.profile-public-page", profile_css)
        self.assertIn(
            'html[data-skin="friends"] body.profile-public-page', profile_css,
        )
        self.assertIn(
            'html[data-theme="dark"][data-skin="friends"] body.profile-public-page',
            profile_css,
        )
        self.assertIn("-webkit-backdrop-filter: blur(18px)", profile_css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
