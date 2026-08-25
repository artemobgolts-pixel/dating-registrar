/* Публичная страница обзора: вход для коллекции и анонимная жалоба.
   Действия гостевой карточки намеренно сюда не подключаются. */
(function () {
  "use strict";

  var body = document.body;
  if (!body || !body.classList.contains("review-share-page")) return;

  var csrf = body.dataset.csrf || "";
  var loginDialog = document.getElementById("loginDlg");
  var reportDialog = document.getElementById("reportDlg");
  var reportForm = document.getElementById("reportForm");
  var toastNode = document.getElementById("toast");
  var toastTimer = 0;

  function toast(message) {
    if (!toastNode) return;
    toastNode.textContent = message;
    toastNode.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toastNode.classList.remove("show");
    }, 2800);
  }

  function openDialog(dialog) {
    if (!dialog) return false;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    return true;
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function openLogin() {
    if (openDialog(loginDialog)) return;
    window.location.href = "/login?next=" + encodeURIComponent(
      window.location.pathname + window.location.search
    );
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var field = document.createElement("textarea");
      field.value = text;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      body.appendChild(field);
      field.select();
      try {
        if (document.execCommand("copy")) resolve();
        else reject(new Error("copy unavailable"));
      } catch (error) {
        reject(error);
      } finally {
        field.remove();
      }
    });
  }

  function copiedFeedback(button) {
    var original = button.dataset.shareLabel || button.textContent.trim();
    button.dataset.shareLabel = original;
    button.textContent = "Ссылка скопирована ✓";
    window.clearTimeout(button._shareFeedbackTimer);
    button._shareFeedbackTimer = window.setTimeout(function () {
      if (button.isConnected) button.textContent = original;
    }, 1700);
  }

  function shareReview(button) {
    var url = button.dataset.shareUrl;
    if (!url) return;
    var mobilePointer = typeof window.matchMedia === "function"
      && window.matchMedia("(pointer: coarse) and (max-width: 900px)").matches;
    if (mobilePointer && typeof navigator.share === "function") {
      navigator.share({
        title: button.dataset.shareTitle || "Обзор события в date4you",
        text: button.dataset.shareText || "Посмотри этот обзор в date4you",
        url: url
      }).catch(function (error) {
        if (!error || error.name === "AbortError") return;
        copyText(url)
          .then(function () { copiedFeedback(button); })
          .catch(function () { toast("Не удалось скопировать ссылку"); });
      });
      return;
    }
    copyText(url)
      .then(function () { copiedFeedback(button); })
      .catch(function () { toast("Не удалось скопировать ссылку"); });
  }

  document.addEventListener("click", function (event) {
    var shareTrigger = event.target.closest("[data-community-share]");
    if (shareTrigger) {
      event.preventDefault();
      shareReview(shareTrigger);
      return;
    }

    var loginTrigger = event.target.closest("#loginOpen, [data-login-open]");
    if (loginTrigger) {
      event.preventDefault();
      openLogin();
      return;
    }

    var reportTrigger = event.target.closest("[data-report-open]");
    if (!reportTrigger) return;
    event.preventDefault();
    var title = document.getElementById("reportTitle");
    var target = document.getElementById("reportTargetId");
    var reason = document.getElementById("reportReason");
    if (title) title.textContent = reportTrigger.dataset.name || "событие";
    if (target && reportTrigger.dataset.id) target.value = reportTrigger.dataset.id;
    if (reason) reason.value = "";
    openDialog(reportDialog);
  });

  var loginClose = document.getElementById("loginClose");
  if (loginClose) {
    loginClose.addEventListener("click", function () { closeDialog(loginDialog); });
  }
  if (loginDialog) {
    loginDialog.addEventListener("click", function (event) {
      if (event.target === loginDialog) closeDialog(loginDialog);
    });
  }

  var reportCancel = document.getElementById("reportCancel");
  if (reportCancel) {
    reportCancel.addEventListener("click", function () { closeDialog(reportDialog); });
  }
  if (reportDialog) {
    reportDialog.addEventListener("click", function (event) {
      if (event.target === reportDialog) closeDialog(reportDialog);
    });
  }

  function errorMessage(response, data) {
    var detail = data && data.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail.msg === "string") return detail.msg;
    return response.status === 429
      ? "Слишком много попыток — попробуй чуть позже"
      : "Не удалось отправить жалобу";
  }

  if (reportForm) {
    reportForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var submit = reportForm.querySelector('[type="submit"]');
      if (submit && submit.disabled) return;
      if (submit) submit.disabled = true;

      fetch(reportForm.action, {
        method: "POST",
        body: new FormData(reportForm),
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "X-CSRF-Token": csrf,
          "X-Requested-With": "fetch"
        }
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok || data.ok === false) {
            throw new Error(errorMessage(response, data));
          }
          closeDialog(reportDialog);
          reportForm.reset();
          toast("Спасибо, жалоба отправлена. Модератор проверит.");
        });
      }).catch(function (error) {
        toast(error.message || "Нет связи — попробуй ещё раз");
      }).finally(function () {
        if (submit) submit.disabled = false;
      });
    });
  }
})();
