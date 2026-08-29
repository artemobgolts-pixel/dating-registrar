// Вход/подключение Telegram. Две независимые поверхности:
//   1) Страница входа (/login) и вход-модалки: официальный Telegram Login Widget
//      + обычные OAuth-ссылки. Согласие с документами показывается пассивным
//      текстом в шаблоне и не блокирует способы входа.
//   2) Кабинет: блоки «Подключить уведомления» (.tg-connect) — deep-link в бота.
(function () {
  "use strict";

  // --- 1. Telegram Login Widget ---------------------------------------------
  function loginSurface(wrap) {
    return wrap.closest(".login-card, .login-dlg");
  }

  function setTelegramState(wrap, state) {
    var surface = loginSurface(wrap);
    wrap.classList.toggle("tg-checking", state === "checking");
    wrap.classList.toggle("tg-unavailable", state === "unavailable");
    if (state === "ready" && wrap._tgRetry) {
      clearInterval(wrap._tgRetry);
      wrap._tgRetry = null;
    }
    if (surface) {
      surface.classList.toggle("tg-login-checking", state === "checking");
      surface.classList.toggle("tg-login-unavailable", state === "unavailable");
    }
  }

  function scheduleTelegramRetry(wrap) {
    if (wrap._tgRetry) return;
    wrap._tgRetry = setInterval(function () {
      if (!document.documentElement.contains(wrap)) {
        clearInterval(wrap._tgRetry);
        wrap._tgRetry = null;
        return;
      }
      if (wrap.dataset.tgLoading) return;
      // Повторяем загрузку разрешённого CSP официального скрипта. Отдельный
      // image/fetch-пробник здесь неприменим: строгая политика страницы
      // намеренно разрешает telegram.org только в script-src.
      wrap.replaceChildren();
      delete wrap.dataset.tgLoaded;
      delete wrap.dataset.tgLoading;
      setTelegramState(wrap, "checking");
      loadLoginWidget(wrap);
    }, 6000);
  }

  function verifyLoginWidget(wrap) {
    setTimeout(function () {
      if (wrap.querySelector("iframe")) {
        setTelegramState(wrap, "ready");
      } else {
        setTelegramState(wrap, "unavailable");
        scheduleTelegramRetry(wrap);
      }
    }, 1200);
  }

  function loadLoginWidget(wrap) {
    if (!wrap || wrap.dataset.tgLoaded || wrap.dataset.tgLoading ||
        !wrap.getAttribute("data-bot")) return;
    wrap.dataset.tgLoading = "1";
    wrap.dataset.tgLoaded = "1";
    setTelegramState(wrap, "checking");
    var s = document.createElement("script");
    s.async = true;
    s.src = "https://telegram.org/js/telegram-widget.js?22";
    s.setAttribute("data-telegram-login", wrap.getAttribute("data-bot"));
    s.setAttribute("data-size", "large");
    s.setAttribute("data-radius", "12");
    s.setAttribute("data-auth-url", wrap.getAttribute("data-auth-url") || "/auth/widget");
    s.setAttribute("data-request-access", "write");
    s.addEventListener("load", function () {
      delete wrap.dataset.tgLoading;
      verifyLoginWidget(wrap);
    });
    s.addEventListener("error", function () {
      delete wrap.dataset.tgLoading;
      setTelegramState(wrap, "unavailable");
      scheduleTelegramRetry(wrap);
    });
    wrap.appendChild(s);
    // В некоторых браузерах заблокированный скрипт не присылает error;
    // контрольный таймаут всё равно схлопнет поверхность через несколько секунд.
    setTimeout(function () {
      if (wrap.classList.contains("tg-checking")) verifyLoginWidget(wrap);
    }, 4200);
  }

  function prepareLoginWidget(wrap) {
    if (!wrap || wrap.dataset.tgPrepared) return;
    wrap.dataset.tgPrepared = "1";
    var dialog = wrap.closest("dialog");
    if (!dialog || dialog.open) {
      loadLoginWidget(wrap);
      return;
    }
    // Telegram превращает скрипт в iframe с нулевыми размерами, если выполнить
    // его внутри закрытого dialog. Ждём появления атрибута open и грузим один раз.
    var observer = new MutationObserver(function () {
      if (!dialog.open) return;
      observer.disconnect();
      loadLoginWidget(wrap);
    });
    observer.observe(dialog, { attributes: true, attributeFilter: ["open"] });
  }

  function prepareLoginWidgets() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-tg-widget]"),
      prepareLoginWidget);
  }
  prepareLoginWidgets();

  // --- 2. Подключение бота в кабинете (deep-link + поллинг) --------------------
  function wire(box) {
    var btn = box.matches("[data-tg-connect]")
      ? box : box.querySelector("[data-tg-connect]");
    if (!btn) return;
    var hint = box.querySelector("[data-tg-hint]");
    var link = box.querySelector("[data-tg-link]");
    var errBox = box.querySelector("[data-tg-error]");
    var compact = btn.hasAttribute("data-tg-compact");
    var label = compact ? btn.getAttribute("aria-label") : btn.textContent;
    var returnTo = box.getAttribute("data-return-to") || "";
    var timer = null;

    function setConnectLabel(value) {
      if (!compact) {
        btn.textContent = value;
        return;
      }
      btn.setAttribute("aria-label", value);
      btn.title = value;
      var stateLabel = btn.querySelector("[data-tg-state-label]");
      if (stateLabel) stateLabel.textContent = value;
    }

    function showError(msg) {
      if (errBox) { errBox.textContent = msg; errBox.hidden = false; }
      btn.disabled = false;
      setConnectLabel(label);
      delete btn.dataset.tgDirect;
    }

    function poll(code) {
      fetch("/auth/poll?code=" + encodeURIComponent(code), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status === "ok") { window.location = d.redirect || "/admin/"; return; }
          if (d.status === "expired") { clearInterval(timer); showError("Код истёк — нажми кнопку ещё раз."); return; }
          if (d.status === "banned") { clearInterval(timer); showError("Доступ закрыт. Напиши в поддержку."); return; }
          if (d.status === "conflict") { clearInterval(timer); showError("Этот Telegram уже связан с другим аккаунтом."); return; }
          if (d.status === "forbidden") { clearInterval(timer); showError("Код создан в другой сессии. Начни подключение заново."); return; }
        })
        .catch(function () { /* временная сетевая ошибка — продолжаем поллинг */ });
    }

    btn.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      // Если браузер заблокировал первое окно, повторный клик уже открывает
      // готовый deep-link синхронно из пользовательского жеста. В крайнем
      // случае уходим в Telegram в этой же вкладке — действие не теряется.
      if (btn.dataset.tgDirect) {
        var directWindow = window.open(btn.dataset.tgDirect, "_blank", "noopener");
        if (!directWindow) window.location.assign(btn.dataset.tgDirect);
        return;
      }
      if (errBox) errBox.hidden = true;
      btn.disabled = true;
      setConnectLabel("Открываю Telegram…");
      // Popup создаём до async fetch: Safari и строгие блокировщики считают
      // window.open после Promise уже не связанным с исходным click.
      var telegramWindow = window.open("about:blank", "_blank");
      if (telegramWindow) {
        try { telegramWindow.opener = null; } catch (_) {}
      }
      var startUrl = "/auth/start" + (returnTo
        ? "?return_to=" + encodeURIComponent(returnTo) : "");
      fetch(startUrl, { method: "POST", credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) throw new Error("start failed");
          return r.json();
        })
        .then(function (d) {
          if (telegramWindow && !telegramWindow.closed) {
            try {
              telegramWindow.location.replace(d.url);
            } catch (_) {}
          }
          // Оставляем прямой повторный вход даже после успешного открытия:
          // notification CTA не имеет отдельной fallback-ссылки, а пользователь
          // мог случайно закрыть Telegram до нажатия Start/Подтвердить.
          btn.dataset.tgDirect = d.url;
          btn.disabled = false;
          setConnectLabel("Открыть Telegram");
          if (link) {
            link.href = d.url;
            link.textContent = "Открыть Telegram";
            link.hidden = false;
          }
          if (hint) hint.hidden = false;
          clearInterval(timer);
          timer = setInterval(function () { poll(d.code); }, 2000);
        })
        .catch(function () {
          if (telegramWindow && !telegramWindow.closed) telegramWindow.close();
          showError("Не получилось начать. Попробуй ещё раз.");
        });
    });
  }

  function wireAll() {
    Array.prototype.forEach.call(document.querySelectorAll(".tg-connect"), function (box) {
      if (box.dataset.wired) return;     // не вешаем повторно (Turbo пере-рендер)
      box.dataset.wired = "1";
      wire(box);
    });
  }
  wireAll();
  // Под Turbo блок «Подключить уведомления» — новый узел после перехода:
  // переинициализируем общий CTA после замены страницы.
  document.addEventListener("turbo:load", function () {
    prepareLoginWidgets();
    wireAll();
  });
})();
