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
    // «Пройти обучение заново» (профиль): снимаем флаг и запускаем тур. Сам тур
    // живёт в tour.js и вешает window.d4yStartTour.
    document.addEventListener("click", function (e) {
      var tourButton = e.target.closest("[data-tour-start]");
      if (tourButton && window.d4yStartTour) {
        e.preventDefault();
        window.d4yStartTour(tourButton.getAttribute("data-tour-start") || "dashboard");
      }
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
    // ГЛАВНОЕ для «реактивации» (баг: реактивированное свидание оставалось в
    // «Неактивных» до жёсткого refresh). Turbo.cache.clear() выше чистит СНИМКИ
    // страниц; но у списков стоит `turbo-cache-control: no-cache`, а стойкий
    // источник устаревшего вида — ОТДЕЛЬНЫЙ кэш префетча (turbo-prefetch): при
    // наведении на вкладку Turbo заранее тянет её и запоминает, а clear() его не
    // трогает. Клик после мутации отдавал этот префетч-снимок. Префетч отключён
    // на уровне мета `turbo-prefetch=false` в base.html — навигация остаётся
    // мгновенной (Turbo Drive), а «мимо-clear()» кэша больше нет.
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

  // --- редактор свидания: РЕДАКТИРУЕМОЕ ПРЕВЬЮ (click-to-edit + галерея) ------
  // Вся правка идёт прямо по карточке-превью. Видимые поля — contenteditable /
  // кастомные виджеты, значения пишутся в скрытые input формы (name/place/
  // starts_at/ends_at/comment/links/image_focuses) — бэкенд не меняется.
  function initDateForm() {
    var form = document.getElementById("dateForm");
    if (!form || !window.UI || form.dataset.edReady) return;
    if (!document.getElementById("edCard")) return;   // это точно новый редактор
    form.dataset.edReady = "1";
    var did = form.dataset.did || "";

    // --- инлайн-текст: название, место, ссылки (однострочные/списком) ----------
    if (UI.inlineEdit) {
      UI.inlineEdit({ view: document.getElementById("edTitle"),
                      field: form.querySelector('[name="name"]') });
      UI.inlineEdit({ view: document.getElementById("edPlace"),
                      field: form.querySelector('[name="place"]') });
      UI.inlineEdit({ view: document.getElementById("edLinks"),
                      field: document.getElementById("linksInput"), multiline: true });
    }

    // --- детали: тот же WYSIWYG, но встроенный в превью -------------------------
    var edDesc = document.getElementById("edDesc");
    var descTb = document.getElementById("descToolbar");
    if (UI.richEditor) {
      UI.richEditor({
        textarea: document.getElementById("descInput"),
        editable: edDesc,
        toolbar: descTb,
      });
    }
    // тулбар разметки показываем, только когда правишь детали (иначе он лишний)
    if (edDesc && descTb) {
      edDesc.addEventListener("focus", function () { descTb.hidden = false; });
      edDesc.addEventListener("blur", function () {
        setTimeout(function () {
          if (!descTb.contains(document.activeElement)) descTb.hidden = true;
        }, 200);
      });
      // клик по кнопке тулбара не должен прятать его (mousedown до blur)
      descTb.addEventListener("mousedown", function (e) { e.preventDefault(); });
    }

    // --- время: день + ЧЧ:ММ–ЧЧ:ММ ---------------------------------------------
    if (UI.timeRange) UI.timeRange(document.getElementById("edWhen"));

    // --- оплата: отражаем модификатор пилюлей на карточке ----------------------
    // D4: если есть фото — бейдж ПОВЕРХ фото (.ed-gallery-pay); если фото нет —
    // в заголовке (.pay). Видимость нужного места ставит галерея (galleryHasMedia).
    var payPill = document.querySelector('.pcard [data-preview="pay"]');          // в заголовке
    var payPhoto = document.querySelector('.pcard [data-preview="pay-photo"]');   // на фото
    var PAY = { "1": "💸 50/50", "2": "👌 Я плачу", "3": "🫵 Ты платишь" };
    var galleryHasMedia = !!(document.querySelector("#edSlides .ed-slide"));
    function syncPay() {
      var ch = form.querySelector('[data-bind="pay"]:checked');
      var v = ch ? ch.value : "0";
      var label = PAY[v] || "";
      var onPhoto = galleryHasMedia && !!label;
      if (payPhoto) {
        if (onPhoto) { payPhoto.textContent = label; payPhoto.hidden = false; }
        else payPhoto.hidden = true;
      }
      if (payPill) {
        if (label && !onPhoto) { payPill.textContent = label; payPill.hidden = false; }
        else payPill.hidden = true;
      }
    }
    form.querySelectorAll('[data-bind="pay"]').forEach(function (r) {
      r.addEventListener("change", syncPay);
    });
    syncPay();

    // --- ГАЛЕРЕЯ: существующие фото/видео + новые (кнопка +), листание, кадр ----
    // Галерея зовёт onMediaChange(hasMedia), когда меняется наличие слайдов —
    // чтобы модификатор перескакивал фото↔заголовок.
    initEdGallery(form, did, function (hasMedia) {
      galleryHasMedia = hasMedia; syncPay();
    });
  }

  // Галерея редактируемого превью. Слайды двух видов:
  //   • сохранённые: <div.ed-slide data-pid> (удаление/фокус — на сервер сразу)
  //   • новые:       <div.ed-slide.new data-idx> (уезжают с формой; фокус → image_focuses)
  function initEdGallery(form, did, onMediaChange) {
    var gallery = document.getElementById("edGallery");
    var slidesEl = document.getElementById("edSlides");
    if (!gallery || !slidesEl) return;
    var emptyEl = document.getElementById("edEmpty");
    var prev = document.getElementById("edPrev"), next = document.getElementById("edNext");
    var dots = document.getElementById("edDots"), focusHint = document.getElementById("edFocusHint");
    var slots = parseInt(form.dataset.slots || "0", 10);
    var vslots = parseInt(form.dataset.vslots || "2", 10);
    var focusesField = document.getElementById("imageFocuses");
    var cur = 0;

    // загрузчики: только собирают файлы в скрытые input (превью рисуем сами)
    // slots/vslots уже «оставшиеся» (сервер вычел сохранённые), поэтому keptCount=0
    var photoUp = UI.uploader({
      zone: gallery, input: document.getElementById("imagesInput"),
      preview: document.createElement("div"),        // свой контейнер не показываем
      max: slots || 5, keptCount: function () { return 0; },
      focusable: true, noZoneBind: true,
      onError: adminToast,
      onChange: function () { syncFocuses(); renderNew(); },
      onFocus: syncFocuses,
    });
    var videoUp = UI.uploader({
      zone: gallery, input: document.getElementById("videosInput"),
      preview: document.createElement("div"), kind: "video",
      max: vslots || 2, keptCount: function () { return 0; },
      noZoneBind: true, onError: adminToast, onChange: function () { renderNew(); },
    });
    UI.mediaUploader({ zone: gallery, input: document.getElementById("mediaInput"),
                       photo: photoUp, video: videoUp, onError: adminToast });

    function syncFocuses() { if (focusesField) focusesField.value = photoUp.focuses().join(","); }

    function adminToast(msg) {
      var t = document.getElementById("adminToast");
      if (!t) { t = document.createElement("div"); t.id = "adminToast"; t.className = "admin-toast"; document.body.appendChild(t); }
      t.textContent = msg; t.classList.add("show");
      clearTimeout(t._h); t._h = setTimeout(function () { t.classList.remove("show"); }, 2600);
    }

    // перерисовать слайды новых фото/видео (сохранённые уже в DOM с сервера)
    function renderNew() {
      slidesEl.querySelectorAll(".ed-slide.new").forEach(function (s) { s.remove(); });
      photoUp.files().forEach(function (f, idx) {
        var url = URL.createObjectURL(f);
        var s = document.createElement("div");
        s.className = "ed-slide new"; s.dataset.kind = "image"; s.dataset.idx = idx;
        s.dataset.focus = photoUp.focuses()[idx] || "50% 50%";
        s.innerHTML = '<img alt="" draggable="false"><button type="button" class="ed-slide-rm" aria-label="Убрать">✕</button>';
        var img = s.querySelector("img"); img.src = url; img.style.objectPosition = s.dataset.focus;
        s.querySelector(".ed-slide-rm").addEventListener("click", function (e) {
          e.stopPropagation();
          var files = photoUp.files(); files.splice(idx, 1);
          photoUp.clear(); photoUp.addFiles(files);   // пересобрать без idx
        });
        slidesEl.appendChild(s);
      });
      videoUp.files().forEach(function (f, idx) {
        var s = document.createElement("div");
        s.className = "ed-slide new"; s.dataset.kind = "video"; s.dataset.vidx = idx;
        s.innerHTML = '<video muted playsinline preload="metadata"></video><span class="ed-vtag">🎬</span><button type="button" class="ed-slide-rm" aria-label="Убрать">✕</button>';
        s.querySelector("video").src = URL.createObjectURL(f);
        s.querySelector(".ed-slide-rm").addEventListener("click", function (e) {
          e.stopPropagation();
          var files = videoUp.files(); files.splice(idx, 1);
          videoUp.clear(); videoUp.addFiles(files);
        });
        slidesEl.appendChild(s);
      });
      var n = slidesEl.querySelectorAll(".ed-slide").length;
      if (cur >= n) cur = Math.max(0, n - 1);
      layout();
    }

    // добавить фото/видео: открываем общий media-input (фото И видео)
    function openPicker() { document.getElementById("mediaInput").click(); }
    var addFirst = document.getElementById("edAddFirst");
    if (addFirst) addFirst.addEventListener("click", openPicker);

    // удаление сохранённого фото/видео — сразу на сервер (как было в старом редакторе)
    slidesEl.addEventListener("click", function (e) {
      var rmP = e.target.closest("[data-rm-saved]");
      var rmV = e.target.closest("[data-rm-video]");
      if (rmP) {
        e.stopPropagation();
        if (!confirm("Удалить фото?")) return;
        var pid = rmP.getAttribute("data-rm-saved");
        postForm("/admin/dates/" + did + "/images/" + pid + "/delete").then(function (ok) {
          if (ok) { var s = rmP.closest(".ed-slide"); if (s) s.remove(); renderNew(); }
          else adminToast("Не удалось удалить фото");
        });
      } else if (rmV) {
        e.stopPropagation();
        if (!confirm("Удалить видео?")) return;
        var vid = rmV.getAttribute("data-rm-video");
        postForm("/admin/dates/" + did + "/videos/" + vid + "/delete").then(function (ok) {
          if (ok) { var s = rmV.closest(".ed-slide"); if (s) s.remove(); renderNew(); }
          else adminToast("Не удалось удалить видео");
        });
      }
    });

    function postForm(url, extra) {
      var fd = new FormData();
      fd.append("csrf", document.body.dataset.csrf);
      if (extra) Object.keys(extra).forEach(function (k) { fd.append(k, extra[k]); });
      return fetch(url, { method: "POST", body: fd })
        .then(function (r) { return r.ok; }).catch(function () { return false; });
    }

    // --- листание + кнопки + по краям + точки ----------------------------------
    function slides() { return Array.prototype.slice.call(slidesEl.querySelectorAll(".ed-slide")); }
    function canAddMore() {
      // slots/vslots — «оставшиеся» места (сервер уже вычел сохранённые)
      return photoUp.files().length < (slots || 5)
          || videoUp.files().length < (vslots || 2);
    }
    var lastHasMedia = null;
    function layout() {
      var all = slides();
      var n = all.length;
      if (emptyEl) emptyEl.hidden = n > 0;
      // D4: сообщаем наверх о наличии медиа — модификатор прыгает фото↔заголовок
      if (onMediaChange && (n > 0) !== lastHasMedia) { lastHasMedia = n > 0; onMediaChange(n > 0); }
      all.forEach(function (s, i) { s.style.display = (i === cur) ? "" : "none"; });
      // точки
      if (dots) {
        dots.innerHTML = "";
        if (n > 1) for (var k = 0; k < n; k++) {
          var d = document.createElement("i"); if (k === cur) d.className = "on"; dots.appendChild(d);
        }
      }
      // Навигация краёв: если есть слайд слева — стрелка ‹, иначе (и если можно
      // добавить) — кнопка + (вставить перед). Аналогично справа.
      var addMore = canAddMore();
      setEdge(prev, cur > 0, addMore, "prev");
      setEdge(next, cur < n - 1, addMore, "next");
      if (focusHint) focusHint.hidden = !(all[cur] && all[cur].dataset.kind === "image");
      bindFocusDrag(all[cur]);
    }
    function setEdge(btn, canNav, canAdd, dir) {
      if (!btn) return;
      if (canNav) { btn.hidden = false; btn.classList.remove("as-add"); btn.textContent = dir === "prev" ? "‹" : "›"; }
      else if (canAdd) { btn.hidden = false; btn.classList.add("as-add"); btn.textContent = "+"; }
      else { btn.hidden = true; }
    }
    if (prev) prev.addEventListener("click", function () {
      if (prev.classList.contains("as-add")) openPicker();
      else { cur = Math.max(0, cur - 1); layout(); }
    });
    if (next) next.addEventListener("click", function () {
      if (next.classList.contains("as-add")) openPicker();
      else { cur = Math.min(slides().length - 1, cur + 1); layout(); }
    });

    // свайп на телефоне (как на гостевой)
    var tx = null;
    gallery.addEventListener("touchstart", function (e) { tx = e.touches[0].clientX; }, { passive: true });
    gallery.addEventListener("touchend", function (e) {
      if (tx === null) return;
      var dx = e.changedTouches[0].clientX - tx; tx = null;
      if (Math.abs(dx) > 44) {
        if (dx < 0) cur = Math.min(slides().length - 1, cur + 1);
        else cur = Math.max(0, cur - 1);
        layout();
      }
    }, { passive: true });

    // --- зона кадра ПЕРЕТАСКИВАНИЕМ (клик убрали, #14) --------------------------
    var dragBound = null;
    function bindFocusDrag(slide) {
      if (!slide || slide.dataset.kind !== "image" || slide === dragBound) return;
      dragBound = slide;
      var img = slide.querySelector("img");
      if (!img || img.dataset.dragReady) return;
      img.dataset.dragReady = "1";
      var dragging = false;
      function apply(e) {
        var rect = img.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        var cx = (e.touches ? e.touches[0].clientX : e.clientX);
        var cy = (e.touches ? e.touches[0].clientY : e.clientY);
        var x = Math.round(Math.min(1, Math.max(0, (cx - rect.left) / rect.width)) * 100);
        var y = Math.round(Math.min(1, Math.max(0, (cy - rect.top) / rect.height)) * 100);
        var focus = x + "% " + y + "%";
        img.style.objectPosition = focus; slide.dataset.focus = focus;
        return focus;
      }
      function save(focus) {
        if (!focus) return;
        if (slide.dataset.pid) {
          postForm("/admin/dates/" + did + "/images/" + slide.dataset.pid + "/focus",
                   { focus: focus }).then(function (ok) { if (!ok) adminToast("Зона не сохранена"); });
        } else if (slide.classList.contains("new")) {
          photoUp.setFocus(parseInt(slide.dataset.idx, 10), focus); syncFocuses();
        }
      }
      img.addEventListener("pointerdown", function (e) {
        dragging = true; img.setPointerCapture && img.setPointerCapture(e.pointerId);
        slide.classList.add("dragging"); apply(e); e.preventDefault();
      });
      img.addEventListener("pointermove", function (e) { if (dragging) apply(e); });
      function end() { if (!dragging) return; dragging = false; slide.classList.remove("dragging"); save(slide.dataset.focus); }
      img.addEventListener("pointerup", end);
      img.addEventListener("pointercancel", end);
    }

    layout();
  }

  // --- редактор категории: превью-картинка, предупреждение, порядок свиданий --
  function initCategory() {
    if (!window.UI) return;

    // WYSIWYG-редактор описания категории — тот же, что у комментария свидания
    var catEditable = document.getElementById("catDescEditable");
    if (catEditable && UI.richEditor && !catEditable.dataset.ready) {
      catEditable.dataset.ready = "1";
      UI.richEditor({
        textarea: document.getElementById("catDescInput"),
        editable: catEditable,
        toolbar: document.getElementById("catDescToolbar"),
      });
    }

    var ogWarn = document.getElementById("ogWarn");
    var warnDismissed = false;
    // Показываем предупреждение «превью изменено» ТОЛЬКО когда значение реально
    // отличается от исходного (серверного). Раньше оно вылезало на любой input —
    // даже когда пользователь ничего не менял. Сравниваем с defaultValue.
    var ogChanged = { img: false };
    var titleField = document.getElementById("ogTitleField");
    var descField = document.getElementById("ogDescField");
    // исходные значения фиксируем СЕЙЧАС (defaultValue у hidden-инпутов ведёт себя
    // неодинаково в движках — надёжнее свой снимок).
    var ogBase = { title: titleField ? titleField.value : "",
                   desc: descField ? descField.value : "" };
    function recompute() {
      if (!ogWarn || warnDismissed) return;
      var changed = ogChanged.img
        || (titleField && titleField.value !== ogBase.title)
        || (descField && descField.value !== ogBase.desc);
      ogWarn.hidden = !changed;
    }
    var wx = ogWarn && ogWarn.querySelector("[data-dismiss]");
    if (wx) wx.addEventListener("click", function () { warnDismissed = true; });

    // #12: превью ссылки редактируется прямо на месте — заголовок/описание
    // click-to-edit пишут в скрытые og_title/og_desc; клик по картинке открывает
    // выбор своей. Отдельных полей больше нет.
    var ogPreview = document.getElementById("ogPreview");
    if (ogPreview && !ogPreview.dataset.ready) {
      ogPreview.dataset.ready = "1";
      if (UI.inlineEdit) {
        UI.inlineEdit({ view: document.getElementById("ogTitleEd"), field: titleField,
                        onChange: recompute });
        UI.inlineEdit({ view: document.getElementById("ogDescEd"), field: descField,
                        onChange: recompute });
      }
      var ogInput = document.getElementById("ogInput");
      var ogImg = document.getElementById("ogPreviewImg");
      var ogPick = document.getElementById("ogImgPick");
      var focusHint = document.getElementById("ogFocusHint");
      var cid = ogPreview.dataset.cid;
      // есть ли своя картинка (её можно двигать/кропать). Меняется, когда
      // пользователь выбирает новую (новая ещё не сохранена — двигать нельзя до сабмита).
      var hasSavedImage = ogPreview.dataset.hasImage === "1";

      // --- смена картинки: клик открывает выбор файла --------------------------
      // Клик именно по бейджу/подсказке (или короткий тап), а не по завершению
      // перетаскивания кадра — иначе каждый drag открывал бы диалог выбора файла.
      var dragMoved = false;
      if (ogPick && ogInput) {
        ogPick.addEventListener("click", function () {
          if (dragMoved) { dragMoved = false; return; }
          ogInput.click();
        });
        ogInput.addEventListener("change", function () {
          if (ogInput.files && ogInput.files.length && ogImg) {
            ogImg.src = URL.createObjectURL(ogInput.files[0]);
            ogImg.style.objectPosition = "50% 50%";
            ogChanged.img = true;
            // новая картинка ещё не на сервере — двигать кадр можно после сохранения
            hasSavedImage = false;
            ogPreview.classList.remove("has-image");
            if (focusHint) focusHint.hidden = true;
            recompute();
          }
        });
      }

      // --- перетаскивание кадра своей картинки (WYSIWYG-кроп og:image) ----------
      // Только для УЖЕ сохранённой картинки: точка фокуса летит на сервер, og:image
      // пересобирается по ней. Для только что выбранной (несохранённой) — сперва
      // «Сохранить», у формы нет поля фокуса до записи файла.
      if (ogImg && hasSavedImage && cid) {
        var dragging = false, curFocus = ogImg.style.objectPosition || "50% 50%";
        function applyFocus(e) {
          var rect = ogImg.getBoundingClientRect();
          if (!rect.width || !rect.height) return;
          var px = (e.touches ? e.touches[0].clientX : e.clientX);
          var py = (e.touches ? e.touches[0].clientY : e.clientY);
          var x = Math.round(Math.min(1, Math.max(0, (px - rect.left) / rect.width)) * 100);
          var y = Math.round(Math.min(1, Math.max(0, (py - rect.top) / rect.height)) * 100);
          curFocus = x + "% " + y + "%";
          ogImg.style.objectPosition = curFocus;
        }
        ogImg.addEventListener("pointerdown", function (e) {
          dragging = true; dragMoved = false;
          ogImg.setPointerCapture && ogImg.setPointerCapture(e.pointerId);
          ogPreview.classList.add("dragging"); applyFocus(e); e.preventDefault();
        });
        ogImg.addEventListener("pointermove", function (e) {
          if (dragging) { dragMoved = true; applyFocus(e); }
        });
        function endDrag() {
          if (!dragging) return;
          dragging = false; ogPreview.classList.remove("dragging");
          var fd = new FormData();
          fd.append("csrf", document.body.dataset.csrf);
          fd.append("focus", curFocus);
          fetch("/admin/categories/" + cid + "/og_focus", { method: "POST", body: fd })
            .then(function (r) { if (!r.ok) throw 0; })
            .catch(function () { /* не сохранилось — тихо, кадр вернётся при перезагрузке */ });
        }
        ogImg.addEventListener("pointerup", endDrag);
        ogImg.addEventListener("pointercancel", endDrag);
      }
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
    // Настройки оформления визуально вынесены в отдельную карточку, но связаны
    // с profileForm через атрибут form=. Их события не всплывают внутрь формы.
    document.querySelectorAll('[form="profileForm"]').forEach(function (control) {
      control.addEventListener("input", schedule);
      control.addEventListener("change", schedule);
      if (control.name === "cursor_effects") {
        control.addEventListener("change", function () {
          document.body.setAttribute("data-ink-interactive", control.checked ? "1" : "0");
        });
      }
    });
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
