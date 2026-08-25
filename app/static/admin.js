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
    // КЛЮЧЕВОЕ для «реактивации»: любая успешная POST-форма (сохранение события,
    // привязка категории, архив/удаление, правка категории) меняет содержимое
    // СПИСКОВ (Активные/Архив, дашборд). Turbo кэширует снимок каждой
    // посещённой страницы и при возврате может показать устаревшее состояние.
    // Мета `turbo-cache-control: no-cache` спасала
    // не всегда (снимок мог кэшироваться до перехода). Надёжнее: после КАЖДОГО
    // успешного сабмита сбрасываем весь кэш Turbo — навигация станет чуть менее
    // «мгновенной», но списки всегда свежие. Это единственный корректный вариант.
    // Turbo.cache.clear() выше чистит СНИМКИ страниц; но у списков стоит
    // `turbo-cache-control: no-cache`, а стойкий
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

  // Оформление кабинета хранится в профиле. Turbo заменяет <body>, но не
  // атрибуты <html>, поэтому на каждом переходе синхронизируем оба узла.
  // В профиле тема применяется сразу, не дожидаясь фонового автосохранения.
  function applyAdminSkin(skin, source, event) {
    if (skin !== "romantic") skin = "friends";
    if (window.d4yAppearance) {
      if (source && typeof window.d4yAppearance.animateSkin === "function") {
        window.d4yAppearance.animateSkin(skin, source, event);
      } else if (typeof window.d4yAppearance.applySkin === "function") {
        window.d4yAppearance.applySkin(skin);
      }
      return;
    }
    var changed = document.documentElement.dataset.skin !== skin;
    document.documentElement.dataset.skin = skin;
    document.body.dataset.skin = skin;
    if (changed) {
      document.dispatchEvent(new CustomEvent("d4y:skinchange", {
        detail: { skin: skin }
      }));
    }
  }

  function initSkinSettings() {
    applyAdminSkin(document.body.dataset.skin || "friends");

    var profilePick = document.querySelector('[name="admin_skin"]:checked');
    if (profilePick) {
      var profileGroup = profilePick.closest(".skin-pick");
      if (profileGroup && !profileGroup.dataset.skinReady) {
        profileGroup.dataset.skinReady = "1";
        profileGroup.addEventListener("change", function (event) {
          if (event.target.matches('[name="admin_skin"]')) {
            applyAdminSkin(
              event.target.value,
              event.target.closest(".skin-option") || event.target,
              event
            );
          }
        });
      }
    }
  }

  // --- список событий: меню ⋯, стеклянные вкладки, переключатель вида -------
  function initDates() {
    if (window.UI && UI.cardMenu) UI.cardMenu(document);
    if (window.UI && UI.glassTabs) UI.glassTabs(document.querySelector(".tabs"));

    // На телефоне — только карточки: если сервер всё же отдал list-разметку,
    // каждый раз исправляем cookie и перезагружаем страницу. После перезагрузки
    // `.dlist` исчезнет, поэтому отдельный sessionStorage-флаг от цикла не нужен.
    // Он, напротив, ломал сценарий mobile → desktop/list → mobile в одной вкладке.
    var dlist = document.querySelector(".dlist");
    if (dlist && window.matchMedia(
          "(max-width: 720px), (max-width: 950px) and (max-height: 600px) and (pointer: coarse)"
        ).matches) {
      document.cookie = "layout=cards;path=/admin;max-age=31536000;samesite=lax";
      if (window.Turbo && Turbo.visit) Turbo.visit(location.href, { action: "replace" });
      else location.reload();
      return;
    }

    var bulkForm = document.getElementById("datesBulkForm");
    if (bulkForm && !bulkForm.dataset.ready) {
      bulkForm.dataset.ready = "1";
      var bulkAll = bulkForm.querySelector("[data-bulk-all]");
      var bulkCount = bulkForm.querySelector("[data-bulk-count]");
      var bulkItems = Array.from(document.querySelectorAll("[data-bulk-item]"));
      var bulkActions = Array.from(bulkForm.querySelectorAll('button[name="action"]'));

      function syncBulkSelection() {
        var selected = bulkItems.filter(function (item) { return item.checked; }).length;
        if (bulkCount) bulkCount.textContent = "Выбрано: " + selected;
        if (bulkAll) {
          bulkAll.checked = bulkItems.length > 0 && selected === bulkItems.length;
          bulkAll.indeterminate = selected > 0 && selected < bulkItems.length;
        }
        bulkActions.forEach(function (button) { button.disabled = selected === 0; });
        bulkItems.forEach(function (item) {
          var row = item.closest(".drow");
          if (row) row.classList.toggle("is-selected", item.checked);
        });
      }

      if (bulkAll) bulkAll.addEventListener("change", function () {
        bulkItems.forEach(function (item) { item.checked = bulkAll.checked; });
        syncBulkSelection();
      });
      bulkItems.forEach(function (item) {
        item.addEventListener("change", syncBulkSelection);
      });
      syncBulkSelection();
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

  // --- редактор события: РЕДАКТИРУЕМОЕ ПРЕВЬЮ (click-to-edit + галерея) ------
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
    slidesEl.addEventListener("click", async function (e) {
      var rmP = e.target.closest("[data-rm-saved]");
      var rmV = e.target.closest("[data-rm-video]");
      if (rmP) {
        e.stopPropagation();
        if (!await window.d4yConfirm("Удалить фото?", { danger: true })) return;
        var pid = rmP.getAttribute("data-rm-saved");
        postForm("/admin/dates/" + did + "/images/" + pid + "/delete").then(function (ok) {
          if (ok) { var s = rmP.closest(".ed-slide"); if (s) s.remove(); renderNew(); }
          else adminToast("Не удалось удалить фото");
        });
      } else if (rmV) {
        e.stopPropagation();
        if (!await window.d4yConfirm("Удалить видео?", { danger: true })) return;
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
    var tx = null, touchEditsFocus = false;
    gallery.addEventListener("touchstart", function (e) {
      tx = e.touches[0].clientX;
      // На фото один палец двигает зону кадра. Не трактуем тот же жест как
      // перелистывание галереи; перейти к соседнему слайду можно стрелками.
      touchEditsFocus = !!e.target.closest('.ed-slide[data-kind="image"] img');
    }, { passive: true });
    gallery.addEventListener("touchend", function (e) {
      if (tx === null) return;
      var dx = e.changedTouches[0].clientX - tx;
      tx = null;
      if (touchEditsFocus) { touchEditsFocus = false; return; }
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
      img.addEventListener("pointermove", function (e) {
        if (!dragging) return;
        apply(e);
        e.preventDefault();
      });
      function end(e) {
        if (!dragging) return;
        dragging = false;
        slide.classList.remove("dragging");
        save(slide.dataset.focus);
        if (e) {
          if (img.hasPointerCapture && img.hasPointerCapture(e.pointerId)) {
            img.releasePointerCapture(e.pointerId);
          }
          e.preventDefault();
        }
      }
      img.addEventListener("pointerup", end);
      img.addEventListener("pointercancel", end);
      // Дополнительная защита для Safari/iOS: совместимый click может прийти
      // уже после pointerup, отдельным событием. Он не должен всплыть к picker.
      img.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
      });
    }

    layout();
  }

  // --- редактор категории: превью-картинка, предупреждение, порядок событий --
  function initCategory() {
    if (!window.UI) return;

    // WYSIWYG-редактор описания категории — тот же, что у комментария события
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
    var categorySkinField = document.querySelector('[name="category_skin"]:checked');
    var ogBase = { title: titleField ? titleField.value : "",
                   desc: descField ? descField.value : "",
                   skin: categorySkinField ? categorySkinField.value : "" };
    function recompute() {
      if (!ogWarn || warnDismissed) return;
      var selectedSkin = document.querySelector('[name="category_skin"]:checked');
      var changed = ogChanged.img
        || (titleField && titleField.value !== ogBase.title)
        || (descField && descField.value !== ogBase.desc)
        || (selectedSkin && selectedSkin.value !== ogBase.skin);
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
      var focusField = document.getElementById("ogFocusField");
      var focusStatus = document.getElementById("ogFocusStatus");
      var cid = ogPreview.dataset.cid;
      // Есть ли уже сохранённая custom-картинка. Новую картинку тоже можно сразу
      // двигать: её focus уйдёт вместе с файлом через основную форму.
      var hasSavedImage = ogPreview.dataset.hasImage === "1";
      var savedImageName = ogPreview.dataset.imageName || "";
      var initialFocus = (focusField && focusField.value) ||
        (ogImg && ogImg.style.objectPosition) || "50% 50%";
      var curFocus = initialFocus;
      var lastSavedFocus = initialFocus;
      var imageFileChanged = false;
      var focusChanged = false;
      var focusStatusTimer = null;
      var focusSaveChain = Promise.resolve();
      var focusSaveGeneration = 0;

      function syncImageChanged() {
        ogChanged.img = imageFileChanged || focusChanged;
        recompute();
      }
      function setFocusStatus(message, state, clearAfter) {
        if (!focusStatus) return;
        clearTimeout(focusStatusTimer);
        focusStatus.textContent = message || "";
        focusStatus.classList.remove("is-saving", "is-success", "is-error");
        if (state) focusStatus.classList.add("is-" + state);
        if (clearAfter) {
          focusStatusTimer = setTimeout(function () {
            focusStatus.textContent = "";
            focusStatus.classList.remove("is-saving", "is-success", "is-error");
          }, clearAfter);
        }
      }
      function setFocus(value) {
        curFocus = value || "50% 50%";
        if (ogImg) ogImg.style.objectPosition = curFocus;
        if (focusField) focusField.value = curFocus;
      }

      // Переключатель оформления сразу показывает дружеские/романтические
      // стандартные тексты и картинку. Свои тексты и изображение не трогаем.
      var skinPick = document.querySelector(".category-skin-setting .skin-pick");
      function previewSkin(skin) {
        if (skin !== "romantic") skin = "friends";
        ogPreview.dataset.previewSkin = skin;
        var titleView = document.getElementById("ogTitleEd");
        var descView = document.getElementById("ogDescEd");
        if (titleView) titleView.dataset.ph =
          skin === "friends" ? "Собираемся вместе" : "✎ Тебя ждёт сюрприз ♥";
        if (descView) descView.dataset.ph =
          skin === "friends" ? "Открой и выбери удобный вариант" : "✎ Открой — внутри кое-что приятное";
        if (ogImg) {
          if (ogPreview.dataset.autoImage === "1") {
            ogImg.src = "/admin/categories/" + cid + "/og-preview?skin=" +
              encodeURIComponent(skin) + "&v=" +
              encodeURIComponent((ogPreview.dataset.previewRevision || "") + "-" + skin);
          } else if (ogPreview.dataset.defaultImage === "1") {
            ogImg.src = skin === "friends"
              ? ogPreview.dataset.friendsSrc
              : ogPreview.dataset.romanticSrc;
          }
        }
      }
      if (skinPick && !skinPick.dataset.skinReady) {
        skinPick.dataset.skinReady = "1";
        skinPick.addEventListener("change", function (event) {
          if (!event.target.matches('[name="category_skin"]')) return;
          previewSkin(event.target.value);
          recompute();
        });
      }

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
            // Ответ от instant-save прежней картинки больше не должен менять
            // локальный crop только что выбранного файла.
            focusSaveGeneration += 1;
            ogImg.src = URL.createObjectURL(ogInput.files[0]);
            setFocus("50% 50%");
            imageFileChanged = true;
            focusChanged = false;
            // После выбора файла это уже не авто-/фирменная картинка. Иначе
            // смена темы или reorder могли поверх локального preview подставить
            // старый URL до нажатия «Сохранить».
            ogPreview.dataset.autoImage = "0";
            ogPreview.dataset.defaultImage = "0";
            ogPreview.dataset.hasImage = "0";
            // Пока файл локальный, endpoint focus ещё неприменим, но сам crop уже
            // доступен и будет атомарно сохранён основной формой.
            hasSavedImage = false;
            ogPreview.classList.add("has-image");
            if (focusHint) focusHint.hidden = false;
            setFocusStatus("Двигай картинку — положение кадра сохранится вместе с категорией.", "saving");
            syncImageChanged();
          }
        });
      }

      // --- перетаскивание кадра своей картинки (WYSIWYG-кроп og:image) ----------
      // Сохранённую картинку обновляем сразу; новую отправляем с hidden og_focus
      // вместе с файлом. Очередь не даёт быстрым drag-запросам завершиться в
      // обратном порядке, а при ошибке возвращает последний подтверждённый кадр.
      if (ogImg && cid) {
        var dragging = false, dragStartX = 0, dragStartY = 0;
        var dragOriginFocus = curFocus;
        var DRAG_THRESHOLD = 6;
        function pointerPoint(e) {
          return {
            x: e.touches ? e.touches[0].clientX : e.clientX,
            y: e.touches ? e.touches[0].clientY : e.clientY
          };
        }
        function applyFocus(e) {
          var rect = ogImg.getBoundingClientRect();
          if (!rect.width || !rect.height) return;
          var point = pointerPoint(e);
          var x = Math.round(Math.min(1, Math.max(0, (point.x - rect.left) / rect.width)) * 100);
          var y = Math.round(Math.min(1, Math.max(0, (point.y - rect.top) / rect.height)) * 100);
          setFocus(x + "% " + y + "%");
        }
        function saveFocus(value) {
          var generation = ++focusSaveGeneration;
          setFocusStatus("Сохраняем положение кадра…", "saving");
          focusSaveChain = focusSaveChain.catch(function () {}).then(function () {
            // expected_* превращают endpoint в compare-and-set: уже летящий
            // запрос не сможет затереть focus после обычного submit или замены
            // картинки, даже если завершится позже них.
            var expectedValue = lastSavedFocus;
            var fd = new FormData();
            fd.append("csrf", document.body.dataset.csrf);
            fd.append("focus", value);
            fd.append("expected_image", savedImageName);
            fd.append("expected_focus", expectedValue);
            return fetch("/admin/categories/" + cid + "/og_focus", {
              method: "POST", body: fd,
            }).then(function (response) {
              return response.json().catch(function () { return {}; }).then(function (payload) {
                if (!response.ok) {
                  throw new Error(payload.detail || "Не удалось сохранить положение кадра");
                }
                return payload;
              });
            });
          }).then(function (payload) {
            lastSavedFocus = (payload && payload.focus) || value;
            if (payload && payload.preview_revision) {
              ogPreview.dataset.previewRevision = payload.preview_revision;
            }
            if (generation !== focusSaveGeneration) return;
            setFocus(lastSavedFocus);
            focusChanged = lastSavedFocus !== initialFocus;
            syncImageChanged();
            setFocusStatus("Положение кадра сохранено", "success", 2400);
          }).catch(function () {
            if (generation !== focusSaveGeneration) return;
            setFocus(lastSavedFocus);
            focusChanged = lastSavedFocus !== initialFocus;
            syncImageChanged();
            setFocusStatus(
              "Не удалось сохранить положение кадра. Вернули предыдущий вариант — попробуй ещё раз.",
              "error"
            );
          });
        }
        ogImg.addEventListener("pointerdown", function (e) {
          if (!ogPreview.classList.contains("has-image")) return;
          var point = pointerPoint(e);
          dragging = true;
          dragMoved = false;
          dragOriginFocus = curFocus;
          dragStartX = point.x;
          dragStartY = point.y;
          ogImg.setPointerCapture && ogImg.setPointerCapture(e.pointerId);
          e.preventDefault();
        });
        ogImg.addEventListener("pointermove", function (e) {
          if (!dragging) return;
          var point = pointerPoint(e);
          if (!dragMoved && Math.hypot(
                point.x - dragStartX, point.y - dragStartY
              ) < DRAG_THRESHOLD) return;
          if (!dragMoved) {
            dragMoved = true;
            ogPreview.classList.add("dragging");
          }
          applyFocus(e);
        });
        function endDrag() {
          if (!dragging) return;
          dragging = false; ogPreview.classList.remove("dragging");
          // Короткий click предназначен для выбора новой картинки. Не меняем
          // точку фокуса и не отправляем POST, пока кадр действительно не
          // сдвинут дальше небольшого порога движения.
          if (!dragMoved) return;
          focusChanged = curFocus !== initialFocus;
          syncImageChanged();
          if (hasSavedImage) saveFocus(curFocus);
          else setFocusStatus(
            "Положение кадра сохранится после нажатия «Сохранить».", "success"
          );
        }
        function cancelDrag() {
          if (!dragging) return;
          dragging = false;
          ogPreview.classList.remove("dragging");
          setFocus(dragOriginFocus);
          dragMoved = false;
        }
        ogImg.addEventListener("pointerup", endDrag);
        ogImg.addEventListener("pointercancel", cancelDrag);
      }
    }

    var tb = document.getElementById("catRows");
    if (tb && !tb.dataset.ready) {
      tb.dataset.ready = "1";
      var categoryOrderChain = Promise.resolve();
      var categoryOrderGeneration = 0;
      UI.sortable(tb, { selector: "tr.drag-row", onChange: function () {
        var order = Array.prototype.map.call(tb.querySelectorAll("tr.drag-row"),
          function (t) { return t.dataset.did; }).join(",");
        var generation = ++categoryOrderGeneration;
        // Drag-end может наступить снова, пока предыдущий POST ещё в сети.
        // Сериализуем снимки порядка: последним в БД гарантированно окажется
        // последнее действие пользователя, а не самый медленный запрос.
        categoryOrderChain = categoryOrderChain.catch(function () {}).then(function () {
          var fd = new FormData();
          fd.append("csrf", document.body.dataset.csrf);
          fd.append("order", order);
          return fetch("/admin/categories/" + tb.dataset.cid + "/dates_reorder", {
            method: "POST", body: fd,
          });
        })
          .then(function (r) {
            if (!r.ok) throw 0;
            return r.json();
          })
          .then(function (payload) {
            if (generation !== categoryOrderGeneration) return;
            var preview = document.getElementById("ogPreview");
            var image = document.getElementById("ogPreviewImg");
            // Перестановка влияет только на авто-коллаж. Свою картинку и
            // принудительное стандартное превью никогда не перезаписываем.
            if (!preview || !image || preview.dataset.autoImage !== "1") return;
            var revision = payload && payload.preview_revision
              ? payload.preview_revision
              : (preview.dataset.previewRevision || "");
            var skin = preview.dataset.previewSkin === "romantic"
              ? "romantic" : "friends";
            preview.dataset.previewRevision = revision;
            image.src = "/admin/categories/" + tb.dataset.cid +
              "/og-preview?skin=" + encodeURIComponent(skin) + "&v=" +
              encodeURIComponent(revision + "-" + Date.now().toString(36));
          })
          .catch(function () {
            if (generation === categoryOrderGeneration) {
              alert("Нет связи — порядок не сохранён");
            }
          });
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
    var saveChain = Promise.resolve();
    async function save(fd) {
      flash("Сохранение…");
      try {
        var r = await fetch("/admin/profile", {
          method: "POST", body: fd, keepalive: true,
          headers: { "X-Requested-With": "fetch" }
        });
        flash(r.ok ? "Сохранено ✓" : "Не удалось сохранить", r.ok);
      } catch (_) {
        flash("Нет связи — не сохранено", false);
      }
    }
    function runSave() {
      var name = form.querySelector('[name="display_name"]');
      if (name && !name.value.trim()) {
        flash("Имя не может быть пустым", false);
        return Promise.resolve(false);
      }
      // Снимок формы ставим в последовательную очередь. При быстрых кликах
      // старый POST физически не может завершиться после нового и перезаписать
      // выбранное оформление устаревшим значением.
      var fd = new FormData(form);
      fd.delete("avatar");
      var pending = saveChain.catch(function () {}).then(function () {
        return save(fd);
      });
      saveChain = pending;
      // profile.js дождётся именно этого запроса перед переходом в редактор:
      // иначе быстрый клик после смены skin мог получить старую тему с сервера.
      window.d4yProfileSave = pending;
      pending.finally(function () {
        if (window.d4yProfileSave === pending) window.d4yProfileSave = null;
      });
      return pending;
    }
    function schedule() {
      flash("Изменения сохраняются автоматически");
      clearTimeout(timer);
      timer = setTimeout(runSave, 700);
    }
    form.addEventListener("input", schedule);
    form.addEventListener("change", schedule);
    // Смена оформления должна успеть сохраниться даже если человек сразу
    // перейдёт в другой раздел: внешний вид применён мгновенно выше, а здесь
    // отправляем профиль без обычной задержки автосохранения.
    form.addEventListener("change", function (event) {
      if (!event.target.matches('[name="admin_skin"]')) return;
      clearTimeout(timer);
      runSave();
    });
    var effectsControl = form.querySelector('[name="cursor_effects"]');
    if (effectsControl) {
      effectsControl.addEventListener("change", function () {
        document.body.setAttribute("data-ink-interactive", effectsControl.checked ? "1" : "0");
      });
    }
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
        var title = share.getAttribute("data-name") || "Подборка событий";
        var text = share.getAttribute("data-text") || "Собираемся вместе";
        if (navigator.share) {
          navigator.share({ title: title, text: text, url: url }).catch(function () {});
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

  // --- главная: лента событий комьюнити (бесконечный скролл + виджет) --------
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
    var reportDlg = document.getElementById("communityReportDlg");
    var reportForm = document.getElementById("communityReportForm");
    var reportName = document.getElementById("communityReportName");
    var reportTarget = document.getElementById("communityReportTargetId");
    var reportReason = document.getElementById("communityReportReason");
    var reportCancel = document.getElementById("communityReportCancel");

    var loading = false, done = false, loadedAny = false;
    var io = null;

    // На телефоне отдаём ссылку системному меню, на компьютере сразу копируем.
    // Проверяем не только ширину: узкое окно ПК всё равно остаётся ПК, а планшет
    // с coarse-pointer получает привычный нативный share sheet.
    function canUseSystemShare() {
      if (typeof navigator.share !== "function") return false;
      var coarse = window.matchMedia &&
        window.matchMedia("(hover: none) and (pointer: coarse)").matches;
      var compactTouch = navigator.maxTouchPoints > 0 && window.innerWidth <= 900;
      return Boolean(coarse || compactTouch);
    }

    function legacyCopy(text) {
      return new Promise(function (resolve, reject) {
        var field = document.createElement("textarea");
        field.value = text;
        field.setAttribute("readonly", "");
        field.style.position = "fixed";
        field.style.opacity = "0";
        document.body.appendChild(field);
        field.select();
        try {
          if (document.execCommand && document.execCommand("copy")) resolve();
          else reject(new Error("copy unavailable"));
        } catch (err) {
          reject(err);
        } finally {
          field.remove();
        }
      });
    }

    function copyShareUrl(url) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(url).catch(function () {
          return legacyCopy(url);
        });
      }
      return legacyCopy(url);
    }

    function copiedFeedback(button) {
      var old = button.getAttribute("data-share-label") || button.textContent;
      button.setAttribute("data-share-label", old);
      button.textContent = "Ссылка скопирована ✓";
      clearTimeout(button._shareFeedback);
      button._shareFeedback = setTimeout(function () {
        button.textContent = old;
      }, 1700);
      toast("Ссылка на событие скопирована");
    }

    function shareCommunityEvent(button) {
      var url = button.getAttribute("data-share-url");
      var title = button.getAttribute("data-share-title") || "Событие date4you";
      if (!url) return;
      if (canUseSystemShare()) {
        navigator.share({
          title: title,
          text: "Посмотри это событие в date4you",
          url: url
        }).catch(function (err) {
          // Закрытие системного меню — нормальное действие; при технической
          // ошибке всё равно сохраняем пользователю ссылку в буфер.
          if (err && err.name === "AbortError") return;
          copyShareUrl(url)
            .then(function () { copiedFeedback(button); })
            .catch(function () { toast("Не удалось поделиться ссылкой"); });
        });
        return;
      }
      copyShareUrl(url)
        .then(function () { copiedFeedback(button); })
        .catch(function () { toast("Не удалось скопировать ссылку"); });
    }

    function addCommunityEvent(button, closeAfter) {
      if (!button || button.disabled) return;
      button.disabled = true;
      var old = button.textContent;
      button.textContent = "Добавляю…";
      fetch(button.getAttribute("data-add"), {
        method: "POST", credentials: "same-origin",
        headers: {
          "X-Requested-With": "fetch",
          "X-CSRF-Token": document.body.dataset.csrf || ""
        }
      })
        .then(function (r) {
          return r.json().then(function (j) { return { ok: r.ok, j: j }; });
        })
        .then(function (res) {
          if (!res.ok || !res.j || !res.j.ok) {
            button.disabled = false;
            button.textContent = old;
            toast((res.j && res.j.detail) || "Не удалось добавить");
            return;
          }
          button.textContent = "Добавлено ✓";
          toast("Событие добавлено в твою коллекцию");
          if (closeAfter) setTimeout(closeWidget, 900);
        })
        .catch(function () {
          button.disabled = false;
          button.textContent = old;
          toast("Нет связи");
        });
    }

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

    // открыть/закрыть виджет события
    function openWidget(id) {
      if (!dlg || !cwidBody) return;
      cwidBody.innerHTML = '<p class="muted" style="text-align:center;padding:30px">Загружаю…</p>';
      if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
      fetch("/admin/community/date/" + encodeURIComponent(id),
            { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (html) {
          cwidBody.innerHTML = html;
          if (window.UI && UI.lazyVideos) UI.lazyVideos(cwidBody);
        })
        .catch(function () {
          cwidBody.innerHTML = '<p class="muted" style="text-align:center;padding:30px">Не удалось открыть событие</p>';
        });
    }
    function closeWidget() {
      if (!dlg) return;
      if (typeof dlg.close === "function" && dlg.open) dlg.close();
      else dlg.removeAttribute("open");
      if (cwidBody) cwidBody.innerHTML = "";
    }

    function openReport(button) {
      if (!reportDlg || !reportForm || !button) return;
      reportForm.setAttribute("action", button.getAttribute("data-report-url") || "");
      if (reportTarget) reportTarget.value = button.getAttribute("data-report-id") || "";
      if (reportName) reportName.textContent = button.getAttribute("data-report-name") || "событие";
      if (reportReason) reportReason.value = "";
      if (typeof reportDlg.showModal === "function") reportDlg.showModal();
      else reportDlg.setAttribute("open", "");
      if (reportReason) reportReason.focus();
    }

    function closeReport() {
      if (!reportDlg) return;
      if (typeof reportDlg.close === "function" && reportDlg.open) reportDlg.close();
      else reportDlg.removeAttribute("open");
    }

    // Клик по карточке открывает виджет; обе кнопки действий остаются на месте.
    feed.addEventListener("click", function (e) {
      var add = e.target.closest("[data-community-add]");
      if (add) {
        e.preventDefault();
        e.stopPropagation();
        addCommunityEvent(add, false);
        return;
      }
      var share = e.target.closest("[data-community-share]");
      if (share) {
        e.preventDefault();
        e.stopPropagation();
        shareCommunityEvent(share);
        return;
      }
      var report = e.target.closest("[data-community-report]");
      if (report) {
        e.preventDefault();
        e.stopPropagation();
        openReport(report);
        return;
      }
      if (e.target.closest("[data-stop]")) return;
      var card = e.target.closest(".cfeed-card");
      if (card) openWidget(card.getAttribute("data-widget"));
    });
    feed.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " ") return;
      if (e.target.closest("[data-stop]")) return;
      var card = e.target.closest(".cfeed-card");
      if (card) { e.preventDefault(); openWidget(card.getAttribute("data-widget")); }
    });

    if (cwidClose) cwidClose.addEventListener("click", closeWidget);
    if (dlg) dlg.addEventListener("click", function (e) {
      if (e.target === dlg) closeWidget();               // клик по подложке
    });
    if (reportCancel) reportCancel.addEventListener("click", closeReport);
    if (reportDlg) reportDlg.addEventListener("click", function (e) {
      if (e.target === reportDlg) closeReport();
    });
    if (reportForm) reportForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var submit = reportForm.querySelector('[type="submit"]');
      if (!reportForm.getAttribute("action") || (submit && submit.disabled)) return;
      var old = submit ? submit.textContent : "";
      if (submit) {
        submit.disabled = true;
        submit.textContent = "Отправляю…";
      }
      fetch(reportForm.getAttribute("action"), {
        method: "POST",
        credentials: "same-origin",
        body: new FormData(reportForm),
        headers: {
          "Accept": "application/json",
          "X-Requested-With": "fetch",
          "X-CSRF-Token": document.body.dataset.csrf || ""
        }
      })
        .then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (j) { return { ok: r.ok, j: j }; });
        })
        .then(function (res) {
          if (!res.ok || !res.j || !res.j.ok) {
            var detail = res.j && res.j.detail;
            if (detail && typeof detail === "object") detail = detail.msg;
            toast(detail || "Не удалось отправить жалобу");
            return;
          }
          closeReport();
          reportForm.reset();
          toast("Спасибо, жалоба отправлена. Модератор проверит");
        })
        .catch(function () { toast("Нет связи"); })
        .finally(function () {
          if (submit) {
            submit.disabled = false;
            submit.textContent = old;
          }
        });
    });

    // Независимые действия внутри виджета (контент подгружается): отметка
    // «Хочу сходить» хранит связь с оригиналом, «Добавить» создаёт копию.
    if (cwidBody) cwidBody.addEventListener("click", function (e) {
      var share = e.target.closest("[data-community-share]");
      if (share) {
        e.preventDefault();
        shareCommunityEvent(share);
        return;
      }
      var want = e.target.closest("[data-want]");
      if (want) {
        want.disabled = true;
        var wantOld = want.textContent;
        want.textContent = "Сохраняю…";
        var wantData = new FormData();
        wantData.append("csrf", document.body.dataset.csrf || "");
        fetch(want.getAttribute("data-want"), {
          method: "POST", credentials: "same-origin", body: wantData,
          headers: { "X-Requested-With": "fetch" }
        })
          .then(function (r) {
            return r.json().then(function (j) { return { ok: r.ok, j: j }; });
          })
          .then(function (res) {
            if (!res.ok || !res.j || !res.j.ok) {
              want.textContent = wantOld;
              toast((res.j && res.j.detail) || "Не удалось сохранить");
              return;
            }
            var wanted = Boolean(res.j.wanted);
            want.dataset.wanted = wanted ? "1" : "0";
            want.setAttribute("aria-pressed", wanted ? "true" : "false");
            want.textContent = wanted ? "Убрать из «Хочу сходить»" : "Хочу сходить";
            want.classList.toggle("primary", !wanted);
            want.classList.toggle("ghost", wanted);
            toast(res.j.message || (wanted
              ? "Добавлено в «Хочу сходить»"
              : "Убрано из «Хочу сходить»"));
          })
          .catch(function () { want.textContent = wantOld; toast("Нет связи"); })
          .finally(function () { want.disabled = false; });
        return;
      }
      var btn = e.target.closest("[data-add]");
      if (!btn) return;
      addCommunityEvent(btn, true);
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
      if (e.key === "Escape") {
        closeWidget();
        closeReport();
      }
    });

    load();     // первая страница
  }

  // --- удобный выбор дедлайна голосования ---------------------------------
  function initDeadlinePickers() {
    var MOSCOW_TIME_ZONE = "Europe/Moscow";
    var pad = function (value) { return String(value).padStart(2, "0"); };
    var moscowParts = new Intl.DateTimeFormat("en-GB-u-hc-h23", {
      timeZone: MOSCOW_TIME_ZONE,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hourCycle: "h23"
    });
    var readableMoscow = new Intl.DateTimeFormat("ru-RU", {
      timeZone: "UTC", weekday: "short", day: "numeric", month: "long",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23"
    });
    var toMoscowWallDate = function (instant) {
      var values = {};
      moscowParts.formatToParts(instant).forEach(function (part) {
        if (part.type !== "literal") values[part.type] = Number(part.value);
      });
      // UTC используется только как нейтральный контейнер календарных полей.
      // Само значение datetime-local остаётся московским wall-clock без offset.
      return new Date(Date.UTC(
        values.year, values.month - 1, values.day,
        values.hour, values.minute, values.second
      ));
    };
    var toValue = function (wallDate) {
      return wallDate.getUTCFullYear() + "-" + pad(wallDate.getUTCMonth() + 1) + "-" +
        pad(wallDate.getUTCDate()) + "T" + pad(wallDate.getUTCHours()) + ":" +
        pad(wallDate.getUTCMinutes());
    };
    var parseValue = function (value) {
      var match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value || "");
      if (!match) return null;
      var date = new Date(Date.UTC(
        +match[1], +match[2] - 1, +match[3], +match[4], +match[5]
      ));
      if (!Number.isFinite(date.getTime()) ||
          date.getUTCFullYear() !== +match[1] ||
          date.getUTCMonth() !== +match[2] - 1 ||
          date.getUTCDate() !== +match[3] ||
          date.getUTCHours() !== +match[4] ||
          date.getUTCMinutes() !== +match[5]) return null;
      return date;
    };

    document.querySelectorAll("[data-deadline-picker]").forEach(function (picker) {
      if (picker.dataset.deadlineReady) return;
      picker.dataset.deadlineReady = "1";
      var input = picker.querySelector('input[name="voting_deadline"]');
      var readable = picker.querySelector("[data-deadline-readable]");
      var presets = Array.from(picker.querySelectorAll("[data-deadline-hours]"));
      if (!input) return;

      // Оба экрана получают актуальную нижнюю границу даже после возврата из
      // Turbo-кэша. Сервер всё равно остаётся окончательным валидатором.
      var now = toMoscowWallDate(new Date());
      // datetime-local не передаёт секунды, а сервер требует строго будущее
      // значение. Следующая минута не оставляет формально доступный, но уже
      // прошедший вариант и учитывает задержку до submit.
      now.setUTCMinutes(now.getUTCMinutes() + 1, 0, 0);
      if (!input.min || input.min < toValue(now)) input.min = toValue(now);

      function renderReadable() {
        var selected = parseValue(input.value);
        if (!readable) return;
        if (!selected) {
          readable.textContent = "Выбери дату и время по Москве";
          return;
        }
        readable.textContent = "Выбрано: " + readableMoscow.format(selected) + " МСК";
      }

      presets.forEach(function (button) {
        button.addEventListener("click", function () {
          var hours = Number(button.dataset.deadlineHours);
          if (!Number.isFinite(hours) || hours <= 0) return;
          var current = toMoscowWallDate(new Date());
          var selected = picker.dataset.deadlineMode === "extend"
            ? parseValue(input.value) : null;
          // В редакторе категории быстрые кнопки именно продлевают уже
          // выбранный срок. Если поле пустое или срок успел пройти, безопасно
          // начинаем от текущего московского времени. На экране создания база
          // всегда текущая, как и раньше.
          if (!selected || selected.getTime() < current.getTime()) {
            var quarterHour = 15 * 60 * 1000;
            selected = new Date(Math.ceil(current.getTime() / quarterHour) * quarterHour);
          }
          selected.setUTCHours(selected.getUTCHours() + hours);
          input.value = toValue(selected);
          presets.forEach(function (item) {
            item.setAttribute("aria-pressed", item === button ? "true" : "false");
          });
          clearTimeout(input._deadlineHighlightTimer);
          input.classList.add("deadline-value-updated");
          input._deadlineHighlightTimer = setTimeout(function () {
            input.classList.remove("deadline-value-updated");
          }, 450);
          input.dispatchEvent(new Event("input", { bubbles: true }));
          input.dispatchEvent(new Event("change", { bubbles: true }));
        });
      });
      input.addEventListener("input", function (event) {
        if (event.isTrusted) {
          presets.forEach(function (item) { item.setAttribute("aria-pressed", "false"); });
        }
        renderReadable();
      });
      renderReadable();
    });
  }

  // Запускаем инициализаторы на каждой загрузке (в т.ч. после Turbo-перехода).
  // Каждый сам проверяет наличие своих элементов, поэтому безопасно звать все.
  function initPage() {
    initSkinSettings();
    initNav();
    if (window.UI && UI.numberSteppers) UI.numberSteppers(document);
    if (window.UI && UI.lazyVideos) UI.lazyVideos(document);
    if (window.UI && UI.voteCountdowns) UI.voteCountdowns(document);
    document.querySelectorAll("[data-picker-only]").forEach(function (input) {
      if (input.dataset.pickerReady) return;
      input.dataset.pickerReady = "1";
      input.addEventListener("click", function () {
        if (typeof input.showPicker === "function") {
          try { input.showPicker(); } catch (_) {}
        }
      });
      input.addEventListener("keydown", function (event) {
        if (event.key === "Tab") return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          if (typeof input.showPicker === "function") {
            try { input.showPicker(); } catch (_) {}
          }
          return;
        }
      });
    });
    initDeadlinePickers();
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
