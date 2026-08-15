#!/usr/bin/env python3
"""Регрессии гостевого виджета предложения события."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class GuestProposalWidgetContractTests(unittest.TestCase):
    def test_guest_copy_and_accessibility_are_consistent(self):
        template = (APP / "templates/public/category.html").read_text("utf-8")
        guest_js = (APP / "static/guest.js").read_text("utf-8")
        public_css = (APP / "static/public.css").read_text("utf-8")

        self.assertGreaterEqual(template.count("Предложить своё событие"), 3)
        self.assertNotIn("Создать своё событие", template)
        self.assertIn('id="propEdTitle"', template)
        self.assertIn('aria-required="true"', template)
        self.assertIn('asset(\'date-default-friends.jpg\' if active_skin == \'friends\'', template)
        self.assertIn('toast("Укажи название события")', guest_js)
        self.assertIn("await propMediaManager.whenReady()", guest_js)
        self.assertIn("up.files().length || upv.files().length", guest_js)
        self.assertIn("submitSession !== propSession || !propDlg.open", guest_js)
        self.assertIn("function resetPropProgress()", guest_js)
        self.assertIn('name="videos" id="propVideo"', template)
        self.assertIn('id="propMediaOrder"', template)
        self.assertIn('UI.sortable($("#propMediaOrder")', guest_js)
        self.assertIn('fd.append("keep_video_order", videoOrder.join(","))', guest_js)
        self.assertIn('savedVideos = (meta && meta.videos) ? meta.videos.slice() : []', guest_js)
        self.assertIn('data-proposal-empty-cta', template)
        self.assertIn('hint.hidden = true', guest_js)
        self.assertIn('if (propRequest && propRequest.abort) propRequest.abort()', guest_js)
        self.assertIn('.date-widget-dialog .ed-dd:empty::before', public_css)
        self.assertIn('content: "ГГГГ"', public_css)

    def test_empty_gallery_has_only_the_central_add_action(self):
        guest_js = (APP / "static/guest.js").read_text("utf-8")

        self.assertIn('direction === "next" && items.length > 0', guest_js)
        self.assertIn('button.classList.toggle("as-add", asAdd)', guest_js)


class GuestProposalMediaBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - зависит от dev-окружения
            raise unittest.SkipTest(f"playwright недоступен: {exc!r}") from exc
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - браузер может быть не установлен
            if getattr(cls, "playwright", None):
                cls.playwright.stop()
            raise unittest.SkipTest(f"playwright chromium недоступен: {exc!r}") from exc

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()

    def make_page(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content("""
          <form id="form">
            <div id="zone"></div>
            <input id="media" type="file" multiple>
            <input id="photos" name="images" type="file" multiple>
            <input id="video" name="videos" type="file" multiple>
          </form>
          <div id="photoPreview"></div>
          <div id="videoPreview"></div>
        """)
        page.add_script_tag(content=(APP / "static/ui.js").read_text("utf-8"))
        page.evaluate("""() => {
          window.photoUpload = UI.uploader({
            zone: document.querySelector('#zone'),
            input: document.querySelector('#photos'),
            preview: document.querySelector('#photoPreview'),
            max: 5,
            noZoneBind: true,
          });
          window.videoUpload = UI.uploader({
            zone: document.querySelector('#zone'),
            input: document.querySelector('#video'),
            preview: document.querySelector('#videoPreview'),
            max: 2,
            kind: 'video',
            noZoneBind: true,
          });
          window.mediaManager = UI.mediaUploader({
            zone: document.querySelector('#zone'),
            input: document.querySelector('#media'),
            photo: window.photoUpload,
            video: window.videoUpload,
          });
        }""")
        return page

    def test_mixed_picker_finishes_before_form_data_is_built(self):
        page = self.make_page()
        page.locator("#media").set_input_files([
            {"name": "idea.png", "mimeType": "image/png", "buffer": b"small-image"},
            {"name": "idea.mp4", "mimeType": "video/mp4", "buffer": b"small-video"},
        ])
        page.evaluate("mediaManager.whenReady()")

        result = page.evaluate("""() => ({
          photos: photoUpload.files().map(file => file.name),
          videos: videoUpload.files().map(file => file.name),
          formFiles: [...new FormData(document.querySelector('#form')).entries()]
            .filter(([, value]) => value instanceof File && value.name)
            .map(([name, value]) => [name, value.name]),
        })""")
        self.assertEqual(result["photos"], ["idea.png"])
        self.assertEqual(result["videos"], ["idea.mp4"])
        self.assertEqual(result["formFiles"], [
            ["images", "idea.png"], ["videos", "idea.mp4"],
        ])

    def test_empty_or_generic_mime_video_is_routed_by_extension_up_to_limit(self):
        page = self.make_page()
        result = page.evaluate("""async () => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(['one'], 'first.WEBM', { type: '' }));
          transfer.items.add(new File(
            ['two'], 'second.mp4', { type: 'application/octet-stream' }
          ));
          const input = document.querySelector('#media');
          input.files = transfer.files;
          input.dispatchEvent(new Event('change', { bubbles: true }));
          await mediaManager.whenReady();
          return {
            photos: photoUpload.files().map(file => file.name),
            videos: videoUpload.files().map(file => file.name),
          };
        }""")
        self.assertEqual(result["photos"], [])
        self.assertEqual(result["videos"], ["first.WEBM", "second.mp4"])

    def test_reorder_files_updates_real_form_input_without_reprocessing(self):
        page = self.make_page()
        result = page.evaluate("""async () => {
          await photoUpload.addFiles([
            new File(['a'], 'first.png', { type: 'image/png' }),
            new File(['b'], 'second.png', { type: 'image/png' }),
          ]);
          const current = photoUpload.files();
          const changed = photoUpload.reorderFiles([current[1], current[0]]);
          return {
            changed,
            upload: photoUpload.files().map(file => file.name),
            input: [...document.querySelector('#photos').files].map(file => file.name),
          };
        }""")
        self.assertTrue(result["changed"])
        self.assertEqual(result["upload"], ["second.png", "first.png"])
        self.assertEqual(result["input"], result["upload"])

    def test_reset_cancels_a_photo_still_being_prepared(self):
        page = self.make_page()
        count = page.evaluate("""async () => {
          window.createImageBitmap = () => new Promise(resolve => {
            setTimeout(() => resolve({ width: 20, height: 20 }), 40);
          });
          const file = new File(
            [new Uint8Array(1300 * 1024)], 'large.png', { type: 'image/png' }
          );
          const adding = photoUpload.addFiles([file]);
          setTimeout(() => photoUpload.clear(), 5);
          await adding;
          return photoUpload.files().length;
        }""")
        self.assertEqual(count, 0)

    def test_cancel_pending_drops_a_queued_picker_change(self):
        page = self.make_page()
        count = page.evaluate("""async () => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(['x'], 'stale.png', { type: 'image/png' }));
          const input = document.querySelector('#media');
          input.files = transfer.files;
          input.dispatchEvent(new Event('change', { bubbles: true }));
          mediaManager.cancelPending();
          await mediaManager.whenReady();
          return photoUpload.files().length;
        }""")
        self.assertEqual(count, 0)

    def test_cancelled_shrink_does_not_block_a_new_form_session(self):
        page = self.make_page()
        result = page.evaluate("""async () => {
          window.createImageBitmap = () => new Promise(resolve => {
            setTimeout(() => resolve({ width: 20, height: 20 }), 240);
          });
          const transfer = new DataTransfer();
          transfer.items.add(new File(
            [new Uint8Array(1300 * 1024)], 'stale-large.png',
            { type: 'image/png' }
          ));
          const input = document.querySelector('#media');
          input.files = transfer.files;
          input.dispatchEvent(new Event('change', { bubbles: true }));
          // Даём route() войти в асинхронный shrink, затем имитируем закрытие
          // диалога: отделяем очередь новой сессии и очищаем оба uploader-а.
          await new Promise(resolve => setTimeout(resolve, 10));
          mediaManager.cancelPending();
          photoUpload.clear();
          videoUpload.clear();
          const started = performance.now();
          await mediaManager.whenReady();
          const waitMs = performance.now() - started;
          await new Promise(resolve => setTimeout(resolve, 280));
          return { waitMs, photos: photoUpload.files().length };
        }""")
        self.assertLess(result["waitMs"], 80, result)
        self.assertEqual(result["photos"], 0)

    def test_proposal_reorder_reset_abort_and_deadline_are_session_safe(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.set_content("""
          <body data-token="test" data-auth="1" data-csrf="csrf" data-skin="friends"
                data-max-photos="5" data-max-videos="2">
            <style>
              #propMediaOrder { display: flex; gap: 8px; }
              #propMediaOrder .ptile { width: 60px; height: 60px; flex: 0 0 60px; }
            </style>
            <div id="toast"></div>

            <dialog id="askDlg"><form id="askForm"></form></dialog>
            <button id="askCancel" type="button"></button>
            <dialog id="reportDlg"><form id="reportForm"></form></dialog>
            <button id="reportCancel" type="button"></button>
            <dialog id="timeDlg">
              <form id="timeForm">
                <input id="timeStart" type="datetime-local">
                <input id="timeEnd" type="datetime-local">
              </form>
            </dialog>
            <button id="timeCancel" type="button"></button>

            <button id="fabPropose" type="button">Предложить</button>
            <span data-proposal-empty-cta>Предложить первое событие</span>
            <div class="mine-actions"><button class="edit" id="editProposal"
                 type="button">Изменить</button></div>
            <dialog id="propDlg">
              <button id="propCancel" type="button">Закрыть</button>
              <h3 id="propHead"></h3>
              <form id="propForm">
                <input name="name" type="hidden">
                <input name="place" type="hidden">
                <textarea name="comment" id="propComment" hidden></textarea>
                <textarea name="links" id="propLinks" hidden></textarea>
                <input name="images" id="propFiles" type="file" multiple hidden>
                <input name="videos" id="propVideo" type="file" multiple hidden>
                <input id="propMedia" type="file" multiple hidden>

                <div id="propZone">
                  <button id="propAddMedia" type="button">Медиа</button>
                  <div id="propSlides"></div>
                  <div id="propEmpty"></div>
                  <button id="propPrev" type="button"></button>
                  <button id="propNext" type="button"></button>
                   <div id="propDots"></div>
                   <span id="propPayPhoto"></span>
                 </div>
                 <div id="propMediaOrderWrap" hidden>
                   <div id="propMediaOrder"></div>
                 </div>
                <span id="propEdTitle"></span>
                <span id="propEdPlace"></span>
                <span id="propEdLinks"></span>
                <span id="propPayPill"></span>
                <div id="propDescEditable" contenteditable="true"></div>
                <div id="propDescToolbar"></div>
                <div id="propEdWhen">
                  <input data-tr-day type="hidden">
                  <input name="starts_at" data-tr-start type="hidden">
                  <input name="ends_at" data-tr-end type="hidden">
                  <span data-tr-dd></span><span data-tr-mo></span><span data-tr-yy></span>
                  <span data-tr-hh></span><span data-tr-mm></span>
                  <span data-tr-ehh></span><span data-tr-emm></span>
                </div>
                <label><input name="pay" value="0" type="radio" checked></label>
                <span data-number-stepper>
                  <button data-step="-1" type="button"></button>
                  <input id="propCapacity" name="capacity" type="number"
                         min="1" max="100" value="1">
                  <button data-step="1" type="button"></button>
                </span>
                <div id="propBar" hidden><i></i></div>
                <button id="propCancelBottom" type="button">Отмена</button>
                <button id="propSubmit" type="submit">Предложить</button>
              </form>
            </dialog>

            <dialog id="calDlg"></dialog>
            <button id="calCancel" type="button"></button>
            <a id="calGoogle"></a><a id="calIcs"></a>
            <div id="lightbox"><img><button id="lbX"></button></div>
            <button id="lbPrev"></button><button id="lbNext"></button>
            <div id="lbCount"></div>
          </body>
        """)
        page.add_script_tag(content=(APP / "static/ui.js").read_text("utf-8"))
        page.evaluate("""() => {
          window.__proposalPosts = [];
          window.__proposalCompleted = [];
          window.__proposalAborts = 0;
          window.__proposalKeepOrders = [];
          window.__proposalVideoKeepOrders = [];
          window.__xhrDelay = 0;
          document.querySelector('#editProposal').dataset.meta = JSON.stringify({
            id: 41, name: 'Сохранённое предложение', place: '', links: '',
            comment: '', starts_at: '', ends_at: '', pay: 0, capacity: 1,
            photos: [
              { id: 101, filename: 'first.webp' },
              { id: 102, filename: 'second.webp' },
            ],
            videos: [{ id: 201, filename: 'saved.mp4' }],
          });
          window.XMLHttpRequest = class {
            constructor() {
              this.readyState = 0;
              this.aborted = false;
              this.timer = null;
              this.upload = { addEventListener: (name, callback) => {
                if (name === 'progress') this.onprogress = callback;
              }};
            }
            open(method, url) {
              this.method = method;
              this.url = url;
              this.readyState = 1;
            }
            setRequestHeader() {}
            send(body) {
              window.__proposalPosts.push([this.method, this.url]);
              window.__proposalKeepOrders.push(body.get('keep_order'));
              window.__proposalVideoKeepOrders.push(body.get('keep_video_order'));
              if (this.onprogress) {
                this.onprogress({ lengthComputable: true, loaded: 1, total: 2 });
              }
              this.timer = setTimeout(() => {
                if (this.aborted) return;
                this.readyState = 4;
                this.status = 200;
                this.responseText = '{"ok":true,"moderated":false}';
                window.__proposalCompleted.push([this.method, this.url]);
                if (this.onload) this.onload();
              }, window.__xhrDelay);
            }
            abort() {
              if (this.readyState === 4 || this.aborted) return;
              this.aborted = true;
              this.readyState = 4;
              clearTimeout(this.timer);
              window.__proposalAborts += 1;
              if (this.onabort) this.onabort();
            }
          };
          window.createImageBitmap = () => new Promise(resolve => {
            setTimeout(() => resolve({ width: 20, height: 20 }), 120);
          });
        }""")
        page.add_script_tag(content=(APP / "static/guest.js").read_text("utf-8"))

        # В одной видимой ленте saved+new действительно двигаются, а POST
        # получает новый файл на первом месте через совместимый токен n0.
        page.locator("#editProposal").click()
        page.locator("#propMedia").set_input_files({
            "name": "new.png", "mimeType": "image/png", "buffer": b"new",
        })
        page.wait_for_function(
            "document.querySelectorAll('#propMediaOrder .ptile').length === 4"
        )
        self.assertEqual(
            page.locator("#propMediaOrder .ptile").evaluate_all(
                "tiles => tiles.map(tile => tile.dataset.kind)"
            ),
            ["image", "image", "image", "video"],
        )
        tiles = page.locator("#propMediaOrder .ptile")
        source = tiles.nth(2).bounding_box()
        target = tiles.nth(0).bounding_box()
        page.mouse.move(source["x"] + source["width"] / 2,
                        source["y"] + source["height"] / 2)
        page.mouse.down()
        page.mouse.move(target["x"] + 4, target["y"] + target["height"] / 2,
                        steps=5)
        page.mouse.up()
        page.wait_for_function(
            "document.querySelector('#propMediaOrder .ptile').dataset.orderKey.startsWith('image:n:')"
        )
        self.assertEqual(
            page.locator("#propMediaOrder .ptile").evaluate_all(
                "tiles => tiles.map(tile => tile.dataset.kind)"
            ),
            ["image", "image", "image", "video"],
        )
        page.locator("#propSubmit").click()
        page.wait_for_function("window.__proposalPosts.length === 1")
        self.assertEqual(page.evaluate("window.__proposalKeepOrders[0]"), "n0,s101,s102")
        self.assertEqual(page.evaluate("window.__proposalVideoKeepOrders[0]"), "s201")
        page.wait_for_function("!document.querySelector('#propDlg').open")
        page.evaluate("""() => {
          window.__proposalPosts = [];
          window.__proposalCompleted = [];
          window.__proposalAborts = 0;
          window.__proposalKeepOrders = [];
          window.__proposalVideoKeepOrders = [];
        }""")

        page.locator("#fabPropose").click()
        page.locator("#propEdTitle").evaluate("""element => {
          element.textContent = 'Большое фото';
          element.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        page.locator("#propMedia").set_input_files({
            "name": "large.png",
            "mimeType": "image/png",
            "buffer": b"x" * (1300 * 1024),
        })
        page.locator("#propSubmit").click()
        self.assertTrue(page.locator("#propSubmit").is_disabled())
        page.locator("#propCancel").click()
        page.wait_for_timeout(180)

        self.assertFalse(page.locator("#propDlg").evaluate("dialog => dialog.open"))
        self.assertFalse(page.locator("#propSubmit").is_disabled())
        self.assertEqual(page.evaluate("window.__proposalPosts"), [])

        # Теперь пропускаем подготовку и закрываем уже после старта XHR. Его
        # progress/onload не должны протечь в заново открытый экземпляр формы.
        page.locator("#fabPropose").click()
        page.locator("#propEdTitle").evaluate("""element => {
          element.textContent = 'Быстрое фото';
          element.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        page.locator("#propMedia").set_input_files({
            "name": "small.png", "mimeType": "image/png", "buffer": b"small",
        })
        page.evaluate("window.__xhrDelay = 260")
        page.locator("#propSubmit").click()
        page.wait_for_function("window.__proposalPosts.length === 1")
        self.assertFalse(page.locator("#propBar").evaluate("bar => bar.hidden"))
        self.assertEqual(
            page.locator("#propBar i").evaluate("fill => fill.style.width"), "50%"
        )

        page.locator("#propCancel").click()
        page.wait_for_function("document.querySelector('#propBar').hidden")
        page.wait_for_function("window.__proposalAborts === 1")
        self.assertTrue(page.locator("#propBar").evaluate("bar => bar.hidden"))
        self.assertEqual(
            page.locator("#propBar i").evaluate("fill => fill.style.width"), "0%"
        )
        page.locator("#fabPropose").click()
        page.wait_for_timeout(300)
        self.assertTrue(page.locator("#propDlg").evaluate("dialog => dialog.open"))
        self.assertTrue(page.locator("#propBar").evaluate("bar => bar.hidden"))
        self.assertEqual(
            page.locator("#propBar i").evaluate("fill => fill.style.width"), "0%"
        )
        self.assertEqual(page.evaluate("window.__proposalCompleted"), [])

        # Повторно открытая форма отправляет уже новый запрос; abort старой
        # загрузки не должен блокировать или завершать новую сессию.
        page.locator("#propEdTitle").evaluate("""element => {
          element.textContent = 'Повторная отправка';
          element.dispatchEvent(new Event('input', { bubbles: true }));
        }""")
        page.evaluate("window.__xhrDelay = 0")
        page.locator("#propSubmit").click()
        page.wait_for_function("window.__proposalCompleted.length === 1")
        page.wait_for_function("!document.querySelector('#propDlg').open")
        self.assertEqual(page.evaluate("window.__proposalAborts"), 1)

        # Истёкший реальный countdown публикует d4y:voting-ended: все proposal
        # CTA, включая текст пустого состояния, исчезают без reload.
        page.evaluate("""() => {
          const timer = document.createElement('div');
          timer.setAttribute('role', 'timer');
          timer.innerHTML = '<span data-countdown-label></span>' +
            '<b data-vote-countdown data-deadline="2000-01-01T00:00:00+03:00"></b>';
          document.body.appendChild(timer);
          UI.voteCountdowns(document);
        }""")
        self.assertTrue(page.locator("#fabPropose").evaluate("el => el.hidden"))
        self.assertTrue(page.locator(".mine-actions").evaluate("el => el.hidden"))
        self.assertTrue(
            page.locator("[data-proposal-empty-cta]").evaluate("el => el.hidden")
        )
        self.assertEqual(page_errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
