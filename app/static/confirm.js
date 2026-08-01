/* Единое подтверждение действий внутри интерфейса date4you.
 *
 * window.d4yConfirm(message, options) возвращает Promise<boolean>. Формы с
 * data-confirm (на форме или на конкретной submit-кнопке) перехватываются в
 * capture-фазе и после согласия повторно отправляются тем же submitter через
 * requestSubmit: сохраняются name/value, formaction и HTML-валидация.
 */
(function () {
  "use strict";

  if (window.__d4yConfirmInstalled) return;
  window.__d4yConfirmInstalled = true;

  var dialog = null;
  var panel = null;
  var titleNode = null;
  var messageNode = null;
  var cancelButton = null;
  var confirmButton = null;
  var active = null;
  var approvedForms = new WeakSet();
  var pendingForms = new WeakSet();
  var lastSubmitters = new WeakMap();

  function installStyles() {
    if (document.getElementById("d4y-confirm-styles")) return;
    var style = document.createElement("style");
    style.id = "d4y-confirm-styles";
    style.textContent =
      "dialog.d4y-confirm{border:0;padding:0;width:min(430px,calc(100vw - 32px));" +
      "max-width:none;background:transparent;color:var(--ink,var(--op-ink,#322d31));overflow:visible}" +
      "dialog.d4y-confirm::backdrop{background:rgba(19,20,31,.48);backdrop-filter:blur(7px);" +
      "-webkit-backdrop-filter:blur(7px)}" +
      ".d4y-confirm__panel{box-sizing:border-box;width:100%;padding:24px;border:1px solid var(--line,var(--op-line,#eadde1));" +
      "border-radius:24px;background:var(--card,var(--op-surface-solid,#fff));box-shadow:0 24px 80px rgba(17,18,30,.3);" +
      "text-align:left;animation:d4y-confirm-in .2s ease-out}" +
      ".d4y-confirm__mark{display:grid;place-items:center;width:42px;height:42px;margin:0 0 16px;" +
      "border-radius:14px;background:var(--rose-soft,var(--op-accent-soft,#f8e8eb));" +
      "color:var(--rose,var(--op-accent,#b65f6f));" +
      "font:800 24px/1 system-ui,sans-serif}" +
      ".d4y-confirm__title{margin:0;color:var(--ink,var(--op-ink,#322d31));font:700 21px/1.25 system-ui,sans-serif}" +
      ".d4y-confirm__message{margin:10px 0 0;color:var(--muted,var(--op-muted,#71696d));" +
      "font:400 15px/1.55 system-ui,sans-serif;white-space:pre-wrap;overflow-wrap:anywhere}" +
      ".d4y-confirm__actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:22px}" +
      ".d4y-confirm__button{min-height:46px;padding:10px 16px;border:1px solid var(--line,var(--op-line,#ded7da));" +
      "border-radius:14px;background:var(--card,var(--op-surface-solid,#fff));" +
      "color:var(--ink,var(--op-ink,#322d31));font:700 15px/1.2 system-ui,sans-serif;" +
      "cursor:pointer;transition:transform .15s ease,box-shadow .15s ease,background .15s ease}" +
      ".d4y-confirm__button:hover{transform:translateY(-1px)}" +
      ".d4y-confirm__button:focus-visible{outline:3px solid color-mix(in srgb,var(--rose,var(--op-accent,#b65f6f)) 32%,transparent);" +
      "outline-offset:2px}" +
      ".d4y-confirm__button--ok{border-color:transparent;background:var(--rose,var(--op-accent,#b65f6f));color:#fff;" +
      "box-shadow:0 8px 22px color-mix(in srgb,var(--rose,var(--op-accent,#b65f6f)) 28%,transparent)}" +
      ".d4y-confirm--fallback[open]{position:fixed;inset:0;z-index:2147483000;display:grid;place-items:center;" +
      "box-sizing:border-box;width:100%;height:100%;padding:16px;background:rgba(19,20,31,.48)}" +
      ".d4y-confirm--fallback[open] .d4y-confirm__panel{width:min(430px,100%)}" +
      ":root.d4y-confirm-open{overflow:hidden}" +
      "@keyframes d4y-confirm-in{from{opacity:0;transform:translateY(8px) scale(.98)}" +
      "to{opacity:1;transform:none}}" +
      "@media(max-width:480px){.d4y-confirm__panel{padding:20px;border-radius:20px}" +
      ".d4y-confirm__actions{gap:8px}.d4y-confirm__button{padding-inline:10px}}" +
      "@media(prefers-reduced-motion:reduce){.d4y-confirm__panel{animation:none}" +
      ".d4y-confirm__button{transition:none}}";
    document.head.appendChild(style);
  }

  function makeElement(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function focusableElements() {
    if (!dialog) return [];
    return Array.prototype.filter.call(
      dialog.querySelectorAll("button, [href], input, select, textarea, [tabindex]"),
      function (element) {
        return !element.disabled && element.getAttribute("tabindex") !== "-1" &&
          !element.hasAttribute("hidden");
      }
    );
  }

  function restoreFocus(target) {
    if (!target || !target.isConnected || typeof target.focus !== "function") return;
    try {
      target.focus({ preventScroll: true });
    } catch (_) {
      target.focus();
    }
  }

  function settle(result) {
    if (!active) return;
    var request = active;
    active = null;
    document.documentElement.classList.remove("d4y-confirm-open");
    if (dialog) {
      if (dialog.open && typeof dialog.close === "function") {
        dialog.close(result ? "confirm" : "cancel");
      } else {
        dialog.removeAttribute("open");
      }
      dialog.classList.remove("d4y-confirm--fallback");
    }
    restoreFocus(request.trigger);
    request.resolve(Boolean(result));
  }

  function ensureDialog() {
    installStyles();
    if (dialog && dialog.isConnected) return dialog;

    dialog = makeElement("dialog", "d4y-confirm");
    dialog.setAttribute("role", "alertdialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "d4y-confirm-title");
    dialog.setAttribute("aria-describedby", "d4y-confirm-message");

    panel = makeElement("div", "d4y-confirm__panel");
    var mark = makeElement("div", "d4y-confirm__mark", "?");
    mark.setAttribute("aria-hidden", "true");
    titleNode = makeElement("h2", "d4y-confirm__title", "Подтвердите действие");
    titleNode.id = "d4y-confirm-title";
    messageNode = makeElement("p", "d4y-confirm__message");
    messageNode.id = "d4y-confirm-message";

    var actions = makeElement("div", "d4y-confirm__actions");
    cancelButton = makeElement("button", "d4y-confirm__button", "Отмена");
    cancelButton.type = "button";
    confirmButton = makeElement(
      "button", "d4y-confirm__button d4y-confirm__button--ok", "Подтвердить"
    );
    confirmButton.type = "button";
    actions.appendChild(cancelButton);
    actions.appendChild(confirmButton);
    panel.appendChild(mark);
    panel.appendChild(titleNode);
    panel.appendChild(messageNode);
    panel.appendChild(actions);
    dialog.appendChild(panel);
    (document.body || document.documentElement).appendChild(dialog);

    cancelButton.addEventListener("click", function () { settle(false); });
    confirmButton.addEventListener("click", function () { settle(true); });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) settle(false);
    });
    dialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      settle(false);
    });
    dialog.addEventListener("close", function () {
      if (active) settle(dialog.returnValue === "confirm");
    });
    dialog.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        settle(false);
        return;
      }
      if (event.key !== "Tab") return;
      var elements = focusableElements();
      if (!elements.length) {
        event.preventDefault();
        return;
      }
      var first = elements[0];
      var last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    return dialog;
  }

  window.d4yConfirm = function (message, options) {
    // Модальный диалог не позволяет осмысленно начать второе действие. Быстрый
    // повторный клик безопасно отклоняем, чтобы один ответ не запустил два POST.
    if (active) return Promise.resolve(false);
    options = options && typeof options === "object" ? options : {};
    ensureDialog();

    titleNode.textContent = String(options.title || "Подтвердите действие");
    messageNode.textContent = String(message || "Подтвердить действие?");
    cancelButton.textContent = String(options.cancelLabel || "Отмена");
    confirmButton.textContent = String(options.confirmLabel || "Подтвердить");

    var trigger = options.trigger || document.activeElement;
    var promise = new Promise(function (resolve) {
      active = { resolve: resolve, trigger: trigger };
    });
    document.documentElement.classList.add("d4y-confirm-open");
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.classList.add("d4y-confirm--fallback");
      dialog.setAttribute("open", "");
    }
    window.requestAnimationFrame(function () {
      if (active && cancelButton) cancelButton.focus();
    });
    return promise;
  };

  function submitControl(target) {
    if (!target || typeof target.closest !== "function") return null;
    var control = target.closest("button, input");
    if (!control || !control.form) return null;
    var type = String(control.type || "").toLowerCase();
    return type === "submit" || type === "image" ? control : null;
  }

  document.addEventListener("click", function (event) {
    var submitter = submitControl(event.target);
    if (submitter) {
      var form = submitter.form;
      lastSubmitters.set(form, submitter);
      // Нужен только как короткий fallback для браузеров без event.submitter.
      // Не сохраняем старую кнопку после не прошедшей HTML-валидации.
      window.setTimeout(function () {
        if (lastSubmitters.get(form) === submitter) lastSubmitters.delete(form);
      }, 0);
    }
  }, true);

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || String(form.tagName).toLowerCase() !== "form") return;
    if (approvedForms.has(form)) {
      approvedForms.delete(form);
      lastSubmitters.delete(form);
      return;
    }

    var submitter = event.submitter || lastSubmitters.get(form) || null;
    if (submitter && submitter.form !== form) submitter = null;
    var source = submitter && submitter.hasAttribute("data-confirm")
      ? submitter
      : (form.hasAttribute("data-confirm") ? form : null);
    if (!source || !event.cancelable) return;

    event.preventDefault();
    // Не даём старым bubbling-обработчикам открыть системный confirm. После
    // согласия новый submit-event пройдёт через все обработчики как обычно.
    event.stopImmediatePropagation();
    if (pendingForms.has(form)) return;
    pendingForms.add(form);

    window.d4yConfirm(source.getAttribute("data-confirm"), {
      trigger: submitter || document.activeElement
    }).then(function (accepted) {
      pendingForms.delete(form);
      if (!accepted || !form.isConnected) return;
      approvedForms.add(form);
      try {
        if (typeof form.requestSubmit === "function") {
          if (submitter && submitter.form === form && submitter.isConnected) {
            form.requestSubmit(submitter);
          } else {
            form.requestSubmit();
          }
        } else if (submitter && submitter.form === form && submitter.isConnected) {
          submitter.click();
        } else {
          form.submit();
        }
      } finally {
        // Если повторная HTML-валидация не породила submit-event, разрешение не
        // должно случайно сохраниться до следующей пользовательской попытки.
        window.setTimeout(function () { approvedForms.delete(form); }, 0);
      }
    }, function () {
      pendingForms.delete(form);
    });
  }, true);

  document.addEventListener("turbo:before-cache", function () {
    if (active) settle(false);
    if (dialog && dialog.isConnected) dialog.remove();
  });
})();
