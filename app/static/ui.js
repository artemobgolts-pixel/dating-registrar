/* Общие интерактивные кусочки гостевой страницы и админки.
   Без зависимостей; работает мышью и пальцем (pointer events). */
window.UI = (() => {
  "use strict";

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

    const end = () => {
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
                      onChange, onError, kind, focusable, onFocus }) {
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
      clear() { files = []; focuses = []; sync(); },
    };
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

      var pay = field("pay");
      if (pay && pv.pay) pv.pay.hidden = !pay.checked;

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
    if (toolbar) toolbar.querySelectorAll("[data-wrap]").forEach(function (btn) {
      btn.addEventListener("mousedown", function (e) { e.preventDefault(); });  // не терять выделение
      btn.addEventListener("click", function () {
        ed.focus();
        var w = btn.getAttribute("data-wrap");
        if (CMD[w]) { try { document.execCommand(CMD[w], false, null); } catch (_) {} }
        else {                                                   // ссылка
          var sel = window.getSelection();
          var text = sel && sel.toString();
          var url = window.prompt("Ссылка (https://…):", "https://");
          if (url) {
            if (text) { try { document.execCommand("createLink", false, url); } catch (_) {} }
            else { try { document.execCommand("insertHTML", false,
              '<a href="' + url.replace(/"/g, "&quot;") + '">' + escapeHTML(url) + "</a>"); } catch (_) {} }
          }
        }
        toTextarea();
      });
    });

    // если форма сабмитится — на всякий случай добиваем актуальный markdown
    if (ta.form) ta.form.addEventListener("submit", function () { if (!syncing) toTextarea(); });
    return { toTextarea: toTextarea };
  }

  /* --- Меню «⋯» на карточках списка (делегированно, клик-вне закрывает) -- */
  function cardMenu(root) {
    root = root || document;
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

  return { sortable, burst, uploader, dateChips, postWithProgress,
           editorPreview, richEditor, cardMenu, renderMarkup };
})();
