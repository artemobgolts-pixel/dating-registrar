"""A11Y-02: фактический контраст текста в Chromium, включая состояния вкладок."""
import itertools
from io import BytesIO
import json
import math
import os
from pathlib import Path
import unittest

from playwright.sync_api import expect, sync_playwright
from PIL import Image

from live_backend import LiveBackend


AXE = Path(__file__).resolve().parent / "vendor" / "axe.min.js"


class FinalContrastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.backend.close()

    def raster_contrast(self, page, selector, incomplete, name):
        """Градиенты/короткие числа: реальный фон под диапазонами текста + исходный цвет.

        Скриншот без заливки глифов сохраняет currentColor, все фоны и opacity.
        Берём каждый пиксель диапазона, поэтому минимум консервативен и учитывает
        градиенты. Неизвестные причины axe или композиция с opacity предков
        остаются ошибкой, а не автоматически разрешённым incomplete.
        """
        data = page.evaluate("""({selector, incomplete}) => {
          window.getSelection().removeAllRanges();
          const region = document.querySelector(selector);
          const query = selector === '.profile-tabs' ? 'a, b' : 'h4 > span, li > strong';
          const nodes = Array.from(region.querySelectorAll(query));
          const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
          const ctx = canvas.getContext('2d', {willReadFrequently:true});
          const records = nodes.map(node => {
            const style = getComputedStyle(node);
            ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = style.color; ctx.fillRect(0, 0, 1, 1);
            const rgba = Array.from(ctx.getImageData(0, 0, 1, 1).data);
            const ancestors = [];
            for (let parent = node.parentElement; parent; parent = parent.parentElement) {
              if (Number(getComputedStyle(parent).opacity) !== 1) ancestors.push({
                tag:parent.tagName, className:parent.className, opacity:getComputedStyle(parent).opacity});
            }
            const ranges = [];
            for (const child of node.childNodes) {
              if (child.nodeType !== Node.TEXT_NODE || !child.textContent.trim()) continue;
              const range = document.createRange(); range.selectNodeContents(child);
              ranges.push(...Array.from(range.getClientRects(), rect => ({
                x:rect.x, y:rect.y, width:rect.width, height:rect.height})));
            }
            return {html:node.outerHTML, text:node.textContent.trim(), rgba,
              opacity:Number(style.opacity), ancestors, ranges};
          });
          const resolutions = incomplete.map(item => ({target:item.target,
            index:item.target.length === 1 ? nodes.indexOf(document.querySelector(item.target[0])) : -1,
            reasons:item.checks.map(check => check.data?.messageKey)}));
          window.__contrastStyles = [region, ...region.querySelectorAll('*')].map(node => ({
            node, style:node.getAttribute('style')}));
          for (const {node} of window.__contrastStyles) {
            node.style.setProperty('-webkit-text-fill-color', 'transparent', 'important');
            node.style.setProperty('text-shadow', 'none', 'important');
          }
          return {records, resolutions};
        }""", {"selector": selector, "incomplete": incomplete})
        try:
            page.evaluate("""() => new Promise(resolve => requestAnimationFrame(() =>
              requestAnimationFrame(resolve)))""")
            background = Image.open(BytesIO(page.screenshot())).convert("RGB")
        finally:
            page.evaluate("""() => {
              for (const {node, style} of window.__contrastStyles) {
                if (style === null) node.removeAttribute('style'); else node.setAttribute('style', style);
              }
              delete window.__contrastStyles;
            }""")

        def luminance(rgb):
            linear = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4
                      for channel in (value / 255 for value in rgb)]
            return sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))

        for record in data["records"]:
            self.assertEqual(record["ancestors"], [], record)
            self.assertTrue(record["ranges"], record)
            colors = set()
            for rect in record["ranges"]:
                left, top = math.floor(rect["x"]), math.floor(rect["y"])
                right, bottom = math.ceil(rect["x"] + rect["width"]), math.ceil(rect["y"] + rect["height"])
                self.assertGreaterEqual(min(left, top), 0, record)
                self.assertLessEqual(right, background.width, record)
                self.assertLessEqual(bottom, background.height, record)
                pixels = background.load()
                colors.update(pixels[x, y] for y in range(top, bottom) for x in range(left, right))
            alpha = record["rgba"][3] / 255 * record["opacity"]
            ratios = []
            for bg in colors:
                fg = [record["rgba"][index] * alpha + bg[index] * (1 - alpha) for index in range(3)]
                low, high = sorted((luminance(bg), luminance(fg)))
                ratios.append(((high + .05) / (low + .05), bg, fg))
            ratio, bg, fg = min(ratios)
            record.update({"contrastRatio": round(ratio, 4), "backgroundAtMinimum": bg,
                           "foregroundAtMinimum": fg, "backgroundColorsSampled": len(colors)})
        if self.artifacts and any(record["contrastRatio"] < 4.5 for record in data["records"]):
            background.save(self.artifacts / f"{name}-background.png")
        return data

    def measure(self, page, selector, name):
        region = page.locator(selector)
        expect(region).to_be_visible()
        region.scroll_into_view_if_needed()
        region.evaluate("""async node => {
          await Promise.all(node.getAnimations({subtree:true})
            .filter(animation => animation.effect.getComputedTiming().iterations !== Infinity)
            .map(animation => animation.finished.catch(() => {})));
        }""")
        result = page.evaluate("""async selector => {
          const result = await axe.run(selector, {
            runOnly: {type:'rule', values:['color-contrast']},
            resultTypes:['violations','incomplete','passes']
          });
          return Object.fromEntries(['violations','incomplete','passes'].map(kind => [kind,
            result[kind].flatMap(rule => rule.nodes.map(node => ({
              target:node.target, html:node.html, summary:node.failureSummary,
              checks:[...node.any,...node.all,...node.none].map(check => ({
                id:check.id, data:check.data, message:check.message
              }))
            }))) ]));
        }""", selector)
        result["raster"] = self.raster_contrast(page, selector, result["incomplete"], name)
        if self.artifacts:
            (self.artifacts / f"{name}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            if name.endswith("normal"):
                region.screenshot(path=str(self.artifacts / f"{name}.png"))
        with self.subTest(state=name, check="axe"):
            self.assertEqual(result["violations"], [], result["violations"])
        with self.subTest(state=name, check="resolved_incomplete"):
            for resolution in result["raster"]["resolutions"]:
                self.assertGreaterEqual(resolution["index"], 0, resolution)
                self.assertTrue(resolution["reasons"], resolution)
                self.assertTrue(set(resolution["reasons"]) <= {"bgGradient", "shortTextContent", "imgNode"}, resolution)
        measured = [record["contrastRatio"] for record in result["raster"]["records"]]
        with self.subTest(state=name, check="measured_text"):
            self.assertEqual(len(measured), 6 if selector == ".profile-tabs" else 13)
            self.assertGreaterEqual(min(measured), 4.5, measured)

    def contrast(self, surface, skin, theme):
        uid, cookie = self.backend.user_cookie(skin=skin)
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
        self.addCleanup(context.close)
        context.add_cookies([cookie, {"name": "d4y_theme", "value": theme, "url": self.backend.url},
                            {"name": "d4y_skin", "value": skin, "url": self.backend.url}])
        self.artifacts = None
        if output := os.environ.get("CONTRAST_REMEDIATION_ARTIFACTS"):
            self.artifacts = Path(output)
            self.artifacts.mkdir(parents=True, exist_ok=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        path = {"profile": f"/u/{uid}?skin={skin}", "owner": "/admin/profile",
                "landing": f"/?skin={skin}"}[surface]
        response = page.goto(self.backend.url + path)
        self.assertEqual(response.status, 200)
        expect(page.locator("html")).to_have_attribute("data-skin", skin)
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        page.evaluate("document.fonts.ready")
        page.evaluate(AXE.read_text(encoding="utf-8"))
        prefix = f"{surface}-{skin}-{theme}"
        selector = ".settings-fragment" if surface == "landing" else ".profile-tabs"
        self.measure(page, selector, prefix + "-normal")
        if surface != "landing":
            links = page.locator(".profile-tabs a")
            expect(links).to_have_count(3)
            expect(page.locator(".profile-tabs a.on")).to_have_count(1)
            self.assertEqual(page.locator(".profile-tabs [disabled], .profile-tabs [aria-disabled='true']").count(), 0)
            for index in range(3):
                link = links.nth(index)
                for state in ("hover", "focus", "active"):
                    if state == "hover":
                        page.evaluate("document.activeElement.blur()")
                        link.hover()
                        self.assertTrue(link.evaluate("node => node.matches(':hover')"))
                    elif state == "focus":
                        page.mouse.move(0, 0)
                        link.focus()
                        page.keyboard.press("Shift+Tab")
                        page.keyboard.press("Tab")
                        expect(link).to_be_focused()
                        self.assertTrue(link.evaluate("node => node.matches(':focus-visible')"))
                    else:
                        page.evaluate("document.activeElement.blur()")
                        link.hover()
                        page.mouse.down()
                        self.assertTrue(link.evaluate("node => node.matches(':active')"))
                    try:
                        self.measure(page, selector, f"{prefix}-{index}-{state}")
                    finally:
                        if state == "active":
                            # Отпускаем вне ссылки: состояние проверено без навигации.
                            page.mouse.move(0, 0)
                            page.mouse.up()
                            page.evaluate("window.getSelection().removeAllRanges()")
        else:
            # Это статичный макет: искусственные disabled/active к нему не применимы.
            self.assertEqual(page.locator(".settings-fragment :is(a,button,input,select,textarea)").count(), 0)
        self.assertEqual(errors, [])


def make_case(surface, skin, theme):
    def test(self):
        self.contrast(surface, skin, theme)
    return test


for _surface, _skin, _theme in itertools.product(
        ("profile", "owner", "landing"), ("friends", "romantic"), ("light", "dark")):
    setattr(FinalContrastTests, f"test_{_surface}_{_skin}_{_theme}", make_case(_surface, _skin, _theme))


if __name__ == "__main__":
    unittest.main(verbosity=2)
