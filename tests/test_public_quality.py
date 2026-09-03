#!/usr/bin/env python3
"""Регрессии публичного лендинга, SEO и безопасной HTML-страницы 404."""

from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))
os.chdir(APP)

_DATA = tempfile.TemporaryDirectory(prefix="date4you-public-quality-")
os.environ.update({
    "DATA_DIR": _DATA.name,
    "COOKIE_SECURE": "false",
    "DOMAIN": "quality.test",
    "SECRET_KEY": "public-quality-test-secret",
    "TG_BOT_TOKEN": "",
    "TG_BOT_USERNAME": "",
    "SUPPORT_CONTACT": "",
    "SUPPORT_URL": "https://t.me/artiwayn",
})

from starlette.testclient import TestClient  # noqa: E402

import main  # noqa: E402


@main.app.get("/__test__/unhandled-error")
def _unhandled_error_fixture():
    raise RuntimeError("internal error details must stay private")


class PublicSeoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_root_is_an_indexable_semantic_landing(self):
        response = self.client.get("/", headers={"accept": "text/html"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        source = response.text
        self.assertEqual(len(re.findall(r"<h1\b", source)), 1)
        self.assertEqual(len(re.findall(r"<main\b", source)), 1)
        self.assertIn('id="main-content"', source)
        self.assertIn('<meta name="description" content="', source)
        self.assertIn(
            '<meta name="robots" content="index, follow, max-image-preview:large">',
            source,
        )
        self.assertIn('<link rel="canonical" href="https://quality.test/">', source)
        for signal in (
            'property="og:title"',
            'property="og:description"',
            'property="og:url" content="https://quality.test/"',
            'property="og:image"',
            'name="twitter:card" content="summary_large_image"',
        ):
            with self.subTest(signal=signal):
                self.assertIn(signal, source)
        self.assertIn('href="https://t.me/artiwayn"', source)
        self.assertIn("favicon-standard.png", source)
        self.assertIn("favicon-romantic.png", source)
        self.assertIn("ink.js", source)
        self.assertIn("ink-runtime.js", source)
        self.assertIn("ink-worker.js", source)
        self.assertIn("ink-static-friends-light.webp", source)
        self.assertIn("ink-static-friends-light-portrait.webp", source)
        self.assertIn("ink-static-romantic-dark.webp", source)
        self.assertIn("landing-story.js", source)
        self.assertNotIn("landing-video.js", source)
        self.assertNotRegex(source, r"<video\b")
        self.assertNotIn("landing-brand-loop.mp4", source)
        self.assertNotIn("landing-brand-loop-mobile.mp4", source)
        self.assertEqual(source.count("data-demo-card"), 1)
        self.assertIn('data-demo-skin="friends"', source)
        self.assertIn('data-demo-slide-skin="friends"', source)
        self.assertIn('data-demo-slide-skin="romantic"', source)
        self.assertNotRegex(source, r'<[^>]+\sstyle="')

        match = re.search(
            r'<script type="application/ld\+json" nonce="([^"]+)">\s*(.*?)\s*</script>',
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertTrue(match.group(1))
        structured = json.loads(html.unescape(match.group(2)))
        self.assertEqual(structured["@context"], "https://schema.org")
        self.assertEqual(
            {node["@type"] for node in structured["@graph"]},
            {"WebSite", "WebApplication"},
        )

    def test_head_robots_and_sitemap_are_consistent(self):
        self.assertEqual(self.client.head("/").status_code, 200)

        robots = self.client.get("/robots.txt")
        self.assertEqual(robots.status_code, 200)
        lines = robots.text.splitlines()
        self.assertIn("User-agent: *", lines)
        self.assertIn("Allow: /", lines)
        self.assertNotIn("Disallow: /", lines)
        self.assertIn("Sitemap: https://quality.test/sitemap.xml", lines)

        sitemap = self.client.get("/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        self.assertTrue(sitemap.headers["content-type"].startswith("application/xml"))
        root = ElementTree.fromstring(sitemap.content)
        namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locations = [node.text for node in root.findall("sm:url/sm:loc", namespace)]
        self.assertEqual(locations, ["https://quality.test/", "https://quality.test/about"])

    def test_about_is_indexable_and_uses_existing_support_url(self):
        response = self.client.get("/about", headers={"accept": "text/html"})

        self.assertEqual(response.status_code, 200)
        self.assertIn('<meta name="robots" content="index, follow">', response.text)
        self.assertIn('<meta name="description" content="', response.text)
        self.assertIn(
            '<link rel="canonical" href="https://quality.test/about">',
            response.text,
        )
        self.assertIn('href="https://t.me/artiwayn"', response.text)
        self.assertNotIn("Контакт поддержки появится здесь", response.text)

        about_source = (APP / "templates/public/about.html").read_text("utf-8")
        self.assertIn(".skip-link:focus", about_source)

    def test_legal_pages_use_the_same_existing_support_contact(self):
        for path in ("/terms", "/privacy"):
            with self.subTest(path=path):
                response = self.client.get(path, headers={"accept": "text/html"})
                self.assertEqual(response.status_code, 200)
                self.assertIn('href="https://t.me/artiwayn"', response.text)
                self.assertIn("Связаться с поддержкой", response.text)

    def test_landing_dark_buttons_keep_a_dedicated_contrast_fill(self):
        css = (APP / "static/landing.css").read_text("utf-8")

        self.assertIn("--accent-fill-strong: #46539c", css)
        self.assertIn(".button--light { color: var(--accent-fill-strong)", css)
        self.assertIn("padding: 30px 22px", css)
        self.assertIn("padding: 38px 22px", css)

    def test_landing_story_respects_reduced_motion_without_remote_runtime(self):
        script = (APP / "static/landing-story.js").read_text("utf-8")
        css = (APP / "static/landing.css").read_text("utf-8")
        home = (APP / "templates/public/home.html").read_text("utf-8")

        self.assertRegex(
            script,
            r"matchMedia\([\"']\(prefers-reduced-motion:\s*reduce\)[\"']\)",
        )
        self.assertIn("is-reduced-motion", script)
        self.assertNotRegex(script, r"https?://")
        self.assertIn("vendor/gsap-3.15.0.min.js", home)
        self.assertIn("vendor/ScrollTrigger-3.15.0.min.js", home)
        self.assertRegex(script, r"gsap\.registerPlugin\(")
        self.assertRegex(script, r"\bscrollTrigger\s*:")
        self.assertRegex(css, r"@media\s*\(prefers-reduced-motion:\s*reduce\)")
        self.assertIn(".is-reduced-motion", css)

    def test_private_surfaces_keep_noindex(self):
        for relative in (
            "templates/auth/login.html",
            "templates/public/category.html",
            "templates/public/share.html",
            "templates/admin/base.html",
        ):
            with self.subTest(template=relative):
                source = (APP / relative).read_text("utf-8")
                self.assertRegex(source, r'<meta name="robots" content="noindex[^\"]*">')


class SafeHtmlNotFoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app, follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_browser_get_receives_safe_branded_html_404(self):
        secret = "super-secret-link-fragment"
        response = self.client.get(
            f"/missing/{secret}?from=private",
            headers={"accept": "text/html,application/xhtml+xml"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertEqual(response.headers.get("x-robots-tag"), "noindex, nofollow")
        self.assertIn("Страница не найдена", response.text)
        self.assertIn('href="https://t.me/artiwayn"', response.text)
        self.assertNotIn(secret, response.text)
        self.assertNotIn("private", response.text)

    def test_api_and_non_html_requests_keep_fastapi_json(self):
        for method, accept in (
            ("get", "application/json"),
            ("get", "*/*"),
            ("post", "text/html"),
        ):
            with self.subTest(method=method, accept=accept):
                response = getattr(self.client, method)(
                    "/missing-api-route", headers={"accept": accept}
                )
                self.assertEqual(response.status_code, 404)
                self.assertTrue(
                    response.headers["content-type"].startswith("application/json")
                )
                self.assertEqual(response.json(), {"detail": "Not Found"})


class SafeHtmlServerErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(
            main.app, follow_redirects=False, raise_server_exceptions=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_browser_receives_safe_branded_html_500(self):
        response = self.client.get(
            "/__test__/unhandled-error",
            headers={"accept": "text/html,application/xhtml+xml"},
        )

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertEqual(response.headers.get("x-robots-tag"), "noindex, nofollow")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers.get("x-frame-options"), "DENY")
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertRegex(response.headers.get("x-request-id", ""), r"^[0-9a-f]{32}$")
        self.assertIn("Что-то пошло не так", response.text)
        self.assertIn('class="not-found__code"', response.text)
        self.assertNotIn("internal error details", response.text)

    def test_non_html_client_keeps_compact_500_response(self):
        response = self.client.get(
            "/__test__/unhandled-error", headers={"accept": "application/json"},
        )

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertNotIn("internal error details", response.text)


class DeploymentSeoContractTests(unittest.TestCase):
    def test_www_redirect_is_enabled_without_changing_the_canonical_host(self):
        source = (ROOT / "Caddyfile").read_text("utf-8")

        self.assertRegex(source, r"(?m)^www\.\{\$DOMAIN\} \{")
        self.assertIn("redir https://{$DOMAIN}{uri} permanent", source)


if __name__ == "__main__":
    unittest.main()
