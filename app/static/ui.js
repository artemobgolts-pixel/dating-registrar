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
                      onChange, onError, kind }) {
    let files = [];
    let urls = [];
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
          w.querySelector("img").src = u;
        }
        w.querySelector(".rm").addEventListener("click", () => {
          files.splice(idx, 1);
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

    sortable(preview, {
      selector: ".ptile.new",
      onChange: () => {
        files = [...preview.querySelectorAll(".ptile.new")].map((t) => files[+t.dataset.idx]);
        sync();
      },
    });

    return { files: () => files.slice(), clear() { files = []; sync(); } };
  }

  /* --- Чипы быстрого выбора даты и времени ------------------------------ */
  function dateChips(root, startInput, endInput) {
    const pad = (n) => String(n).padStart(2, "0");
    const dstr = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const flash = (el) => { el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 450); };

    root.querySelectorAll("[data-day]").forEach((ch) => {
      ch.addEventListener("click", () => {
        const kind = ch.dataset.day;
        const d = new Date();
        if (kind === "tomorrow") d.setDate(d.getDate() + 1);
        if (kind === "sat") d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7 || 7));
        if (kind === "sun") d.setDate(d.getDate() + ((0 - d.getDay() + 7) % 7 || 7));
        const t = (startInput.value.split("T")[1]) || "19:00";
        startInput.value = `${dstr(d)}T${t}`;
        flash(startInput);
      });
    });
    root.querySelectorAll("[data-time]").forEach((ch) => {
      ch.addEventListener("click", () => {
        const day = (startInput.value.split("T")[0]) || dstr(new Date());
        startInput.value = `${day}T${ch.dataset.time}`;
        flash(startInput);
      });
    });
    root.querySelectorAll("[data-dur]").forEach((ch) => {
      ch.addEventListener("click", () => {
        if (!endInput) return;
        if (!startInput.value) { flash(startInput); return; }
        const d = new Date(startInput.value);
        d.setMinutes(d.getMinutes() + Number(ch.dataset.dur) * 60);
        endInput.value = `${dstr(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
        flash(endInput);
      });
    });
  }

  /* --- POST с прогрессом загрузки (fetch не умеет upload-progress) ------- */
  function postWithProgress(url, formData, onProgress) {
    return new Promise((resolve) => {
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

  return { sortable, burst, uploader, dateChips, postWithProgress };
})();
