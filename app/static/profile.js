(function () {
  "use strict";

  function initProfileCollection() {
    var section = document.getElementById("profileCollection");
    // JS-property намеренно не сериализуется в Turbo cache: восстановленный
    // clone получит свежие listeners, а живой DOM не инициализируется дважды.
    if (!section || section._d4yWidgetReady) return;
    section._d4yWidgetReady = true;

    var dialog = document.getElementById("profileEventDlg");
    var body = dialog && dialog.querySelector("[data-profile-widget-body]");
    var close = dialog && dialog.querySelector("[data-profile-widget-close]");
    var widgetRequest = null;
    var editorNavigationPending = false;

    function toast(message) {
      var node = document.getElementById("profileToast");
      if (!node) {
        node = document.createElement("div");
        node.id = "profileToast";
        node.className = "profile-toast";
        node.setAttribute("role", "status");
        document.body.appendChild(node);
      }
      node.textContent = message;
      node.classList.add("show");
      clearTimeout(node._hideTimer);
      node._hideTimer = setTimeout(function () {
        node.classList.remove("show");
      }, 2600);
    }

    function setDialogOpen() {
      if (!dialog.open && typeof dialog.showModal === "function") dialog.showModal();
      else if (!dialog.open) dialog.setAttribute("open", "");
    }

    function stopWidgetRequest() {
      if (widgetRequest) widgetRequest.abort();
      widgetRequest = null;
    }

    function closeWidget() {
      stopWidgetRequest();
      if (!dialog) return;
      if (dialog.open && typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
      if (body) body.replaceChildren();
    }

    function enableWidgetVideos(root) {
      root.querySelectorAll("video[data-src]").forEach(function (video) {
        if (!video.getAttribute("src")) {
          video.src = video.getAttribute("data-src");
          video.load();
        }
      });
    }

    function openWidget(url) {
      if (!dialog || !body || !url) return;
      stopWidgetRequest();
      var controller = new AbortController();
      widgetRequest = controller;
      body.innerHTML = '<p class="profile-widget-status">Загружаю…</p>';
      setDialogOpen();
      fetch(url, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
        signal: controller.signal
      })
        .then(function (response) {
          // Истёкшая сессия даёт 303 на /login, а fetch молча следует ему.
          // Не вставляем полноценную страницу входа внутрь dialog.
          if (!response.ok || response.redirected) {
            throw new Error("widget unavailable");
          }
          return response.text();
        })
        .then(function (html) {
          body.innerHTML = html;
          enableWidgetVideos(body);
        })
        .catch(function (error) {
          if (error && error.name === "AbortError") return;
          body.innerHTML = '<p class="profile-widget-status">Не удалось открыть карточку</p>';
        })
        .finally(function () {
          if (widgetRequest === controller) widgetRequest = null;
        });
    }

    function jsonRequest(url, options) {
      return fetch(url, options).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok || !data.ok) {
            throw new Error(data.detail || "Не удалось сохранить");
          }
          return data;
        });
      });
    }

    function updateReviewDisplay(root, rating, text) {
      if (!root) return;
      var stars = root.querySelector(".review-stars");
      if (stars && Number.isFinite(rating)) {
        stars.setAttribute("aria-label", "Оценка " + rating + " из 5");
        stars.querySelectorAll("span").forEach(function (star, index) {
          star.classList.toggle("off", index >= rating);
        });
      }
      var copy = root.querySelector(".profile-review-copy, .review-copy");
      if (copy) {
        copy.textContent = text || "Без текста";
        copy.classList.toggle("empty", !text);
      }
    }

    function saveReview(form) {
      if (form.dataset.saving === "1") return;
      var submit = form.querySelector('[type="submit"]');
      var previous = submit ? submit.textContent : "";
      var payload = new FormData(form);
      form.dataset.saving = "1";
      if (submit) {
        submit.disabled = true;
        submit.textContent = "Сохраняю…";
      }
      jsonRequest(form.action, {
        method: "POST",
        credentials: "same-origin",
        body: payload,
        headers: {
          "Accept": "application/json",
          "X-Requested-With": "fetch"
        }
      }).then(function (result) {
        var saved = result.review || result;
        var rating = parseInt(
          saved.rating != null ? saved.rating : payload.get("rating"),
          10
        );
        var textValue = saved.text;
        if (textValue == null) textValue = saved.review_text;
        if (textValue == null) textValue = payload.get("text") || "";
        textValue = String(textValue).trim();
        updateReviewDisplay(form.closest(".profile-review-widget"), rating, textValue);
        updateReviewDisplay(
          document.getElementById("review-" + form.dataset.reviewId),
          rating,
          textValue
        );
        toast(result.message || "Обзор сохранён");
      }).catch(function (error) {
        toast(error.message || "Не удалось сохранить обзор");
      }).finally(function () {
        form.dataset.saving = "0";
        if (submit) {
          submit.disabled = false;
          submit.textContent = previous;
        }
      });
    }

    function toggleWant(button) {
      var data = new FormData();
      data.append("csrf", document.body.dataset.csrf || "");
      var previous = button.textContent;
      button.disabled = true;
      button.textContent = "Сохраняю…";
      jsonRequest(button.dataset.want, {
        method: "POST",
        credentials: "same-origin",
        body: data,
        headers: { "X-Requested-With": "fetch" }
      }).then(function (result) {
        var wanted = Boolean(result.wanted);
        button.dataset.wanted = wanted ? "1" : "0";
        button.setAttribute("aria-pressed", wanted ? "true" : "false");
        button.textContent = wanted ? "Убрать из «Хочу сходить»" : "Хочу сходить";
        button.classList.toggle("primary", !wanted);
        button.classList.toggle("ghost", wanted);
        toast(result.message || "Сохранено");
      }).catch(function (error) {
        button.textContent = previous;
        toast(error.message || "Нет связи");
      }).finally(function () {
        button.disabled = false;
      });
    }

    function addToCollection(button) {
      var previous = button.textContent;
      button.disabled = true;
      button.textContent = "Добавляю…";
      jsonRequest(button.dataset.add, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-Requested-With": "fetch",
          "X-CSRF-Token": document.body.dataset.csrf || ""
        }
      }).then(function () {
        button.textContent = "Добавлено ✓";
        toast("Событие добавлено в твою коллекцию");
      }).catch(function (error) {
        button.disabled = false;
        button.textContent = previous;
        toast(error.message || "Нет связи");
      });
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
        document.body.appendChild(field);
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

    function shareEvent(button) {
      var url = button.dataset.shareUrl;
      if (!url) return;
      var mobilePointer = typeof window.matchMedia === "function"
        && window.matchMedia("(pointer: coarse) and (max-width: 900px)").matches;
      if (mobilePointer && typeof navigator.share === "function") {
        navigator.share({
          title: button.dataset.shareTitle || "Событие date4you",
          text: button.dataset.shareText || "Посмотри это событие в date4you",
          url: url
        }).catch(function (error) {
          if (!error || error.name !== "AbortError") {
            copyText(url).then(function () { toast("Ссылка скопирована"); });
          }
        });
      } else {
        copyText(url)
          .then(function () { toast("Ссылка скопирована"); })
          .catch(function () { toast("Не удалось скопировать ссылку"); });
      }
    }

    function activateProfileEvent(opener) {
      if (opener.dataset.profileEditor) {
        if (editorNavigationPending) return;
        editorNavigationPending = true;
        var target = opener.dataset.profileEditor;
        var pendingSave = window.d4yProfileSave;
        if (pendingSave && typeof pendingSave.finally === "function") {
          pendingSave.finally(function () { window.location.assign(target); });
        } else {
          window.location.assign(target);
        }
      } else {
        openWidget(opener.dataset.profileWidget);
      }
    }

    section.addEventListener("click", function (event) {
      var opener = event.target.closest("[data-profile-widget], [data-profile-editor]");
      if (!opener || !section.contains(opener)) return;
      if (event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      var interactive = event.target.closest(
        "a, button, input, textarea, select, label, summary, form, details"
      );
      if (interactive && interactive !== opener) return;
      event.preventDefault();
      activateProfileEvent(opener);
    });
    section.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" && event.key !== " ") return;
      var opener = event.target.closest("[data-profile-widget], [data-profile-editor]");
      if (!opener || event.target !== opener || !section.contains(opener)) return;
      event.preventDefault();
      activateProfileEvent(opener);
    });

    if (close) close.addEventListener("click", closeWidget);
    if (dialog) {
      dialog.addEventListener("click", function (event) {
        if (event.target === dialog) closeWidget();
      });
      dialog.addEventListener("close", function () {
        stopWidgetRequest();
        if (body) body.replaceChildren();
      });
    }

    if (body) body.addEventListener("click", function (event) {
      var want = event.target.closest("[data-want]");
      if (want) {
        event.preventDefault();
        toggleWant(want);
        return;
      }
      var add = event.target.closest("[data-add]");
      if (add) {
        event.preventDefault();
        addToCollection(add);
        return;
      }
      var share = event.target.closest("[data-community-share]");
      if (share) {
        event.preventDefault();
        shareEvent(share);
      }
    });
    if (body) body.addEventListener("submit", function (event) {
      var form = event.target.closest(".profile-review-editor");
      if (!form || !body.contains(form)) return;
      event.preventDefault();
      saveReview(form);
    });
  }

  document.addEventListener("DOMContentLoaded", initProfileCollection);
  document.addEventListener("turbo:load", initProfileCollection);
  if (document.readyState !== "loading") initProfileCollection();
})();
