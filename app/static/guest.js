/* date4you — весь JS гостевой страницы.
   Вынесен из шаблона: кэшируется браузером, под CSP грузится как 'self'.
   Параметры приходят через data-атрибуты <body>: token, name, mybooking. */
(() => {
  "use strict";
  const TOKEN = document.body.dataset.token;
  const CSRF = document.body.dataset.csrf || "";
  let MYNAME = document.body.dataset.name || "";
  const AUTH = document.body.dataset.auth === "1";   // залогинен ли посетитель
  const FRIENDS = (document.body.dataset.skin ||
    document.documentElement.dataset.skin) === "friends";
  const CHECK_ICON =
    '<svg class="ui-icon ui-icon-check" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
      '<circle cx="12" cy="12" r="9"></circle><path d="m8 12 2.7 2.8L16.6 9"></path>' +
    '</svg>';
  // База для действий: на странице категории — /c/<токен>, на странице
  // отдельного события (шаринг) — /d/<токен>. Бэкенд там и там даёт
  // совместимые ручки book/question/suggest_time.
  const ACT = document.body.dataset.actionBase || ("/c/" + TOKEN);

  const $ = (s) => document.querySelector(s);
  const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");
  const GESTURE_HYSTERESIS = 10;
  const toastEl = $("#toast");
  let toastTimer;
  function toast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2600);
  }

  // Компактное меню аккаунта остаётся нативным <details>, но закрывается как
  // привычный popover: снаружи и по Escape, с возвратом фокуса на trigger.
  const accountMenus = Array.from(document.querySelectorAll(".public-account-menu"));
  if (accountMenus.length) {
    document.addEventListener("pointerdown", (event) => {
      accountMenus.forEach((menu) => {
        if (menu.open && !menu.contains(event.target)) menu.open = false;
      });
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const menu = accountMenus.find((item) => item.open);
      if (!menu) return;
      menu.open = false;
      const trigger = menu.querySelector("summary");
      if (trigger) trigger.focus();
    });
  }

  // Apple-style projection: the release position is not the resting position.
  // A short history smooths noisy final pointer events before velocity handoff.
  function projectedDistance(velocity, decelerationRate = 0.998) {
    return (velocity / 1000) * decelerationRate / (1 - decelerationRate);
  }

  function rubberband(overshoot, dimension, constant = 0.55) {
    return (overshoot * dimension * constant) /
      (dimension + constant * Math.abs(overshoot));
  }

  function recentVelocity(history, now = performance.now()) {
    const recent = history.filter((sample) => now - sample.t <= 120);
    if (recent.length < 2) return 0;
    const first = recent[0];
    const last = recent[recent.length - 1];
    const seconds = (last.t - first.t) / 1000;
    const velocity = seconds > 0 ? (last.p - first.p) / seconds : 0;
    // Coalesced/synthetic events can share an almost identical timestamp.
    // Cap only impossible spikes; real flick velocity is preserved below it.
    return Math.max(-5000, Math.min(5000, velocity));
  }

  // Native <dialog> owns focus containment. We additionally remember the
  // exact source so Escape, Cancel and successful completion return agency to
  // the control that opened the task.
  const dialogOpeners = new WeakMap();
  function openModal(dialog, trigger, focusTarget) {
    if (!dialog || typeof dialog.showModal !== "function") return false;
    const source = trigger || document.activeElement;
    if (source instanceof HTMLElement && source !== document.body) {
      dialogOpeners.set(dialog, source);
    }
    if (!dialog.open) dialog.showModal();
    requestAnimationFrame(() => {
      const target = typeof focusTarget === "string"
        ? dialog.querySelector(focusTarget) : focusTarget;
      if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
    });
    return true;
  }

  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("close", () => {
      // A native close event is queued. A very fast reopen may already have
      // started a new modal session by the time it is delivered.
      if (dialog.open) return;
      const source = dialogOpeners.get(dialog);
      dialogOpeners.delete(dialog);
      if (source && source.isConnected && typeof source.focus === "function") {
        requestAnimationFrame(() => source.focus({ preventScroll: true }));
      }
    });
  });

  const controlDisabledState = new WeakMap();
  function setControlBusy(control, busy) {
    if (!control) return;
    const form = control.form;
    if (busy) {
      if (!controlDisabledState.has(control)) {
        controlDisabledState.set(control, Boolean(control.disabled));
      }
      control.disabled = true;
      control.setAttribute("aria-busy", "true");
      if (form) form.setAttribute("aria-busy", "true");
      return;
    }
    const wasDisabled = controlDisabledState.get(control);
    controlDisabledState.delete(control);
    control.disabled = Boolean(wasDisabled);
    control.removeAttribute("aria-busy");
    if (form) form.removeAttribute("aria-busy");
  }

  function setVoteButtonLabel(button, mine, fullLabel) {
    if (fullLabel) {
      button.textContent = fullLabel;
      return;
    }
    button.innerHTML = CHECK_ICON + " " +
      `<span class="vote-button-label">${mine ? "Выбрано" : "Выбрать"}</span>`;
  }

  const feedbackTimers = new WeakMap();
  function replayFeedback(element, className, holdMs = 520) {
    if (!element) return;
    const timers = feedbackTimers.get(element) || {};
    clearTimeout(timers[className]);
    element.classList.remove(className);
    // Reflow is intentional: a repeated choice must restart from the currently
    // visible state instead of being swallowed by the previous one-shot cue.
    void element.offsetWidth;
    element.classList.add(className);
    timers[className] = setTimeout(() => {
      element.classList.remove(className);
      delete timers[className];
    }, holdMs);
    feedbackTimers.set(element, timers);
  }

  function markVotingEnded() {
    document.querySelectorAll(".btn.book[data-id]").forEach((button) => {
      button.disabled = true;
      button.classList.remove("on");
      button.classList.add("vote-ended");
      button.removeAttribute("data-id");
      button.removeAttribute("aria-busy");
      delete button.dataset.busy;
      button.textContent = "Голосование завершено";
    });
  }

  /* Вход обязателен для персональных действий. Жалоба остаётся доступна без
     аккаунта; выбор, вопрос, предложение и сохранение открывают вход. После
     входа Telegram вернёт на эту же страницу. Если модалки нет — фолбэк на
     страницу /login. */
  const loginDlg = $("#loginDlg");
  const loginTitle = $("#loginDlgTitle");
  const loginDescription = $("#loginDlgDescription");
  const loginDefaultCopy = {
    title: loginTitle ? loginTitle.textContent : "Войти",
    description: loginDescription ? loginDescription.textContent : "Вход через Telegram — быстро и без пароля",
  };
  const loginCopy = {
    vote: {
      title: "Войти, чтобы выбрать событие",
      description: "После входа выбор будет связан с твоим профилем; до дедлайна его можно изменить или снять.",
    },
    question: {
      title: "Войти, чтобы задать вопрос",
      description: "Вопрос увидит организатор, а ответ появится на этой странице.",
    },
    propose: {
      title: "Войти, чтобы предложить событие",
      description: "После входа можно отправить идею организатору и затем изменить или удалить её.",
    },
    time: {
      title: "Войти, чтобы предложить дату",
      description: "Организатор увидит, кто предложил время, и сможет учесть его при планировании.",
    },
    manage: {
      title: "Войти, чтобы управлять участием",
      description: "После входа можно изменить свой выбор или управлять своим предложением.",
    },
  };
  function goLogin(action = "default", trigger) {
    if (loginDlg && typeof loginDlg.showModal === "function") {
      const copy = loginCopy[action] || loginDefaultCopy;
      if (loginTitle) loginTitle.textContent = copy.title;
      if (loginDescription) loginDescription.textContent = copy.description;
      openModal(loginDlg, trigger, "#loginClose");
      return;
    }
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
  }
  if (loginDlg) {
    const lc = $("#loginClose");
    // любой триггер входа: угловая кнопка #loginOpen и любой [data-login-open]
    // (например «Войти» в CTA на странице события) открывают ту же модалку
    document.addEventListener("click", (e) => {
      if (e.target.closest("#loginOpen, [data-login-open]")) {
        e.preventDefault();
        goLogin("default", e.target.closest("#loginOpen, [data-login-open]"));
      }
    });
    if (lc) lc.addEventListener("click", () => loginDlg.close());
    loginDlg.addEventListener("click", (e) => { if (e.target === loginDlg) loginDlg.close(); });
  }
  function requireAuth(fn, action, trigger) {
    if (!AUTH) { goLogin(action, trigger); return; }
    fn();
  }

  /* плавное появление карточек по очереди */
  document.querySelectorAll("section.cards").forEach((sec) => {
    [...sec.querySelectorAll(".card")].forEach((c, i) =>
      c.style.setProperty("--i", Math.min(i, 7)));
  });

  async function post(url, fd, options = {}) {
    let r;
    try {
      r = await fetch(url, {
        method: "POST", body: fd, credentials: "same-origin",
        headers: { "X-CSRF-Token": CSRF }
      });
    }
    catch (_) { toast("Нет связи — попробуй ещё раз"); return { ok: false }; }
    let j = {};
    try { j = await r.json(); } catch (_) {}
    if (r.status === 401 && j.detail && j.detail.need_login) {
      if (options.allowAnonymous) {
        toast("Не удалось отправить без входа — попробуй ещё раз позже");
      } else {
        goLogin();
      }
      return { ok: false, status: r.status, j };
    }
    if (!r.ok) {
      const d = j.detail;
      toast(typeof d === "string" ? d : (d && d.msg) || "Что-то пошло не так");
      return { ok: false, status: r.status, j };
    }
    return { ok: true, j };
  }

  /* --- выбор события: обновляем карточки на месте, без перезагрузки -------*/
  function setCardState(card, mine) {
    if (!card) return;
    const btn = card.querySelector(".btn.book");
    if (btn) {
      btn.classList.toggle("on", mine);
      setVoteButtonLabel(btn, mine);
      btn.title = mine ? "Нажми, чтобы отменить выбор" : "";
    }
    card.classList.toggle("booked-me", mine);
    const seal = card.querySelector(".seal");
    if (seal) seal.hidden = !mine;                 // знак выбора (только карточки с фото)
    const who = card.querySelector(".bo-who");     // подпись на оверлее
    if (who) who.textContent = mine ? (MYNAME || "ты") : "";
  }

  function renderParticipants(progress, update) {
    progress.querySelectorAll(".participants, .vote-empty").forEach((el) => el.remove());
    const people = Array.isArray(update.participants) ? update.participants : [];
    if (!people.length) {
      const empty = document.createElement("p");
      empty.className = "vote-empty";
      empty.textContent = "Пока без голосов — можно стать первым.";
      progress.append(empty);
      return;
    }

    const roster = document.createElement("div");
    roster.className = "participants";
    roster.setAttribute("aria-label", "Участники");
    people.forEach((person) => {
      const item = document.createElement("span");
      item.className = "participant" + (person.withdrawn ? " withdrawn" : "");
      if (person.is_me) item.dataset.currentUser = "1";

      if (person.has_avatar && person.user_id) {
        const base = `${ACT}/participant-avatar/${encodeURIComponent(person.user_id)}`;
        const avatar = document.createElement("img");
        avatar.src = `${base}?w=64`;
        avatar.srcset = `${base}?w=64 64w, ${base}?w=96 96w, ${base}?w=128 128w`;
        avatar.sizes = "26px";
        avatar.alt = "";
        avatar.width = 26;
        avatar.height = 26;
        avatar.loading = "lazy";
        avatar.decoding = "async";
        item.append(avatar);
      } else {
        const placeholder = document.createElement("span");
        placeholder.className = "participant-ph";
        placeholder.setAttribute("aria-hidden", "true");
        placeholder.textContent = Array.from((person.name || "У").trim())[0]?.toUpperCase() || "У";
        item.append(placeholder);
      }

      const label = document.createElement("span");
      label.textContent = `${person.name || "Участник"}${person.is_me ? " · ты" : ""}`;
      item.append(label);
      if (person.withdrawn) {
        const note = document.createElement("small");
        note.textContent = "отказался(-ась)";
        item.append(note);
      }
      roster.append(item);
    });
    if (update.hidden_count > 0) {
      const more = document.createElement("span");
      more.className = "participants-more";
      more.textContent = `ещё ${update.hidden_count}`;
      roster.append(more);
    }
    progress.append(roster);
  }

  function applyVoteUpdate(update, votingStatus) {
    const card = document.getElementById(`date-${update.date_id}`);
    if (!card) return;
    setCardState(card, Boolean(update.mine));

    const count = Number(update.vote_count) || 0;
    const capacity = Math.max(1, Number(update.capacity) || 1);
    const full = Boolean(update.is_full);
    const hideEmptySingleCounter = capacity === 1 && count === 0;
    // У legacy-архива под прогрессом могла быть дублирующая строка «было: …».
    // После снятия старого выбора актуальный ограниченный ростер уже содержит
    // всю нужную информацию, поэтому не оставляем устаревшую подпись.
    if (!update.mine) {
      const pastSummary = card.querySelector(".booked");
      if (pastSummary) pastSummary.remove();
    }
    const progress = card.querySelector(".vote-progress");
    if (progress) {
      const currentTrack = progress.querySelector(".vote-progress-track");
      const previousCount = currentTrack
        ? Number(currentTrack.getAttribute("aria-valuenow")) : NaN;
      const rosterLabel = progress.querySelector(".vote-progress-head span");
      if (rosterLabel) rosterLabel.textContent = "участников";
      progress.classList.toggle("full", full);
      const head = progress.querySelector(".vote-progress-head");
      if (head) head.hidden = hideEmptySingleCounter;
      const countLabel = head && head.querySelector("b");
      if (countLabel) {
        countLabel.textContent = `${count}/${capacity}`;
        if (Number.isFinite(previousCount) && previousCount !== count) {
          replayFeedback(countLabel, "vote-count-updated", 430);
        }
      }
      const track = currentTrack;
      if (track) {
        track.hidden = hideEmptySingleCounter;
        track.setAttribute("aria-valuemax", String(capacity));
        track.setAttribute("aria-valuenow", String(count));
        const fill = track.querySelector("i");
        if (fill) {
          fill.style.setProperty(
            "--vote-width", `${Math.min(100, count * 100 / capacity)}%`);
          if (Number.isFinite(previousCount) && previousCount !== count) {
            replayFeedback(fill, "vote-progress-updated", 430);
          }
        }
      }
      renderParticipants(progress, update);
    }

    const button = card.querySelector(".btn.book[data-id]");
    if (!button) return;
    delete button.dataset.busy;
    button.removeAttribute("aria-busy");
    if (votingStatus === "unconfigured" && !update.mine) {
      button.remove();
      return;
    }
    button.disabled = !update.mine && full;
    button.classList.toggle("on", Boolean(update.mine));
    setVoteButtonLabel(
      button,
      Boolean(update.mine),
      !update.mine && full ? `Набрано ${count}/${capacity}` : "");
  }

  let voteBusy = false;
  async function doBook(btn) {
    if (voteBusy) return;
    voteBusy = true;
    const wasDisabled = btn.disabled;
    btn.disabled = true;
    btn.dataset.busy = "1";
    btn.setAttribute("aria-busy", "true");
    const fd = new FormData();
    fd.append("date_id", btn.dataset.id);
    const res = await post(`${ACT}/book`, fd);
    if (!res.ok) {
      const detail = res.j && res.j.detail;
      const code = detail && typeof detail === "object" ? detail.code : "";
      if (code === "voting_deadline_passed" || code === "voting_closed") {
        markVotingEnded();
      } else {
        delete btn.dataset.busy;
        btn.removeAttribute("aria-busy");
        btn.disabled = wasDisabled;
      }
      voteBusy = false;
      return;
    }
    MYNAME = res.j.name || MYNAME;
    const card = btn.closest(".card");
    (res.j.updates || []).forEach(
      (update) => applyVoteUpdate(update, res.j.voting_status));
    if (res.j.booked) {
      let notifyRevealed = false;
      document.querySelectorAll("[data-deferred-notify]").forEach((box) => {
        notifyRevealed = notifyRevealed || box.hidden;
        box.hidden = false;
        box.classList.add("is-revealed");
      });
      replayFeedback(card, "vote-confirmed", 520);
      replayFeedback(btn, "vote-label-confirmed", 430);
      const r = btn.getBoundingClientRect();
      // Один короткий акцент из причины действия; второй залп по карточке
      // создавал визуальный шум и разрывал причинно-следственную связь.
      UI.burst(r.left + r.width / 2, r.top + r.height / 2);
      toast(notifyRevealed
        ? "Голос учтён. Теперь можно подключить уведомления в Telegram"
        : "Голос учтён");
    } else {
      toast("Голос снят");
    }
    delete btn.dataset.busy;
    btn.removeAttribute("aria-busy");
    voteBusy = false;
  }
  document.querySelectorAll(".btn.book[data-id]").forEach((b) => {
    b.addEventListener("click", () => requireAuth(() => doBook(b), "vote", b));
  });
  // ui.js инициализирует таймер раньше guest.js. Если дедлайн успел наступить
  // между серверным рендером и загрузкой этого файла, событие уже прошло, но
  // data-countdown-ended сохраняет конечное состояние для синхронизации.
  document.addEventListener("d4y:voting-ended", markVotingEnded);
  if (document.querySelector("[data-vote-countdown][data-countdown-ended='1']")) {
    markVotingEnded();
  }

  document.querySelectorAll(".withdraw-vote").forEach((b) => {
    b.addEventListener("click", () => requireAuth(async () => {
      if (!await window.d4yConfirm(
        "Отказаться от участия? Победитель и результат голосования не изменятся.",
        { trigger: b }
      )) return;
      setControlBusy(b, true);
      const withdrawUrl = document.body.dataset.withdrawUrl || (`/c/${TOKEN}/withdraw`);
      const res = await post(withdrawUrl, new FormData());
      if (!res.ok) { setControlBusy(b, false); return; }
      toast("Организатор уведомлён");
      setTimeout(() => location.reload(), 500);
    }, "manage", b));
  });

  /* --- вопрос --------------------------------------------------------------*/
  const askDlg = $("#askDlg"), askForm = $("#askForm");
  document.querySelectorAll(".btn.ask").forEach((b) => {
    b.addEventListener("click", () => requireAuth(() => {
      $("#askDateId").value = b.dataset.id;
      $("#askTitle").textContent = b.dataset.name;
      $("#askText").value = "";
      openModal(askDlg, b, "#askText");
    }, "question", b));
  });
  $("#askCancel").onclick = () => askDlg.close();
  askForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submit = e.submitter || askForm.querySelector('[type="submit"]');
    if (submit && submit.getAttribute("aria-busy") === "true") return;
    setControlBusy(submit, true);
    const res = await post(`${ACT}/question`, new FormData(askForm));
    if (!res.ok) { setControlBusy(submit, false); return; }
    setControlBusy(submit, false);
    askDlg.close();
    toast(FRIENDS ? "Вопрос отправлен — ответ появится здесь же"
      : "Вопрос отправлен 💌 Ответ появится здесь же");
    setTimeout(() => location.reload(), 1200);
  });

  /* --- жалоба --------------------------------------------------------------*/
  const reportDlg = $("#reportDlg"), reportForm = $("#reportForm");
  document.querySelectorAll(".report-link[data-id]").forEach((b) => {
    b.addEventListener("click", () => {
      $("#reportType").value = "date";
      $("#reportTargetId").value = b.dataset.id;
      $("#reportTitle").textContent = b.dataset.name;
      $("#reportReason").value = "";
      openModal(reportDlg, b, "#reportReason");
    });
  });
  $("#reportCancel").onclick = () => reportDlg.close();
  reportForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submit = e.submitter || reportForm.querySelector('[type="submit"]');
    if (submit && submit.getAttribute("aria-busy") === "true") return;
    setControlBusy(submit, true);
    const res = await post(`${ACT}/report`, new FormData(reportForm), { allowAnonymous: true });
    if (!res.ok) { setControlBusy(submit, false); return; }
    setControlBusy(submit, false);
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
      openModal(timeDlg, b, "#timeStart");
    }, "time", b));
  });
  $("#timeCancel").onclick = () => timeDlg.close();
  timeForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submit = e.submitter || timeForm.querySelector('[type="submit"]');
    if (submit && submit.getAttribute("aria-busy") === "true") return;
    setControlBusy(submit, true);
    const res = await post(`${ACT}/suggest_time`, new FormData(timeForm));
    if (!res.ok) { setControlBusy(submit, false); return; }
    setControlBusy(submit, false);
    timeDlg.close();
    toast(FRIENDS ? "Предложение времени отправлено" : "Предложение отправлено 📅");
    setTimeout(() => location.reload(), 1100);
  });

  /* --- предложение / редактирование ----------------------------------------*/
  const propDlg = $("#propDlg"), propForm = $("#propForm");
  // Блок «предложить своё событие» есть только на странице категории. На
  // странице отдельного события (шаринг) этих элементов нет — пропускаем.
  if (propDlg && propForm) {
  const propSlides = $("#propSlides");
  let editId = null, removed = new Set(), savedPhotos = [];
  let savedVideos = [], removedVideos = new Set();
  let propSlide = 0, propObjectUrls = [];
  let propSession = 0;
  let propRequest = null;
  let propMediaOrder = [], propMediaKeys = new WeakMap(), propMediaSeq = 0;
  const MAX_PHOTOS = Math.max(1, Number(document.body.dataset.maxPhotos) || 5);
  const MAX_VIDEOS = Math.max(1, Number(document.body.dataset.maxVideos) || 2);
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
    max: MAX_VIDEOS,
    // Сохранённые видео занимают свои слоты, пока гость явно их не убрал.
    keptCount: () => savedVideos.filter((v) => !removedVideos.has(v.id)).length,
    onError: toast,
    onChange: renderPropGallery,
    noZoneBind: true,                 // дроп-зону держит общий mediaUploader
  });
  // общий блок «Медиа»: одна зона принимает и фото, и видео
  const propMediaManager = UI.mediaUploader ? UI.mediaUploader({
    zone: $("#propZone"),
    input: $("#propMedia"),
    photo: up,
    video: upv,
    onError: toast,
  }) : null;
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
    if (upload.removeAt) upload.removeAt(idx);
  }

  function propFileKey(file, kind) {
    let key = propMediaKeys.get(file);
    if (!key) {
      key = `${kind}:n:${propMediaSeq++}`;
      propMediaKeys.set(file, key);
    }
    return key;
  }

  function availablePropItems() {
    const items = [];
    savedPhotos.filter((photo) => !removed.has(photo.id)).forEach((photo) => {
      items.push({
        key: `image:s:${photo.id}`, kind: "image", saved: photo.id,
        src: `/c/${TOKEN}/image/${photo.filename}?w=960`,
      });
    });
    up.files().forEach((file, idx) => {
      items.push({
        key: propFileKey(file, "image"), kind: "image", file: file,
        upload: up, idx: idx,
      });
    });
    savedVideos.filter((video) => !removedVideos.has(video.id)).forEach((video) => {
      items.push({
        key: `video:s:${video.id}`, kind: "video", savedVideo: video.id,
        src: `/c/${TOKEN}/video/${video.filename}`,
      });
    });
    upv.files().forEach((file, idx) => {
      items.push({
        key: propFileKey(file, "video"), kind: "video", file: file,
        upload: upv, idx: idx,
      });
    });
    return items;
  }

  function orderedPropItems() {
    const available = availablePropItems();
    const byKey = new Map(available.map((item) => [item.key, item]));
    propMediaOrder = propMediaOrder.filter((key, idx, all) =>
      byKey.has(key) && all.indexOf(key) === idx);
    available.forEach((item) => {
      if (!propMediaOrder.includes(item.key)) propMediaOrder.push(item.key);
    });
    const ordered = propMediaOrder.map((key) => byKey.get(key)).filter(Boolean);
    // Схема хранит position фото и видео раздельно, а карточка тоже выводит
    // сначала фото, затем видео. Нормализуем видимую ленту тем же образом:
    // drag меняет честно сохраняемый порядок внутри своего типа и не создаёт
    // временного mixed-порядка, который исчез бы после reload.
    const normalized = ordered.filter((item) => item.kind === "image")
      .concat(ordered.filter((item) => item.kind === "video"));
    propMediaOrder = normalized.map((item) => item.key);
    return normalized;
  }

  function renderPropGallery() {
    propObjectUrls.forEach((url) => URL.revokeObjectURL(url));
    propObjectUrls = [];
    propSlides.replaceChildren();
    const items = orderedPropItems();
    items.forEach((item) => {
      if (!item.file) return;
      item.src = URL.createObjectURL(item.file);
      propObjectUrls.push(item.src);
    });
    items.forEach((item, idx) => {
      const slide = document.createElement("div");
      slide.className = "ed-slide";
      slide.dataset.orderKey = item.key;
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
          removedVideos.add(item.savedVideo);
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

    const order = $("#propMediaOrder");
    order.replaceChildren();
    items.forEach((item, idx) => {
      const tile = document.createElement("div");
      tile.className = "ptile" + (item.kind === "video" ? " vid" : "") +
        (idx === propSlide ? " current" : "");
      tile.dataset.orderKey = item.key;
      tile.dataset.kind = item.kind;
      tile.tabIndex = 0;
      tile.setAttribute("role", "button");
      tile.setAttribute("aria-label", `${item.kind === "video" ? "Видео" : "Фото"} ${idx + 1}`);
      if (item.kind === "video") {
        tile.innerHTML = '<video muted playsinline preload="metadata"></video>' +
          '<span class="vtag">🎬</span>';
        tile.querySelector("video").src = item.src;
      } else {
        tile.innerHTML = '<img alt="" draggable="false">';
        tile.querySelector("img").src = item.src;
      }
      const select = () => {
        const selected = propMediaOrder.indexOf(item.key);
        if (selected < 0 || selected === propSlide) return;
        propSlide = selected;
        renderPropGallery();
      };
      tile.addEventListener("click", select);
      tile.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        select();
      });
      order.appendChild(tile);
    });
    $("#propMediaOrderWrap").hidden = items.length < 2;

    function setEdge(button, canNavigate, direction) {
      const canAdd = savedPhotos.filter((p) => !removed.has(p.id)).length +
        up.files().length < MAX_PHOTOS ||
        savedVideos.filter((v) => !removedVideos.has(v.id)).length +
        upv.files().length < MAX_VIDEOS;
      // Добавление показываем только справа после последнего слайда. На пустом
      // превью достаточно центральной кнопки; две боковые «+» выглядели как
      // сломанная навигация и открывали один и тот же picker трижды.
      const asAdd = direction === "next" && items.length > 0 && !canNavigate && canAdd;
      button.hidden = !canNavigate && !asAdd;
      button.classList.toggle("as-add", asAdd);
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

  function syncPropMediaSort() {
    const current = $("#propMediaOrder .current");
    const currentKey = current && current.dataset.orderKey;
    propMediaOrder = [...$("#propMediaOrder").querySelectorAll(".ptile[data-order-key]")]
      .map((tile) => tile.dataset.orderKey);
    const normalized = orderedPropItems();
    propSlide = Math.max(0, normalized.findIndex((item) => item.key === currentKey));
    renderPropGallery();
  }
  // В БД фото и видео имеют независимые position и на карточке идут двумя
  // группами. Два sortable сохраняют честный порядок внутри каждой группы и
  // не обещают неподдерживаемого чередования video между фотографиями.
  UI.sortable($("#propMediaOrder"), {
    selector: '.ptile[data-kind="image"]', onChange: syncPropMediaSort,
  });
  UI.sortable($("#propMediaOrder"), {
    selector: '.ptile[data-kind="video"]', onChange: syncPropMediaSort,
  });
  function movePropSlide(delta) {
    const count = propSlides.children.length;
    if (!count) return;
    propSlide = (propSlide + delta + count) % count;
    renderPropGallery();
  }

  function resetPropProgress() {
    const bar = $("#propBar");
    const fill = bar && bar.querySelector("i");
    if (fill) fill.style.width = "0%";
    if (bar) bar.hidden = true;
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
    propSession += 1;
    resetPropProgress();
    propForm.reset();
    removed = new Set();
    savedPhotos = (meta && meta.photos) ? meta.photos.slice() : [];
    savedVideos = (meta && meta.videos) ? meta.videos.slice() : [];
    removedVideos = new Set();
    propMediaOrder = [];
    propMediaKeys = new WeakMap();
    propMediaSeq = 0;
    propSlide = 0;
    up.clear();
    upv.clear();
    var payValue = String((meta && meta.pay) || 0);
    propForm.querySelectorAll('input[name="pay"]').forEach((input) => {
      input.checked = input.value === payValue;
    });
    $("#propCapacity").value = (meta && meta.capacity) || 1;
    editId = meta ? meta.id : null;
    $("#propHead").textContent = meta ? "Изменить своё событие" : "Предложить своё событие";
    $("#propSubmit").textContent = meta ? "Сохранить изменения" : "Предложить своё событие";
    setControlBusy($("#propSubmit"), false);
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
    openModal(propDlg, document.activeElement, "#propEdTitle");
  }

  const proposeTriggers = Array.from(
    document.querySelectorAll("#fabPropose, [data-propose-open]")
  );
  proposeTriggers.forEach((trigger) => {
    trigger.onclick = () => requireAuth(
      () => openPropose(null), "propose", trigger);
  });
  function closeProposalActionsAtDeadline() {
    proposeTriggers.forEach((trigger) => { trigger.hidden = true; });
    document.querySelectorAll(".mine-actions").forEach((actions) => { actions.hidden = true; });
    document.querySelectorAll("[data-proposal-empty-cta]").forEach((hint) => {
      hint.hidden = true;
    });
    if (propDlg.open) {
      propDlg.close();
      toast("Голосование завершено — варианты уже зафиксированы");
    }
  }
  document.addEventListener("d4y:voting-ended", closeProposalActionsAtDeadline);
  if (document.querySelector("[data-vote-countdown][data-countdown-ended='1']")) {
    closeProposalActionsAtDeadline();
  }
  document.querySelectorAll(".mine-actions .edit").forEach((b) => {
    b.addEventListener("click", () => requireAuth(
      () => openPropose(JSON.parse(b.dataset.meta)), "manage", b));
  });
  $("#propCancel").onclick = () => propDlg.close();
  $("#propCancelBottom").onclick = () => propDlg.close();
  propDlg.addEventListener("close", () => {
    // Escape, крестик и «Отмена» одинаково отменяют ещё и фоновую подготовку
    // выбранных файлов. Иначе закончившееся позже сжатие могло попасть в форму
    // при следующем открытии редактора.
    if (propMediaManager && propMediaManager.cancelPending) {
      propMediaManager.cancelPending();
    }
    if (propRequest && propRequest.abort) propRequest.abort();
    propRequest = null;
    propSession += 1;
    setControlBusy($("#propSubmit"), false);
    resetPropProgress();
    up.clear();
    upv.clear();
  });

  propForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    // Название редактируется в contenteditable, поэтому нативная проверка
    // hidden input его не подсветит. Возвращаем пользователя прямо к полю.
    propTitleEdit.toField();
    const titleView = $("#propEdTitle");
    const titleField = propForm.querySelector('[name="name"]');
    if (!titleField.value.trim()) {
      toast("Укажи название события");
      titleView.scrollIntoView({ block: "center", behavior: "smooth" });
      titleView.focus();
      return;
    }
    const sub = $("#propSubmit");
    const submitSession = propSession;
    if (sub.getAttribute("aria-busy") === "true") return;
    setControlBusy(sub, true);
    // Большие фото сжимаются асинхронно. Дожидаемся их подготовки до сборки
    // FormData, иначе быстрый submit отправлял событие без только что выбранных
    // фото (особенно заметно на телефоне).
    if (propMediaManager && propMediaManager.whenReady) {
      await propMediaManager.whenReady();
    }
    // За время подготовки большого фото пользователь мог закрыть диалог или
    // открыть его заново. Закрытая/уже другая форма не должна отправляться.
    if (submitSession !== propSession || !propDlg.open) {
      if (!propDlg.open) setControlBusy(sub, false);
      return;
    }

    // Видимая лента хранит общий порядок saved+new. Перед FormData переносим
    // его в реальные file-input, чтобы n0/n1 на сервере означали именно те
    // файлы, которые пользователь видит на соответствующих местах.
    const orderedBeforeSubmit = orderedPropItems();
    up.reorderFiles(orderedBeforeSubmit
      .filter((item) => item.kind === "image" && item.file)
      .map((item) => item.file));
    upv.reorderFiles(orderedBeforeSubmit
      .filter((item) => item.kind === "video" && item.file)
      .map((item) => item.file));
    const orderedMedia = orderedPropItems();
    const newPhotoIndex = new Map(up.files().map((file, idx) => [file, idx]));
    const newVideoIndex = new Map(upv.files().map((file, idx) => [file, idx]));
    const fd = new FormData(propForm);
    let url = `/c/${TOKEN}/propose`;
    if (editId) {
      url = `/c/${TOKEN}/propose/${editId}/edit`;
      removed.forEach((id) => fd.append("remove_image", id));
      removedVideos.forEach((id) => fd.append("remove_video", id));
      const imageOrder = orderedMedia.filter((item) => item.kind === "image")
        .map((item) => item.saved ? `s${item.saved}` : `n${newPhotoIndex.get(item.file)}`);
      const videoOrder = orderedMedia.filter((item) => item.kind === "video")
        .map((item) => item.savedVideo ? `s${item.savedVideo}` : `n${newVideoIndex.get(item.file)}`);
      fd.append("keep_order", imageOrder.join(","));
      fd.append("keep_video_order", videoOrder.join(","));
    }
    const bar = $("#propBar"), fill = bar.querySelector("i");
    if (up.files().length || upv.files().length) {
      fill.style.width = "0%";
      bar.hidden = false;
    }
    const request = UI.postWithProgress(url, fd, (p) => {
      if (submitSession === propSession && propDlg.open) {
        fill.style.width = Math.round(p * 100) + "%";
      }
    });
    propRequest = request;
    const raw = await request;
    if (propRequest === request) propRequest = null;
    // Ответ старого запроса не должен закрыть повторно открытый редактор.
    if (submitSession !== propSession || !propDlg.open) return;
    setControlBusy(sub, false);
    resetPropProgress();
    if (raw.status === 401 && raw.j.detail && raw.j.detail.need_login) {
      goLogin("propose", sub);
      return;
    }
    if (raw.status < 200 || raw.status >= 300) {
      const d = raw.j.detail;
      toast(raw.status === 0 ? "Нет связи — попробуй ещё раз"
            : (typeof d === "string" ? d : (d && d.msg) || "Что-то пошло не так"));
      return;
    }
    propDlg.close();
    toast(editId ? (FRIENDS ? "Сохранено" : "Сохранено ♥")
          : (raw.j.moderated ? "Отправлено! Появится после проверки ⏳"
            : (FRIENDS ? "Добавлено! Спасибо за идею" : "Добавлено! Спасибо за идею ♥")));
    setTimeout(() => location.reload(), 1100);
  });

  document.querySelectorAll(".mine-actions .del").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!AUTH) { goLogin("manage", b); return; }
      if (!await window.d4yConfirm(`Удалить «${b.dataset.name}»?`, { trigger: b })) return;
      setControlBusy(b, true);
      const res = await post(`/c/${TOKEN}/propose/${b.dataset.id}/delete`, new FormData());
      if (!res.ok) { setControlBusy(b, false); return; }
      toast("Удалено");
      setTimeout(() => location.reload(), 700);
    });
  });
  }  /* /if (propDlg) — конец блока «предложить событие» */

  /* --- календарь -------------------------------------------------------------*/
  const calDlg = $("#calDlg");
  document.querySelectorAll(".cal-link").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      $("#calGoogle").href = a.dataset.gcal;
      $("#calIcs").href = a.dataset.ics;
      openModal(calDlg, a, "#calGoogle");
    });
  });
  $("#calCancel").onclick = () => calDlg.close();
  $("#calGoogle").addEventListener("click", () => setTimeout(() => calDlg.close(), 300));
  $("#calIcs").addEventListener("click", () => setTimeout(() => calDlg.close(), 300));

  /* --- физическая модель для галерей и полноэкранного просмотра --------------*/
  function springValue(from, target, initialVelocity, update, complete, options = {}) {
    if (REDUCED_MOTION.matches) {
      update(target);
      if (complete) complete();
      return () => {};
    }
    let value = from;
    let velocity = Number.isFinite(initialVelocity) ? initialVelocity : 0;
    let frame = 0;
    let stopped = false;
    let previous = performance.now();
    const response = options.response || 0.36;
    const damping = options.damping || 1;
    const omega = 2 * Math.PI / response;
    const stiffness = omega * omega;
    const friction = 2 * damping * omega;

    const tick = (now) => {
      if (stopped) return;
      const dt = Math.min(0.032, Math.max(0.001, (now - previous) / 1000));
      previous = now;
      const acceleration = -stiffness * (value - target) - friction * velocity;
      velocity += acceleration * dt;
      value += velocity * dt;
      update(value);
      if (Math.abs(value - target) < 0.45 && Math.abs(velocity) < 5) {
        update(target);
        if (complete) complete();
        return;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => {
      stopped = true;
      if (frame) cancelAnimationFrame(frame);
    };
  }

  /* --- галереи: 1:1 Pointer Events, проекция скорости и snap -----------------*/
  document.querySelectorAll(".gal-wrap").forEach((w) => {
    const g = w.querySelector(".gallery");
    const slides = [...g.children];       // фото и видео — единая лента
    const n = slides.length;
    if (!n) return;
    const cardTitle = w.closest(".card")?.querySelector(".title")?.textContent?.trim();
    g.setAttribute("role", "region");
    g.setAttribute("aria-roledescription", "карусель");
    g.setAttribute("aria-label", cardTitle ? `Галерея события «${cardTitle}»` : "Галерея события");
    slides.forEach((slide, index) => {
      if (slide.matches("video") && !slide.getAttribute("aria-label")) {
        slide.setAttribute("aria-label", `Видео ${index + 1} из ${n}`);
      }
    });

    if (n < 2) return;
    const dots = document.createElement("div");
    dots.className = "gal-dots";
    dots.setAttribute("aria-hidden", "true");
    for (let k = 0; k < n; k++) dots.appendChild(document.createElement("i"));
    const cnt = document.createElement("div");
    cnt.className = "gal-count count-badge count-badge--overlay";
    cnt.setAttribute("aria-live", "polite");
    cnt.setAttribute("aria-atomic", "true");
    const prev = document.createElement("button");
    prev.type = "button"; prev.className = "gal-nav prev"; prev.textContent = "‹";
    prev.setAttribute("aria-label", "Предыдущий элемент галереи");
    const next = document.createElement("button");
    next.type = "button"; next.className = "gal-nav next"; next.textContent = "›";
    next.setAttribute("aria-label", "Следующий элемент галереи");
    w.append(dots, cnt, prev, next);

    const upd = () => {
      const i = Math.max(0, Math.min(n - 1,
        Math.round(g.scrollLeft / Math.max(1, g.clientWidth))));
      [...dots.children].forEach((d, k) => d.classList.toggle("on", k === i));
      cnt.textContent = `${i + 1}/${n}`;
      cnt.setAttribute("aria-label", `Элемент ${i + 1} из ${n}`);
      prev.disabled = i === 0;
      next.disabled = i === n - 1;
    };
    upd();
    let scrollFramePending = false;
    g.addEventListener("scroll", () => {
      if (scrollFramePending) return;
      scrollFramePending = true;
      requestAnimationFrame(() => { scrollFramePending = false; upd(); });
    }, { passive: true });

    let cancelScrollSpring = null;
    let cancelEdgeSpring = null;
    let edgePull = 0;
    let gesture = null;
    function stopGalleryMotion() {
      if (cancelScrollSpring) cancelScrollSpring();
      if (cancelEdgeSpring) cancelEdgeSpring();
      cancelScrollSpring = cancelEdgeSpring = null;
    }
    function setEdgePull(value) {
      edgePull = value;
      slides.forEach((slide) => {
        slide.style.transform = value ? `translate3d(${value}px, 0, 0)` : "";
        slide.style.willChange = value ? "transform" : "";
      });
    }
    function releaseEdge(initialVelocity = 0) {
      if (!edgePull) return;
      if (cancelEdgeSpring) cancelEdgeSpring();
      cancelEdgeSpring = springValue(edgePull, 0, initialVelocity, setEdgePull, () => {
        cancelEdgeSpring = null;
        setEdgePull(0);
      }, { response: 0.3, damping: 1 });
    }
    function snapGallery(target, velocity = 0) {
      const width = Math.max(1, g.clientWidth);
      const max = width * (n - 1);
      const clamped = Math.max(0, Math.min(max, target));
      if (cancelScrollSpring) cancelScrollSpring();
      cancelScrollSpring = springValue(g.scrollLeft, clamped, velocity, (value) => {
        g.scrollLeft = value;
      }, () => {
        cancelScrollSpring = null;
        g.scrollLeft = clamped;
        g.style.scrollSnapType = "";
        upd();
      }, { response: 0.36, damping: Math.abs(velocity) > 140 ? 0.88 : 1 });
    }

    prev.addEventListener("click", (e) => {
      e.stopPropagation();
      g.style.scrollSnapType = "none";
      snapGallery(g.scrollLeft - g.clientWidth);
    });
    next.addEventListener("click", (e) => {
      e.stopPropagation();
      g.style.scrollSnapType = "none";
      snapGallery(g.scrollLeft + g.clientWidth);
    });

    g.addEventListener("dragstart", (event) => event.preventDefault());
    g.addEventListener("pointerdown", (event) => {
      if (!event.isPrimary || event.button !== 0) return;
      const video = event.target.closest("video");
      // Leave the native transport/seek controls untouched. A drag on the
      // visual part of a video may still page the carousel after hysteresis.
      if (video && event.clientY >= video.getBoundingClientRect().bottom - 56) return;
      stopGalleryMotion();
      g.style.scrollSnapType = "none";
      gesture = {
        id: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        startScroll: g.scrollLeft,
        mode: "pending",
        capturePending: Boolean(video),
        history: [{ p: g.scrollLeft, t: performance.now() }],
      };
      if (!video) g.setPointerCapture(event.pointerId);
    });
    g.addEventListener("pointermove", (event) => {
      if (!gesture || event.pointerId !== gesture.id) return;
      const dx = event.clientX - gesture.x;
      const dy = event.clientY - gesture.y;
      if (gesture.mode === "pending") {
        if (Math.max(Math.abs(dx), Math.abs(dy)) < GESTURE_HYSTERESIS) return;
        if (Math.abs(dy) >= Math.abs(dx)) {
          gesture.mode = "vertical";
          g.style.scrollSnapType = "";
          if (g.hasPointerCapture(event.pointerId)) g.releasePointerCapture(event.pointerId);
          return;
        }
        gesture.mode = "horizontal";
        if (gesture.capturePending) g.setPointerCapture(event.pointerId);
        g.classList.add("is-dragging");
      }
      if (gesture.mode !== "horizontal") return;
      event.preventDefault();
      const width = Math.max(1, g.clientWidth);
      const max = width * (n - 1);
      const desired = gesture.startScroll - dx;
      if (desired < 0) {
        g.scrollLeft = 0;
        setEdgePull(rubberband(-desired, width));
      } else if (desired > max) {
        g.scrollLeft = max;
        setEdgePull(-rubberband(desired - max, width));
      } else {
        g.scrollLeft = desired;
        setEdgePull(0);
      }
      const now = performance.now();
      gesture.history.push({ p: desired, t: now });
      gesture.history = gesture.history.filter((sample) => now - sample.t <= 140);
    }, { passive: false });

    function finishGalleryGesture(event, cancelled) {
      if (!gesture || event.pointerId !== gesture.id) return;
      const current = gesture;
      gesture = null;
      if (g.hasPointerCapture(event.pointerId)) g.releasePointerCapture(event.pointerId);
      g.classList.remove("is-dragging");
      if (current.mode !== "horizontal") {
        g.style.scrollSnapType = "";
        return;
      }
      g._d4ySuppressClickUntil = performance.now() + 350;
      const velocity = cancelled ? 0 : recentVelocity(current.history);
      const width = Math.max(1, g.clientWidth);
      const projected = g.scrollLeft + projectedDistance(velocity);
      const target = Math.round(projected / width) * width;
      releaseEdge(-velocity * 0.08);
      snapGallery(target, velocity);
    }
    g.addEventListener("pointerup", (event) => finishGalleryGesture(event, false));
    g.addEventListener("pointercancel", (event) => finishGalleryGesture(event, true));
  });

  /* --- native lightbox: focus-safe, interruptible, velocity-aware ------------*/
  const lb = $("#lightbox"), lbImg = lb.querySelector("img"), lbCnt = $("#lbCount");
  const lbPrev = $("#lbPrev"), lbNext = $("#lbNext");
  let lbList = [];
  let lbI = 0;
  let lbOffset = 0;
  let lbGesture = null;
  let cancelLbSpring = null;

  function setLbOffset(value) {
    lbOffset = value;
    lbImg.style.transform = value ? `translate3d(${value}px, 0, 0)` : "";
    lbImg.style.willChange = value ? "transform" : "";
  }
  function stopLbMotion() {
    if (cancelLbSpring) cancelLbSpring();
    cancelLbSpring = null;
  }
  function preloadLbNeighbors() {
    [lbList[lbI - 1], lbList[lbI + 1]].filter(Boolean).forEach((item) => {
      const image = new Image();
      image.src = item.src;
    });
  }
  function lbShow(i) {
    if (!lbList.length) return;
    lbI = Math.max(0, Math.min(i, lbList.length - 1));
    const item = lbList[lbI];
    lbImg.src = item.src;
    lbImg.alt = item.alt || `Фото ${lbI + 1} из ${lbList.length}`;
    lbCnt.textContent = `${lbI + 1}/${lbList.length}`;
    lbCnt.setAttribute("aria-label", `Фото ${lbI + 1} из ${lbList.length}`);
    const single = lbList.length < 2;
    lbCnt.hidden = single;
    lbPrev.hidden = lbNext.hidden = single;
    lbPrev.disabled = lbI === 0;
    lbNext.disabled = lbI === lbList.length - 1;
    preloadLbNeighbors();
  }
  function springLbTo(target, velocity, complete, momentum = false) {
    stopLbMotion();
    cancelLbSpring = springValue(lbOffset, target, velocity, setLbOffset, () => {
      cancelLbSpring = null;
      setLbOffset(target);
      if (complete) complete();
    }, { response: momentum ? 0.32 : 0.36, damping: momentum ? 0.88 : 1 });
  }
  function moveLbBy(delta, velocity = 0) {
    const nextIndex = lbI + delta;
    if (nextIndex < 0 || nextIndex >= lbList.length) {
      springLbTo(0, velocity);
      return;
    }
    if (REDUCED_MOTION.matches) {
      setLbOffset(0);
      lbShow(nextIndex);
      return;
    }
    const width = Math.max(1, lb.clientWidth || window.innerWidth);
    const exitTarget = delta > 0 ? -width : width;
    springLbTo(exitTarget, velocity, () => {
      lbShow(nextIndex);
      setLbOffset(delta > 0 ? width : -width);
      const incomingVelocity = delta > 0
        ? Math.min(-180, velocity) : Math.max(180, velocity);
      springLbTo(0, incomingVelocity, null, true);
    }, true);
  }
  function openLightbox(img) {
    const gallery = img.closest(".gallery");
    const imgs = [...gallery.querySelectorAll("img")];
    const title = gallery.closest(".card")?.querySelector(".title")?.textContent?.trim();
    // Карточка использует responsive-копию; оригинал загружается только после
    // явного открытия. Текстовое имя остаётся полезным и без декоративного alt.
    lbList = imgs.map((source, index) => ({
      src: source.dataset.full || source.currentSrc || source.src,
      alt: `Фото ${index + 1} из ${imgs.length}${title ? `: ${title}` : ""}`,
    }));
    stopLbMotion();
    setLbOffset(0);
    lbShow(imgs.indexOf(img));
    if (!lb.open) {
      openModal(lb, img, "#lbX");
      lb.classList.add("open");
    }
  }
  function lbClose() {
    stopLbMotion();
    if (lb.open) lb.close();
  }
  lb.addEventListener("close", () => {
    if (lb.open) return;
    stopLbMotion();
    lbGesture = null;
    lb.classList.remove("open", "is-dragging");
    setLbOffset(0);
    lbImg.removeAttribute("src");
    lbList = [];
  });
  lb.addEventListener("cancel", () => stopLbMotion());

  document.addEventListener("click", (event) => {
    const img = event.target.closest(".gallery img");
    if (!img) return;
    const gallery = img.closest(".gallery");
    if (gallery._d4ySuppressClickUntil > performance.now()) {
      event.preventDefault();
      return;
    }
    openLightbox(img);
  });
  document.querySelectorAll(".gallery img").forEach((img) => {
    // Legacy cards receive the same semantics even if they were rendered by an
    // older cached template while this script is already fresh.
    img.setAttribute("role", "button");
    img.tabIndex = 0;
    img.setAttribute("aria-haspopup", "dialog");
    img.setAttribute("aria-controls", "lightbox");
    img.draggable = false;
    img.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openLightbox(img);
        return;
      }
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      const imgs = [...img.closest(".gallery").querySelectorAll("img")];
      const direction = event.key === "ArrowRight" ? 1 : -1;
      const target = imgs[imgs.indexOf(img) + direction];
      if (!target) return;
      event.preventDefault();
      target.focus({ preventScroll: true });
      target.scrollIntoView({ behavior: REDUCED_MOTION.matches ? "auto" : "smooth", block: "nearest", inline: "center" });
    });
  });

  lbPrev.addEventListener("click", (event) => { event.stopPropagation(); moveLbBy(-1); });
  lbNext.addEventListener("click", (event) => { event.stopPropagation(); moveLbBy(1); });
  $("#lbX").addEventListener("click", lbClose);
  lb.addEventListener("click", (event) => { if (event.target === lb) lbClose(); });
  lb.addEventListener("keydown", (event) => {
    if (!lb.open) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      moveLbBy(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      moveLbBy(1);
    }
  });

  lbImg.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || event.button !== 0) return;
    stopLbMotion();                         // grab the presentation value now
    lbGesture = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      startOffset: lbOffset,
      mode: "pending",
      history: [{ p: lbOffset, t: performance.now() }],
    };
    lbImg.setPointerCapture(event.pointerId);
  });
  lbImg.addEventListener("pointermove", (event) => {
    if (!lbGesture || event.pointerId !== lbGesture.id) return;
    const dx = event.clientX - lbGesture.x;
    const dy = event.clientY - lbGesture.y;
    if (lbGesture.mode === "pending") {
      if (Math.max(Math.abs(dx), Math.abs(dy)) < GESTURE_HYSTERESIS) return;
      if (Math.abs(dy) >= Math.abs(dx)) {
        lbGesture.mode = "vertical";
        if (lbImg.hasPointerCapture(event.pointerId)) lbImg.releasePointerCapture(event.pointerId);
        return;
      }
      lbGesture.mode = "horizontal";
      lb.classList.add("is-dragging");
    }
    if (lbGesture.mode !== "horizontal") return;
    event.preventDefault();
    const width = Math.max(1, lb.clientWidth || window.innerWidth);
    let value = lbGesture.startOffset + dx;
    if (value > 0 && lbI === 0) value = rubberband(value, width);
    if (value < 0 && lbI === lbList.length - 1) value = -rubberband(-value, width);
    if (!REDUCED_MOTION.matches) setLbOffset(value);
    const now = performance.now();
    lbGesture.history.push({ p: value, t: now });
    lbGesture.history = lbGesture.history.filter((sample) => now - sample.t <= 140);
  }, { passive: false });

  function finishLbGesture(event, cancelled) {
    if (!lbGesture || event.pointerId !== lbGesture.id) return;
    const current = lbGesture;
    lbGesture = null;
    if (lbImg.hasPointerCapture(event.pointerId)) lbImg.releasePointerCapture(event.pointerId);
    lb.classList.remove("is-dragging");
    if (current.mode !== "horizontal") return;
    const velocity = cancelled ? 0 : recentVelocity(current.history);
    const width = Math.max(1, lb.clientWidth || window.innerWidth);
    const currentOffset = REDUCED_MOTION.matches
      ? current.history[current.history.length - 1].p : lbOffset;
    const projected = currentOffset + projectedDistance(velocity);
    const snapPoints = [{ value: 0, delta: 0 }];
    if (lbI > 0) snapPoints.push({ value: width, delta: -1 });
    if (lbI < lbList.length - 1) snapPoints.push({ value: -width, delta: 1 });
    const target = snapPoints.reduce((nearest, point) =>
      Math.abs(point.value - projected) < Math.abs(nearest.value - projected)
        ? point : nearest, snapPoints[0]);
    if (target.delta) moveLbBy(target.delta, velocity);
    else springLbTo(0, velocity);
  }
  lbImg.addEventListener("pointerup", (event) => finishLbGesture(event, false));
  lbImg.addEventListener("pointercancel", (event) => finishLbGesture(event, true));
})();
