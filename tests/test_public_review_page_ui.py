from pathlib import Path
import unittest


APP = Path(__file__).resolve().parents[1] / "app"


def source(relative: str) -> str:
    return (APP / relative).read_text("utf-8")


class PublicReviewPageUiTests(unittest.TestCase):
    def test_page_is_a_full_guest_surface_with_add_and_report_actions(self):
        page = source("templates/public/profile_review.html")

        self.assertIn('{% include "public/_profile_review_widget.html" %}', page)
        self.assertIn('class="review-share-page"', page)
        self.assertIn('class="bg-gather"', page)
        self.assertIn('class="bg-hearts"', page)
        self.assertIn('class="corner-actions"', page)
        self.assertIn('class="hero review-share-hero"', page)
        self.assertIn('class="report-link review-share-report"', page)
        self.assertIn('data-report-open', page)
        self.assertIn('id="reportDlg"', page)
        self.assertIn('action="/d/{{ token }}/report"', page)
        self.assertIn('method="post" action="/d/{{ token }}/add"', page)
        self.assertIn('Добавить событие в коллекцию', page)
        self.assertIn('{% include "auth/_login_methods.html" %}', page)
        self.assertIn('<footer>', page)
        self.assertIn("{{ asset('confirm.js') }}", page)
        self.assertIn("{{ asset('public_review.js') }}", page)
        self.assertLess(page.index("{{ asset('confirm.js') }}"),
                        page.index("{{ asset('public_review.js') }}"))
        self.assertNotIn("{{ asset('guest.js') }}", page)

    def test_page_keeps_social_and_browser_metadata(self):
        page = source("templates/public/profile_review.html")

        self.assertIn('property="og:title"', page)
        self.assertIn('property="og:description"', page)
        self.assertIn('property="og:image"', page)
        self.assertIn('name="theme-color"', page)
        self.assertIn("favicon-standard.png", page)
        self.assertIn("favicon-romantic.png", page)
        self.assertIn("{{ asset('theme.js') }}", page)

    def test_dedicated_script_handles_login_report_csrf_and_toast(self):
        script = source("static/public_review.js")
        css = source("static/public.css")

        self.assertIn('#loginOpen, [data-login-open]', script)
        self.assertIn('[data-report-open]', script)
        self.assertIn('fetch(reportForm.action', script)
        self.assertIn('"X-CSRF-Token": csrf', script)
        self.assertIn('"Accept": "application/json"', script)
        self.assertIn('toast("Спасибо, жалоба отправлена.', script)
        self.assertIn('[data-community-share]', script)
        self.assertIn('window.matchMedia("(pointer: coarse) and (max-width: 900px)")', script)
        self.assertIn('navigator.share({', script)
        self.assertIn('navigator.clipboard.writeText(text)', script)
        self.assertIn('toast("Ссылка скопирована")', script)
        self.assertIn('.review-share-page .profile-review-page .profile-review-widget', css)
        self.assertIn('.review-share-card-shell.has-cover .review-share-report', css)
        self.assertIn('html[data-skin="friends"] .review-share-page', css)
        self.assertIn('html[data-theme="dark"] .review-share-page', css)


if __name__ == "__main__":
    unittest.main()
