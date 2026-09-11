"""Геометрия лендинга после resize, загрузки шрифтов и изменения motion preference."""
import re
import unittest

from playwright.sync_api import expect, sync_playwright
from live_backend import LiveBackend


class LandingRefreshBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = LiveBackend()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.backend.close()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width':1280,'height':900})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.open()

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def open(self):
        self.page.goto(self.backend.url + '/')
        self.page.evaluate('document.fonts.ready')
        expect(self.page.locator('[data-landing-story]')).to_have_class(re.compile('is-story-ready'))
        # Ждём отложенного setup/load refresh ScrollTrigger.
        self.page.wait_for_timeout(300)

    def seek(self, scene):
        self.page.evaluate('''scene => {
          const trigger = ScrollTrigger.getAll()[0];
          const time = scene === 'float' ? trigger.animation.duration() : trigger.animation.labels[scene] + .1;
          // Измеряем сцену в заданной точке, без промежуточных кадров smooth scroll.
          window.scrollTo({top: trigger.start + (trigger.end-trigger.start) * time/trigger.animation.duration(), behavior: 'instant'});
          ScrollTrigger.update();
        }''', scene)
        expect(self.page.locator('[data-landing-story]')).to_have_attribute('data-active-scene',scene)

    def rect(self):
        return self.page.locator('[data-demo-experience]').evaluate('''node => {
          const r=node.getBoundingClientRect(); return {width:r.width,height:r.height,top:r.top,left:r.left};
        }''')

    def test_all_scenes_and_gallery_are_live_after_repeated_refresh(self):
        for width in (390,1280):
            self.page.set_viewport_size({'width':width,'height':900})
            self.page.evaluate('ScrollTrigger.refresh()')
            for scene in ('photo','essentials','details','people','float'):
                self.seek(scene)
            self.assertEqual(self.page.evaluate('ScrollTrigger.getAll().length'), 1)
            before = self.rect()
            self.page.evaluate('ScrollTrigger.refresh(); ScrollTrigger.refresh()')
            self.seek('float')
            after = self.rect()
            for key in before:
                self.assertAlmostEqual(before[key],after[key],delta=1)
            self.assertGreaterEqual(after['left'],-1)
            self.assertLessEqual(after['left']+after['width'],width+1)
            self.assertGreaterEqual(after['top'],-1)
            self.assertLessEqual(after['top']+after['height'],901)
            button = self.page.locator('[data-demo-gallery-next]')
            expect(button).to_be_enabled()
            button.click()
            expect(self.page.locator('[data-demo-gallery-status]')).not_to_be_empty()

    def test_native_resize_geometry_matches_fresh_load_with_ready_fonts(self):
        self.seek('float')
        self.page.set_viewport_size({'width':390,'height':844})
        # Штатный resize refresh ScrollTrigger отложен на 200 ms.
        self.page.wait_for_timeout(500)
        self.seek('float')
        resized = self.rect()
        self.open()
        self.seek('float')
        fresh = self.rect()
        for key in fresh:
            self.assertAlmostEqual(resized[key],fresh[key],delta=1,msg=str((resized,fresh)))

    def test_motion_preference_and_turbo_cleanup_do_not_reuse_old_geometry(self):
        self.seek('float')
        self.page.emulate_media(reduced_motion='reduce')
        expect(self.page.locator('[data-landing-story]')).to_have_class(re.compile('is-reduced-motion'))
        self.assertEqual(self.page.evaluate('ScrollTrigger.getAll().length'),0)
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.emulate_media(reduced_motion='no-preference')
        expect(self.page.locator('[data-landing-story]')).not_to_have_class(re.compile('is-reduced-motion'))
        self.assertEqual(self.page.evaluate('ScrollTrigger.getAll().length'),1)
        self.seek('float')
        self.page.evaluate("document.dispatchEvent(new Event('turbo:before-cache'))")
        self.assertEqual(self.page.evaluate('ScrollTrigger.getAll().length'),0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
