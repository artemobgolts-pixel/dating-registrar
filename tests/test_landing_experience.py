#!/usr/bin/env python3
"""Устойчивые продуктовые контракты интерактивного лендинга date4you.

Проверки привязаны к семантике, доступным названиям и ``data-*`` hooks. Они не
фиксируют декоративные классы или точные значения таймлайна: композицию и
motion-полировку можно менять, не переписывая продуктовый контракт.
"""

from __future__ import annotations

import html
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
STATIC = APP / "static"
HOME_PATH = APP / "templates/public/home.html"
CARD_PATH = APP / "templates/public/_landing_demo_card.html"
CSS_PATH = STATIC / "landing.css"
STORY_PATH = STATIC / "landing-story.js"


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


def _tags(source: str) -> list[str]:
    return re.findall(r"<[a-z][\w-]*\b[^>]*>", source, re.IGNORECASE | re.DOTALL)


def _css_rules(source: str) -> list[tuple[str, dict[str, str]]]:
    """Минимальный parser плоских CSS rules, достаточный для контрактов."""

    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    rules: list[tuple[str, dict[str, str]]] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", source, re.DOTALL):
        selectors = match.group(1).strip()
        declarations: dict[str, str] = {}
        for declaration in match.group(2).split(";"):
            if ":" not in declaration:
                continue
            name, value = declaration.split(":", 1)
            declarations[name.strip().lower()] = value.strip().lower()
        for selector in selectors.split(","):
            rules.append((selector.strip(), declarations))
    return rules


def _merged_css_declarations(source: str, predicate) -> dict[str, str]:
    """Возвращает каскад деклараций подходящих rules в порядке файла."""

    merged: dict[str, str] = {}
    for selector, declarations in _css_rules(source):
        if predicate(selector):
            merged.update(declarations)
    return merged


def _static_path(src: str) -> Path | None:
    path = urlsplit(src).path
    prefix = "/static/"
    if not path.startswith(prefix):
        return None
    candidate = (STATIC / path.removeprefix(prefix)).resolve()
    try:
        candidate.relative_to(STATIC.resolve())
    except ValueError:
        return None
    return candidate


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
        cls.story = STORY_PATH.read_text("utf-8")

    def demo_card(self) -> str:
        match = re.search(
            r'<article\b(?=[^>]*\bdata-demo-card(?:\b|=))[^>]*>.*?</article>',
            self.home,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "Нет полноценной интерактивной demo-card")
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

    def test_hero_starts_near_header_and_has_a_smaller_statement(self):
        hero = re.search(
            r'<section\b[^>]*class="[^"]*\bhero\b[^"]*"[^>]*>.*?</section>',
            self.home,
            re.DOTALL,
        )
        self.assertIsNotNone(hero)
        hero_text = _plain_text(hero.group(0))
        for copy in (
            "date4you",
            "создан, чтобы встречаться",
            "Создать подборку",
            "Посмотреть в действии",
        ):
            self.assertIn(copy, hero_text)

        hero_rules = [
            declarations
            for selector, declarations in _css_rules(self.css)
            if selector.endswith(".hero.hero--brand")
        ]
        self.assertTrue(hero_rules, "Не найдены стили первого экрана")
        hero_declarations: dict[str, str] = {}
        for rule in hero_rules:
            hero_declarations.update(rule)
        self.assertNotEqual(hero_declarations.get("min-height"), "100svh")
        self.assertNotEqual(hero_declarations.get("height"), "100svh")
        self.assertNotEqual(hero_declarations.get("align-items"), "flex-end")

        statement_declarations: dict[str, str] = {}
        for selector, declarations in _css_rules(self.css):
            if selector.endswith(".hero__statement"):
                statement_declarations.update(declarations)
        statement_sizes = [
            int(value)
            for value in re.findall(
                r"(?<![\w.-])(\d+)px\b",
                statement_declarations.get("font-size", ""),
            )
        ]
        self.assertTrue(statement_sizes, "Размер hero statement должен быть задан явно")
        self.assertLessEqual(max(statement_sizes), 64)

    def test_header_drops_about_service_but_keeps_primary_navigation(self):
        header = re.search(r"<header\b.*?</header>", self.home, re.DOTALL)
        footer = re.search(r"<footer\b.*?</footer>", self.home, re.DOTALL)
        self.assertIsNotNone(header)
        self.assertIsNotNone(footer)
        header_source = header.group(0)
        footer_source = footer.group(0)

        self.assertIn('aria-label="Разделы страницы"', header_source)
        self.assertIn('href="#how-it-works"', header_source)
        self.assertIn('href="#possibilities"', header_source)
        self.assertNotIn("О сервисе", _plain_text(header_source))
        self.assertNotIn('href="/about', header_source)
        self.assertIn('data-theme-toggle', header_source)
        self.assertIn('href="/login"', header_source)

        # Структура нижней панели остаётся прежней, но слоган из hero в ней
        # больше не дублируется.
        self.assertIn('class="site-footer__brand"', footer_source)
        self.assertIn('href="/about?return_to=%2F"', footer_source)
        self.assertIn('href="/terms?return_to=%2F"', footer_source)
        self.assertIn('href="/privacy?return_to=%2F"', footer_source)
        self.assertNotIn("создан, чтобы встречаться", _plain_text(footer_source))
        hero = re.search(r"<section\b[^>]*\bhero\b.*?</section>", self.home, re.DOTALL)
        self.assertIsNotNone(hero)
        self.assertIn("создан, чтобы встречаться", _plain_text(hero.group(0)))

    def test_story_has_one_shell_and_the_agreed_six_phase_arc(self):
        self.assertEqual(self.home.count("data-landing-story"), 1)
        self.assertEqual(self.home.count("data-story-stage"), 1)
        self.assertEqual(self.home.count("data-demo-card"), 1)
        self.assertNotIn("data-story-feed", self.home)

        card_tag = _opening_tag(self.home, "data-demo-card", tag="article")
        self.assertIsNone(_attribute(card_tag, "hidden"))
        self.assertNotEqual(_attribute(card_tag, "aria-hidden"), "true")

        aliases = {
            "photo": {"photo", "image", "media"},
            "surface": {"surface", "shell", "glass", "card"},
            "essentials": {"essentials", "core", "meta"},
            "details": {"details", "content"},
            "people": {"people", "participants", "voting"},
            "float": {"float", "hover", "assembled"},
        }
        reverse_aliases = {
            alias: canonical
            for canonical, variants in aliases.items()
            for alias in variants
        }
        phases: list[str] = []
        for tag in _tags(self.home):
            phase = _attribute(tag, "data-story-phase")
            if phase is None and _attribute(tag, "data-story-step") is not None:
                phase = _attribute(tag, "data-scene")
            if phase is None:
                continue
            normalized = reverse_aliases.get(phase.strip().lower(), phase.strip().lower())
            if not phases or phases[-1] != normalized:
                phases.append(normalized)
        self.assertEqual(
            phases,
            [
                "photo",
                "surface",
                "essentials",
                "details",
                "people",
                "float",
            ],
        )
        self.assertEqual(self.home.count("data-story-step"), 6)
        self.assertRegex(self.home, r"<span>/0?6</span>")

        step_sources = re.findall(
            r'<li\b(?=[^>]*\bdata-story-step(?:\b|=))[^>]*>(.*?)</li>',
            self.home,
            re.DOTALL,
        )
        self.assertEqual(len(step_sources), 6)
        self.assertEqual(
            [
                _plain_text(re.search(r"<h3\b[^>]*>(.*?)</h3>", step, re.DOTALL).group(1))
                for step in step_sources
            ],
            [
                "Сначала — фотография",
                "Вокруг появляется карточка",
                "Название, дата и место",
                "Все детали внутри",
                "Видно, кто выбирает",
                "Карточка готова",
            ],
        )
        for step in step_sources:
            self.assertNotRegex(step, r"<p\b", "У шага не должно быть нижнего описания")
        for removed_phase in ("focus", "swipe", "interactive", "return"):
            self.assertNotRegex(
                self.story,
                rf"\.addLabel\(\s*[\"']{removed_phase}[\"']",
            )
        final_phase = self.story.split('.addLabel("float"', 1)
        self.assertEqual(len(final_phase), 2, "Не найден финальный этап float")
        self.assertIn("scales().focus", final_phase[1])
        self.assertIn("galleryController.setAutoProgress", final_phase[1])

    def test_feed_show_all_opens_login_and_preview_uses_real_feed_anatomy(self):
        feed = re.search(
            r'<article\b[^>]*class="[^"]*\bpossibility-story--feed\b[^"]*"[^>]*>'
            r"(.*?)(?=<article\b[^>]*class=\"[^\"]*\bpossibility-story--profile\b)",
            self.home,
            re.DOTALL,
        )
        self.assertIsNotNone(feed)
        source = feed.group(1)
        show_all = re.search(
            r'<a\b[^>]*>(?:\s|<[^>]+>)*Смотреть все(?:\s|<[^>]+>)*</a>',
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(show_all, "«Смотреть все» должно быть ссылкой")
        show_all_tag = show_all.group(0).split(">", 1)[0] + ">"
        self.assertEqual(_attribute(show_all_tag, "href"), "/login")

        preview = re.search(
            r'<div\b[^>]*class="[^"]*\bfeed-fragment\b[^"]*"[^>]*>',
            source,
        )
        self.assertIsNotNone(preview)
        self.assertNotEqual(_attribute(preview.group(0), "role"), "img")

        cards = re.findall(
            r'<article\b(?=[^>]*\bdata-feed-preview-card(?:\b|=))[^>]*>'
            r"(.*?)</article>",
            source,
            re.DOTALL,
        )
        self.assertGreaterEqual(len(cards), 2)
        for card in cards:
            with self.subTest(card=_plain_text(card)[:40]):
                self.assertRegex(card, r"<img\b")
                self.assertRegex(card, r"<h[1-6]\b")
                self.assertIn("data-feed-preview-owner", card)
                self.assertIn("data-feed-preview-meta", card)
                self.assertIn("data-feed-preview-actions", card)
                text = _plain_text(card)
                self.assertIn("Добавить", text)
                self.assertIn("Поделиться", text)

    def test_classic_theme_has_no_extra_orbits_or_dots(self):
        self.assertNotIn('class="bg-gather', self.home)
        self.assertNotIn("landing-skin-decor--friends", self.home)
        self.assertIn("landing-skin-decor--romantic", self.home)

    def test_card_matches_the_real_public_event_anatomy(self):
        card = self.demo_card()
        card_text = _plain_text(card)
        # Romantic copy is stored in data attributes until the skin changes.
        combined = self.home + "\n" + self.story

        for copy in (
            "Антикафе на Невском",
            "Кинопоказ на крыше",
            "Я плачу",
        ):
            self.assertIn(copy, combined)

        forbidden_visible_copy = (
            "5 мест",
            "Для двоих",
            "На карте",
            "Страница площадки",
        )
        for copy in forbidden_visible_copy:
            with self.subTest(copy=copy):
                self.assertNotIn(copy, card_text)
        self.assertNotRegex(
            card,
            r"<h[1-6]\b[^>]*>\s*(?:Участники|Голосование)\s*</h[1-6]>",
        )
        self.assertNotRegex(card, r">\s*Событие\s*<")

        self.assertRegex(card, r"<time\b[^>]*\bdatetime=")
        self.assertRegex(card_text, r"(?i)в календарь")
        place_match = re.search(
            r'<div\b[^>]*class="[^"]*\blanding-demo-card__place\b[^"]*"[^>]*>'
            r'.*?(<a\b[^>]*>)',
            card,
            re.DOTALL,
        )
        self.assertIsNotNone(place_match)
        place = place_match.group(1)
        self.assertRegex(_attribute(place, "href") or "", r"^https?://")
        self.assertEqual(_attribute(place, "target"), "_blank")
        self.assertRegex(_attribute(place, "rel") or "", r"\bnoopener\b")

        for class_name in (
            "landing-demo-card__calendar",
            "landing-demo-card__links",
        ):
            with self.subTest(link=class_name):
                if class_name == "landing-demo-card__calendar":
                    match = re.search(
                        rf'<a\b[^>]*class="[^"]*\b{class_name}\b[^"]*"[^>]*>',
                        card,
                        re.DOTALL,
                    )
                else:
                    match = re.search(
                        rf'<div\b[^>]*class="[^"]*\b{class_name}\b[^"]*"[^>]*>'
                        r".*?(<a\b[^>]*>)",
                        card,
                        re.DOTALL,
                    )
                self.assertIsNotNone(match)
                link = match.group(0) if class_name.endswith("calendar") else match.group(1)
                href = _attribute(link, "href") or ""
                self.assertTrue(href and href != "#")
                self.assertRegex(href, r"^https?://")
                self.assertEqual(_attribute(link, "target"), "_blank")
                self.assertRegex(_attribute(link, "rel") or "", r"\bnoopener\b")

        progress = next(
            tag for tag in _tags(card) if _attribute(tag, "role") == "progressbar"
        )
        for attribute in (
            "aria-label",
            "aria-valuemin",
            "aria-valuemax",
            "aria-valuenow",
            "aria-valuetext",
        ):
            self.assertIsNotNone(_attribute(progress, attribute), attribute)
        self.assertEqual(card.count('data-participant-skin="friends"'), 3)
        self.assertEqual(card.count("data-demo-voter"), 1)

        vote_match = re.search(
            r'<button\b(?=[^>]*\bdata-demo-vote(?:\b|=))[^>]*>.*?</button>',
            card,
            re.DOTALL,
        )
        self.assertIsNotNone(vote_match)
        vote = vote_match.group(0)
        vote_tag = vote.split(">", 1)[0] + ">"
        self.assertEqual(_attribute(vote_tag, "type"), "button")
        self.assertIn("Выбрать", _plain_text(vote))
        card_tag = _opening_tag(card, "data-demo-card", tag="article")
        for skin, before, after, total in (
            ("friends", "3", "4", "5"),
            ("romantic", "0", "1", "1"),
        ):
            with self.subTest(skin=skin):
                self.assertEqual(
                    _attribute(card_tag, f"data-vote-before-{skin}"), before)
                self.assertEqual(
                    _attribute(card_tag, f"data-vote-after-{skin}"), after)
                self.assertEqual(
                    _attribute(card_tag, f"data-vote-total-{skin}"), total)
        ask_match = re.search(
            r'<button\b(?=[^>]*\bdata-demo-question-toggle(?:\b|=))[^>]*>'
            r'.*?</button>',
            card,
            re.DOTALL,
        )
        self.assertIsNotNone(ask_match)
        ask = ask_match.group(0)
        ask_tag = ask.split(">", 1)[0] + ">"
        self.assertEqual(_attribute(ask_tag, "type"), "button")
        self.assertIn("Спросить", _plain_text(ask))

    def test_gallery_vote_and_question_keep_keyboard_access(self):
        card = self.demo_card()
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
            self.assertIsNotNone(_attribute(image, "alt"))

        live = _opening_tag(card, "data-demo-vote-status")
        self.assertEqual(_attribute(live, "aria-live"), "polite")
        self.assertEqual(_attribute(live, "aria-atomic"), "true")

        toggle = _opening_tag(card, "data-demo-question-toggle", tag="button")
        self.assertEqual(_attribute(toggle, "aria-expanded"), "false")
        panel_id = _attribute(toggle, "aria-controls")
        self.assertTrue(panel_id)
        panel = _opening_tag(card, "data-demo-question-panel", tag="form")
        self.assertEqual(_attribute(panel, "id"), panel_id)
        self.assertIsNotNone(_attribute(panel, "hidden"))
        self.assertRegex(card, r'<label\b[^>]*\bfor="[^"]+"')
        submit = _opening_tag(card, "data-demo-question-submit", tag="button")
        self.assertEqual(_attribute(submit, "type"), "submit")

        submit_handler = re.search(
            r"function\s+submitQuestion\s*\([^)]*\)\s*\{"
            r"(.*?)\n\s*\}\n\s*\n\s*listen\(",
            self.story,
            re.DOTALL,
        )
        self.assertIsNotNone(submit_handler)
        handler_source = submit_handler.group(1)
        self.assertIn(".value.trim()", handler_source)
        truthy_branch = re.search(
            r"if\s*\([^)]*\.value\.trim\(\)[^)]*\)\s*\{(.*?)\n\s*\}",
            handler_source,
            re.DOTALL,
        )
        self.assertIsNotNone(truthy_branch)
        self.assertIn("closeQuestion()", truthy_branch.group(1))

    def test_skin_controls_are_classic_and_romantic_without_replacing_shell(self):
        html_tag = re.search(r"<html\b[^>]*>", self.home)
        self.assertIsNotNone(html_tag)
        self.assertEqual(_attribute(html_tag.group(0), "data-skin"), "friends")
        self.assertIsNotNone(_attribute(html_tag.group(0), "data-skin-switchable"))
        self.assertIn("asset('theme.js')", self.home_source)

        expected = (("friends", "Классика", "true"), ("romantic", "Романтика", "false"))
        for skin, label, pressed in expected:
            with self.subTest(skin=skin):
                control = re.search(
                    rf'<button\b(?=[^>]*\bdata-skin-set="{skin}")[^>]*>.*?</button>',
                    self.home,
                    re.DOTALL,
                )
                self.assertIsNotNone(control)
                opening = control.group(0).split(">", 1)[0] + ">"
                self.assertEqual(_attribute(opening, "aria-pressed"), pressed)
                self.assertEqual(_plain_text(control.group(0)), label)

        switch = re.search(
            r'<div\b[^>]*class="[^"]*landing-skin-switch[^"]*".*?</div>',
            self.home,
            re.DOTALL,
        )
        self.assertIsNotNone(switch)
        switch_text = _plain_text(switch.group(0))
        self.assertNotIn("Стандартное", switch_text)
        self.assertNotIn("Романтическое", switch_text)
        self.assertEqual(self.home.count("data-demo-card"), 1)

    def test_settings_show_the_exact_agreed_lists_without_toggles(self):
        settings = re.search(
            r'<article\b[^>]*class="[^"]*\bpossibility-story--settings\b[^"]*"[^>]*>'
            r".*?</article>",
            self.home,
            re.DOTALL,
        )
        self.assertIsNotNone(settings)
        source = settings.group(0)
        text = _plain_text(source)
        self.assertIn("Тонкая настройка", text)
        self.assertNotIn("Права гостей", text)
        self.assertNotRegex(source, r'role=["\']switch["\']')
        self.assertNotRegex(source, r'<input\b[^>]*type=["\']checkbox["\']')
        self.assertNotRegex(source, r'<button\b')

        lists = re.findall(r"<(?:ul|ol)\b[^>]*>(.*?)</(?:ul|ol)>", source, re.DOTALL)
        self.assertEqual(len(lists), 2, "Нужны отдельные списки подборки и события")

        def items(list_source: str) -> list[str]:
            labels = []
            for item in re.findall(r"<li\b[^>]*>(.*?)</li>", list_source, re.DOTALL):
                label = re.search(r"<strong\b[^>]*>(.*?)</strong>", item, re.DOTALL)
                self.assertIsNotNone(label, "У каждой настройки нужен заголовок")
                self.assertNotRegex(item, r"<small\b", "Описания настроек нужно убрать")
                labels.append(_plain_text(label.group(1)).rstrip("."))
            return labels

        self.assertEqual(
            items(lists[0]),
            [
                "Описание",
                "Превью ссылки",
                "Варианты и дедлайн голосования",
                "Состав и порядок событий",
                "Доступ по приватной ссылке",
            ],
        )
        self.assertEqual(
            items(lists[1]),
            [
                "Название",
                "Фото и видео",
                "Порядок и кадрирование медиа",
                "Дата и время",
                "Место и карта",
                "Описание и внешние ссылки",
                "Условия оплаты",
                "Максимум участников",
                "Видимость в общей ленте",
            ],
        )

    def test_feed_profile_and_alexey_have_real_local_images(self):
        expected_assets = (
            "landing-feed-light-exhibition.webp",
            "landing-feed-water-walk.webp",
            "landing-feed-ceramics.webp",
            "landing-feed-summer-cinema.webp",
            "landing-profile-jazz.webp",
            "landing-avatar-alexey.webp",
        )
        image_tags = re.findall(r"<img\b[^>]*>", self.home, re.DOTALL)
        for filename in expected_assets:
            matches = []
            for image in image_tags:
                source = (
                    _attribute(image, "src")
                    or _attribute(image, "data-deferred-src")
                    or ""
                )
                if Path(urlsplit(source).path).name == filename:
                    matches.append(image)
            self.assertGreaterEqual(len(matches), 1, f"Не используется изображение {filename}")
            src = (
                _attribute(matches[0], "src")
                or _attribute(matches[0], "data-deferred-src")
                or ""
            )
            local_path = _static_path(src)
            self.assertIsNotNone(local_path, f"Изображение {filename} должно быть локальным")
            self.assertTrue(local_path.is_file(), local_path)
            self.assertGreater(local_path.stat().st_size, 1024, local_path)
            self.assertIsNotNone(_attribute(matches[0], "alt"))

    def test_gsap_scrolltrigger_are_local_registered_and_reduced_motion_safe(self):
        script_sources = re.findall(
            r'<script\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*>',
            self.home,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertFalse(any(re.match(r"https?://", src) for src in script_sources))

        def script_index(predicate) -> int:
            for index, src in enumerate(script_sources):
                if predicate(Path(urlsplit(src).path).name.lower()):
                    return index
            self.fail(f"Не найден локальный script: {predicate}")

        gsap_index = script_index(lambda name: "gsap" in name and "scrolltrigger" not in name)
        trigger_index = script_index(lambda name: "scrolltrigger" in name)
        story_index = script_index(lambda name: name == "landing-story.js")
        self.assertLess(gsap_index, trigger_index)
        self.assertLess(trigger_index, story_index)
        for src in (script_sources[gsap_index], script_sources[trigger_index]):
            path = _static_path(src)
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file(), path)

        self.assertRegex(
            self.story,
            r"gsap\.registerPlugin\(\s*(?:window\.)?ScrollTrigger\s*\)",
        )
        self.assertRegex(self.story, r"gsap\.timeline\s*\(")
        self.assertRegex(self.story, r"\bscrollTrigger\s*:")
        self.assertRegex(self.story, r"\bpin\s*:")
        self.assertRegex(self.story, r"\bscrub\s*:")
        self.assertNotRegex(self.story, r"\bmarkers\s*:\s*true")
        self.assertRegex(self.story, r"(?:\.revert\s*\(|\.kill\s*\()")

        self.assertRegex(
            self.story,
            r"matchMedia\([\"']\(prefers-reduced-motion:\s*reduce\)[\"']\)",
        )
        self.assertIn("is-reduced-motion", self.story)
        self.assertRegex(self.css, r"@media\s*\(prefers-reduced-motion:\s*reduce\)")

    def test_reduced_motion_mode_can_be_left_after_the_preference_changes(self):
        self.assertRegex(
            self.story,
            r'listen\(\s*reducedMotion\s*,\s*["\']change["\']',
            "Изменение системной настройки должно переинициализировать сцену",
        )
        self.assertRegex(
            self.story,
            r'classList\.(?:remove\(\s*["\']is-reduced-motion["\']'
            r'|toggle\(\s*["\']is-reduced-motion["\']\s*,)',
            "При возврате к обычному motion класс reduced-motion должен сниматься",
        )

    def test_gsap_gallery_transform_has_no_competing_css_transition(self):
        self.assertIn("slide.style.transform", self.story)
        slide_rules = [
            declarations
            for selector, declarations in _css_rules(self.css)
            if selector == ".landing-demo-card__slide"
        ]
        self.assertTrue(slide_rules, "Не найдены базовые стили слайдов галереи")
        declarations: dict[str, str] = {}
        for rule in slide_rules:
            declarations.update(rule)
        transition = declarations.get("transition", "none")
        self.assertRegex(
            transition,
            r"^none(?:\s*!important)?$",
            "CSS transition не должен дополнительно сглаживать transform, которым управляет GSAP",
        )

    def test_reverse_scroll_releases_a_manual_gallery_swipe_immediately(self):
        sync_scene = re.search(
            r"function\s+syncScene\s*\(\s*trigger\s*\)\s*\{"
            r"(.*?)\n\s*\}",
            self.story,
            re.DOTALL,
        )
        self.assertIsNotNone(sync_scene)
        source = sync_scene.group(1)
        self.assertRegex(source, r"trigger\.direction\s*<\s*0")
        self.assertIn("galleryController.releaseToTimeline(autoSwipe.progress)", source)

    def test_story_intro_does_not_inherit_generic_section_whitespace(self):
        declarations = _merged_css_declarations(
            self.css,
            lambda selector: selector.endswith(".landing-story__intro"),
        )
        self.assertEqual(declarations.get("padding-block"), "0")

    def test_fit_uses_real_stage_geometry_for_each_camera_offset(self):
        scales = re.search(
            r"function\s+scales\s*\(\s*\)\s*\{(.*?)\n\s*function\s+clipForBottom",
            self.story,
            re.DOTALL,
        )
        self.assertIsNotNone(scales, "Не найден расчёт масштаба карточки")
        source = scales.group(1)
        self.assertIn("stageRect.width", source)
        self.assertIn("stageRect.height", source)
        self.assertIn("naturalHeight", source)
        self.assertIn("function fitAt(offset)", source)
        self.assertIn("stageCenter + offset", source)
        self.assertIn("center - safeTop", source)
        self.assertIn("safeBottom - center", source)
        self.assertIn('fitAt(sceneY("base"))', source)
        self.assertIn('fitAt(sceneY("focus"))', source)
        self.assertNotIn("narrativeRect", source)

    def test_stage_has_no_glass_backplate_and_cannot_clip_the_card(self):
        stage_rules = [
            declarations
            for selector, declarations in _css_rules(self.css)
            if selector.endswith(".landing-story__stage")
        ]
        self.assertTrue(stage_rules)
        declarations: dict[str, str] = {}
        for rule in stage_rules:
            declarations.update(rule)
        for prop in ("background", "background-color"):
            if prop in declarations:
                self.assertIn(declarations[prop], {"none", "transparent"})
        if "border" in declarations:
            self.assertIn(declarations["border"], {"0", "none"})
        if "box-shadow" in declarations:
            self.assertEqual(declarations["box-shadow"], "none")
        for prop, value in declarations.items():
            if prop.endswith("backdrop-filter"):
                self.assertEqual(value, "none")
        self.assertNotIn(declarations.get("overflow"), {"hidden", "clip"})

        pseudo_rules = [
            declarations
            for selector, declarations in _css_rules(self.css)
            if selector.endswith(".landing-story__stage::before")
        ]
        pseudo_declarations: dict[str, str] = {}
        for rule in pseudo_rules:
            pseudo_declarations.update(rule)
        pseudo_is_hidden = pseudo_declarations.get("display") == "none"
        pseudo_has_no_content = pseudo_declarations.get("content", "none") in {
            "none", "normal",
        }
        self.assertTrue(pseudo_is_hidden or pseudo_has_no_content)

        self.assertNotIn("data-story-feed", self.home)


if __name__ == "__main__":
    unittest.main(verbosity=2)
