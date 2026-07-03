/* Общие интерактивные кусочки гостевой страницы и админки.
   Без зависимостей; работает мышью и пальцем (pointer events). */
window.UI = (() => {
  "use strict";

  // Геометрия скользящего индикатора вкладок, сохраняемая МЕЖДУ Turbo-переходами
  // (узлы .tab-ind пересоздаются при подмене <body>, а объект в замыкании живёт).
  // Ключи: "nav" (главная навигация) и "tabs" (под-вкладки списка свиданий).
  const _tabInd = {};

  // ЕДИНЫЙ обработчик resize для всех таб-строк. Раньше каждый glassTabs() вешал
  // свой window.addEventListener("resize") — а под Turbo glassTabs зовётся на
  // КАЖДОМ переходе (контейнер пересоздаётся), и слушатели копились: через
  // десяток навигаций один resize дёргал N устаревших замыканий с отсоединённым
  // DOM. Это и была «вкладки со временем грузятся медленно» (#9). Теперь —
  // один слушатель, а активные контейнеры держим в Weak-множестве через список
  // репозиционеров, отсеивая отсоединённые от документа. (перф-фикс #9)
  let _tabRepos = [];
  let _tabRt;
  window.addEventListener("resize", function () {
    clearTimeout(_tabRt);
    _tabRt = setTimeout(function () {
      _tabRepos = _tabRepos.filter(function (fn) { return fn(); });
    }, 120);
  });

  /* --- Перестановка перетаскиванием (FLIP, как в Telegram) -------------- */
  /* Плитка следует за пальцем (transform с поправкой на смену слота),
     соседи плавно «доезжают» на свои места через FLIP-анимацию. */
  function sortable(container, opts = {}) {
    const sel = opts.selector || "[data-sort]";
    let drag = null, started = false;
    let startPX = 0, startPY = 0;      // палец в момент захвата
    let lastDX = 0, lastDY = 0;        // текущий визуальный сдвиг плитки
    let slot0X = 0, slot0Y = 0;        // слот плитки в момент захвата

    function flip(els, mutate) {
      const first = new Map(els.map((el) => [el, el.getBoundingClientRect()]));
      mutate();
      els.forEach((el) => {
        const f = first.get(el), l = el.getBoundingClientRect();
        const dx = f.left - l.left, dy = f.top - l.top;
        if (!dx && !dy) return;
        el.style.transition = "none";
        el.style.transform = `translate(${dx}px, ${dy}px)`;
        requestAnimationFrame(() => {
          el.style.transition = "transform .18s ease";
          el.style.transform = "";
          el.addEventListener("transitionend", () => { el.style.transition = ""; },
                              { once: true });
        });
      });
    }

    function follow(e) {
      // слот = позиция без нашего transform; сдвиг = палец − слот
      const r = drag.getBoundingClientRect();
      const slotX = r.left - lastDX, slotY = r.top - lastDY;
      lastDX = (e.clientX - startPX) - (slotX - slot0X);
      lastDY = (e.clientY - startPY) - (slotY - slot0Y);
      drag.style.transform = `translate(${lastDX}px, ${lastDY}px) scale(1.07)`;
    }

    container.addEventListener("pointerdown", (e) => {
      const it = e.target.closest(sel);
      if (!it || !container.contains(it)) return;
      if (e.target.closest("button, a, input, textarea, select")) return;
      drag = it; started = false;
      startPX = e.clientX; startPY = e.clientY;
      lastDX = 0; lastDY = 0;
      try { it.setPointerCapture(e.pointerId); } catch (_) {}
    });

    container.addEventListener("pointermove", (e) => {
      if (!drag) return;
      if (!started) {
        if (Math.hypot(e.clientX - startPX, e.clientY - startPY) < 7) return; // не мешаем тапам
        started = true;
        const r = drag.getBoundingClientRect();
        slot0X = r.left; slot0Y = r.top;
        drag.classList.add("dragging");
        drag.style.transition = "none";
      }
      e.preventDefault();
      follow(e);

      const others = [...container.querySelectorAll(sel)].filter((x) => x !== drag);
      const over = others.find((x) => {
        const r = x.getBoundingClientRect();
        return e.clientX > r.left && e.clientX < r.right &&
               e.clientY > r.top && e.clientY < r.bottom;
      });
      if (over) {
        const ro = over.getBoundingClientRect();
        const before = e.clientX < ro.left + ro.width / 2;
        const ref = before ? over : over.nextSibling;
        if (ref !== drag && ref !== drag.nextSibling) {
          flip(others, () => container.insertBefore(drag, ref));
          follow(e);                  // слот сменился — без рывка доводим transform
        }
      }
    });

    const end = (e) => {
      if (!drag) return;
      const moved = started;
      const el = drag;
      drag = null; started = false;
      if (moved) {
        // плавный «доезд» плитки в свой слот
        el.style.transition = "transform .18s ease";
        el.style.transform = "";
        const cleanup = () => { el.classList.remove("dragging"); el.style.transition = ""; };
        el.addEventListener("transitionend", cleanup, { once: true });
        setTimeout(cleanup, 260);     // подстраховка
        if (opts.onChange) opts.onChange();
      } else {
        el.classList.remove("dragging");
        el.style.transition = "";
        // тап без перетаскивания: pointer capture мог проглотить click, поэтому
        // вызываем onTap отсюда (нужно для выбора зоны фокуса фото)
        if (opts.onTap && e && !e.target.closest("button, a, input, textarea, select")) {
          opts.onTap(el, e);
        }
      }
    };
    container.addEventListener("pointerup", end);
    container.addEventListener("pointercancel", end);
  }

  /* --- Салют из сердечек ------------------------------------------------ */
  function burst(x, y) {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    for (let i = 0; i < 9; i++) {
      const h = document.createElement("span");
      h.className = "confetti-heart";
      h.textContent = i % 3 ? "♥" : "♡";
      const ang = (Math.random() * 120 - 60) * Math.PI / 180;
      const dist = 44 + Math.random() * 52;
      h.style.left = x + "px";
      h.style.top = y + "px";
      h.style.setProperty("--dx", Math.sin(ang) * dist + "px");
      h.style.setProperty("--dy", -(Math.cos(ang) * dist + 26) + "px");
      h.style.setProperty("--s", (0.6 + Math.random() * 0.8).toFixed(2));
      h.style.setProperty("--hue", i % 4 ? "" : "hue-rotate(-12deg)");
      document.body.appendChild(h);
      setTimeout(() => h.remove(), 1100);
    }
  }

  /* --- Загрузка фото: дроп-зона + превью + перестановка ----------------- */
  /* Держим выбранные файлы в массиве и каждый раз пересобираем input.files
     через DataTransfer — поэтому обычный submit формы (админка) работает,
     а порядок файлов соответствует порядку плиток. */
  function uploader({ zone, input, preview, max, maxBytes, keptCount,
                      onChange, onError, kind, focusable, onFocus, noZoneBind }) {
    let files = [];
    let urls = [];
    let focuses = [];                                 // зона кадра на каждое фото («X% Y%»)
    kind = kind || "image";                           // "image" | "video"
    const isVideo = kind === "video";
    const noun = isVideo ? "видео" : "фото";
    keptCount = keptCount || (() => 0);
    maxBytes = maxBytes || (isVideo ? 60 : 10) * 1024 * 1024;
    onError = onError || ((m) => alert(m));

    function sync() {
      try {
        const dt = new DataTransfer();
        files.forEach((f) => dt.items.add(f));
        input.files = dt.files;
      } catch (_) { /* очень старые браузеры — файлы уйдут как выбраны */ }
      render();
      if (onChange) onChange(files);
    }

    function render() {
      // ВАЖНО: blob-ссылки отзываем только при перерисовке, а не в onload —
      // ранний revoke в Safari иногда оставляет превью пустым.
      urls.forEach((u) => URL.revokeObjectURL(u));
      urls = [];
      preview.querySelectorAll(".ptile.new").forEach((t) => t.remove());
      files.forEach((f, idx) => {
        const u = URL.createObjectURL(f);
        urls.push(u);
        const w = document.createElement("div");
        w.className = "ptile new" + (isVideo ? " vid" : "");
        w.dataset.sort = "1";
        w.dataset.idx = idx;
        if (isVideo) {
          w.innerHTML = '<video muted playsinline preload="metadata"></video>' +
                        '<span class="vtag">🎬</span>' +
                        '<button type="button" class="rm" aria-label="Убрать">✕</button>';
          const v = w.querySelector("video");
          v.src = u;
        } else {
          w.innerHTML = '<img alt=""><button type="button" class="rm" aria-label="Убрать">✕</button>';
          var img = w.querySelector("img");
          img.src = u;
          // выбор зоны кадра прямо на новой плитке (до сохранения): клик по фото →
          // object-position в процентах. Включается только при focusable:true.
          if (focusable) {
            w.classList.add("focusable");
            img.style.objectPosition = focuses[idx] || "50% 50%";
            img.style.pointerEvents = "auto";         // перебиваем .ptile img{pointer-events:none}
          }
        }
        w.querySelector(".rm").addEventListener("click", () => {
          files.splice(idx, 1);
          focuses.splice(idx, 1);
          sync();
        });
        preview.appendChild(w);
      });
    }

    async function shrink(f) {
      // Большие фото сжимаем прямо в браузере: загрузка с телефона
      // ускоряется в разы, а сервер не пережёвывает 30-мегабайтные оригиналы.
      if (isVideo) return f;                            // видео не трогаем
      if (!/^image\//.test(f.type) || f.type === "image/gif") return f;
      if (f.size < 1200 * 1024) return f;
      try {
        const bmp = await createImageBitmap(f);
        const k = Math.min(1, 2200 / Math.max(bmp.width, bmp.height));
        const w = Math.max(1, Math.round(bmp.width * k));
        const h = Math.max(1, Math.round(bmp.height * k));
        const cv = document.createElement("canvas");
        cv.width = w; cv.height = h;
        cv.getContext("2d").drawImage(bmp, 0, 0, w, h);
        const blob = await new Promise((res) => cv.toBlob(res, "image/jpeg", .85));
        if (blob && blob.size < f.size) {
          return new File([blob], f.name.replace(/\.\w+$/, "") + ".jpg",
                          { type: "image/jpeg" });
        }
      } catch (_) { /* не декодируется (например, HEIC в Chrome) — шлём как есть */ }
      return f;
    }

    async function add(list) {
      for (const f of [...list]) {
        if (!f) continue;
        const okType = isVideo
          ? (!f.type || f.type.startsWith("video/"))
          : (!f.type || f.type.startsWith("image/"));  // пустой type у HEIC — пропускаем
        if (!okType) {
          onError(`«${f.name}» — не ${isVideo ? "видео" : "изображение"}`);
          continue;
        }
        if (files.length + keptCount() >= max) {
          onError(`Можно не больше ${max} ${noun}`);
          break;
        }
        const g = await shrink(f);
        if (g.size > maxBytes) {
          onError(`«${f.name}» больше ${Math.round(maxBytes / 1048576)} МБ — не добавил`);
          continue;
        }
        files.push(g);
        focuses.push("50% 50%");                      // зона по умолчанию — центр
      }
      sync();
    }

    // В режиме общего блока «Медиа» одна видимая дроп-зона на два загрузчика
    // (фото/видео): привязку зоны и input берёт на себя mediaUploader, а здесь
    // её пропускаем (noZoneBind), оставляя только превью и сортировку.
    if (!noZoneBind) {
      zone.addEventListener("click", (e) => {
        if (e.target.closest(".ptile, button, a")) return;
        input.click();
      });
      zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
      zone.addEventListener("dragleave", () => zone.classList.remove("over"));
      zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("over");
        if (e.dataTransfer && e.dataTransfer.files) add(e.dataTransfer.files);
      });
      input.addEventListener("change", () => add(input.files));
    }

    // выбор зоны кадра на новых плитках: клик по фото → object-position в %.
    // Тот же расчёт, что для сохранённых фото в редакторе свидания.
    if (focusable) {
      preview.addEventListener("click", (e) => {
        const tile = e.target.closest(".ptile.new");
        if (!tile || e.target.closest(".rm")) return;
        const img = tile.querySelector("img");
        if (!img) return;
        const rect = img.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        const x = Math.round(Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)) * 100);
        const y = Math.round(Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)) * 100);
        const focus = x + "% " + y + "%";
        img.style.objectPosition = focus;
        focuses[+tile.dataset.idx] = focus;
        if (onFocus) onFocus(+tile.dataset.idx, focus, files);
      });
    }

    sortable(preview, {
      selector: ".ptile.new",
      onChange: () => {
        const tiles = [...preview.querySelectorAll(".ptile.new")];
        files = tiles.map((t) => files[+t.dataset.idx]);
        focuses = tiles.map((t) => focuses[+t.dataset.idx]);   // зоны едут вместе с плитками
        sync();
      },
    });

    return {
      files: () => files.slice(),
      focuses: () => focuses.slice(),
      addFiles: (list) => add(list),
      // задать зону кадра плитке idx извне (клик по большому предпросмотру).
      // Обновляет и модель focuses, и превью-картинку самой плитки.
      setFocus(idx, focus) {
        if (idx < 0 || idx >= files.length) return;
        focuses[idx] = focus;
        const tile = preview.querySelector('.ptile.new[data-idx="' + idx + '"]');
        const img = tile && tile.querySelector("img");
        if (img) img.style.objectPosition = focus;
        if (onFocus) onFocus(idx, focus, files);
      },
      hasFiles: () => files.length > 0,
      clear() { files = []; focuses = []; sync(); },
    };
  }

  /* --- Общий блок «Медиа»: одна дроп-зона на фото И видео --------------- */
  /* Одна видимая зона/инпут принимают и фото, и видео. Файлы маршрутизируем по
     MIME в ДВА базовых uploader (photos→input name="images", videos→name="videos"),
     поэтому бэкенд не меняется. Превью у фото и видео — отдельные, но визуально
     идут в одной зоне (плитки добавляются в общий контейнер). */
  function mediaUploader({ zone, input, photo, video, onError }) {
    onError = onError || ((m) => alert(m));
    function route(list) {
      const imgs = [], vids = [];
      [...list].forEach((f) => {
        if (!f) return;
        const t = f.type || "";
        if (t.startsWith("video/")) vids.push(f);
        else imgs.push(f);                 // пустой type (HEIC) считаем фото
      });
      if (imgs.length) photo.addFiles(imgs);
      if (vids.length) video.addFiles(vids);
    }
    zone.addEventListener("click", (e) => {
      if (e.target.closest(".ptile, button, a")) return;
      input.click();
    });
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("over"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("over");
      if (e.dataTransfer && e.dataTransfer.files) route(e.dataTransfer.files);
    });
    input.addEventListener("change", () => { route(input.files); input.value = ""; });
    return { photo, video };
  }

  /* --- Чипы быстрого выбора даты и времени ------------------------------ */
  function dateChips(root, startInput, endInput) {
    const pad = (n) => String(n).padStart(2, "0");
    const dstr = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const flash = (el) => { el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 450); };
    const touch = (el) => { flash(el); el.dispatchEvent(new Event("input", { bubbles: true })); };

    root.querySelectorAll("[data-day]").forEach((ch) => {
      ch.addEventListener("click", () => {
        const kind = ch.dataset.day;
        const d = new Date();
        if (kind === "tomorrow") d.setDate(d.getDate() + 1);
        if (kind === "sat") d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7 || 7));
        if (kind === "sun") d.setDate(d.getDate() + ((0 - d.getDay() + 7) % 7 || 7));
        const t = (startInput.value.split("T")[1]) || "19:00";
        startInput.value = `${dstr(d)}T${t}`;
        touch(startInput);
      });
    });
    root.querySelectorAll("[data-time]").forEach((ch) => {
      ch.addEventListener("click", () => {
        const day = (startInput.value.split("T")[0]) || dstr(new Date());
        startInput.value = `${day}T${ch.dataset.time}`;
        touch(startInput);
      });
    });
    root.querySelectorAll("[data-dur]").forEach((ch) => {
      ch.addEventListener("click", () => {
        if (!endInput) return;
        if (!startInput.value) { flash(startInput); return; }
        // накопительно: прибавляем к уже выбранному концу, иначе — к началу.
        // конец не может быть раньше начала.
        const base = (endInput.value && endInput.value > startInput.value)
          ? endInput.value : startInput.value;
        const d = new Date(base);
        d.setMinutes(d.getMinutes() + Number(ch.dataset.dur) * 60);
        endInput.value = `${dstr(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
        flash(endInput);
        endInput.dispatchEvent(new Event("input", { bubbles: true }));  // обновить предпросмотр
      });
    });
  }

  /* --- Редактор свидания: тулбар разметки + живой предпросмотр ---------- */
  /* Делегированно, без инлайнов. renderMarkup повторяет helpers.rich():
     сначала экранируем HTML, затем те же правила в том же порядке —
     ссылки, **жирный**, __подчёркнутый__, ~~зачёркнутый~~, *курсив*. */
  function escapeHTML(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&#34;").replace(/'/g, "&#39;");
  }

  function renderMarkup(text) {
    if (!text) return "";
    var s = escapeHTML(text);
    // [текст](https://…) — как _RICH_LINK на сервере (текст ≤100, url ≤500)
    s = s.replace(/\[([^\]\n]{1,100})\]\((https?:\/\/[^\s)]{1,500})\)/g,
                  '<a href="$2" target="_blank" rel="noopener">$1</a>');
    s = s.replace(/\*\*([\s\S]+?)\*\*/g, "<b>$1</b>");
    s = s.replace(/__([\s\S]+?)__/g, "<u>$1</u>");
    s = s.replace(/~~([\s\S]+?)~~/g, "<s>$1</s>");
    s = s.replace(/\*([\s\S]+?)\*/g, "<i>$1</i>");
    return s;
  }

  var RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"];

  // Форматирование даты как на сервере (helpers.fmt_when), вход — "YYYY-MM-DDTHH:MM".
  function fmtPoint(s, withYear) {
    var p = s.split("T"); var d = p[0].split("-"); var t = p[1] || "00:00";
    var day = parseInt(d[2], 10), mon = parseInt(d[1], 10) - 1, year = d[0];
    var base = day + " " + (RU_MONTHS[mon] || "");
    if (withYear !== false) base += " " + year;
    return base + ", " + t.slice(0, 5);
  }
  function fmtWhen(starts, ends) {
    if (!starts) return "";
    if (!ends) return fmtPoint(starts);
    var ds = starts.split("T")[0], de = ends.split("T")[0];
    if (ds === de) return fmtPoint(starts) + "–" + (ends.split("T")[1] || "").slice(0, 5);
    return fmtPoint(starts) + " — " + fmtPoint(ends);
  }
  function hostOf(u) {
    try { return new URL(/^https?:\/\//.test(u) ? u : "https://" + u).host; }
    catch (_) { return u.slice(0, 40); }
  }

  function editorPreview(form) {
    var pv = {};
    var scope = form.closest(".split") || document;
    scope.querySelectorAll("[data-preview]").forEach(function (el) {
      pv[el.getAttribute("data-preview")] = el;
    });
    var descPrev = document.getElementById("descPreview");
    function field(name) { return form.querySelector('[data-bind="' + name + '"]'); }

    // состояние превью-медиа: показываем блок .photo, только если есть обложка
    // ИЛИ видео; иначе прячем целиком (без «битой картинки»/пустого плейсхолдера).
    var hasCover = !!(pv.cover && pv.cover.getAttribute("src"));
    var hasVideo = !!(pv.vbadge && !pv.vbadge.hidden);
    function syncPhotoBox() {
      if (pv.photo) pv.photo.hidden = !(hasCover || hasVideo);
    }

    function setCover(url) {
      if (!pv.cover) return;
      if (url) { pv.cover.src = url; pv.cover.hidden = false; hasCover = true; }
      else { pv.cover.removeAttribute("src"); pv.cover.hidden = true; hasCover = false; }
      syncPhotoBox();
    }
    function setVideo(has) {
      if (pv.vbadge) pv.vbadge.hidden = !has;
      hasVideo = !!has;
      syncPhotoBox();
    }

    function sync() {
      var title = field("title");
      if (title && pv.title) pv.title.textContent = title.value || "Без названия";

      var desc = field("desc");
      var html = desc ? renderMarkup(desc.value) : "";
      if (pv.desc) pv.desc.innerHTML = html;
      if (descPrev) descPrev.innerHTML = desc && desc.value ? "превью: " + html : "";

      // оплата: радиогруппа (0 не важно / 1 50-50 / 2 я плачу / 3 ты оплатишь)
      if (pv.pay) {
        var payChecked = form.querySelector('[data-bind="pay"]:checked');
        var payVal = payChecked ? payChecked.value : "0";
        var PAY = { "1": "💸 50/50", "2": "👌 Я плачу", "3": "🫵 Ты платишь" };
        if (PAY[payVal]) { pv.pay.textContent = PAY[payVal]; pv.pay.hidden = false; }
        else { pv.pay.hidden = true; }
      }

      // мета: когда (🕐) + место (📍)
      if (pv.meta) {
        var bits = [];
        var fs = document.getElementById("fStart"), fe = document.getElementById("fEnd");
        var when = fs && fs.value ? fmtWhen(fs.value, fe && fe.value) : "";
        if (when) bits.push('<span>🕐 ' + escapeHTML(when) + "</span>");
        var place = field("place");
        var pvv = place ? (place.value || "").trim() : "";
        if (pvv) bits.push('<span>📍 ' + (/^https?:\/\//.test(pvv) ? "Место на карте" : escapeHTML(pvv)) + "</span>");
        pv.meta.innerHTML = bits.join("");
      }

      // ссылки (textarea name="links", по одной на строку)
      if (pv.links) {
        var la = form.querySelector('[name="links"]');
        var out = "";
        if (la) {
          la.value.split("\n").forEach(function (line) {
            var u = line.trim();
            if (u) out += '<span class="plink">🔗 ' + escapeHTML(hostOf(u)) + "</span>";
          });
        }
        pv.links.innerHTML = out;
      }
    }

    form.addEventListener("input", sync);
    form.addEventListener("change", sync);
    sync();
    return { sync: sync, setCover: setCover, setVideo: setVideo };
  }

  /* --- WYSIWYG-редактор описания: форматирование видно прямо в поле -------
     contenteditable показывает жирный/курсив/подчёркнутый/зачёркнутый/ссылки
     как настоящее оформление, а в скрытую <textarea> синхронно пишется
     markdown — сервер по-прежнему рендерит его helpers.rich(). Под нашу CSP:
     весь код здесь, никаких инлайнов. execCommand для b/i/u/strike ещё
     поддержан во всех браузерах; для нас этого достаточно. */
  function htmlToMarkdown(node) {
    var out = "";
    node.childNodes.forEach(function (n) {
      if (n.nodeType === 3) { out += n.nodeValue; return; }      // текст
      if (n.nodeType !== 1) return;
      var tag = n.nodeName.toLowerCase();
      if (tag === "br") { out += "\n"; return; }
      if (tag === "div" || tag === "p") {                        // перевод строки между блоками
        if (out && !/\n$/.test(out)) out += "\n";
        out += htmlToMarkdown(n);
        return;
      }
      var inner = htmlToMarkdown(n);
      var style = n.getAttribute && n.getAttribute("style") || "";
      if (tag === "b" || tag === "strong" || /font-weight\s*:\s*(bold|[6-9]00)/.test(style)) out += "**" + inner + "**";
      else if (tag === "i" || tag === "em" || /font-style\s*:\s*italic/.test(style)) out += "*" + inner + "*";
      else if (tag === "u" || /text-decoration[^;]*underline/.test(style)) out += "__" + inner + "__";
      else if (tag === "s" || tag === "strike" || tag === "del" || /line-through/.test(style)) out += "~~" + inner + "~~";
      else if (tag === "a" && n.getAttribute("href")) out += "[" + inner + "](" + n.getAttribute("href") + ")";
      else out += inner;
    });
    return out;
  }

  function richEditor(opts) {
    var ta = opts.textarea, ed = opts.editable, toolbar = opts.toolbar;
    if (!ta || !ed) return null;

    // начальное наполнение: markdown из textarea → видимый HTML
    ed.innerHTML = renderMarkup(ta.value) || "";

    var syncing = false;
    function toTextarea() {
      syncing = true;
      var md = htmlToMarkdown(ed).replace(/ /g, " ").replace(/\n{3,}/g, "\n\n").trim();
      ta.value = md;
      ta.dispatchEvent(new Event("input", { bubbles: true }));   // обновить живой предпросмотр
      syncing = false;
    }
    ed.addEventListener("input", toTextarea);
    ed.addEventListener("blur", toTextarea);

    // тулбар: применяем оформление к ВЫДЕЛЕНИЮ прямо в редакторе
    var CMD = { "**|**": "bold", "*|*": "italic", "__|__": "underline", "~~|~~": "strikeThrough" };

    function applyWrap(w) {
      ed.focus();
      if (CMD[w]) { try { document.execCommand(CMD[w], false, null); } catch (_) {} }
      else {                                                   // ссылка
        var sel = window.getSelection();
        var text = sel && sel.toString();
        // caret/выделение внутри уже существующей ссылки → снимаем её (toggle)
        if (currentLink()) { try { document.execCommand("unlink", false, null); } catch (_) {} }
        else {
          var url = window.prompt("Ссылка (https://…):", "https://");
          if (url) {
            if (text) { try { document.execCommand("createLink", false, url); } catch (_) {} }
            else { try { document.execCommand("insertHTML", false,
              '<a href="' + url.replace(/"/g, "&quot;") + '">' + escapeHTML(url) + "</a>"); } catch (_) {} }
          }
        }
      }
      toTextarea();
      syncActive();                                            // обновить подсветку кнопок
    }

    if (toolbar) toolbar.querySelectorAll("[data-wrap]").forEach(function (btn) {
      btn.addEventListener("mousedown", function (e) { e.preventDefault(); });  // не терять выделение
      btn.addEventListener("click", function () { applyWrap(btn.getAttribute("data-wrap")); });
    });

    // --- всплывающее меню форматирования по выделению (как в Telegram) -------
    // Появляется у выделенного текста и на ПК, и на телефоне; на мобильном это
    // основной способ форматирования (статичного тулбара там может не быть).
    var pop = document.createElement("div");
    pop.className = "fmt-pop";
    pop.setAttribute("role", "toolbar");
    pop.innerHTML =
      '<button type="button" class="b" data-wrap="**|**" title="Жирный">Ж</button>' +
      '<button type="button" class="i" data-wrap="*|*" title="Курсив">К</button>' +
      '<button type="button" class="u" data-wrap="__|__" title="Подчёркнутый">П</button>' +
      '<button type="button" class="s" data-wrap="~~|~~" title="Зачёркнутый">З</button>' +
      '<button type="button" data-wrap="[|](https://)" title="Ссылка">🔗</button>';
    document.body.appendChild(pop);
    pop.querySelectorAll("[data-wrap]").forEach(function (btn) {
      btn.addEventListener("mousedown", function (e) { e.preventDefault(); });  // не терять выделение
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        applyWrap(btn.getAttribute("data-wrap"));
        positionPop();                                         // оформление меняет геометрию
      });
    });

    // --- отражение АКТИВНОГО форматирования на кнопках --------------------
    // Раньше execCommand оставлял режим (жирный/зачёркнутый/…) «залипшим» на
    // вводе нового текста, а в интерфейсе это никак не показывалось — печатаешь,
    // а буквы вдруг зачёркнуты/в ссылке. Теперь на каждое изменение выделения
    // подсвечиваем кнопки активных стилей (класс .active): видно, что включено,
    // и повторный клик по подсвеченной кнопке это выключает.
    var STATE_CMD = { "**|**": "bold", "*|*": "italic", "__|__": "underline", "~~|~~": "strikeThrough" };

    function currentLink() {
      var sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return null;
      var node = sel.getRangeAt(0).commonAncestorContainer;
      if (node && node.nodeType === 3) node = node.parentNode;
      while (node && node !== ed) {
        if (node.nodeName === "A") return node;
        node = node.parentNode;
      }
      return null;
    }

    function markActive(btn, on) {
      if (!btn) return;
      btn.classList.toggle("active", !!on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    }

    function syncActive() {
      // считаем состояние только когда каретка в редакторе
      var sel = window.getSelection();
      var inEd = sel && sel.rangeCount && ed.contains(
        sel.getRangeAt(0).commonAncestorContainer.nodeType === 3
          ? sel.getRangeAt(0).commonAncestorContainer.parentNode
          : sel.getRangeAt(0).commonAncestorContainer);
      var link = inEd ? currentLink() : null;
      var groups = toolbar ? [toolbar, pop] : [pop];
      groups.forEach(function (box) {
        box.querySelectorAll("[data-wrap]").forEach(function (btn) {
          var w = btn.getAttribute("data-wrap");
          var on = false;
          if (inEd && STATE_CMD[w]) {
            try { on = document.queryCommandState(STATE_CMD[w]); } catch (_) { on = false; }
            // ссылка рисуется подчёркнутой по умолчанию → queryCommandState даёт
            // ложный «underline» внутри <a>, хотя это не __подчёркнутый__ markdown.
            // Гасим индикатор подчёркивания, когда каретка в ссылке.
            if (STATE_CMD[w] === "underline" && link) on = false;
          } else if (!STATE_CMD[w]) {                          // кнопка ссылки
            on = !!link;
          }
          markActive(btn, on);
        });
      });
    }

    function selectionInEditor() {
      var sel = window.getSelection();
      if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
      var r = sel.getRangeAt(0);
      var node = r.commonAncestorContainer;
      if (node.nodeType === 3) node = node.parentNode;
      return ed.contains(node) ? r : null;
    }

    function positionPop() {
      var r = selectionInEditor();
      if (!r) { pop.classList.remove("show"); return; }
      var rect = r.getBoundingClientRect();
      if (!rect.width && !rect.height) { pop.classList.remove("show"); return; }
      pop.classList.add("show");
      var pw = pop.offsetWidth, ph = pop.offsetHeight;
      var left = rect.left + rect.width / 2 - pw / 2;
      left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
      var top = rect.top - ph - 8;
      if (top < 8) top = rect.bottom + 8;                      // не влезло сверху — снизу
      pop.style.left = left + "px";
      pop.style.top = top + "px";
    }

    function maybeShow() { setTimeout(function () { positionPop(); syncActive(); }, 0); }  // даём финализировать выделение
    ed.addEventListener("mouseup", maybeShow);
    ed.addEventListener("keyup", maybeShow);
    ed.addEventListener("input", syncActive);                  // ввод текста меняет активный стиль
    document.addEventListener("selectionchange", function () {
      if (document.activeElement === ed) maybeShow();
    });
    ed.addEventListener("blur", function () {
      setTimeout(function () {                                 // клик по кнопке меню — до blur
        if (!pop.contains(document.activeElement)) pop.classList.remove("show");
      }, 150);
    });
    window.addEventListener("scroll", function () {
      if (pop.classList.contains("show")) positionPop();
    }, true);

    // если форма сабмитится — на всякий случай добиваем актуальный markdown
    if (ta.form) ta.form.addEventListener("submit", function () { if (!syncing) toTextarea(); });
    // fromTextarea — перерисовать редактор из текущего значения textarea (нужно,
    // когда диалог переиспользуется: открыли на редактирование — подставили текст).
    function fromTextarea() { ed.innerHTML = renderMarkup(ta.value) || ""; }
    return { toTextarea: toTextarea, fromTextarea: fromTextarea };
  }

  /* --- Меню «⋯» на карточках списка (делегированно, клик-вне закрывает) -- */
  function cardMenu(root) {
    root = root || document;
    // Идемпотентность: initDates() зовёт cardMenu(document) на КАЖДЫЙ turbo:load.
    // Без защиты на document копились дубли слушателей — второй сразу закрывал
    // только что открытое меню (wasOpen=true), и «⋯» переставало работать.
    if (root.__cardMenuInit) return;
    root.__cardMenuInit = true;
    function closeAll() {
      root.querySelectorAll(".menu.open").forEach(function (m) {
        m.classList.remove("open");
        var b = m.parentNode && m.parentNode.querySelector(".more");
        if (b) b.setAttribute("aria-expanded", "false");
      });
    }
    root.addEventListener("click", function (e) {
      var btn = e.target.closest(".more");
      if (btn && root.contains(btn)) {
        e.stopPropagation();
        var wrap = btn.closest(".menu-wrap") || btn.parentNode;
        var menu = wrap.querySelector(".menu");
        var wasOpen = menu && menu.classList.contains("open");
        closeAll();
        if (menu && !wasOpen) { menu.classList.add("open"); btn.setAttribute("aria-expanded", "true"); }
        return;
      }
      // клик по пункту меню (submit) не трогаем — форма уходит как обычно;
      // любой клик вне меню закрывает открытые
      if (!e.target.closest(".menu")) closeAll();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeAll();
    });
  }

  /* --- POST с прогрессом загрузки (fetch не умеет upload-progress) ------- */
  function postWithProgress(url, formData, onProgress) {    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      if (xhr.upload && onProgress) {
        xhr.upload.addEventListener("progress", (e) => {
          if (e.lengthComputable) onProgress(e.loaded / e.total);
        });
      }
      xhr.onload = () => {
        let j = {};
        try { j = JSON.parse(xhr.responseText || "{}"); } catch (_) {}
        resolve({ status: xhr.status, j });
      };
      xhr.onerror = () => resolve({ status: 0, j: {} });
      xhr.send(formData);
    });
  }

  /* --- «жидкое стекло» для вкладок: скользящий индикатор активной вкладки ----
     Вкладки — обычные ссылки (навигацию ведёт Turbo, подменяя <body>). Индикатор
     на новой странице — НОВЫЙ DOM-узел, поэтому анимацию клика нельзя доиграть на
     уходящей странице (её узел уничтожается — отсюда прежний «обрыв»/дёрганье).
     Решение: запоминаем геометрию индикатора между переходами (UI._tabInd по роли
     контейнера) и на странице-НАЗНАЧЕНИИ плавно перетекаем от прошлой позиции к
     новой активной — одна управляемая анимация, которую видно целиком. */
  function glassTabs(container) {
    if (!container) return;
    if (container.dataset.glassReady) return;   // не навешиваем повторно (Turbo)
    container.dataset.glassReady = "1";
    const tabs = [...container.querySelectorAll("a")];
    if (!tabs.length) return;
    // ключ переноса позиции между переходами: по роли навигации
    const key = container.classList.contains("glass-nav") ? "nav" : "tabs";

    let ind = container.querySelector(".tab-ind");
    if (!ind) {
      ind = document.createElement("span");
      ind.className = "tab-ind";
      container.appendChild(ind);
    }
    const geom = (el) => ({ w: el.offsetWidth, x: el.offsetLeft - container.scrollLeft });
    const put = (g, animate) => {
      if (!animate) container.classList.add("no-anim");
      ind.style.width = g.w + "px";
      ind.style.transform = "translateX(" + g.x + "px)";
      if (!animate) {
        // форсируем reflow и снимаем no-anim, чтобы дальнейшие переходы анимировались
        void ind.offsetWidth;
        container.classList.remove("no-anim");
      }
      _tabInd[key] = g;
    };
    const active = () => container.querySelector("a.on") || tabs[0];

    function settle() {
      const g = geom(active());
      const prev = _tabInd[key];
      // знаем прошлую позицию и она отличается → мгновенно ставим индикатор туда,
      // затем анимируем к новой активной (плавный «перетёк» на видимой странице).
      if (prev && (Math.abs(prev.x - g.x) > 1 || Math.abs(prev.w - g.w) > 1)) {
        put(prev, false);
        requestAnimationFrame(() => put(g, true));
      } else {
        put(g, false);            // первый заход/та же позиция — без анимации
      }
    }
    // после кадра — раскладка и шрифты применились (иначе offsetLeft/Width кривые)
    requestAnimationFrame(settle);

    // репозиционер для общего resize-слушателя: возвращает false, когда контейнер
    // отсоединён от документа (Turbo подменил <body>) — тогда его выкинут из списка.
    // Заодно на регистрации подчищаем уже отсоединённые — список не растёт.
    _tabRepos = _tabRepos.filter(function (fn) { return fn.alive(); });
    var repos = function () {
      if (!container.isConnected) return false;
      put(geom(active()), false);
      return true;
    };
    repos.alive = function () { return container.isConnected; };
    _tabRepos.push(repos);
    tabs.forEach((a) => {
      a.addEventListener("click", (e) => {
        // только левый клик без модификаторов и не «открыть в новой вкладке»
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
        // Индикатор на КЛИКЕ не двигаем: его узел вот-вот уничтожит Turbo, и
        // недоигранная анимация выглядела бы рывком. Позиция уже сохранена в
        // UI._tabInd (settle прошлой страницы) — страница-назначение продолжит
        // от неё. Меняем только подсветку текста активной вкладки.
        tabs.forEach((t) => t.classList.toggle("on", t === a));
      });
    });
  }

  /* --- Инлайн-редактирование текста прямо в превью (click-to-edit) --------
     Оборачивает видимый элемент: клик делает его contenteditable, по blur/Enter
     пишет значение в скрытый <input>/<textarea> и зовёт onChange. Плейсхолдер
     показывается, когда пусто (через :empty + data-ph в CSS). Многострочность —
     для «деталей»; для однострочных Enter завершает ввод. */
  function inlineEdit(opts) {
    var view = opts.view, field = opts.field;
    if (!view || !field) return null;
    var multiline = !!opts.multiline;
    view.setAttribute("contenteditable", "true");
    view.setAttribute("role", "textbox");
    if (opts.placeholder) view.setAttribute("data-ph", opts.placeholder);

    function toField() {
      var val = (view.innerText || "").replace(/ /g, " ");
      // Пустое поле показывает подсказку через CSS `::before { content: attr(data-ph) }`.
      // В Chrome `innerText` ВКЛЮЧАЕТ содержимое ::before — из-за этого в скрытое
      // поле попадал текст плейсхолдера вместо "" (ложное «превью изменено» и мусор
      // при сохранении). `textContent` генерируемый контент не включает — им и
      // определяем реальную пустоту.
      if (!(view.textContent || "").trim()) { val = ""; }
      else if (!multiline) val = val.replace(/\s*\n\s*/g, " ").trim();
      else val = val.replace(/\n{3,}/g, "\n\n").replace(/[ \t]+$/gm, "");
      field.value = val;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      if (opts.onChange) opts.onChange(val);
    }
    view.addEventListener("input", toField);
    view.addEventListener("blur", toField);
    if (!multiline) {
      view.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); view.blur(); }
      });
    }
    return { toField: toField,
             set: function (v) { view.innerText = v || ""; toField(); } };
  }

  /* --- Виджет времени: день (native date) + «ЧЧ:ММ–ЧЧ:ММ» кликом ----------
     Пишет в скрытые starts_at/ends_at в формате YYYY-MM-DDTHH:MM (как datetime-
     local). День — обязателен для времени; без дня время не уходит на сервер.
     Части ЧЧ/ММ — редактируемые кликом (вводишь число). Быстрых чипов нет (#13). */
  function timeRange(root) {
    var dayInput = root.querySelector("[data-tr-day]");    // скрытый YYYY-MM-DD
    var sh = root.querySelector("[data-tr-hh]"), sm = root.querySelector("[data-tr-mm]");
    var eh = root.querySelector("[data-tr-ehh]"), em = root.querySelector("[data-tr-emm]");
    // D1: дата — такими же кликабельными частями, как время (ДД / ММ / ГГГГ)
    var dd = root.querySelector("[data-tr-dd]"), mo = root.querySelector("[data-tr-mo]"),
        yy = root.querySelector("[data-tr-yy]");
    // скрытые поля формы (могут лежать вне root — ищем и глобально)
    var startHidden = root.querySelector("[data-tr-start]") || document.querySelector("[data-tr-start]");
    var endHidden = root.querySelector("[data-tr-end]") || document.querySelector("[data-tr-end]");
    if (!dayInput || !startHidden) return null;
    var onChange = null;

    function pad(n) { return String(n).padStart(2, "0"); }
    function clampNum(el, max, fallback) {
      var d = (el.innerText || "").replace(/\D/g, "").slice(-2);
      var n = d === "" ? null : Math.min(parseInt(d, 10), max);
      return { has: n !== null, val: n === null ? fallback : n };
    }
    // как clampNum, но с диапазоном [min..max] и произвольной длиной (для года)
    function clampRange(el, len, min, max) {
      var d = (el.innerText || "").replace(/\D/g, "").slice(-len);
      if (d === "") return { has: false, val: min };
      var n = Math.min(Math.max(parseInt(d, 10), min), max);
      return { has: true, val: n };
    }
    function part(el, max) {
      // делает span редактируемым числом 00..max (2 цифры)
      el.setAttribute("contenteditable", "true");
      el.addEventListener("focus", function () { el._had = el.innerText; });
      el.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); el.blur(); }
        if (e.key.length === 1 && !/[0-9]/.test(e.key)) e.preventDefault();
      });
      el.addEventListener("input", function () {
        var d = (el.innerText || "").replace(/\D/g, "");
        if (d.length >= 2) { el.innerText = d.slice(-2); placeCaretEnd(el); sync(); }
      });
      el.addEventListener("blur", function () {
        var r = clampNum(el, max, 0);
        el.innerText = r.has ? pad(r.val) : "00";
        sync();
      });
    }
    // редактируемая часть даты: день (1..31), месяц (1..12), год (4 цифры)
    function datePart(el, len, min, max, padTo) {
      if (!el) return;
      el.setAttribute("contenteditable", "true");
      el.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); el.blur(); }
        if (e.key.length === 1 && !/[0-9]/.test(e.key)) e.preventDefault();
      });
      el.addEventListener("input", function () {
        var d = (el.innerText || "").replace(/\D/g, "");
        if (d.length >= len) { el.innerText = d.slice(-len); placeCaretEnd(el); sync(); }
      });
      el.addEventListener("blur", function () {
        var r = clampRange(el, len, min, max);
        el.innerText = r.has ? String(r.val).padStart(padTo, "0") : "";
        sync();
      });
    }
    function placeCaretEnd(el) {
      try {
        var r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
        var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
      } catch (_) {}
    }

    // собрать YYYY-MM-DD из трёх частей даты (или "" если задана не полностью)
    function composeDay() {
      if (!dd) return dayInput.value;      // нет виджета даты — берём как есть
      var d = clampRange(dd, 2, 1, 31), m = clampRange(mo, 2, 1, 12),
          y = clampRange(yy, 4, 1970, 2100);
      if (d.has && m.has && y.has) {
        return y.val + "-" + pad(m.val) + "-" + pad(d.val);
      }
      return "";
    }

    function sync() {
      var day = composeDay();                         // YYYY-MM-DD или ""
      dayInput.value = day;
      var shv = clampNum(sh, 23, 0), smv = clampNum(sm, 59, 0);
      var ehv = clampNum(eh, 23, 0), emv = clampNum(em, 59, 0);
      // «начало задано» = есть день И заданы часы начала (иначе пусто)
      if (day && (shv.has || smv.has)) {
        startHidden.value = day + "T" + pad(shv.val) + ":" + pad(smv.val);
      } else {
        startHidden.value = "";
      }
      // конец — только если задан день и хотя бы одна часть конца, и есть начало
      if (day && startHidden.value && (ehv.has || emv.has)) {
        endHidden.value = day + "T" + pad(ehv.val) + ":" + pad(emv.val);
      } else {
        endHidden.value = "";
      }
      startHidden.dispatchEvent(new Event("input", { bubbles: true }));
      if (onChange) onChange();
    }

    part(sh, 23); part(sm, 59); part(eh, 23); part(em, 59);
    datePart(dd, 2, 1, 31, 2); datePart(mo, 2, 1, 12, 2); datePart(yy, 4, 1970, 2100, 4);
    dayInput.addEventListener("input", sync);
    dayInput.addEventListener("change", sync);
    // начальное состояние из уже подставленных hidden-значений
    (function initFrom() {
      function fill(hidden) {
        var v = hidden && hidden.value;               // YYYY-MM-DDTHH:MM
        if (!v) return null;
        var t = v.split("T");
        return { day: t[0], hh: (t[1] || "").slice(0, 2), mm: (t[1] || "").slice(3, 5) };
      }
      var s = fill(startHidden), e = fill(endHidden);
      if (s) {
        dayInput.value = s.day;
        if (dd && s.day) {
          var p = s.day.split("-");                    // [YYYY, MM, DD]
          yy.innerText = p[0] || ""; mo.innerText = p[1] || ""; dd.innerText = p[2] || "";
        }
        sh.innerText = s.hh || "00"; sm.innerText = s.mm || "00";
      }
      if (e) { eh.innerText = e.hh || "00"; em.innerText = e.mm || "00"; }
    })();

    return { sync: sync, onChange: function (fn) { onChange = fn; } };
  }

  return { sortable, burst, uploader, mediaUploader, dateChips, postWithProgress,
           editorPreview, richEditor, cardMenu, renderMarkup, glassTabs,
           inlineEdit: inlineEdit, timeRange: timeRange };
})();
