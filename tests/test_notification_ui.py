"""Контракты общей иконки и touch-поведения уведомлений."""

import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class NotificationUiTests(unittest.TestCase):
    def test_bell_shape_is_shared_and_taller(self):
        admin_icons = (APP / "templates/admin/_icon.html").read_text(encoding="utf-8")
        public_icons = (APP / "templates/public/_icons.html").read_text(encoding="utf-8")
        bell = (APP / "templates/_bell_icon_path.html").read_text(encoding="utf-8")

        self.assertIn('{% include "_bell_icon_path.html" %}', admin_icons)
        self.assertIn('{% include "_bell_icon_path.html" %}', public_icons)
        # Новый купол начинается на y=2.5 вместо y=4 и использует почти всю
        # высоту viewBox, поэтому иконка заметно выше при прежней ширине.
        self.assertIn("S5.8 4.9 5.8 9.3", bell)
        self.assertIn("6.8-6.2-6.8", bell)

        env = Environment(loader=FileSystemLoader(APP / "templates"))
        admin_svg = env.get_template("admin/_icon.html").render(icon="bell")
        public_svg = env.from_string(
            '{% from "public/_icons.html" import icon %}{{ icon("bell") }}'
        ).render()
        self.assertIn('d="M18.2 9.3', admin_svg)
        self.assertIn('d="M18.2 9.3', public_svg)

    def test_touch_highlight_is_disabled_for_expandable_controls(self):
        selector = 'a, button, summary, label, [role="button"]'
        for stylesheet in ("admin.css", "public.css"):
            css = (APP / "static" / stylesheet).read_text(encoding="utf-8")
            self.assertIn(selector, css)
            self.assertIn("-webkit-tap-highlight-color: transparent", css)

    def test_answer_and_delete_share_one_action_row(self):
        template = (APP / "templates/admin/questions.html").read_text(
            encoding="utf-8",
        )
        css = (APP / "static/admin.css").read_text(encoding="utf-8")

        self.assertNotIn("Вернуть в новые", template)
        self.assertIn('form="notif-answer-{{ q[\'id\'] }}"', template)
        question_rows = template.split("{% for q in rows %}", 1)[1]
        action_row = question_rows.split('<div class="notif-actions">', 1)[1].split(
            "</div>", 1,
        )[0]
        self.assertIn("Обновить ответ", action_row)
        self.assertIn(">Удалить</button>", action_row)
        self.assertIn("flex-wrap: nowrap", css)
        self.assertIn(".notif-actions .notif-action-btn { flex: 1 1 0", css)

    def test_telegram_state_and_review_queue_have_dedicated_ui(self):
        template = (APP / "templates/admin/questions.html").read_text(
            encoding="utf-8",
        )
        base = (APP / "templates/admin/base.html").read_text(encoding="utf-8")
        profile = (APP / "templates/admin/profile.html").read_text(encoding="utf-8")
        connect = (APP / "templates/admin/_telegram_connect.html").read_text(
            encoding="utf-8",
        )
        css = (APP / "static/admin.css").read_text(encoding="utf-8")
        auth = (APP / "static/auth.js").read_text(encoding="utf-8")

        for surface in (template, base, profile):
            self.assertIn('{% include "admin/_telegram_connect.html" %}', surface)
        combined = template + base + profile + connect
        self.assertNotIn("Бот подключён", combined)
        self.assertNotIn("Бот не подключён", combined)
        self.assertNotIn("Подключить бота", combined)
        self.assertIn("Уведомления в Telegram", connect)
        self.assertIn("{% if not user['bot_linked'] %}", connect)
        self.assertIn('data-tg-connect class="btn primary telegram-connect-button notif-settings-connect"', connect)
        self.assertIn("Подключить уведомления", connect)
        # Кнопка определена ровно в одном partial, а глобальный баннер не
        # дублирует специализированные блоки профиля и уведомлений.
        self.assertEqual(combined.count("data-tg-connect"), 1)
        self.assertIn("active not in ('q', 'profile')", base)
        self.assertIn("telegram_connect_return_to='/admin/questions'", template)
        self.assertIn(".telegram-connect-main {", css)
        self.assertIn(".telegram-connect-button { flex: none; margin-left: auto; }", css)
        self.assertIn(".notif-settings-connect {", css)
        self.assertIn("event.preventDefault();", auth)
        self.assertIn("event.stopPropagation();", auth)
        self.assertIn('window.open("about:blank", "_blank")', auth)
        self.assertIn('btn.dataset.tgDirect = d.url', auth)
        self.assertIn('btn.textContent = "Открыть Telegram"', auth)
        self.assertIn('window.location.assign(btn.dataset.tgDirect)', auth)

        env = Environment(loader=FileSystemLoader(APP / "templates"))
        partial = env.get_template("admin/_telegram_connect.html")
        request = SimpleNamespace(url=SimpleNamespace(path="/admin/profile"))
        unlinked = partial.render(
            user={"bot_linked": 0}, request=request,
            telegram_connect_return_to="/admin/profile",
        )
        self.assertEqual(unlinked.count("data-tg-connect"), 1)
        self.assertIn('data-return-to="/admin/profile"', unlinked)
        self.assertIn("Подключить уведомления", unlinked)
        linked = partial.render(user={"bot_linked": 1}, request=request)
        self.assertNotIn("data-tg-connect", linked)
        self.assertNotIn("Бот подключён", linked)

        self.assertIn("Ждут отзыва", template)
        self.assertIn("{{ question_unread }}", template)
        self.assertIn("{{ review_waiting }}", template)
        self.assertIn("{% for review in review_rows %}", template)
        self.assertIn('/d/{{ review[\'share_token\'] }}#review', template)
        self.assertIn(
            '/admin/questions/reviews/{{ review[\'date_id\'] }}/dismiss',
            template,
        )
        self.assertIn('data-confirm="Полностью удалить это упоминание?"', template)
        self.assertIn("review_deleted", template)
        self.assertIn("declined", template)

    def test_event_feed_hides_author_until_widget_and_submits_reports_with_csrf(self):
        dashboard = (APP / "templates/admin/dashboard.html").read_text(
            encoding="utf-8",
        )
        cards = (APP / "templates/admin/_community_cards.html").read_text(
            encoding="utf-8",
        )
        widget = (APP / "templates/admin/_community_widget.html").read_text(
            encoding="utf-8",
        )
        admin = (APP / "static/admin.js").read_text(encoding="utf-8")
        css = (APP / "static/admin.css").read_text(encoding="utf-8")

        self.assertIn("Лента событий", dashboard)
        self.assertNotIn("Встречи сообщества", dashboard)
        self.assertNotIn("Публичные события других людей", dashboard)
        self.assertIn('id="communityReportDlg"', dashboard)
        self.assertNotIn("d['owner_display']", cards)
        self.assertNotIn('class="cfeed-owner"', cards)
        self.assertIn("d['owner_display']", widget)
        self.assertIn('class="cfeed-owner"', widget)
        self.assertIn('class="cfeed-title-row"', cards)
        self.assertIn("data-community-report", cards)
        self.assertIn('data-report-url="/d/{{ d[\'share_token\'] }}/report"', cards)
        self.assertLess(
            cards.index("data-community-report"),
            cards.index('class="cfeed-meta"'),
        )
        self.assertLess(
            cards.index("data-community-report"),
            cards.index('class="cfeed-card-actions"'),
        )
        self.assertIn('"X-CSRF-Token": document.body.dataset.csrf || ""', admin)
        self.assertIn("new FormData(reportForm)", admin)
        self.assertIn(".report-link {", css)
        self.assertIn("text-decoration: underline dotted", css)

    def test_categories_have_spacing_and_a_default_thumbnail(self):
        categories = (APP / "templates/admin/categories.html").read_text(
            encoding="utf-8",
        )
        detail = (APP / "templates/admin/category_detail.html").read_text(
            encoding="utf-8",
        )
        public = (APP / "templates/public/category.html").read_text(
            encoding="utf-8",
        )
        public_routes = (APP / "public_routes.py").read_text(encoding="utf-8")
        css = (APP / "static/admin.css").read_text(encoding="utf-8")

        self.assertIn('class="card cat-card has-thumb"', categories)
        self.assertIn(
            "src=\"/admin/categories/{{ c['id'] }}/og-preview?skin={{ c['category_skin'] }}",
            categories,
        )
        self.assertIn("c['preview_revision']", categories)
        self.assertNotIn("og-friends.jpg", categories)
        self.assertNotIn("og-default.jpg", categories)
        self.assertIn(".category-create-only { margin-bottom: 16px; }", css)
        self.assertIn(
            "data-friends-src=\"/admin/categories/{{ cat['id'] }}/og-preview?skin=friends",
            detail,
        )
        self.assertIn(
            "data-romantic-src=\"/admin/categories/{{ cat['id'] }}/og-preview?skin=romantic",
            detail,
        )
        self.assertNotIn("og-friends.jpg", detail)
        self.assertNotIn("og-default.jpg", detail)
        self.assertIn(
            "{{ BASE_URL }}/c/{{ token }}/og-image?skin={{ active_skin }}",
            public,
        )
        self.assertIn("&amp;v={{ preview_revision }}", public)
        self.assertIn("images.og_default_path(preview_skin)", public_routes)
        self.assertIn('media_type="image/png"', public_routes)

    def test_event_and_category_versions_share_surface_status_tones(self):
        dates = (APP / "templates/admin/dates.html").read_text(encoding="utf-8")
        categories = (APP / "templates/admin/categories.html").read_text(
            encoding="utf-8",
        )
        detail = (APP / "templates/admin/category_detail.html").read_text(
            encoding="utf-8",
        )
        css = (APP / "static/admin.css").read_text(encoding="utf-8")

        self.assertEqual(
            dates.count('data-status-tone="{{ event_tone(r) }}"'), 2,
            "карточки и строки событий должны получать тон из одного макроса",
        )
        self.assertIn(
            'data-status-tone="{{ category_tone(c[\'voting_status\'], c[\'link_enabled\']) }}"',
            categories,
        )
        self.assertIn('data-status-tone="{{ event_tone(d) }}"', detail)
        self.assertIn(
            ":is(.dcard.dcard, .drow.drow, .cat-card.cat-card)[data-status-tone]",
            css,
        )
        self.assertIn(".category-events-card tr[data-status-tone] > td", css)
        self.assertIn('border-inline-start: 4px solid var(--entity-surface-tone)', css)
        self.assertIn('--entity-surface-wash: color-mix(', css)

        env = Environment(loader=FileSystemLoader(APP / "templates"))
        template = env.from_string(
            "{% from 'admin/_entity_status.html' import event_tone, category_tone %}"
            "{{ event_tone(event) }}|"
            "{{ category_tone(category_status, link_enabled, counting) }}"
        )
        active = {"archived_at": None, "is_draft": 0}
        draft = {"archived_at": None, "is_draft": 1}
        archived = {"archived_at": "2030-01-01", "is_draft": 0}
        self.assertEqual(
            template.render(
                event=active, category_status="resolved",
                link_enabled=True, counting=False,
            ),
            "success|success",
        )
        self.assertEqual(
            template.render(
                event=draft, category_status="open",
                link_enabled=True, counting=True,
            ),
            "warning|warning",
        )
        self.assertEqual(
            template.render(
                event=archived, category_status="open",
                link_enabled=False, counting=False,
            ),
            "neutral|danger",
        )

    def test_countdown_is_continuous_turbo_safe_and_reused_on_dashboard(self):
        category = (APP / "templates/public/category.html").read_text(encoding="utf-8")
        share = (APP / "templates/public/share.html").read_text(encoding="utf-8")
        dashboard = (APP / "templates/admin/dashboard.html").read_text(encoding="utf-8")
        ui = (APP / "static/ui.js").read_text(encoding="utf-8")
        admin = (APP / "static/admin.js").read_text(encoding="utf-8")
        public_css = (APP / "static/public.css").read_text(encoding="utf-8")

        self.assertNotIn(
            "Можно выбрать один вариант — новый выбор заменит предыдущий.", category,
        )
        self.assertNotIn("Участники и прогресс видны всем.", category)
        self.assertIn('class="vote-countdown" role="timer"', category)
        self.assertIn('class="vote-countdown" role="timer"', share)
        self.assertIn('class="vote-countdown-label" data-countdown-label', category)
        self.assertIn('class="vote-countdown-label" data-countdown-label', share)

        self.assertIn('parts.push(secs + " сек.")', ui)
        self.assertNotIn('if (!days) parts.push(secs + " сек.")', ui)
        self.assertIn("var greenHue = 132", ui)
        self.assertIn("var orangeHue = 32", ui)
        self.assertIn("seconds >= day * 2", ui)
        self.assertIn('el.style.setProperty("--countdown-hue"', ui)
        self.assertIn('el.dataset.countdownScale !== "fixed"', ui)
        self.assertIn("if (!el.isConnected)", ui)
        self.assertIn('var label = wrapper && wrapper.querySelector("[data-countdown-label]")', ui)
        self.assertIn('el.textContent = value', ui)
        self.assertIn('"До конца голосования осталось: " + value', ui)

        self.assertIn('data-countdown-scale="fixed"', dashboard)
        self.assertIn("data-countdown-compact", dashboard)
        self.assertIn('el.hasAttribute("data-countdown-compact")', ui)
        self.assertIn("Math.ceil(seconds / day)", ui)
        self.assertIn("Math.ceil(seconds / 3600)", ui)
        self.assertIn("share['voting_status'] == 'open'", dashboard)
        og_preview = dashboard.split('class="og-preview dashboard-og-preview"', 1)[1]
        self.assertLess(og_preview.index('class="dashboard-vote-countdown"'),
                        og_preview.index('class="og-text"'))
        self.assertIn("UI.voteCountdowns(document)", admin)
        self.assertIn(".vote-summary.vote-open [data-vote-countdown]", public_css)
        self.assertIn(".vote-countdown-label", public_css)
        self.assertIn('.vote-summary:not(.vote-open)', public_css)
        self.assertIn("font-variant-numeric: tabular-nums", public_css)

    def test_font_ready_keeps_glass_animation_and_category_form_is_responsive(self):
        ui = (APP / "static/ui.js").read_text(encoding="utf-8")
        css = (APP / "static/admin.css").read_text(encoding="utf-8")
        category_new = (APP / "templates/admin/category_new.html").read_text(
            encoding="utf-8",
        )

        font_ready = ui.split("if (document.fonts && document.fonts.ready)", 1)[1]
        font_ready = font_ready.split("// На клике индикатор", 1)[0]
        self.assertIn("put(geom(active()), true)", font_ready)
        self.assertNotIn("repos();", font_ready)
        self.assertIn(".dates-status-tabs a {", css)
        self.assertIn("display: inline-flex", css)
        self.assertIn("align-items: center", css)
        self.assertIn(".dates-status-tabs a .pill {", css)
        self.assertIn("place-items: center", css)
        status_indicator = css.split(".dates-status-tabs .tab-ind {", 1)[1].split("}", 1)[0]
        self.assertIn("transform .42s cubic-bezier(.34, 1.3, .5, 1)", status_indicator)
        self.assertIn("width .42s cubic-bezier(.34, 1.3, .5, 1)", status_indicator)
        self.assertIn(".dates-status-tabs { overflow: visible; }", css)
        self.assertIn("html[data-skin] .tabs .tab-ind", css)
        self.assertIn("html[data-skin] .dates-status-tabs .tab-ind", css)

        self.assertIn(".category-new-card {", css)
        self.assertIn(".category-new-form {", css)
        self.assertIn(".category-new-actions {", css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", css)
        self.assertIn('class="btn editor-back category-new-back"', category_new)
        self.assertIn(".editor-back.category-new-back { top: 112px; }", css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
