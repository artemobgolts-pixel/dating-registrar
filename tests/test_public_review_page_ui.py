from pathlib import Path
import unittest


APP = Path(__file__).resolve().parents[1] / "app"


def source(relative: str) -> str:
    return (APP / relative).read_text("utf-8")


class PublicReviewPageUiTests(unittest.TestCase):
    def test_page_is_a_full_guest_surface_with_add_and_report_actions(self):
        page = source("templates/public/profile_review.html")

        self.assertIn('{% include "public/_profile_review_widget.html" %}', page)
        self.assertIn('{% include "public/_bg.html" %}', page)
        self.assertIn('class="review-share-page"', page)
        self.assertNotIn('class="bg-gather"', page)
        self.assertNotIn('class="bg-hearts"', page)
        self.assertIn('class="corner-actions"', page)
        self.assertIn('class="hero review-share-hero"', page)
        self.assertIn('<span class="review-share-eyebrow">Обзор</span>', page)
        self.assertNotIn("Публичный обзор", page)
        self.assertNotIn("делится впечатлениями после встречи", page)
        self.assertNotIn("Хочешь сохранить идею?", page)
        self.assertIn("Понравился обзор?", page)
        self.assertNotIn("Добавь событие в свою коллекцию, чтобы", page)
        self.assertNotIn("Войти и добавить", page)
        self.assertNotIn("profile-review-back", page)
        self.assertNotIn("← К событию", page)
        self.assertIn('class="report-link review-share-report"', page)
        self.assertIn('data-report-open', page)
        self.assertIn('id="reportDlg"', page)
        self.assertIn('action="/d/{{ token }}/report"', page)
        self.assertIn('method="post" action="/d/{{ token }}/add"', page)
        self.assertIn('Добавить событие в коллекцию', page)
        self.assertNotIn("Войди, чтобы добавить событие в свою коллекцию.", page)
        self.assertIn('{% include "auth/_login_methods.html" %}', page)
        self.assertIn('<footer>', page)
        self.assertIn("{{ asset('confirm.js') }}", page)
        self.assertIn("{{ asset('public_review.js') }}", page)
        self.assertIn("{{ asset('ink.js') }}", page)
        self.assertIn("data-ink-interactive=", page)
        self.assertLess(page.index("{{ asset('confirm.js') }}"),
                        page.index("{{ asset('public_review.js') }}"))
        self.assertNotIn("{{ asset('guest.js') }}", page)

    def test_page_keeps_social_and_browser_metadata(self):
        page = source("templates/public/profile_review.html")

        self.assertIn('property="og:title"', page)
        self.assertIn('property="og:description"', page)
        self.assertIn('property="og:image"', page)
        self.assertIn('/d/{{ review[\'share_token\'] }}/og-image?skin={{ category_skin }}', page)
        self.assertIn("&amp;v={{ review_og_revision }}", page)
        self.assertIn('name="theme-color"', page)
        self.assertIn("appearance_assets(category_skin)", page)
        self.assertIn("{{ asset('theme.js') }}", page)

    def test_css_fallback_matches_the_dashboard_background(self):
        css = (APP / "static/public.css").read_text(encoding="utf-8")

        self.assertIn(".review-share-page .bg-smoke .turb {", css)
        self.assertIn("opacity: .5; mix-blend-mode: multiply; animation: none", css)
        self.assertIn(".review-share-page .bg-smoke span { filter: blur(80px); }", css)
        self.assertIn("width: 80vw; height: 80vw; left: -16vw; top: -18vh", css)
        self.assertIn("rgba(76,97,184,.34)", css)
        self.assertIn("rgba(70,84,159,.4)", css)

    def test_dedicated_script_handles_login_report_csrf_and_button_feedback(self):
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
        self.assertIn('function copiedFeedback(button)', script)
        self.assertIn('button.textContent = "Ссылка скопирована ✓"', script)
        self.assertIn('button.textContent = original', script)
        self.assertNotIn('toast("Ссылка скопирована")', script)
        self.assertIn('.review-share-page .profile-review-page .profile-review-widget', css)
        self.assertIn('.review-share-card-shell.has-cover .review-share-report', css)
        self.assertIn('html[data-skin="friends"] body.review-share-page', css)
        self.assertIn('html[data-theme="dark"] body.review-share-page', css)
        self.assertIn('html[data-theme="dark"][data-skin="friends"] body.review-share-page', css)
        self.assertIn('body.review-share-page::before', css)
        self.assertIn('body.review-share-page::after { content: none; display: none; }', css)


if __name__ == "__main__":
    unittest.main()
