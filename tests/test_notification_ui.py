"""Контракты общей иконки и touch-поведения уведомлений."""

import unittest
from pathlib import Path

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
        css = (APP / "static/admin.css").read_text(encoding="utf-8")

        title_row = template.split('<span class="notif-settings-title-row">', 1)[1]
        title_row = title_row.split("</span>", 3)[0:3]
        self.assertIn("Уведомления в Telegram", "</span>".join(title_row))
        self.assertIn("Бот подключён", "</span>".join(title_row))
        self.assertIn(".notif-settings-title-row {", css)
        self.assertIn("gap: 6px 10px", css)
        self.assertIn("margin: 0 3px 4px auto", css)

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
        self.assertIn('role="timer" data-vote-countdown', category)
        self.assertIn('role="timer" data-vote-countdown', share)

        self.assertIn('parts.push(secs + " сек.")', ui)
        self.assertNotIn('if (!days) parts.push(secs + " сек.")', ui)
        self.assertIn("var greenHue = 132", ui)
        self.assertIn("var orangeHue = 32", ui)
        self.assertIn("seconds >= day * 2", ui)
        self.assertIn('el.style.setProperty("--countdown-hue"', ui)
        self.assertIn('el.dataset.countdownScale !== "fixed"', ui)
        self.assertIn("if (!el.isConnected)", ui)

        self.assertIn('data-countdown-scale="fixed"', dashboard)
        self.assertIn("share['voting_status'] == 'open'", dashboard)
        self.assertIn("UI.voteCountdowns(document)", admin)
        self.assertIn(".vote-summary.vote-open [data-vote-countdown]", public_css)
        self.assertIn("font-variant-numeric: tabular-nums", public_css)

    def test_font_ready_keeps_glass_animation_and_category_form_is_responsive(self):
        ui = (APP / "static/ui.js").read_text(encoding="utf-8")
        css = (APP / "static/admin.css").read_text(encoding="utf-8")

        font_ready = ui.split("if (document.fonts && document.fonts.ready)", 1)[1]
        font_ready = font_ready.split("// На клике индикатор", 1)[0]
        self.assertIn("put(geom(active()), true)", font_ready)
        self.assertNotIn("repos();", font_ready)
        self.assertIn("transition: transform .3s", css)
        self.assertIn(".dates-status-tabs a {", css)
        self.assertIn("display: inline-flex", css)
        self.assertIn("align-items: center", css)
        self.assertIn(".dates-status-tabs a .pill {", css)
        self.assertIn("place-items: center", css)

        self.assertIn(".category-new-card {", css)
        self.assertIn(".category-new-form {", css)
        self.assertIn(".category-new-actions {", css)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
