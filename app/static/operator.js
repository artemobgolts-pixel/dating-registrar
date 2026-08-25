// Интерактивность операторской панели без зависимостей.
(function () {
  "use strict";

  var body = document.body;
  var sidebar = document.getElementById("operatorSidebar");
  var menuButtons = document.querySelectorAll("[data-op-nav-toggle]");
  var overlay = document.querySelector("[data-op-nav-close]");
  var edge = document.querySelector("[data-op-nav-edge]");
  var main = document.querySelector("[data-op-main]");
  var desktop = window.matchMedia("(min-width: 901px)");
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var HYSTERESIS = 10;
  var navOpen = false;
  var navTargetOpen = false;
  var navTrigger = null;
  var cancelNavSpring = null;
  var navGesture = null;
  var suppressDrawerClickUntil = 0;

  function projectedDistance(velocity, decelerationRate) {
    var rate = decelerationRate || 0.998;
    return (velocity / 1000) * rate / (1 - rate);
  }

  function rubberband(overshoot, dimension, constant) {
    var c = constant || 0.55;
    return (overshoot * dimension * c) /
      (dimension + c * Math.abs(overshoot));
  }

  function recentVelocity(history, now) {
    var time = now || performance.now();
    var recent = history.filter(function (sample) { return time - sample.t <= 120; });
    if (recent.length < 2) return 0;
    var first = recent[0];
    var last = recent[recent.length - 1];
    var seconds = (last.t - first.t) / 1000;
    var velocity = seconds > 0 ? (last.p - first.p) / seconds : 0;
    return Math.max(-5000, Math.min(5000, velocity));
  }

  function focusables(root) {
    if (!root) return [];
    return Array.prototype.slice.call(root.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
      'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(function (element) {
      return !element.hidden && element.getAttribute("aria-hidden") !== "true" &&
        element.getClientRects().length > 0;
    });
  }

  function setMainBlocked(blocked) {
    if (!main) return;
    if (blocked) {
      if ("inert" in main) main.inert = true;
      main.setAttribute("aria-hidden", "true");
    } else {
      if ("inert" in main) main.inert = false;
      main.removeAttribute("aria-hidden");
    }
  }

  function updateToggleState(open) {
    menuButtons.forEach(function (button) {
      button.setAttribute("aria-expanded", open ? "true" : "false");
      button.setAttribute("aria-label", open ? "Закрыть меню" : "Открыть меню");
    });
  }

  function exposeOpenNav(trigger, moveFocus) {
    if (!sidebar || desktop.matches) return;
    navOpen = navTargetOpen = true;
    if (trigger) navTrigger = trigger;
    if (!navTrigger) navTrigger = menuButtons[0] || null;
    body.classList.add("op-nav-open");
    updateToggleState(true);
    sidebar.setAttribute("aria-hidden", "false");
    sidebar.setAttribute("role", "dialog");
    sidebar.setAttribute("aria-modal", "true");
    if ("inert" in sidebar) sidebar.inert = false;
    if (moveFocus && !sidebar.contains(document.activeElement)) {
      var first = focusables(sidebar)[0];
      if (first) first.focus({ preventScroll: true });
    }
    setMainBlocked(true);
  }

  function finalizeClosedNav(restoreFocus) {
    if (!sidebar) return;
    navOpen = navTargetOpen = false;
    body.classList.remove("op-nav-open", "op-nav-gesturing");
    updateToggleState(false);
    sidebar.setAttribute("aria-hidden", "true");
    sidebar.removeAttribute("role");
    sidebar.removeAttribute("aria-modal");
    if ("inert" in sidebar) sidebar.inert = true;
    sidebar.style.transition = "";
    sidebar.style.transform = "";
    setMainBlocked(false);
    if (restoreFocus !== false && navTrigger && navTrigger.isConnected) {
      navTrigger.focus({ preventScroll: true });
    }
  }

  function syncDesktopNav() {
    if (!sidebar) return;
    if (cancelNavSpring) cancelNavSpring();
    cancelNavSpring = null;
    navGesture = null;
    body.classList.remove("op-nav-open", "op-nav-gesturing");
    sidebar.style.transition = "";
    sidebar.style.transform = "";
    setMainBlocked(false);
    if (desktop.matches) {
      navOpen = navTargetOpen = false;
      updateToggleState(false);
      sidebar.removeAttribute("aria-hidden");
      sidebar.removeAttribute("role");
      sidebar.removeAttribute("aria-modal");
      if ("inert" in sidebar) sidebar.inert = false;
      return;
    }
    finalizeClosedNav(false);
  }

  function closedX() {
    return sidebar ? -(sidebar.getBoundingClientRect().width + 24) : -340;
  }

  function presentationX() {
    if (!sidebar) return 0;
    var transform = window.getComputedStyle(sidebar).transform;
    if (!transform || transform === "none") return 0;
    if (window.DOMMatrixReadOnly) {
      try { return new DOMMatrixReadOnly(transform).m41; } catch (_) {}
    }
    var match = transform.match(/^matrix\(([^)]+)\)$/);
    return match ? Number(match[1].split(",")[4]) || 0 : 0;
  }

  function setSidebarX(value) {
    sidebar.style.transform = "translate3d(" + value + "px, 0, 0)";
  }

  function stopNavMotion() {
    if (cancelNavSpring) cancelNavSpring();
    cancelNavSpring = null;
  }

  function springSidebar(from, target, initialVelocity, complete) {
    stopNavMotion();
    if (reducedMotion.matches) {
      setSidebarX(target);
      if (complete) complete();
      return;
    }
    var value = from;
    var velocity = Number.isFinite(initialVelocity) ? initialVelocity : 0;
    var previous = performance.now();
    var frame = 0;
    var stopped = false;
    var response = 0.32;
    var damping = Math.abs(velocity) > 140 ? 0.86 : 1;
    var omega = 2 * Math.PI / response;
    var stiffness = omega * omega;
    var friction = 2 * damping * omega;
    body.classList.add("op-nav-gesturing");
    sidebar.style.transition = "none";
    function tick(now) {
      if (stopped) return;
      var dt = Math.min(0.032, Math.max(0.001, (now - previous) / 1000));
      previous = now;
      var acceleration = -stiffness * (value - target) - friction * velocity;
      velocity += acceleration * dt;
      value += velocity * dt;
      setSidebarX(value);
      if (Math.abs(value - target) < 0.45 && Math.abs(velocity) < 5) {
        setSidebarX(target);
        cancelNavSpring = null;
        if (complete) complete();
        return;
      }
      frame = requestAnimationFrame(tick);
    }
    frame = requestAnimationFrame(tick);
    cancelNavSpring = function () {
      stopped = true;
      if (frame) cancelAnimationFrame(frame);
    };
  }

  function settleNav(open, velocity, trigger, moveFocus) {
    if (!sidebar || desktop.matches) return;
    navTargetOpen = open;
    var from = presentationX();
    var target = open ? 0 : closedX();
    // Keep the CSS-open state while closing so a reversal can grab the exact
    // presentation value instead of jumping to the off-screen logical state.
    body.classList.add("op-nav-open");
    sidebar.style.transition = "none";
    setSidebarX(from);
    if (open) exposeOpenNav(trigger, moveFocus !== false);
    springSidebar(from, target, velocity || 0, function () {
      if (open) {
        navOpen = navTargetOpen = true;
        body.classList.remove("op-nav-gesturing");
        sidebar.style.transition = "";
        sidebar.style.transform = "";
      } else {
        finalizeClosedNav(true);
      }
    });
  }

  menuButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      settleNav(!navTargetOpen, 0, button, true);
    });
  });

  if (overlay) {
    overlay.addEventListener("click", function (event) {
      if (performance.now() < suppressDrawerClickUntil) {
        event.preventDefault();
        return;
      }
      settleNav(false, 0, null, false);
    });
  }

  if (sidebar) {
    sidebar.addEventListener("click", function (event) {
      if (!desktop.matches && event.target.closest("a")) finalizeClosedNav(false);
    });
  }

  function beginDrawerPointer(event, openingFromEdge) {
    if (!sidebar || desktop.matches || !event.isPrimary || event.button !== 0) return;
    if (openingFromEdge && navOpen) return;
    stopNavMotion();
    var start = presentationX();
    sidebar.style.transition = "none";
    setSidebarX(start);
    navGesture = {
      id: event.pointerId,
      capture: event.currentTarget,
      startX: event.clientX,
      startY: event.clientY,
      startOffset: start,
      opening: openingFromEdge,
      mode: "pending",
      resumeOpen: navTargetOpen,
      history: [{ p: start, t: performance.now() }],
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDrawerPointer(event) {
    if (!navGesture || event.pointerId !== navGesture.id) return;
    var dx = event.clientX - navGesture.startX;
    var dy = event.clientY - navGesture.startY;
    if (navGesture.mode === "pending") {
      if (Math.max(Math.abs(dx), Math.abs(dy)) < HYSTERESIS) return;
      if (Math.abs(dy) >= Math.abs(dx)) {
        var resume = navGesture.resumeOpen;
        var opening = navGesture.opening;
        var capture = navGesture.capture;
        navGesture = null;
        if (capture.hasPointerCapture(event.pointerId)) capture.releasePointerCapture(event.pointerId);
        if (!opening) settleNav(resume, 0, null, false);
        else {
          sidebar.style.transition = "";
          sidebar.style.transform = "";
          body.classList.remove("op-nav-open", "op-nav-gesturing");
        }
        return;
      }
      navGesture.mode = "horizontal";
      body.classList.add("op-nav-open", "op-nav-gesturing");
    }
    if (navGesture.mode !== "horizontal") return;
    event.preventDefault();
    var min = closedX();
    var width = Math.max(1, -min);
    var value = navGesture.startOffset + dx;
    if (value > 0) value = rubberband(value, width);
    if (value < min) value = min - rubberband(min - value, width);
    setSidebarX(value);
    var now = performance.now();
    navGesture.history.push({ p: value, t: now });
    navGesture.history = navGesture.history.filter(function (sample) {
      return now - sample.t <= 140;
    });
  }

  function finishDrawerPointer(event, cancelled) {
    if (!navGesture || event.pointerId !== navGesture.id) return;
    var current = navGesture;
    navGesture = null;
    if (current.capture.hasPointerCapture(event.pointerId)) {
      current.capture.releasePointerCapture(event.pointerId);
    }
    if (current.mode !== "horizontal") {
      if (!current.opening) settleNav(current.resumeOpen, 0, null, false);
      else {
        sidebar.style.transition = "";
        sidebar.style.transform = "";
        body.classList.remove("op-nav-open", "op-nav-gesturing");
      }
      return;
    }
    suppressDrawerClickUntil = performance.now() + 350;
    var velocity = cancelled ? 0 : recentVelocity(current.history);
    var value = presentationX();
    var min = closedX();
    var projected = value + projectedDistance(velocity);
    var open = Math.abs(projected) <= Math.abs(projected - min);
    settleNav(open, velocity, current.opening ? (menuButtons[0] || null) : navTrigger, open);
  }

  [edge, sidebar, overlay].filter(Boolean).forEach(function (surface) {
    surface.addEventListener("pointerdown", function (event) {
      beginDrawerPointer(event, surface === edge);
    });
    surface.addEventListener("pointermove", moveDrawerPointer, { passive: false });
    surface.addEventListener("pointerup", function (event) { finishDrawerPointer(event, false); });
    surface.addEventListener("pointercancel", function (event) { finishDrawerPointer(event, true); });
  });

  document.addEventListener("click", function (event) {
    if (performance.now() >= suppressDrawerClickUntil) return;
    if (event.target.closest("#operatorSidebar, [data-op-nav-close], [data-op-nav-edge]")) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  if (desktop.addEventListener) desktop.addEventListener("change", syncDesktopNav);
  else desktop.addListener(syncDesktopNav);
  syncDesktopNav();

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !desktop.matches && body.classList.contains("op-nav-open")) {
      event.preventDefault();
      settleNav(false, 0, null, false);
      return;
    }

    if (event.key === "Tab" && !desktop.matches && navOpen) {
      var items = focusables(sidebar);
      if (!items.length) {
        event.preventDefault();
        sidebar.focus({ preventScroll: true });
        return;
      }
      var first = items[0];
      var last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    // Быстрый переход в поиск: "/" вне поля ввода.
    if (event.key === "/" && !navOpen && !event.ctrlKey && !event.metaKey && !event.altKey) {
      var target = event.target;
      var editing = target && (
        target.matches("input, textarea, select") ||
        target.isContentEditable
      );
      if (!editing) {
        var search = document.querySelector("[data-op-search]");
        if (search) {
          event.preventDefault();
          search.focus();
          search.select();
        }
      }
    }
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    var submitter = event.submitter;
    // data-confirm обрабатывает общий confirm.js в capture-фазе. Сюда
    // подтверждённая форма приходит повторно с исходной submit-кнопкой.
    // Даём понятную обратную связь, но не блокируем повторно формы, для которых
    // браузер остановил отправку из-за HTML-валидации.
    window.setTimeout(function () {
      if (!form.checkValidity()) return;
      var button = submitter || form.querySelector('button[type="submit"], button:not([type])');
      if (!button || button.disabled) return;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      form.setAttribute("aria-busy", "true");
      if (!button.dataset.originalText) button.dataset.originalText = button.textContent;
      button.textContent = "Подождите…";
    }, 0);
  });

  document.addEventListener("click", function (event) {
    var close = event.target.closest("[data-flash-close]");
    if (close) {
      var flash = close.closest(".flash");
      if (flash) flash.remove();
    }
  });
})();
