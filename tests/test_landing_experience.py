#!/usr/bin/env python3
"""Контракты нового интерактивного лендинга date4you.

Проверки намеренно опираются на семантику, текст и ``data-*`` hooks, а не на
декоративные CSS-классы: внешний вид scroll-сцены можно полировать, не меняя
её продуктовый и accessibility-контракт.
"""

from __future__ import annotations

import html
import re
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
HOME_PATH = APP / "templates/public/home.html"
CARD_PATH = APP / "templates/public/_landing_demo_card.html"
CSS_PATH = APP / "static/landing.css"
STORY_PATH = APP / "static/landing-story.js"


def _plain_text(source: str) -> str:
    """Возвращает текст HTML с нормализованными пробелами."""

    without_tags = re.sub(r"<[^>]+>", " ", source)
    return " ".join(html.unescape(without_tags).split())


def _opening_tag(source: str, marker: str, *, tag: str = r"[a-z][\w-]*") -> str:
    match = re.search(
        rf"<{tag}\b(?=[^>]*\b{re.escape(marker)}(?:\b|=))[^>]*>",
        source,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"Не найден открывающий тег с {marker}")
    return match.group(0)


def _attribute(tag: str, name: str) -> str | None:
    match = re.search(
        rf"\b{re.escape(name)}(?:\s*=\s*([\"'])(.*?)\1)?",
        tag,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return None
    return "" if match.group(1) is None else match.group(2)


class LandingExperienceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home_source = HOME_PATH.read_text("utf-8")
        cls.card_source = CARD_PATH.read_text("utf-8")
        environment = Environment(
            loader=FileSystemLoader(APP / "templates"),
            autoescape=select_autoescape(("html", "xml")),
        )
        environment.globals["asset"] = lambda name: f"/static/{name}"
        cls.home = environment.get_template("public/home.html").render(
            BASE_URL="https://date4you.test",
            description="Тестовое описание",
            csp_nonce="test-nonce",
            structured_data={},
            support=None,
        )
        cls.css = CSS_PATH.read_text("utf-8")

    def card(self, skin: str) -> str:
        match = re.search(
            r'<article\b(?=[^>]*\bdata-demo-card(?:\b|=))'
            rf'(?=[^>]*\bdata-demo-skin="{re.escape(skin)}")[^>]*>'
            r".*?</article>",
            self.home,
            re.DOTALL,
        )
        self.assertIsNotNone(match, f"Нет полноценной demo-card для {skin}")
        return match.group(0)

    def test_template_is_valid_jinja_and_keeps_page_landmarks(self):
        Environment().parse(self.home_source)
        Environment().parse(self.card_source)
        self.assertEqual(
            len(re.findall(r'<header\b[^>]*class="[^"]*\bsite-header\b', self.home)),
            1,
        )
        self.assertEqual(len(re.findall(r"<main\b", self.home)), 1)
        self.assertEqual(
            len(re.findall(r'<footer\b[^>]*class="[^"]*\bsite-footer\b', self.home)),
            1,
        )
        self.assertIn('id="main-content"', self.home)
        self.assertIn('<a class="skip-link" href="#main-content">', self.home)
        self.assertNotRegex(self.home, r'<[^>]+\sstyle="')

    def test_hero_is_minimal_and_old_questionnaire_copy_is_gone(self):
        text = _plain_text(self.home)
        hero = re.search(
            r'<section\b[^>]*class="[^"]*\bhero\b[^"]*"[^>]*>.*?</section>',
            self.home,
            re.DOTALL,
        )
        self.assertIsNotNone(hero)
        hero_text = _plain_text(hero.group(0))
        self.assertIn("date4you", hero_text)
        self.assertIn("создан, чтобы встречаться", hero_text)
        self.assertIn("Создать подборку", hero_text)
        self.assertIn("Посмотреть в действии", hero_text)

        for stale_copy in (
            "Удобно на телефоне",
            "Несколько вариантов",
            "Три простых шага",
            "Понятно с первого взгляда",
            "Встреча начинается с общего выбора",
        ):
            with self.subTest(stale_copy=stale_copy):
                self.assertNotIn(stale_copy, text)

        for removed_media in (
            "landing-video.js",
            "landing-brand-loop.mp4",
            "landing-brand-loop-mobile.mp4",
            "landing-brand-poster.webp",
            "landing-brand-poster-mobile.webp",
        ):
            with self.subTest(removed_media=removed_media):
                self.assertNotIn(removed_media, self.home)
        self.assertNotRegex(self.home, r"<video\b")

    def test_header_footer_and_navigation_contract_is_preserved(self):
        header = re.search(r"<header\b.*?</header>", self.home, re.DOTALL)
        footer = re.search(r"<footer\b.*?</footer>", self.home, re.DOTALL)
        self.assertIsNotNone(header)
        self.assertIsNotNone(footer)

        header_source = header.group(0)
        footer_source = footer.group(0)
        self.assertIn('class="site-header"', header_source)
        self.assertIn('class="site-header__inner"', header_source)
        self.assertIn('class="wordmark"', header_source)
        self.assertIn('aria-label="Разделы страницы"', header_source)
        self.assertIn('href="#how-it-works"', header_source)
        self.assertIn('href="#possibilities"', header_source)
        self.assertIn('href="/about?return_to=%2F"', header_source)
        self.assertIn('data-theme-toggle', header_source)
        self.assertIn('aria-label="Включить тёмную тему"', header_source)
        self.assertIn('href="/login"', header_source)

        self.assertIn('class="site-footer"', footer_source)
        self.assertIn('class="site-footer__brand"', footer_source)
        self.assertIn('aria-label="Справочная навигация"', footer_source)
        self.assertIn('href="/about?return_to=%2F"', footer_source)
        self.assertIn('href="/terms?return_to=%2F"', footer_source)
        self.assertIn('href="/privacy?return_to=%2F"', footer_source)
        self.assertIn("создан, чтобы встречаться", _plain_text(footer_source))
        self.assertIn("© date4you", _plain_text(footer_source))

    def test_story_keeps_public_anchors_and_has_all_six_scenes(self):
        for anchor in ("product-preview", "how-it-works", "possibilities"):
            with self.subTest(anchor=anchor):
                self.assertEqual(self.home.count(f'id="{anchor}"'), 1)
                self.assertIn(f'href="#{anchor}"', self.home)

        self.assertEqual(self.home.count("data-landing-story"), 1)
        self.assertEqual(self.home.count("data-story-stage"), 1)
        self.assertEqual(
            re.findall(r'data-story-step[^>]*\bdata-scene="([^"]+)"', self.home),
            ["build", "media", "details", "people", "choice", "feed"],
        )

    def test_both_skins_have_complete_event_cards_and_exact_vote_states(self):
        self.assertEqual(self.home.count("data-demo-card"), 2)
        cases = {
            "friends": {
                "copy": (
                    "Антикафе",
                    "Суббота",
                    "18:00",
                    "Антикафе на Невском",
                    "Настолки, чай и три часа без спешки",
                    "Можно принести свою игру?",
                    "Конечно",
                    "Я плачу",
                ),
                "before": "3",
                "after": "4",
                "total": "5",
            },
            "romantic": {
                "copy": (
                    "Кинопоказ на крыше",
                    "Пятница",
                    "20:30",
                    "Крыша в центре Москвы",
                    "Выберем фильм на месте. Пледы и попкорн уже будут",
                    "А если будет дождь?",
                    "Перенесём вечер домой",
                    "Я плачу",
                ),
                "before": "0",
                "after": "1",
                "total": "1",
            },
        }

        for skin, expected in cases.items():
            with self.subTest(skin=skin):
                card = self.card(skin)
                card_text = _plain_text(card)
                for copy in expected["copy"]:
                    self.assertIn(copy, card_text)

                self.assertGreaterEqual(card.count("data-demo-slide"), 3)
                self.assertRegex(card, r"<time\b[^>]*\bdatetime=")
                self.assertRegex(card, r'<a\b[^>]*href="https?://')

                vote = _opening_tag(card, "data-demo-vote", tag="button")
                self.assertEqual(_attribute(vote, "type"), "button")
                self.assertEqual(
                    _attribute(vote, "data-vote-before"), expected["before"])
                self.assertEqual(
                    _attribute(vote, "data-vote-after"), expected["after"])
                self.assertEqual(
                    _attribute(vote, "data-vote-total"), expected["total"])
                self.assertRegex(
                    card,
                    rf'data-demo-count-current[^>]*>\s*{expected["before"]}\s*<',
                )

    def test_gallery_vote_and_question_are_accessible_without_dragging(self):
        for skin in ("friends", "romantic"):
            with self.subTest(skin=skin):
                card = self.card(skin)
                gallery = _opening_tag(card, "data-demo-gallery")
                self.assertEqual(_attribute(gallery, "role"), "region")
                self.assertEqual(_attribute(gallery, "tabindex"), "0")
                self.assertEqual(_attribute(gallery, "aria-roledescription"), "карусель")
                self.assertTrue(_attribute(gallery, "aria-label"))

                for marker in ("data-demo-gallery-prev", "data-demo-gallery-next"):
                    control = _opening_tag(card, marker, tag="button")
                    self.assertEqual(_attribute(control, "type"), "button")
                    self.assertTrue(_attribute(control, "aria-label"))

                images = re.findall(r"<img\b[^>]*>", card, re.DOTALL)
                self.assertGreaterEqual(len(images), 3)
                for image in images:
                    self.assertTrue(_attribute(image, "alt"))

                progress_candidates = re.findall(
                    r'<[a-z][\w-]*\b(?=[^>]*\brole="progressbar")[^>]*>',
                    card,
                    re.IGNORECASE | re.DOTALL,
                )
                self.assertEqual(len(progress_candidates), 1)
                progress = progress_candidates[0]
                for attribute in (
                    "aria-label",
                    "aria-valuemin",
                    "aria-valuemax",
                    "aria-valuenow",
                    "aria-valuetext",
                ):
                    self.assertIsNotNone(_attribute(progress, attribute), attribute)

                live = _opening_tag(card, "data-demo-vote-status")
                self.assertEqual(_attribute(live, "aria-live"), "polite")
                self.assertEqual(_attribute(live, "aria-atomic"), "true")

                toggle = _opening_tag(card, "data-demo-question-toggle", tag="button")
                self.assertEqual(_attribute(toggle, "type"), "button")
                self.assertEqual(_attribute(toggle, "aria-expanded"), "false")
                panel_id = _attribute(toggle, "aria-controls")
                self.assertTrue(panel_id)
                panel = _opening_tag(card, "data-demo-question-panel", tag="form")
                self.assertEqual(_attribute(panel, "id"), panel_id)
                self.assertIsNotNone(_attribute(panel, "hidden"))
                self.assertRegex(card, r'<label\b[^>]*\bfor="[^"]+"')

    def test_skin_and_light_dark_controls_remain_independent(self):
        html_tag = re.search(r"<html\b[^>]*>", self.home)
        self.assertIsNotNone(html_tag)
        self.assertEqual(_attribute(html_tag.group(0), "data-skin"), "friends")
        self.assertIsNotNone(_attribute(html_tag.group(0), "data-skin-switchable"))
        self.assertIn("asset('theme.js')", self.home_source)

        for skin, pressed in (("friends", "true"), ("romantic", "false")):
            with self.subTest(skin=skin):
                control = re.search(
                    rf'<button\b(?=[^>]*\bdata-skin-set="{skin}")[^>]*>',
                    self.home,
                    re.DOTALL,
                )
                self.assertIsNotNone(control)
                self.assertEqual(_attribute(control.group(0), "aria-pressed"), pressed)

        theme = _opening_tag(self.home, "data-theme-toggle", tag="button")
        self.assertTrue(_attribute(theme, "aria-label"))
        romantic = self.card("romantic")
        romantic_tag = romantic.split(">", 1)[0] + ">"
        self.assertIsNotNone(_attribute(romantic_tag, "hidden"))
        self.assertEqual(_attribute(romantic_tag, "aria-hidden"), "true")

    def test_only_three_approved_product_benefits_remain(self):
        text = _plain_text(self.home)
        benefits = (
            (
                "Лента событий",
                "Нет идей? В ленте событий можно найти идеи пользователей и "
                "сохранить понравившиеся события себе",
            ),
            (
                "Профили",
                "Планы, желания и впечатления — в одном профиле. В нём видны "
                "публичные события, раздел «Хочу сходить» и отзывы о прошедших встречах",
            ),
            (
                "Тонкая настройка",
                "Дизайн, оформление, доступ, голосование и права гостей",
            ),
        )
        for title, description in benefits:
            with self.subTest(title=title):
                self.assertIn(title, text)
                self.assertIn(description, text)

        self.assertNotIn('class="section privacy-card"', self.home)
        self.assertNotIn('id="privacy-title"', self.home)
        self.assertIn("Создайте следующую встречу", text)

    def test_background_and_story_scripts_are_self_hosted(self):
        for asset in (
            "ink.js",
            "ink-runtime.js",
            "ink-worker.js",
            "ink-static-friends-light.webp",
            "ink-static-friends-light-portrait.webp",
            "ink-static-friends-dark.webp",
            "ink-static-romantic-light.webp",
            "ink-static-romantic-dark.webp",
            "landing-story.js",
        ):
            with self.subTest(asset=asset):
                self.assertIn(f"asset('{asset}')", self.home_source)
        self.assertIn('{% include "public/_bg.html" %}', self.home_source)
        background = (APP / "templates/public/_bg.html").read_text("utf-8")
        self.assertIn('class="bg-smoke"', background)
        self.assertNotRegex(self.home, r'<script\b[^>]*\bsrc="https?://')

        self.assertTrue(STORY_PATH.is_file())
        story = STORY_PATH.read_text("utf-8")
        self.assertNotRegex(story, r"https?://")
        self.assertRegex(
            story,
            r"matchMedia\([\"']\(prefers-reduced-motion:\s*reduce\)[\"']\)",
        )
        self.assertIn("is-reduced-motion", story)
        self.assertIn("requestAnimationFrame", story)
        for event_name in ("scroll", "pointerdown", "pointermove", "pointerup"):
            with self.subTest(event_name=event_name):
                self.assertIn(event_name, story)

        self.assertRegex(self.css, r"@media\s*\(prefers-reduced-motion:\s*reduce\)")
        self.assertIn(".is-reduced-motion", self.css)
        self.assertRegex(
            self.css,
            r"\.landing-story__stage-wrap\s*\{[^}]*position:\s*sticky",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
