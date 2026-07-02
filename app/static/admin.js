// Инициализация страниц кабинета. Грузится ОДИН раз (в <head>, с defer) и
// переинициализирует нужные блоки на каждом переходе Turbo через событие
// turbo:load. Почему не inline-скрипты в шаблонах: CSP завязан на per-request
// nonce в HTTP-заголовке, а Turbo подменяет только <body> — nonce страницы B
// не совпадает с активной политикой страницы A, и инлайн-скрипты блокируются.
// Внешний файл этой проблемы лишён.
(function () {
  "use strict";

  // --- глобальные делегированные обработчики (РОВНО ОДИН РАЗ) -----------------
  // Делегирование на document переживает подмену <body> Turbo, поэтому вешаем
  // один раз. Раньше это был inline-скрипт в base.html, но под Turbo+CSP его
  // per-request nonce не переживал переход — поэтому перенесли сюда.
  if (!window.__adminGlobalInit) {
    window.__adminGlobalInit = true;
    window.copyText = function (t, btn) {
      navigator.clipboard.writeText(t).then(function () {
        var o = btn.textContent;
        btn.textContent = "Скопировано ✓";
        setTimeout(function () { btn.textContent = o; }, 1500);
      });
    };
    document.addEventListener("submit", function (e) {
      var msg = e.target.getAttribute("data-confirm");
      if (msg && !confirm(msg)) e.preventDefault();
    });
    document.addEventListener("click", function (e) {
      var b = e.target.closest("[data-copy]");
      if (b) window.copyText(b.getAttribute("data-copy"), b);
    });
    document.addEventListener("click", function (e) {
      var x = e.target.closest("[data-dismiss]");
      if (x) { var box = x.closest(".og-warn"); if (box) box.style.display = "none"; }
    });
    document.addEventListener("change", function (e) {
      if (e.target.matches("[data-autosubmit]")) e.target.form.submit();
    });
    document.addEventListener("turbo:visit", function () {
      document.documentElement.classList.add("turbo-loading");
    });
    document.addEventListener("turbo:load", function () {
      document.documentElement.classList.remove("turbo-loading");
    });
    // КЛЮЧЕВОЕ для «реактивации»: любая успешная POST-форма (сохранение свидания,
    // привязка категории, архив/удаление, правка категории) меняет содержимое
    // СПИСКОВ (Активные/Неактивные/Архив, дашборд). Turbo кэширует снимок каждой
    // посещённой страницы и при возврате (клик по вкладке/назад) показывает
    // устаревший снимок — из-за этого свидание «оставалось» в Неактивных, пока
    // не сделаешь жёсткий refresh. Мета `turbo-cache-control: no-cache` спасала
    // не всегда (снимок мог кэшироваться до перехода). Надёжнее: после КАЖДОГО
    // успешного сабмита сбрасываем весь кэш Turbo — навигация станет чуть менее
    // «мгновенной», но списки всегда свежие. Это единственный корректный вариант.
    document.addEventListener("turbo:submit-end", function (e) {
      var ok = e.detail && e.detail.success;
      if (ok && window.Turbo && Turbo.cache && typeof Turbo.cache.clear === "function") {
        Turbo.cache.clear();
      }
    });
  }

  // --- общие для всех страниц кабинета: стеклянный индикатор главной навигации -
  function initNav() {
    if (window.UI && UI.glassTabs) UI.glassTabs(document.querySelector("nav.glass-nav"));
  }

  // --- список свиданий: меню ⋯, стеклянные вкладки, переключатель вида -------
  function initDates() {
    if (window.UI && UI.cardMenu) UI.cardMenu(document);
    if (window.UI && UI.glassTabs) UI.glassTabs(document.querySelector(".tabs"));

    // на телефоне — только карточки: если из cookie пришёл список, переключаем
    // на карточки один раз (переключатель вида на мобиле скрыт из CSS)
    var dlist = document.querySelector(".dlist");
    if (dlist && window.matchMedia("(max-width: 720px)").matches &&
        !sessionStorage.getItem("forcedCards")) {
      sessionStorage.setItem("forcedCards", "1");
      document.cookie = "layout=cards;path=/admin;max-age=31536000;samesite=lax";
      if (window.Turbo && Turbo.visit) Turbo.visit(location.href, { action: "replace" });
      else location.reload();
      return;
    }

    var tog = document.getElementById("viewtog");
    if (tog && !tog.dataset.ready) {
      tog.dataset.ready = "1";
      tog.addEventListener("click", function (e) {
        var a = e.target.closest("[data-layout]");
        if (!a) return;
        e.preventDefault();
        var v = a.getAttribute("data-layout");
        document.cookie = "layout=" + v + ";path=/admin;max-age=31536000;samesite=lax";
        if (window.Turbo && typeof Turbo.visit === "function") {
          Turbo.visit(location.href, { action: "replace" });
        } else {
          location.reload();
        }
      });
    }
  }

  // --- редактор свидания: чипы дат, WYSIWYG, превью, загрузка фото, фокус -----
  function initDateForm() {
    var form = document.getElementById("dateForm");
    if (!form || !window.UI) return;

    var start = document.getElementById("fStart");
    var end = document.getElementById("fEnd");
    if (start) UI.dateChips(form, start, end);

    if (UI.richEditor) {
      UI.richEditor({
        textarea: document.getElementById("descInput"),
        editable: document.getElementById("descEditable"),
        toolbar: document.getElementById("descToolbar"),
      });
    }

    var preview = UI.editorPreview ? UI.editorPreview(form) : null;
    var hasExistingCover = form.dataset.hasCover === "1";

    var inp = document.getElementById("imagesInput");
    var photoUp = null;
    if (inp) {
      var focusesField = document.getElementById("imageFocuses");
      var focusHint = document.getElementById("newFocusHint");
      photoUp = UI.uploader({
        zone: document.getElementById("mediaZone"),
        input: inp,
        preview: document.getElementById("newTiles"),
        max: parseInt(form.dataset.slots || "0", 10),
        keptCount: function () { return 0; },
        focusable: true,
        noZoneBind: true,
        onFocus: function () {
          if (focusesField) focusesField.value = photoUp.focuses().join(",");
        },
        onChange: function (files) {
          if (focusesField) focusesField.value = photoUp.focuses().join(",");
          if (focusHint) focusHint.style.display = (files && files.length) ? "block" : "none";
          if (preview && !hasExistingCover) {
            if (files && files.length) preview.setCover(URL.createObjectURL(files[0]));
            else preview.setCover(null);
          }
        },
      });
    }

    var vinp = document.getElementById("videosInput");
    var videoUp = null;
    if (vinp) {
      videoUp = UI.uploader({
        zone: document.getElementById("mediaZone"),
        input: vinp,
        preview: document.getElementById("newVidTiles"),
        kind: "video",
        max: parseInt(form.dataset.vslots || "2", 10),
        keptCount: function () { return 0; },
        noZoneBind: true,
        onChange: function (files) {
          if (preview) preview.setVideo(form.dataset.hasVideo === "1" || (files && files.length > 0));
        },
      });
    }

    if (photoUp && videoUp && UI.mediaUploader) {
      UI.mediaUploader({
        zone: document.getElementById("mediaZone"),
        input: document.getElementById("mediaInput"),
        photo: photoUp,
        video: videoUp,
      });
    }

    var th = document.getElementById("thumbs");
    if (th && !th.dataset.ready) {
      th.dataset.ready = "1";
      var status = document.getElementById("orderStatus");
      function flashStatus(text) {
        if (!status) return;
        if (text) status.textContent = text;
        status.style.visibility = "visible";
        setTimeout(function () { status.style.visibility = "hidden"; }, 1600);
      }
      function saveOrder() {
        var order = Array.prototype.map.call(th.querySelectorAll(".thumb"),
          function (t) { return t.dataset.pid; }).join(",");
        var fd = new FormData();
        fd.append("csrf", document.body.dataset.csrf);
        fd.append("order", order);
        fetch("/admin/dates/" + th.dataset.did + "/images/reorder", { method: "POST", body: fd })
          .then(function (r) { r.ok ? flashStatus() : alert("Не удалось сохранить порядок — обнови страницу"); })
          .catch(function () { alert("Нет связи — порядок не сохранён"); });
      }
      // Блок «Медиа» теперь ТОЛЬКО меняет порядок (перетаскивание). Зона фокуса
      // выбирается кликом по картинке в окне предпросмотра — см. ниже.
      UI.sortable(th, { selector: ".thumb", onChange: saveOrder });
    }

    // Выбор зоны кадра прямо в БОЛЬШОМ окне предпросмотра — работает и на ПК, и на
    // телефоне, и для СОХРАНЁННЫХ фото, и для НОВЫХ (ещё не сохранённого свидания).
    //   • есть сохранённая обложка (.thumb) → шлём focus на сервер сразу;
    //   • иначе, если загружены новые фото → пишем зону в photoUp (уедет с формой
    //     в поле image_focuses при сохранении).
    // Раньше обработчик жил внутри блока #thumbs и на новых свиданиях (там нет
    // #thumbs) не навешивался — отсюда «на новых не редактируется», а на телефоне
    // не срабатывал вовсе. Теперь он всегда на предпросмотре.
    var pvCover = document.querySelector('[data-preview="cover"]');
    if (pvCover && !pvCover.dataset.focusReady) {
      pvCover.dataset.focusReady = "1";
      pvCover.classList.add("focus-pickable");
      function firstThumb() { return th ? th.querySelector(".thumb") : null; }
      pvCover.addEventListener("click", function (e) {
        var rect = pvCover.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        var x = Math.round(Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)) * 100);
        var y = Math.round(Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)) * 100);
        var focus = x + "% " + y + "%";
        var t = firstThumb();
        if (t) {
          // сохранённая обложка — пишем сразу на сервер
          pvCover.style.objectPosition = focus;
          var timg = t.querySelector("img");
          if (timg) { timg.style.objectPosition = focus; timg.dataset.focus = focus; }
          var fd = new FormData();
          fd.append("csrf", document.body.dataset.csrf);
          fd.append("focus", focus);
          fetch("/admin/dates/" + th.dataset.did + "/images/" + t.dataset.pid + "/focus",
                { method: "POST", body: fd })
            .then(function (r) { if (!r.ok) alert("Не удалось сохранить зону фокуса"); })
            .catch(function () { alert("Нет связи — зона не сохранена"); });
        } else if (photoUp && photoUp.hasFiles()) {
          // новое фото — зона уедет в форме (image_focuses), сохранится при сабмите
          pvCover.style.objectPosition = focus;
          photoUp.setFocus(0, focus);
        }
        // подсказка: без единого фото фокусировать нечего — молча игнорируем
      });
    }
  }

  // --- редактор категории: превью-картинка, предупреждение, порядок свиданий --
  function initCategory() {
    if (!window.UI) return;

    var ogWarn = document.getElementById("ogWarn");
    var warnDismissed = false;
    function showWarn() { if (ogWarn && !warnDismissed) ogWarn.hidden = false; }
    var wx = ogWarn && ogWarn.querySelector("[data-dismiss]");
    if (wx) wx.addEventListener("click", function () { warnDismissed = true; });
    ["og_title", "og_desc"].forEach(function (n) {
      var el = document.querySelector('[name="' + n + '"]');
      if (el) el.addEventListener("input", showWarn);
    });

    var ogZone = document.getElementById("ogZone");
    var ogInput = document.getElementById("ogInput");
    var ogImg = document.getElementById("ogPreviewImg");
    if (ogZone && ogInput && UI.uploader) {
      UI.uploader({
        zone: ogZone,
        input: ogInput,
        preview: document.getElementById("ogTiles"),
        max: 1,
        keptCount: function () { return 0; },
        kind: "image",
        onChange: function (files) {
          if (ogImg && files && files.length) {
            ogImg.src = URL.createObjectURL(files[0]);
            showWarn();
          }
        },
      });
    }

    var tb = document.getElementById("catRows");
    if (tb && !tb.dataset.ready) {
      tb.dataset.ready = "1";
      UI.sortable(tb, { selector: "tr.drag-row", onChange: function () {
        var order = Array.prototype.map.call(tb.querySelectorAll("tr.drag-row"),
          function (t) { return t.dataset.did; }).join(",");
        var fd = new FormData();
        fd.append("csrf", document.body.dataset.csrf);
        fd.append("order", order);
        fetch("/admin/categories/" + tb.dataset.cid + "/dates_reorder", { method: "POST", body: fd })
          .then(function (r) { if (!r.ok) alert("Не удалось сохранить порядок"); })
          .catch(function () { alert("Нет связи — порядок не сохранён"); });
      }});
    }
  }

  // --- профиль: автосохранение полей, отдельный сабмит аватара ----------------
  function initProfile() {
    var form = document.getElementById("profileForm");
    if (!form) return;
    var inp = document.getElementById("avatarInput");
    var note = document.getElementById("autosaveNote");

    if (inp) inp.addEventListener("change", function () {
      if (inp.files && inp.files.length) form.submit();
    });

    var timer = null;
    function flash(text, ok) {
      if (!note) return;
      note.textContent = text;
      note.classList.toggle("saved", ok === true);
      note.classList.toggle("err", ok === false);
    }
    async function save() {
      var name = form.querySelector('[name="display_name"]');
      if (name && !name.value.trim()) { flash("Имя не может быть пустым", false); return; }
      flash("Сохранение…");
      var fd = new FormData(form);
      fd.delete("avatar");
      try {
        var r = await fetch("/admin/profile", { method: "POST", body: fd, headers: { "X-Requested-With": "fetch" } });
        flash(r.ok ? "Сохранено ✓" : "Не удалось сохранить", r.ok);
      } catch (_) {
        flash("Нет связи — не сохранено", false);
      }
    }
    function schedule() {
      flash("Изменения сохраняются автоматически");
      clearTimeout(timer);
      timer = setTimeout(save, 700);
    }
    form.addEventListener("input", schedule);
    form.addEventListener("change", schedule);
  }

  // --- дашборд: QR-тоггл, скачивание SVG, системное «Поделиться» --------------
  function initDashboard() {
    var qr = document.getElementById("shareQr");
    if (!qr) return;
    var col = qr.closest(".share-qr-col");
    var toggle = document.getElementById("qrToggle");
    if (toggle && col) {
      toggle.addEventListener("click", function () {
        var open = col.classList.toggle("qr-open");
        toggle.textContent = open ? "Скрыть QR-код" : "Показать QR-код";
      });
    }
    var dl = document.getElementById("qrDownload");
    var svg = qr.querySelector("svg");
    if (dl && svg) {
      var markup = new XMLSerializer().serializeToString(svg);
      if (!/^<\?xml/.test(markup)) markup = '<?xml version="1.0" encoding="UTF-8"?>\n' + markup;
      dl.href = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(markup);
    }
    var share = document.getElementById("qrShare");
    if (share) {
      share.addEventListener("click", function () {
        var url = share.getAttribute("data-url");
        var title = share.getAttribute("data-name") || "Подборка свиданий";
        if (navigator.share) {
          navigator.share({ title: title, text: "Тебя ждёт сюрприз ♥", url: url }).catch(function () {});
        } else if (navigator.clipboard) {
          navigator.clipboard.writeText(url).then(function () {
            var o = share.textContent;
            share.textContent = "Ссылка скопирована ✓";
            setTimeout(function () { share.textContent = o; }, 1600);
          });
        }
      });
    }
  }

  // --- главная: лента свиданий комьюнити (бесконечный скролл + виджет) --------
  function initCommunity() {
    var feed = document.getElementById("communityFeed");
    if (!feed || feed.dataset.ready) return;
    feed.dataset.ready = "1";
    var baseUrl = feed.getAttribute("data-feed-url") || "/admin/community";
    var emptyEl = document.getElementById("cfeedEmpty");
    var endEl = document.getElementById("cfeedEnd");
    var dlg = document.getElementById("communityDlg");
    var cwidBody = document.getElementById("cwidBody");
    var cwidClose = document.getElementById("cwidClose");

    var loading = false, done = false, loadedAny = false;
    var io = null;

    function currentCursor() {
      var s = feed.querySelector(".cfeed-sentinel");
      return s ? s.getAttribute("data-next-cursor") : null;
    }

    function load() {
      if (loading || done) return;
      loading = true;
      var cursor = currentCursor();       // null на первой странице
      var sentinel = feed.querySelector(".cfeed-sentinel");
      if (sentinel) sentinel.remove();     // старый маркер заменяем свежей страницей
      var url = baseUrl + (cursor ? "?cursor=" + encodeURIComponent(cursor) : "");
      fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (html) {
          feed.insertAdjacentHTML("beforeend", html.trim());
          var hasCards = feed.querySelector(".cfeed-card");
          if (hasCards) loadedAny = true;
          if (!loadedAny && emptyEl) emptyEl.hidden = false;
          // нет нового маркера курсора → страниц больше нет
          if (!feed.querySelector(".cfeed-sentinel")) {
            done = true;
            if (loadedAny && endEl) endEl.hidden = false;
          } else {
            observeSentinel();
          }
          loading = false;
        })
        .catch(function () { loading = false; });
    }

    function observeSentinel() {
      var sentinel = feed.querySelector(".cfeed-sentinel");
      if (!sentinel || !("IntersectionObserver" in window)) return;
      if (io) io.disconnect();
      io = new IntersectionObserver(function (entries) {
        if (entries.some(function (e) { return e.isIntersecting; })) load();
      }, { rootMargin: "300px" });
      io.observe(sentinel);
    }

    // открыть/закрыть виджет свидания
    function openWidget(id) {
      if (!dlg || !cwidBody) return;
      cwidBody.innerHTML = '<p class="muted" style="text-align:center;padding:30px">Загружаю…</p>';
      if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
      fetch("/admin/community/date/" + encodeURIComponent(id),
            { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (html) { cwidBody.innerHTML = html; })
        .catch(function () {
          cwidBody.innerHTML = '<p class="muted" style="text-align:center;padding:30px">Не удалось открыть свидание</p>';
        });
    }
    function closeWidget() {
      if (!dlg) return;
      if (typeof dlg.close === "function" && dlg.open) dlg.close();
      else dlg.removeAttribute("open");
      if (cwidBody) cwidBody.innerHTML = "";
    }

    // клик по карточке ленты → открыть виджет (пилюля владельца — обычная ссылка)
    feed.addEventListener("click", function (e) {
      if (e.target.closest("[data-stop]")) return;      // клик по профилю владельца
      var card = e.target.closest(".cfeed-card");
      if (card) openWidget(card.getAttribute("data-widget"));
    });
    feed.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " ") return;
      var card = e.target.closest(".cfeed-card");
      if (card) { e.preventDefault(); openWidget(card.getAttribute("data-widget")); }
    });

    if (cwidClose) cwidClose.addEventListener("click", closeWidget);
    if (dlg) dlg.addEventListener("click", function (e) {
      if (e.target === dlg) closeWidget();               // клик по подложке
    });

    // «Добавить себе» внутри виджета (делегированно — контент подгружается)
    if (cwidBody) cwidBody.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-add]");
      if (!btn) return;
      btn.disabled = true;
      var old = btn.textContent;
      btn.textContent = "Добавляю…";
      fetch(btn.getAttribute("data-add"),
            { method: "POST", credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (res.ok && res.j && res.j.ok) {
            btn.textContent = "Добавлено ♥";
            toast("Свидание добавлено в твою коллекцию ♥");
            setTimeout(closeWidget, 900);
          } else {
            btn.disabled = false; btn.textContent = old;
            toast((res.j && res.j.detail) || "Не удалось добавить");
          }
        })
        .catch(function () { btn.disabled = false; btn.textContent = old; toast("Нет связи"); });
    });

    // простой тост (на главной нет гостевого toast'а — рисуем свой)
    function toast(msg) {
      var t = document.getElementById("adminToast");
      if (!t) {
        t = document.createElement("div");
        t.id = "adminToast"; t.className = "admin-toast";
        document.body.appendChild(t);
      }
      t.textContent = msg;
      t.classList.add("show");
      clearTimeout(t._h);
      t._h = setTimeout(function () { t.classList.remove("show"); }, 2600);
    }

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeWidget();
    });

    load();     // первая страница
  }

  // Запускаем инициализаторы на каждой загрузке (в т.ч. после Turbo-перехода).
  // Каждый сам проверяет наличие своих элементов, поэтому безопасно звать все.
  function initPage() {
    initNav();
    initDates();
    initDateForm();
    initCategory();
    initProfile();
    initDashboard();
    initCommunity();
  }

  // Turbo вызывает turbo:load и при первой загрузке, и после каждого перехода.
  // Если Turbo нет (или ещё не инициализировался) — инициализируем сами один раз.
  document.addEventListener("turbo:load", initPage);
  if (!window.Turbo) {
    if (document.readyState !== "loading") initPage();
    else document.addEventListener("DOMContentLoaded", initPage, { once: true });
  }
})();
