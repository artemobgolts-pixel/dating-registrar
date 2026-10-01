// Поиск сохраняет поле ввода, пока сервер обновляет подходящие элементы списка.
// Лента использует собственную AJAX-загрузку и режим submit.
(function () {
  "use strict";

  var states = new WeakMap();
  var active = new Set();
  var delay = 250;

  function inputFor(form) {
    return form.querySelector('input[type="search"][name]');
  }

  function stateFor(form) {
    var state = states.get(form);
    if (!state) {
      state = { form: form, timer: null, request: null, generation: 0 };
      states.set(form, state);
    }
    active.add(state);
    return state;
  }

  function cancel(state) {
    clearTimeout(state.timer);
    state.timer = null;
    state.generation += 1;
    if (state.request) state.request.abort();
    state.request = null;
    state.form.removeAttribute("aria-busy");
  }

  function controls(form) {
    var input = inputFor(form);
    form.querySelectorAll("[data-live-search-clear]").forEach(function (clear) {
      clear.hidden = !input.value;
    });
  }

  function announce(form, message, failed) {
    var status = form.querySelector("[data-live-search-status]");
    if (!status) {
      status = document.createElement("span");
      status.setAttribute("data-live-search-status", "");
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      form.appendChild(status);
    }
    status.className = failed ? "muted live-search-status" : "sr-only";
    status.textContent = message;
    status.hidden = !message;
  }

  function requestURL(form) {
    var url = new URL(form.action, location.href);
    url.search = new URLSearchParams(new FormData(form)).toString();
    var input = inputFor(form);
    if (!input.value.trim()) url.searchParams.delete(input.name);
    return url;
  }

  function syncFilters(form) {
    var input = inputFor(form);
    var path = new URL(form.action, location.href).pathname;
    document.querySelectorAll('form[method="get"]').forEach(function (other) {
      if (other === form || new URL(other.action, location.href).pathname !== path) return;
      var field = other.elements.namedItem(input.name);
      if (!field) {
        field = document.createElement("input");
        field.type = "hidden";
        field.name = input.name;
        other.appendChild(field);
      }
      if (field.type === "hidden") field.value = input.value;
    });
  }

  async function search(state) {
    var form = state.form;
    if (!form.isConnected) return;
    if (form.dataset.liveSearch === "submit") {
      // Отмена запросов, пагинация и ошибки ленты остаются в initCommunity.
      form.requestSubmit();
      return;
    }

    var url = requestURL(form);
    if (state.lastURL === url.href) return;
    var generation = state.generation;
    var request = new AbortController();
    state.request = request;
    form.setAttribute("aria-busy", "true");
    announce(form, "Ищем…", false);
    try {
      var response = await fetch(url.href, {
        credentials: "same-origin", signal: request.signal,
        headers: { "X-Requested-With": "fetch" }
      });
      if (!response.ok) throw new Error("Search failed");
      var html = await response.text();
      if (generation !== state.generation || !form.isConnected) return;
      var page = new DOMParser().parseFromString(html, "text/html");
      var current = document.querySelector("[data-live-search-results]");
      var incoming = page.querySelector("[data-live-search-results]");
      if (!current || !incoming) {
        // Истёкшая сессия и перенаправления сервера идут обычным маршрутом.
        location.assign(response.url || url.href);
        return;
      }

      var input = inputFor(form);
      var focused = document.activeElement === input;
      var start = input.selectionStart, end = input.selectionEnd;
      var direction = input.selectionDirection;
      if (current.contains(form)) {
        var nextForm = incoming.querySelector("form[data-live-search]");
        if (!nextForm) throw new Error("Search form missing");
        nextForm.replaceWith(form);
      }
      incoming.querySelectorAll("script").forEach(function (script) { script.remove(); });
      current.replaceChildren.apply(current, Array.from(incoming.childNodes));
      if (focused) {
        input.focus({ preventScroll: true });
        input.setSelectionRange(start, end, direction);
      }
      history.replaceState(history.state, "", url.pathname + url.search + url.hash);
      state.lastURL = url.href;
      controls(form);
      syncFilters(form);
      announce(form, "Результаты обновлены", false);
      document.dispatchEvent(new CustomEvent("live-search:render", { detail: { url: url.href } }));
    } catch (error) {
      if (error.name !== "AbortError" && generation === state.generation && form.isConnected) {
        announce(form, "Не удалось выполнить поиск. Нажми Enter, чтобы повторить.", true);
      }
    } finally {
      if (generation === state.generation) {
        state.request = null;
        form.removeAttribute("aria-busy");
      }
    }
  }

  function schedule(input, immediate) {
    var form = input.form;
    if (!form || !form.hasAttribute("data-live-search")) return;
    var state = stateFor(form);
    cancel(state);
    controls(form);
    syncFilters(form);
    if (input.dataset.liveSearchComposing) return;
    if (immediate || !input.value) search(state);
    else state.timer = setTimeout(function () { search(state); }, delay);
  }

  document.addEventListener("input", function (event) {
    if (event.target.matches('input[type="search"]')) schedule(event.target, false);
  });
  document.addEventListener("compositionstart", function (event) {
    if (!event.target.matches('input[type="search"]')) return;
    event.target.dataset.liveSearchComposing = "1";
    schedule(event.target, false);
  });
  document.addEventListener("compositionend", function (event) {
    if (!event.target.matches('input[type="search"]')) return;
    delete event.target.dataset.liveSearchComposing;
    schedule(event.target, false);
  });
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches("form[data-live-search]")) return;
    var input = inputFor(form);
    if (form.dataset.liveSearch === "submit") {
      // Нормализация ленты не должна стирать пробел между вводимыми словами.
      var value = input.value, start = input.selectionStart, end = input.selectionEnd;
      var direction = input.selectionDirection;
      cancel(stateFor(form));
      queueMicrotask(function () {
        if (!input.isConnected) return;
        input.value = value;
        input.setSelectionRange(start, end, direction);
        controls(form);
      });
      return;
    }
    event.preventDefault();
    var state = stateFor(form);
    cancel(state);
    search(state);
  }, true);
  document.addEventListener("click", function (event) {
    var clear = event.target.closest("[data-live-search-clear]");
    if (!clear) return;
    var form = clear.closest("form[data-live-search]");
    if (!form) return;
    cancel(stateFor(form));
    if (form.dataset.liveSearch === "submit") return;
    event.preventDefault();
    var input = inputFor(form);
    input.value = "";
    input.focus({ preventScroll: true });
    schedule(input, true);
  }, true);

  function stop() {
    active.forEach(cancel);
    active.clear();
  }
  document.addEventListener("turbo:before-visit", stop);
  document.addEventListener("turbo:before-cache", stop);
  window.addEventListener("pagehide", stop);
})();
