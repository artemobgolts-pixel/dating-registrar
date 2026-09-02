/* Интерактивная история лендинга date4you.
 *
 * Скролл только сообщает CSS прогресс и активную сцену: геометрию, свет и
 * движение карточки описывает stylesheet. Ручные действия внутри карточки
 * принадлежат пользователю и не откатываются последующей прокруткой.
 */
(function () {
  "use strict";

  if (window.__d4yLandingStoryInstalled) return;
  window.__d4yLandingStoryInstalled = true;

  var controller = null;
  var requestFrame = window.requestAnimationFrame
    ? window.requestAnimationFrame.bind(window)
    : function (callback) { return window.setTimeout(callback, 16); };
  var cancelFrame = window.cancelAnimationFrame
    ? window.cancelAnimationFrame.bind(window)
    : window.clearTimeout.bind(window);

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function number(value, fallback) {
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function cssNumber(value) {
    return String(Math.round(value * 10000) / 10000);
  }

  function smoothstep(start, end, value) {
    var progress = clamp((value - start) / Math.max(0.0001, end - start), 0, 1);
    return progress * progress * (3 - 2 * progress);
  }

  function list(root, selector) {
    return Array.prototype.slice.call(root.querySelectorAll(selector));
  }

  function initStory() {
    var story = document.querySelector("[data-landing-story]");
    if (!story) return null;

    var stage = story.querySelector("[data-story-stage]");
    var steps = list(story, "[data-story-step][data-scene]");
    // Старый черновой шаблон мог иметь только data-story-step. Деградация
    // остаётся рабочей, но новый контракт всегда использует data-scene.
    if (!steps.length) steps = list(story, "[data-story-step]");
    var cards = list(story, "[data-demo-card]");
    var destroyed = false;
    var frame = 0;
    var focusFrames = [];
    var storyNearViewport = true;
    var activeSceneIndex = -1;
    var cleanup = [];
    var observers = [];
    var galleries = [];
    var gallerySceneIndex = 0;
    var reducedQuery = window.matchMedia
      ? window.matchMedia("(prefers-reduced-motion: reduce)")
      : { matches: false };

    function listen(target, type, handler, options) {
      if (!target || !target.addEventListener) return;
      target.addEventListener(type, handler, options);
      cleanup.push(function () {
        target.removeEventListener(type, handler, options);
      });
    }

    function schedule(force) {
      if (destroyed || frame || (!storyNearViewport && !force)) return;
      frame = requestFrame(updateStoryFrame);
    }

    function sceneName(index) {
      var step = steps[index];
      if (!step) return "final";
      return step.getAttribute("data-scene")
        || step.getAttribute("data-story-step")
        || String(index);
    }

    function setActiveScene(index, sceneProgress, reduced, stepProgresses) {
      if (!steps.length) {
        story.style.setProperty("--scene-progress", cssNumber(sceneProgress));
        if (stage) stage.style.setProperty("--scene-progress", cssNumber(sceneProgress));
        return;
      }

      index = clamp(index, 0, steps.length - 1);
      var name = sceneName(index);
      if (activeSceneIndex !== index
          || story.getAttribute("data-active-scene") !== name) {
        activeSceneIndex = index;
        story.setAttribute("data-active-scene", name);
        if (stage) stage.setAttribute("data-active-scene", name);
      }

      story.style.setProperty("--scene-progress", cssNumber(sceneProgress));
      story.style.setProperty("--story-scene-index", String(index));
      story.style.setProperty("--story-scene-count", String(steps.length));
      if (stage) {
        stage.style.setProperty("--scene-progress", cssNumber(sceneProgress));
        stage.style.setProperty("--story-scene-index", String(index));
        stage.style.setProperty("--story-scene-count", String(steps.length));
      }

      steps.forEach(function (step, stepIndex) {
        var stepProgress = reduced ? 1 : stepProgresses[stepIndex];
        step.style.setProperty("--step-progress", cssNumber(stepProgress));
        step.classList.toggle("is-active", stepIndex === index);
        step.classList.toggle("is-past", stepIndex < index);
        step.classList.toggle("is-future", stepIndex > index);
        if (stepIndex === index) step.setAttribute("aria-current", "step");
        else step.removeAttribute("aria-current");
      });
    }

    function autoGalleryProgress(index, sceneProgress, reduced) {
      if (reduced) return 1;
      var relative = index - gallerySceneIndex + sceneProgress;
      return smoothstep(0.18, 0.78, relative);
    }

    function updateStoryFrame() {
      frame = 0;
      if (destroyed || !story.isConnected) return;

      var reduced = Boolean(reducedQuery.matches);
      story.classList.toggle("is-reduced-motion", reduced);
      if (stage) stage.classList.toggle("is-reduced-motion", reduced);

      if (reduced) {
        story.style.setProperty("--story-progress", "1");
        if (stage) stage.style.setProperty("--story-progress", "1");
        setActiveScene(Math.max(0, steps.length - 1), 1, true, []);
        galleries.forEach(function (gallery) { gallery.setAutoProgress(1); });
        return;
      }

      var viewportHeight = Math.max(1, window.innerHeight);
      var storyRect = story.getBoundingClientRect();
      var travel = Math.max(1, storyRect.height - viewportHeight);
      var storyProgress = clamp(-storyRect.top / travel, 0, 1);
      var index = 0;
      var sceneProgress = storyProgress;
      var stepProgresses = [];

      if (steps.length) {
        var activationLine = viewportHeight * 0.58;
        var revealStart = viewportHeight * 0.88;
        var revealEnd = viewportHeight * 0.18;
        var rects = steps.map(function (step) {
          return step.getBoundingClientRect();
        });
        stepProgresses = rects.map(function (rect) {
          return clamp(
            (revealStart - rect.top)
              / Math.max(1, rect.height + revealStart - revealEnd),
            0,
            1
          );
        });
        for (var i = 0; i < rects.length; i += 1) {
          if (rects[i].top <= activationLine) index = i;
          else break;
        }

        var currentTop = rects[index].top;
        var nextTop = index + 1 < rects.length
          ? rects[index + 1].top
          : currentTop + Math.max(rects[index].height, viewportHeight * 0.65);
        sceneProgress = clamp(
          (activationLine - currentTop) / Math.max(1, nextTop - currentTop),
          0,
          1
        );
      }

      story.style.setProperty("--story-progress", cssNumber(storyProgress));
      if (stage) stage.style.setProperty("--story-progress", cssNumber(storyProgress));
      setActiveScene(index, sceneProgress, false, stepProgresses);

      var galleryProgress = autoGalleryProgress(index, sceneProgress, false);
      galleries.forEach(function (gallery) {
        gallery.setAutoProgress(galleryProgress);
      });
    }

    function createGallery(gallery) {
      var slides = list(gallery, "[data-demo-slide]");
      if (!slides.length) return null;

      var previous = gallery.querySelector("[data-demo-gallery-prev]");
      var next = gallery.querySelector("[data-demo-gallery-next]");
      var dots = list(gallery, "[data-demo-gallery-dot]");
      var initialIndex = clamp(
        Math.round(number(gallery.getAttribute("data-gallery-index"), 0)),
        0,
        slides.length - 1
      );
      var state = {
        index: initialIndex,
        progress: initialIndex,
        userControlled: false,
        gesture: null,
        suppressClickUntil: 0
      };

      if (!gallery.hasAttribute("role")) gallery.setAttribute("role", "region");
      if (!gallery.hasAttribute("aria-roledescription")) {
        gallery.setAttribute("aria-roledescription", "карусель");
      }
      if (!gallery.hasAttribute("aria-label")) {
        gallery.setAttribute("aria-label", "Фотографии события");
      }
      if (!gallery.hasAttribute("tabindex")) gallery.setAttribute("tabindex", "0");
      gallery.style.setProperty("--gallery-slide-count", String(slides.length));

      function updateSlides(index) {
        state.index = clamp(Math.round(index), 0, slides.length - 1);
        gallery.setAttribute("data-gallery-index", String(state.index));
        gallery.style.setProperty("--gallery-index", String(state.index));
        slides.forEach(function (slide, slideIndex) {
          var active = slideIndex === state.index;
          slide.classList.toggle("is-active", active);
          slide.classList.toggle("is-before", slideIndex < state.index);
          slide.classList.toggle("is-after", slideIndex > state.index);
          slide.setAttribute("aria-hidden", active ? "false" : "true");
        });
        dots.forEach(function (dot, dotIndex) {
          var representedIndex = clamp(Math.round(number(
            dot.getAttribute("data-gallery-dot-index"), dotIndex
          )), 0, slides.length - 1);
          var active = representedIndex === state.index;
          dot.classList.toggle("is-active", active);
          dot.classList.toggle("on", active);
          if (active) dot.setAttribute("aria-current", "true");
          else dot.removeAttribute("aria-current");
        });
        if (previous) previous.disabled = state.index === 0;
        if (next) next.disabled = state.index === slides.length - 1;
      }

      function setProgress(progress, drag) {
        state.progress = clamp(progress, 0, slides.length - 1);
        gallery.style.setProperty("--gallery-progress", cssNumber(state.progress));
        gallery.style.setProperty(
          "--gallery-translate",
          cssNumber(-state.progress * 100) + "%"
        );
        gallery.style.setProperty("--gallery-drag", cssNumber(drag || 0));
        var visibleIndex = clamp(Math.round(state.progress), 0, slides.length - 1);
        if (visibleIndex !== state.index) updateSlides(visibleIndex);
      }

      function takeControl() {
        if (state.userControlled) return;
        state.userControlled = true;
        gallery.classList.add("is-user-controlled");
      }

      function setIndex(index, userControlled) {
        if (userControlled) takeControl();
        index = clamp(Math.round(index), 0, slides.length - 1);
        setProgress(index, 0);
        updateSlides(index);
      }

      function setAutoProgress(progress) {
        if (state.userControlled || state.gesture || slides.length < 2) return;
        // Scroll-сцена намеренно показывает только один красивый переход:
        // остальные фотографии остаются для самостоятельного просмотра.
        var target = clamp(progress, 0, 1) * Math.min(1, slides.length - 1);
        setProgress(target, 0);
      }

      function move(delta) {
        setIndex(state.index + delta, true);
      }

      function onPrevious(event) {
        event.stopPropagation();
        move(-1);
      }

      function onNext(event) {
        event.stopPropagation();
        move(1);
      }

      function onKeyDown(event) {
        if (event.defaultPrevented) return;
        var target = event.target;
        if (target && target !== gallery
            && target.matches("input, textarea, select, [contenteditable='true']")) return;
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          move(-1);
        } else if (event.key === "ArrowRight") {
          event.preventDefault();
          move(1);
        } else if (event.key === "Home") {
          event.preventDefault();
          setIndex(0, true);
        } else if (event.key === "End") {
          event.preventDefault();
          setIndex(slides.length - 1, true);
        }
      }

      function pointerTime(event) {
        return Number.isFinite(event.timeStamp) ? event.timeStamp : performance.now();
      }

      function onPointerDown(event) {
        if (slides.length < 2 || event.isPrimary === false) return;
        if (event.pointerType === "mouse" && event.button !== 0) return;
        if (event.target.closest
            && event.target.closest("button, a, input, textarea, select, [contenteditable='true']")) {
          return;
        }

        var time = pointerTime(event);
        state.gesture = {
          id: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          startProgress: state.progress,
          mode: "pending",
          samples: [{ x: event.clientX, time: time }]
        };
        try { gallery.setPointerCapture(event.pointerId); } catch (_) {}
      }

      function onPointerMove(event) {
        var gesture = state.gesture;
        if (!gesture || event.pointerId !== gesture.id) return;
        var deltaX = event.clientX - gesture.startX;
        var deltaY = event.clientY - gesture.startY;

        if (gesture.mode === "pending") {
          if (Math.max(Math.abs(deltaX), Math.abs(deltaY)) < 8) return;
          if (Math.abs(deltaY) > Math.abs(deltaX)) {
            gesture.mode = "vertical";
            try { gallery.releasePointerCapture(event.pointerId); } catch (_) {}
            return;
          }
          gesture.mode = "horizontal";
          takeControl();
          gallery.classList.add("is-dragging");
        }
        if (gesture.mode !== "horizontal") return;

        event.preventDefault();
        var width = Math.max(1, gallery.clientWidth);
        var progress = gesture.startProgress - deltaX / width;
        // Небольшая упругая граница оставляет движение физичным, но итоговый
        // snap всё равно всегда возвращается к существующему слайду.
        if (progress < 0) progress = -Math.sqrt(-progress) * 0.12;
        if (progress > slides.length - 1) {
          progress = slides.length - 1
            + Math.sqrt(progress - (slides.length - 1)) * 0.12;
        }
        state.progress = progress;
        gallery.style.setProperty("--gallery-progress", cssNumber(progress));
        gallery.style.setProperty(
          "--gallery-translate",
          cssNumber(-progress * 100) + "%"
        );
        gallery.style.setProperty(
          "--gallery-drag",
          cssNumber(progress - gesture.startProgress)
        );

        var time = pointerTime(event);
        gesture.samples.push({ x: event.clientX, time: time });
        gesture.samples = gesture.samples.filter(function (sample) {
          return time - sample.time <= 140;
        });
      }

      function recentVelocity(samples) {
        if (samples.length < 2) return 0;
        var first = samples[0];
        var last = samples[samples.length - 1];
        return (last.x - first.x) / Math.max(1, last.time - first.time);
      }

      function finishPointer(event, cancelled) {
        var gesture = state.gesture;
        if (!gesture || event.pointerId !== gesture.id) return;
        state.gesture = null;
        try {
          if (gallery.hasPointerCapture(event.pointerId)) {
            gallery.releasePointerCapture(event.pointerId);
          }
        } catch (_) {}
        gallery.classList.remove("is-dragging");

        if (gesture.mode !== "horizontal") {
          gallery.style.setProperty("--gallery-drag", "0");
          return;
        }

        var width = Math.max(1, gallery.clientWidth);
        var velocity = cancelled ? 0 : recentVelocity(gesture.samples);
        var projected = state.progress - (velocity * 190) / width;
        var destination = Math.round(projected);
        var travel = event.clientX - gesture.startX;
        var startIndex = Math.round(gesture.startProgress);
        if (!cancelled && destination === startIndex
            && (Math.abs(travel) > width * 0.12 || Math.abs(velocity) > 0.34)) {
          destination = startIndex + (travel < 0 || velocity < -0.34 ? 1 : -1);
        }
        state.suppressClickUntil = performance.now() + 350;
        setIndex(destination, true);
      }

      function suppressDraggedClick(event) {
        if (performance.now() >= state.suppressClickUntil) return;
        event.preventDefault();
        event.stopPropagation();
      }

      function onDragStart(event) {
        event.preventDefault();
      }

      if (previous) listen(previous, "click", onPrevious);
      if (next) listen(next, "click", onNext);
      dots.forEach(function (dot, dotIndex) {
        if (!dot.matches("button, [role='button']")) return;
        listen(dot, "click", function (event) {
          event.stopPropagation();
          setIndex(number(dot.getAttribute("data-gallery-dot-index"), dotIndex), true);
        });
      });
      listen(gallery, "keydown", onKeyDown);
      listen(gallery, "dragstart", onDragStart);
      listen(gallery, "pointerdown", onPointerDown);
      listen(gallery, "pointermove", onPointerMove, { passive: false });
      listen(gallery, "pointerup", function (event) { finishPointer(event, false); });
      listen(gallery, "pointercancel", function (event) { finishPointer(event, true); });
      listen(gallery, "lostpointercapture", function (event) {
        finishPointer(event, true);
      });
      listen(gallery, "click", suppressDraggedClick, true);

      setProgress(initialIndex, 0);
      updateSlides(initialIndex);
      return { setAutoProgress: setAutoProgress };
    }

    function dataNumber(button, card, attribute, fallback) {
      var value = button.getAttribute(attribute);
      if (value === null && card) value = card.getAttribute(attribute);
      return number(value, fallback);
    }

    function initVote(button) {
      var card = button.closest("[data-demo-card]") || story;
      var skin = card.getAttribute("data-demo-skin") === "romantic"
        ? "romantic" : "friends";
      var defaults = skin === "romantic"
        ? { before: 0, after: 1, total: 1 }
        : { before: 3, after: 4, total: 5 };
      var before = dataNumber(button, card, "data-vote-before", defaults.before);
      var after = dataNumber(button, card, "data-vote-after", defaults.after);
      var total = dataNumber(button, card, "data-vote-total", defaults.total);
      var currentNodes = list(card, "[data-demo-count-current]");
      var countContainers = list(card, "[data-demo-count]");
      var statusNodes = list(card, "[data-demo-vote-status]");
      var progressNodes = list(card, "[data-demo-progress]");
      var voterNodes = list(card, "[data-demo-voter]");
      var labelNodes = list(button, "[data-demo-vote-label]");
      var voted = button.getAttribute("aria-pressed") === "true";

      voterNodes.forEach(function (voter) {
        var voterStatus = voter.querySelector("[data-demo-voter-status]")
          || (voter.matches("[data-demo-voter-status]") ? voter : null);
        voter._d4yPendingStatus = voter.getAttribute("data-pending-label")
          || (voterStatus && voterStatus.textContent.trim())
          || "Ждём ответ";
      });

      statusNodes.forEach(function (status) {
        status.setAttribute("role", "status");
        status.setAttribute("aria-live", "polite");
        status.setAttribute("aria-atomic", "true");
      });

      function customText(attribute, fallback) {
        return button.getAttribute(attribute)
          || card.getAttribute(attribute)
          || fallback;
      }

      function render() {
        var current = voted ? after : before;
        var status = voted
          ? customText(
            "data-status-after",
            current + " из " + total + ". Ваш ответ учтён."
          )
          : customText(
            "data-status-before",
            current + " из " + total + ". Ждём остальные ответы."
          );
        var label = voted
          ? customText("data-label-after", "Я пойду")
          : customText("data-label-before", "Я пойду");

        button.setAttribute("aria-pressed", voted ? "true" : "false");
        button.classList.toggle("is-selected", voted);
        card.classList.toggle("is-voted", voted);
        currentNodes.forEach(function (node) { node.textContent = String(current); });
        countContainers.forEach(function (node) {
          node.setAttribute("aria-label", current + " из " + total + " участников подтвердили участие");
        });
        statusNodes.forEach(function (node) { node.textContent = status; });
        labelNodes.forEach(function (node) { node.textContent = label; });
        progressNodes.forEach(function (node) {
          var valueText = current + " из " + total + " участников подтвердили участие";
          node.setAttribute("role", "progressbar");
          node.setAttribute("aria-valuemin", "0");
          node.setAttribute("aria-valuemax", String(total));
          node.setAttribute("aria-valuenow", String(current));
          node.setAttribute("aria-valuetext", valueText);
          node.style.setProperty(
            "--demo-vote-progress",
            cssNumber(total > 0 ? current / total : 0)
          );
          if (node.tagName === "PROGRESS") {
            node.max = total;
            node.value = current;
          }
        });
        voterNodes.forEach(function (voter) {
          var voterStatus = voter.querySelector("[data-demo-voter-status]")
            || (voter.matches("[data-demo-voter-status]") ? voter : null);
          var voterState = voted ? "confirmed" : "pending";
          voter.setAttribute("data-state", voterState);
          voter.classList.toggle("is-confirmed", voted);
          voter.classList.toggle("is-pending", !voted);
          if (voterStatus) {
            voterStatus.textContent = voted
              ? (voter.getAttribute("data-confirmed-label") || "Участвует")
              : voter._d4yPendingStatus;
          }
        });
      }

      listen(button, "click", function () {
        voted = !voted;
        render();
      });
      render();
    }

    function panelFor(toggle) {
      var id = toggle.getAttribute("aria-controls");
      if (id) {
        var controlled = document.getElementById(id);
        if (controlled && story.contains(controlled)
            && controlled.matches("[data-demo-question-panel]")) return controlled;
      }
      var card = toggle.closest("[data-demo-card]") || story;
      return card.querySelector("[data-demo-question-panel]");
    }

    function initQuestion(toggle) {
      var panel = panelFor(toggle);
      if (!panel) return;
      var card = toggle.closest("[data-demo-card]") || story;
      var open = toggle.getAttribute("aria-expanded") === "true" && !panel.hidden;

      function render(shouldFocus) {
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        panel.hidden = !open;
        card.classList.toggle("is-question-open", open);
        if (!open || !shouldFocus) return;

        var target = panel.querySelector(
          "input:not([disabled]), textarea:not([disabled]), select:not([disabled]), "
            + "button:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])"
        );
        if (!target) {
          if (!panel.hasAttribute("tabindex")) panel.setAttribute("tabindex", "-1");
          target = panel;
        }
        var focusFrame = requestFrame(function () {
          focusFrames = focusFrames.filter(function (id) { return id !== focusFrame; });
          if (!destroyed && open && target.isConnected) {
            try { target.focus({ preventScroll: true }); } catch (_) { target.focus(); }
          }
        });
        focusFrames.push(focusFrame);
      }

      listen(toggle, "click", function () {
        open = !open;
        render(open);
      });
      listen(panel, "keydown", function (event) {
        if (event.key !== "Escape" || !open) return;
        event.preventDefault();
        open = false;
        render(false);
        toggle.focus();
      });
      var questionInput = panel.querySelector("textarea, input[type='text']");
      var questionSubmit = panel.querySelector("[data-demo-question-submit]");
      var questionStatus = panel.querySelector("[data-demo-question-status]");
      if (questionSubmit && questionInput) {
        listen(questionSubmit, "click", function () {
          var value = questionInput.value.trim();
          if (!value) {
            questionInput.setCustomValidity("Напишите вопрос");
            questionInput.reportValidity();
            questionInput.focus();
            return;
          }
          questionInput.setCustomValidity("");
          questionInput.value = "";
          if (questionStatus) {
            questionStatus.hidden = false;
            questionStatus.textContent = "Вопрос добавлен в демонстрацию";
          }
        });
        listen(questionInput, "input", function () {
          questionInput.setCustomValidity("");
          if (questionStatus) questionStatus.hidden = true;
        });
      }
      render(false);
    }

    function appearance() {
      var root = document.documentElement;
      var skin = root.getAttribute("data-skin")
        || (document.body && document.body.getAttribute("data-skin"))
        || "friends";
      var theme = root.getAttribute("data-theme") || "light";
      skin = skin === "romantic" ? "romantic" : "friends";
      theme = theme === "dark" ? "dark" : "light";

      story.setAttribute("data-active-skin", skin);
      story.setAttribute("data-active-theme", theme);
      story.classList.toggle("is-romantic", skin === "romantic");
      story.classList.toggle("is-dark-theme", theme === "dark");
      cards.forEach(function (card) {
        var cardSkin = card.getAttribute("data-demo-skin");
        var active = !cardSkin || cardSkin === skin;
        card.classList.toggle("is-active", active);
        card.setAttribute("aria-hidden", active ? "false" : "true");
        card.hidden = !active;
        if ("inert" in card) card.inert = !active;
      });
      list(story, "[data-skin-status]").forEach(function (status) {
        status.textContent = skin === "romantic"
          ? "Романтическое оформление"
          : "Стандартное оформление";
      });
      schedule(true);
    }

    galleries = list(story, "[data-demo-gallery]")
      .map(createGallery)
      .filter(Boolean);
    list(story, "[data-demo-vote]").forEach(initVote);
    list(story, "[data-demo-question-toggle]").forEach(initQuestion);

    var mediaScene = steps.findIndex(function (step) {
      var value = (step.getAttribute("data-scene")
        || step.getAttribute("data-story-step") || "").toLowerCase();
      return /media|gallery|photo|image/.test(value);
    });
    gallerySceneIndex = mediaScene >= 0 ? mediaScene : Math.min(1, Math.max(0, steps.length - 1));

    listen(window, "scroll", function () { schedule(false); }, { passive: true });
    listen(window, "resize", function () { schedule(true); }, { passive: true });
    listen(document, "d4y:skinchange", appearance);
    listen(document, "d4y:themechange", appearance);

    function onReducedMotionChange() {
      schedule(true);
    }
    if (reducedQuery.addEventListener) {
      listen(reducedQuery, "change", onReducedMotionChange);
    } else if (reducedQuery.addListener) {
      reducedQuery.addListener(onReducedMotionChange);
      cleanup.push(function () { reducedQuery.removeListener(onReducedMotionChange); });
    }

    if ("IntersectionObserver" in window) {
      var storyObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.target !== story) return;
          storyNearViewport = entry.isIntersecting;
          story.classList.toggle("is-story-active", entry.isIntersecting);
          if (stage) stage.classList.toggle("is-story-active", entry.isIntersecting);
          schedule(true);
        });
      }, { rootMargin: "100% 0px 100% 0px", threshold: [0, 0.01] });
      storyObserver.observe(story);
      observers.push(storyObserver);

      if (steps.length) {
        var stepObserver = new IntersectionObserver(function (entries) {
          entries.forEach(function (entry) {
            entry.target.classList.toggle("is-in-viewport", entry.isIntersecting);
          });
        }, { rootMargin: "-20% 0px -20% 0px", threshold: [0, 0.01, 0.5] });
        steps.forEach(function (step) { stepObserver.observe(step); });
        observers.push(stepObserver);
      }
    } else {
      story.classList.add("is-story-active");
      if (stage) stage.classList.add("is-story-active");
    }

    if ("ResizeObserver" in window) {
      var resizeObserver = new ResizeObserver(function () { schedule(true); });
      resizeObserver.observe(story);
      if (stage) resizeObserver.observe(stage);
      steps.forEach(function (step) { resizeObserver.observe(step); });
      observers.push(resizeObserver);
    }

    if ("MutationObserver" in window) {
      var appearanceObserver = new MutationObserver(appearance);
      appearanceObserver.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-skin", "data-theme"]
      });
      if (document.body) {
        appearanceObserver.observe(document.body, {
          attributes: true,
          attributeFilter: ["data-skin"]
        });
      }
      observers.push(appearanceObserver);
    }

    appearance();
    schedule(true);

    return {
      destroy: function () {
        if (destroyed) return;
        destroyed = true;
        if (frame) cancelFrame(frame);
        frame = 0;
        focusFrames.forEach(cancelFrame);
        focusFrames = [];
        observers.forEach(function (observer) { observer.disconnect(); });
        observers = [];
        cleanup.splice(0).forEach(function (remove) { remove(); });
      }
    };
  }

  function boot() {
    if (controller) controller.destroy();
    controller = initStory();
  }

  function suspend() {
    if (!controller) return;
    controller.destroy();
    controller = null;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
  document.addEventListener("turbo:load", boot);
  window.addEventListener("pagehide", suspend);
  window.addEventListener("pageshow", function () {
    if (!controller) boot();
  });
})();
