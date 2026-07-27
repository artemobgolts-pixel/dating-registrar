/* date4you — весь JS гостевой страницы.
   Вынесен из шаблона: кэшируется браузером, под CSP грузится как 'self'.
   Параметры приходят через data-атрибуты <body>: token, name, mybooking. */
(() => {
  "use strict";
  const TOKEN = document.body.dataset.token;
  let MYNAME = document.body.dataset.name || "";
  const AUTH = document.body.dataset.auth === "1";   // залогинен ли посетитель
  // База для действий: на странице категории — /c/<токен>, на странице
  // отдельного свидания (шаринг) — /d/<токен>. Бэкенд там и там даёт
  // совместимые ручки book/question/suggest_time.
  const ACT = document.body.dataset.actionBase || ("/c/" + TOKEN);

  const $ = (s) => document.querySelector(s);
  const toastEl = $("#toast");
  let toastTimer;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2600);
  }

  /* Вход обязателен для любого действия. Аноним видит окно входа (модалку с
     Telegram-виджетом) прямо здесь; после входа Telegram вернёт на эту же
     страницу (адрес возврата передаётся прямо в способ входа). Если модалки нет — фолбэк
     на страницу /login. */
  const loginDlg = $("#loginDlg");
  function goLogin() {
    if (loginDlg && typeof loginDlg.showModal === "function") {
      loginDlg.showModal();
      return;
    }
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
  }
  if (loginDlg) {
    const lc = $("#loginClose");
    // любой триггер входа: угловая кнопка #loginOpen и любой [data-login-open]
    // (например «Войти» в CTA на странице свидания) открывают ту же модалку
    document.addEventListener("click", (e) => {
      if (e.target.closest("#loginOpen, [data-login-open]")) {
        e.preventDefault();
        loginDlg.showModal();
      }
    });
    if (lc) lc.addEventListener("click", () => loginDlg.close());
    loginDlg.addEventListener("click", (e) => { if (e.target === loginDlg) loginDlg.close(); });
  }
  function requireAuth(fn) {
    if (!AUTH) { goLogin(); return; }
    fn();
  }

  /* плавное появление карточек по очереди */
  document.querySelectorAll("section.cards").forEach((sec) => {
    [...sec.querySelectorAll(".card")].forEach((c, i) =>
      c.style.setProperty("--i", Math.min(i, 7)));
  });

  async function post(url, fd) {
    let r;
    try { r = await fetch(url, { method: "POST", body: fd }); }
    catch (_) { toast("Нет связи — попробуй ещё раз"); return { ok: false }; }
    let j = {};
    try { j = await r.json(); } catch (_) {}
    if (r.status === 401 && j.detail && j.detail.need_login) { goLogin(); return { ok: false }; }
    if (!r.ok) {
      const d = j.detail;
      toast(typeof d === "string" ? d : (d && d.msg) || "Что-то пошло не так");
      return { ok: false };
    }
    return { ok: true, j };
  }

  /* --- выбор свидания: обновляем карточки на месте, без перезагрузки -------*/
  function setCardState(card, mine) {
    const btn = card.querySelector(".btn.book");
    if (btn) {
      btn.classList.toggle("on", mine);
      btn.textContent = mine ? "Выбрано ♥" : "Выбрать ♥";
      btn.title = mine ? "Нажми, чтобы отменить выбор" : "";
    }
    card.classList.toggle("booked-me", mine);
    const seal = card.querySelector(".seal");
    if (seal) seal.hidden = !mine;                 // восковая печать ♥ (только карточки с фото)
    const who = card.querySelector(".bo-who");     // подпись на оверлее
    if (who) who.textContent = mine ? (MYNAME || "ты ♥") : "";
  }

  async function doBook(btn) {
    const fd = new FormData();
    fd.append("date_id", btn.dataset.id);
    const res = await post(`${ACT}/book`, fd);
    if (!res.ok) return;
    const card = btn.closest(".card");
    const id = Number(btn.dataset.id);
    if (res.j.booked) {
      setCardState(card, true);
      const r = btn.getBoundingClientRect();
      UI.burst(r.left + r.width / 2, r.top + 6);
      const cr = card.getBoundingClientRect();     // второй залп — из сердца карточки
      setTimeout(() => UI.burst(cr.left + cr.width / 2,
                                Math.max(70, cr.top + cr.height / 2)), 140);
      card.classList.remove("glow"); void card.offsetWidth;   // перезапуск анимации
      card.classList.add("glow");
      toast("Голос учтён ♥");
    } else {
      setCardState(card, false);
      toast("Голос снят");
    }
    // Single-режим мог одновременно снять выбор с другой карточки, а общий
    // прогресс и открытый список участников меняются у всех голосующих.
    setTimeout(() => location.reload(), 420);
  }
  document.querySelectorAll(".btn.book[data-id]").forEach((b) => {
    b.addEventListener("click", () => requireAuth(() => doBook(b)));
  });

  document.querySelectorAll(".withdraw-vote").forEach((b) => {
    b.addEventListener("click", () => requireAuth(async () => {
      if (!confirm("Отказаться от участия? Победитель и результат голосования не изменятся.")) return;
      const withdrawUrl = document.body.dataset.withdrawUrl || (`/c/${TOKEN}/withdraw`);
      const res = await post(withdrawUrl, new FormData());
      if (!res.ok) return;
      toast("Организатор уведомлён");
      setTimeout(() => location.reload(), 500);
    }));
  });

  /* --- вопрос --------------------------------------------------------------*/
  const askDlg = $("#askDlg"), askForm = $("#askForm");
  document.querySelectorAll(".btn.ask").forEach((b) => {
    b.addEventListener("click", () => requireAuth(() => {
      $("#askDateId").value = b.dataset.id;
      $("#askTitle").textContent = b.dataset.name;
      $("#askText").value = "";
      askDlg.showModal();
    }));
  });
  $("#askCancel").onclick = () => askDlg.close();
  askForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = await post(`${ACT}/question`, new FormData(askForm));
    if (!res.ok) return;
    askDlg.close();
    toast("Вопрос отправлен 💌 Ответ появится здесь же");
    setTimeout(() => location.reload(), 1200);
  });

  /* --- жалоба --------------------------------------------------------------*/
  const reportDlg = $("#reportDlg"), reportForm = $("#reportForm");
  document.querySelectorAll(".report-link[data-id]").forEach((b) => {
    b.addEventListener("click", () => requireAuth(() => {
      $("#reportType").value = "date";
      $("#reportTargetId").value = b.dataset.id;
      $("#reportTitle").textContent = b.dataset.name;
      $("#reportReason").value = "";
      reportDlg.showModal();
    }));
  });
  $("#reportCancel").onclick = () => reportDlg.close();
  reportForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = await post(`${ACT}/report`, new FormData(reportForm));
    if (!res.ok) return;
    reportDlg.close();
    toast("Спасибо, жалоба отправлена. Модератор проверит 🙏");
  });

  /* --- «назначить дату»: гость предлагает время ----------------------------*/
  const timeDlg = $("#timeDlg"), timeForm = $("#timeForm");
  UI.dateChips(timeDlg, $("#timeStart"), $("#timeEnd"));
  document.querySelectorAll(".chip-suggest").forEach((b) => {
    b.addEventListener("click", () => requireAuth(() => {
      $("#timeDateId").value = b.dataset.id;
      $("#timeTitle").textContent = b.dataset.name;
      timeForm.reset();
      $("#timeDateId").value = b.dataset.id;
      timeDlg.showModal();
    }));
  });
  $("#timeCancel").onclick = () => timeDlg.close();
  timeForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = await post(`${ACT}/suggest_time`, new FormData(timeForm));
    if (!res.ok) return;
    timeDlg.close();
    toast("Предложение отправлено 📅");
    setTimeout(() => location.reload(), 1100);
  });

  /* --- предложение / редактирование ----------------------------------------*/
  const propDlg = $("#propDlg"), propForm = $("#propForm");
  // Блок «предложить своё свидание» есть только на странице категории. На
  // странице отдельного свидания (шаринг) этих элементов нет — пропускаем.
  if (propDlg && propForm) {
  const propSlides = $("#propSlides");
  let editId = null, removed = new Set(), savedPhotos = [];
  let curVid = null, removedVid = false;
  let propSlide = 0, propObjectUrls = [];
  const MAX_PHOTOS = 5;
  const photoPreview = document.createElement("div");
  const videoPreview = document.createElement("div");

  const up = UI.uploader({
    zone: $("#propZone"),
    input: $("#propFiles"),
    preview: photoPreview,
    max: MAX_PHOTOS,
    keptCount: () => savedPhotos.filter((p) => !removed.has(p.id)).length,
    onError: toast,
    onChange: renderPropGallery,
    noZoneBind: true,                 // дроп-зону держит общий mediaUploader
  });
  const upv = UI.uploader({
    zone: $("#propZone"),
    input: $("#propVideo"),
    preview: videoPreview,
    kind: "video",
    max: 1,
    // если у предложения уже есть видео и его не удалили — слот занят
    keptCount: () => (curVid && !removedVid) ? 1 : 0,
    onError: toast,
    onChange: renderPropGallery,
    noZoneBind: true,                 // дроп-зону держит общий mediaUploader
  });
  // общий блок «Медиа»: одна зона принимает и фото, и видео
  if (UI.mediaUploader) UI.mediaUploader({
    zone: $("#propZone"),
    input: $("#propMedia"),
    photo: up,
    video: upv,
    onError: toast,
  });
  if (UI.numberSteppers) UI.numberSteppers(propForm);
  $("#propAddMedia").addEventListener("click", (e) => {
    e.stopPropagation();
    $("#propMedia").click();
  });

  // Редактирование происходит прямо внутри карточки — как в кабинете.
  var propTitleEdit = UI.inlineEdit({
    view: $("#propEdTitle"), field: propForm.querySelector('[name="name"]'),
  });
  var propPlaceEdit = UI.inlineEdit({
    view: $("#propEdPlace"), field: propForm.querySelector('[name="place"]'),
  });
  var propLinksEdit = UI.inlineEdit({
    view: $("#propEdLinks"), field: $("#propLinks"), multiline: true,
  });
  var propRich = UI.richEditor({
    textarea: $("#propComment"),
    editable: $("#propDescEditable"),
    toolbar: $("#propDescToolbar"),
  });
  var propTime = UI.timeRange($("#propEdWhen"));
  $("#propDescEditable").addEventListener("focus", () => {
    $("#propDescToolbar").hidden = false;
  });
  $("#propDescEditable").addEventListener("blur", () => {
    setTimeout(() => {
      if (!$("#propDescToolbar").contains(document.activeElement)) {
        $("#propDescToolbar").hidden = true;
      }
    }, 180);
  });
  $("#propDescToolbar").addEventListener("mousedown", (e) => e.preventDefault());

  const PAY = { "1": "💸 50/50", "2": "👌 Я плачу", "3": "🫵 Ты платишь" };
  function syncPropPay() {
    const selected = propForm.querySelector('input[name="pay"]:checked');
    const text = PAY[selected ? selected.value : "0"] || "";
    const onPhoto = text && propSlides.children.length > 0;
    $("#propPayPhoto").textContent = text;
    $("#propPayPhoto").hidden = !onPhoto;
    $("#propPayPill").textContent = text;
    $("#propPayPill").hidden = !text || onPhoto;
  }
  propForm.querySelectorAll('input[name="pay"]').forEach((r) => {
    r.addEventListener("change", syncPropPay);
  });

  function setPropTime(start, end) {
    const root = $("#propEdWhen");
    const parts = (start || "").split("T");
    const day = (parts[0] || "").split("-");
    const st = parts[1] || "";
    const et = ((end || "").split("T")[1] || "");
    root.querySelector("[data-tr-yy]").textContent = day[0] || "";
    root.querySelector("[data-tr-mo]").textContent = day[1] || "";
    root.querySelector("[data-tr-dd]").textContent = day[2] || "";
    root.querySelector("[data-tr-hh]").textContent = st.slice(0, 2);
    root.querySelector("[data-tr-mm]").textContent = st.slice(3, 5);
    root.querySelector("[data-tr-ehh]").textContent = et.slice(0, 2);
    root.querySelector("[data-tr-emm]").textContent = et.slice(3, 5);
    if (propTime) propTime.sync();
  }

  function removeNew(upload, idx) {
    const files = upload.files();
    files.splice(idx, 1);
    upload.clear();
    if (files.length) upload.addFiles(files);
  }

  function renderPropGallery() {
    propObjectUrls.forEach((url) => URL.revokeObjectURL(url));
    propObjectUrls = [];
    propSlides.replaceChildren();
    const items = [];
    savedPhotos.filter((p) => !removed.has(p.id)).forEach((p) => {
      items.push({ kind: "image", src: `/c/${TOKEN}/image/${p.filename}`, saved: p.id });
    });
    up.files().forEach((file, idx) => {
      const src = URL.createObjectURL(file);
      propObjectUrls.push(src);
      items.push({ kind: "image", src: src, upload: up, idx: idx });
    });
    if (curVid && !removedVid) {
      items.push({ kind: "video", src: `/c/${TOKEN}/video/${curVid.filename}`, savedVideo: true });
    }
    upv.files().forEach((file, idx) => {
      const src = URL.createObjectURL(file);
      propObjectUrls.push(src);
      items.push({ kind: "video", src: src, upload: upv, idx: idx });
    });
    items.forEach((item, idx) => {
      const slide = document.createElement("div");
      slide.className = "ed-slide";
      if (item.kind === "video") {
        slide.innerHTML = '<video controls muted playsinline preload="metadata"></video>' +
          '<button type="button" class="ed-slide-rm" aria-label="Удалить видео">✕</button>';
        slide.querySelector("video").src = item.src;
      } else {
        slide.innerHTML = '<img alt="" draggable="false">' +
          '<button type="button" class="ed-slide-rm" aria-label="Удалить фото">✕</button>';
        slide.querySelector("img").src = item.src;
      }
      slide.querySelector(".ed-slide-rm").addEventListener("click", (event) => {
        event.stopPropagation();
        if (item.saved) {
          removed.add(item.saved);
          renderPropGallery();
        } else if (item.savedVideo) {
          removedVid = true;
          renderPropGallery();
        } else {
          removeNew(item.upload, item.idx);
        }
      });
      propSlides.appendChild(slide);
      slide.hidden = idx !== propSlide;
    });
    propSlide = Math.min(propSlide, Math.max(0, items.length - 1));
    [...propSlides.children].forEach((slide, idx) => { slide.hidden = idx !== propSlide; });
    $("#propEmpty").hidden = items.length > 0;
    function setEdge(button, canNavigate, direction) {
      const canAdd = savedPhotos.filter((p) => !removed.has(p.id)).length +
        up.files().length < MAX_PHOTOS || (!curVid || removedVid) && upv.files().length < 1;
      button.hidden = !canNavigate && !canAdd;
      button.classList.toggle("as-add", !canNavigate && canAdd);
      button.textContent = canNavigate ? (direction === "prev" ? "‹" : "›") : "+";
    }
    setEdge($("#propPrev"), propSlide > 0, "prev");
    setEdge($("#propNext"), propSlide < items.length - 1, "next");
    const dots = $("#propDots");
    dots.replaceChildren();
    if (items.length > 1) {
      items.forEach((_, idx) => {
        const dot = document.createElement("i");
        if (idx === propSlide) dot.className = "on";
        dots.appendChild(dot);
      });
    }
    syncPropPay();
  }
  function movePropSlide(delta) {
    const count = propSlides.children.length;
    if (!count) return;
    propSlide = (propSlide + delta + count) % count;
    renderPropGallery();
  }
  $("#propPrev").addEventListener("click", (e) => {
    e.stopPropagation();
    if (e.currentTarget.classList.contains("as-add")) $("#propMedia").click();
    else movePropSlide(-1);
  });
  $("#propNext").addEventListener("click", (e) => {
    e.stopPropagation();
    if (e.currentTarget.classList.contains("as-add")) $("#propMedia").click();
    else movePropSlide(1);
  });

  function openPropose(meta) {
    propForm.reset();
    up.clear();
    removed = new Set();
    savedPhotos = (meta && meta.photos) ? meta.photos.slice() : [];
    curVid = (meta && meta.videos && meta.videos[0]) || null;
    removedVid = false;
    propSlide = 0;
    upv.clear();
    var payValue = String((meta && meta.pay) || 0);
    propForm.querySelectorAll('input[name="pay"]').forEach((input) => {
      input.checked = input.value === payValue;
    });
    $("#propCapacity").value = (meta && meta.capacity) || 1;
    editId = meta ? meta.id : null;
    $("#propHead").textContent = meta ? "Изменить своё свидание" : "Создать своё свидание";
    $("#propSubmit").textContent = meta ? "Сохранить изменения" : "Создать своё свидание";
    propTitleEdit.set(meta ? meta.name : "");
    propPlaceEdit.set(meta ? meta.place : "");
    propLinksEdit.set(meta ? meta.links : "");
    $("#propComment").value = meta ? meta.comment : "";
    setPropTime(meta ? meta.starts_at : "", meta ? meta.ends_at : "");
    // редактор форматирования переинициализируем из textarea (reset её очистил,
    // а для редактирования мы только что подставили сохранённый текст)
    if (propRich) propRich.fromTextarea();
    if (UI.numberSteppers) UI.numberSteppers(propForm);
    renderPropGallery();
    propDlg.showModal();
  }

  const fabPropose = $("#fabPropose");
  if (fabPropose) fabPropose.onclick = () => requireAuth(() => openPropose(null));
  document.querySelectorAll(".mine-actions .edit").forEach((b) => {
    b.addEventListener("click", () => requireAuth(() => openPropose(JSON.parse(b.dataset.meta))));
  });
  $("#propCancel").onclick = () => propDlg.close();
  $("#propCancelBottom").onclick = () => propDlg.close();

  propForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(propForm);
    let url = `/c/${TOKEN}/propose`;
    if (editId) {
      url = `/c/${TOKEN}/propose/${editId}/edit`;
      removed.forEach((id) => fd.append("remove_image", id));
      const keep = savedPhotos
        .filter((photo) => !removed.has(photo.id))
        .map((photo) => photo.id);
      fd.append("keep_order", keep.join(","));
      if (removedVid && curVid) fd.append("remove_video", curVid.id);
    }
    const bar = $("#propBar"), fill = bar.querySelector("i");
    const sub = $("#propSubmit");
    sub.disabled = true;
    if (up.files().length) { fill.style.width = "0%"; bar.hidden = false; }
    const raw = await UI.postWithProgress(url, fd, (p) => {
      fill.style.width = Math.round(p * 100) + "%";
    });
    sub.disabled = false;
    bar.hidden = true;
    if (raw.status === 401 && raw.j.detail && raw.j.detail.need_login) {
      goLogin();
      return;
    }
    if (raw.status < 200 || raw.status >= 300) {
      const d = raw.j.detail;
      toast(raw.status === 0 ? "Нет связи — попробуй ещё раз"
            : (typeof d === "string" ? d : (d && d.msg) || "Что-то пошло не так"));
      return;
    }
    propDlg.close();
    toast(editId ? "Сохранено ♥"
          : (raw.j.moderated ? "Отправлено! Появится после проверки ⏳" : "Добавлено! Спасибо за идею ♥"));
    setTimeout(() => location.reload(), 1100);
  });

  document.querySelectorAll(".mine-actions .del").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!AUTH) { goLogin(); return; }
      if (!confirm(`Удалить «${b.dataset.name}»?`)) return;
      const res = await post(`/c/${TOKEN}/propose/${b.dataset.id}/delete`, new FormData());
      if (!res.ok) return;
      toast("Удалено");
      setTimeout(() => location.reload(), 700);
    });
  });
  }  /* /if (propDlg) — конец блока «предложить свидание» */

  /* --- календарь -------------------------------------------------------------*/
  const calDlg = $("#calDlg");
  document.querySelectorAll(".cal-link").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      $("#calGoogle").href = a.dataset.gcal;
      $("#calIcs").href = a.dataset.ics;
      calDlg.showModal();
    });
  });
  $("#calCancel").onclick = () => calDlg.close();
  $("#calGoogle").addEventListener("click", () => setTimeout(() => calDlg.close(), 300));
  $("#calIcs").addEventListener("click", () => setTimeout(() => calDlg.close(), 300));

  /* --- галереи: точки, счётчик, стрелки ----------------------------------------*/
  document.querySelectorAll(".gal-wrap").forEach((w) => {
    const g = w.querySelector(".gallery");
    const n = g.children.length;          // фото и видео — единая лента
    if (n < 2) return;
    const dots = document.createElement("div");
    dots.className = "gal-dots";
    for (let k = 0; k < n; k++) dots.appendChild(document.createElement("i"));
    const cnt = document.createElement("div");
    cnt.className = "gal-count";
    const prev = document.createElement("button");
    prev.type = "button"; prev.className = "gal-nav prev"; prev.textContent = "‹";
    prev.setAttribute("aria-label", "Предыдущее фото");
    const next = document.createElement("button");
    next.type = "button"; next.className = "gal-nav next"; next.textContent = "›";
    next.setAttribute("aria-label", "Следующее фото");
    w.append(dots, cnt, prev, next);
    const upd = () => {
      const i = Math.round(g.scrollLeft / Math.max(1, g.clientWidth));
      [...dots.children].forEach((d, k) => d.classList.toggle("on", k === i));
      cnt.textContent = (i + 1) + "/" + n;
      prev.disabled = i === 0;
      next.disabled = i === n - 1;
    };
    upd();
    g.addEventListener("scroll", () => requestAnimationFrame(upd), { passive: true });
    prev.addEventListener("click", (e) => { e.stopPropagation(); g.scrollBy({ left: -g.clientWidth, behavior: "smooth" }); });
    next.addEventListener("click", (e) => { e.stopPropagation(); g.scrollBy({ left: g.clientWidth, behavior: "smooth" }); });
  });

  /* --- лайтбокс: листание всех фото карточки -----------------------------------*/
  const lb = $("#lightbox"), lbImg = lb.querySelector("img"), lbCnt = $("#lbCount");
  const lbPrev = $("#lbPrev"), lbNext = $("#lbNext");
  let lbList = [], lbI = 0;
  function lbShow(i) {
    lbI = Math.max(0, Math.min(i, lbList.length - 1));
    lbImg.src = lbList[lbI];
    lbCnt.textContent = (lbI + 1) + "/" + lbList.length;
    const single = lbList.length < 2;
    lbCnt.hidden = single;
    lbPrev.hidden = lbNext.hidden = single;
    lbPrev.disabled = lbI === 0;
    lbNext.disabled = lbI === lbList.length - 1;
  }
  function lbClose() { lb.classList.remove("open"); lbImg.src = ""; }
  document.addEventListener("click", (e) => {
    const img = e.target.closest(".gallery img");
    if (!img) return;
    const imgs = [...img.closest(".gallery").querySelectorAll("img")];
    lbList = imgs.map((x) => x.src);
    lb.classList.add("open");
    lbShow(imgs.indexOf(img));
  });
  lbPrev.addEventListener("click", (e) => { e.stopPropagation(); lbShow(lbI - 1); });
  lbNext.addEventListener("click", (e) => { e.stopPropagation(); lbShow(lbI + 1); });
  $("#lbX").addEventListener("click", lbClose);
  lb.addEventListener("click", (e) => { if (e.target === lb || e.target === lbImg) lbClose(); });
  document.addEventListener("keydown", (e) => {
    if (!lb.classList.contains("open")) return;
    if (e.key === "Escape") lbClose();
    if (e.key === "ArrowLeft") lbShow(lbI - 1);
    if (e.key === "ArrowRight") lbShow(lbI + 1);
  });
  let touchX = null;
  lb.addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, { passive: true });
  lb.addEventListener("touchend", (e) => {
    if (touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    touchX = null;
    if (Math.abs(dx) > 44) lbShow(lbI + (dx < 0 ? 1 : -1));
  }, { passive: true });
})();
