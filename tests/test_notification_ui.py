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
        action_row = template.split('<div class="notif-actions">', 1)[1].split(
            "</div>", 1,
        )[0]
        self.assertIn("Обновить ответ", action_row)
        self.assertIn(">Удалить</button>", action_row)
        self.assertIn("flex-wrap: nowrap", css)
        self.assertIn(".notif-actions .notif-action-btn { flex: 1 1 0", css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
