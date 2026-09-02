(function () {
  "use strict";

  var disposeCurrent = function () {};

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function toArray(value) {
    return Array.prototype.slice.call(value || []);
  }

  function listen(target, type, handler, options, cleaners) {
    if (!target) return;
    target.addEventListener(type, handler, options);
    cleaners.push(function () {
      target.removeEventListener(type, handler, options);
    });
  }

  function createGallery(gallery, gsap, cleaners) {
    var slides = toArray(gallery.querySelectorAll("[data-demo-slide]"));
    var dots = toArray(gallery.querySelectorAll("[data-demo-gallery-dot]"));
    var previous = gallery.querySelector("[data-demo-gallery-prev]");
    var next = gallery.querySelector("[data-demo-gallery-next]");
    var status = gallery.querySelector("[data-demo-gallery-status]");
    var settleTween = null;
    var state = {
      skin: document.documentElement.dataset.skin === "romantic" ? "romantic" : "friends",
      progress: 0,
      userControlled: false,
      dragging: false,
      pointerId: null,
      startX: 0,
      startProgress: 0,
      startedAt: 0
    };

    function activeSlides() {
      return slides.filter(function (slide) {
        return slide.dataset.demoSlideSkin === state.skin;
      });
    }

    function render(announce) {
      var currentSlides = activeSlides();
      var nearest = clamp(Math.round(state.progress), 0, currentSlides.length - 1);

      slides.forEach(function (slide) {
        var activeSkin = slide.dataset.demoSlideSkin === state.skin;
        var index = Number(slide.dataset.slideIndex || 0);
        slide.hidden = !activeSkin;
        slide.setAttribute("aria-hidden", activeSkin && index === nearest ? "false" : "true");
        slide.classList.toggle("is-active", activeSkin && index === nearest);
        if (activeSkin) {
          slide.style.transform = "translate3d(" + ((index - state.progress) * 100) + "%, 0, 0)";
        }
      });

      dots.forEach(function (dot, index) {
        dot.classList.toggle("is-active", index === nearest);
      });
      gallery.dataset.galleryIndex = String(nearest);
      if (previous) previous.disabled = nearest === 0 && !state.dragging;
      if (next) next.disabled = nearest === currentSlides.length - 1 && !state.dragging;
      if (announce && status) status.textContent = "Фото " + (nearest + 1) + " из " + currentSlides.length;
    }

    function stopSettling() {
      if (!settleTween) return;
      settleTween.kill();
      settleTween = null;
    }

    function settle(target, announce) {
      var maximum = Math.max(0, activeSlides().length - 1);
      target = clamp(target, 0, maximum);
      stopSettling();
      if (!gsap) {
        state.progress = target;
        render(announce);
        return;
      }
      settleTween = gsap.to(state, {
        progress: target,
        duration: 0.52,
        ease: "power3.out",
        overwrite: true,
        onUpdate: function () { render(false); },
        onComplete: function () {
          settleTween = null;
          render(announce);
        }
      });
    }

    function takeControl() {
      state.userControlled = true;
      stopSettling();
    }

    function moveBy(direction) {
      takeControl();
      settle(Math.round(state.progress) + direction, true);
    }

    function onPointerDown(event) {
      if (event.button !== undefined && event.button !== 0) return;
      if (event.target.closest && event.target.closest("button")) return;
      takeControl();
      state.dragging = true;
      state.pointerId = event.pointerId;
      state.startX = event.clientX;
      state.startProgress = state.progress;
      state.startedAt = performance.now();
      gallery.classList.add("is-dragging");
      if (gallery.setPointerCapture) gallery.setPointerCapture(event.pointerId);
    }

    function onPointerMove(event) {
      if (!state.dragging || event.pointerId !== state.pointerId) return;
      var width = Math.max(1, gallery.getBoundingClientRect().width);
      var maximum = Math.max(0, activeSlides().length - 1);
      var raw = state.startProgress - ((event.clientX - state.startX) / width);
      state.progress = clamp(raw, 0, maximum);
      render(false);
    }

    function finishPointer(event) {
      if (!state.dragging || event.pointerId !== state.pointerId) return;
      var delta = event.clientX - state.startX;
      var elapsed = Math.max(1, performance.now() - state.startedAt);
      var width = Math.max(1, gallery.getBoundingClientRect().width);
      var fast = Math.abs(delta / elapsed) > 0.35;
      var far = Math.abs(delta) > width * 0.12;
      var target = Math.round(state.progress);
      if (fast || far) target = Math.round(state.startProgress) + (delta < 0 ? 1 : -1);
      state.dragging = false;
      state.pointerId = null;
      gallery.classList.remove("is-dragging");
      settle(target, true);
    }

    function onKeyDown(event) {
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        moveBy(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        moveBy(1);
      }
    }

    listen(gallery, "pointerdown", onPointerDown, undefined, cleaners);
    listen(gallery, "pointermove", onPointerMove, undefined, cleaners);
    listen(gallery, "pointerup", finishPointer, undefined, cleaners);
    listen(gallery, "pointercancel", finishPointer, undefined, cleaners);
    listen(gallery, "keydown", onKeyDown, undefined, cleaners);
    listen(previous, "click", function () { moveBy(-1); }, undefined, cleaners);
    listen(next, "click", function () { moveBy(1); }, undefined, cleaners);

    render(false);

    return {
      setAutoProgress: function (progress) {
        if (state.userControlled || state.dragging) return;
        state.progress = clamp(progress, 0, 1);
        render(false);
      },
      setSkin: function (skin) {
        stopSettling();
        state.skin = skin === "romantic" ? "romantic" : "friends";
        state.progress = 0;
        state.userControlled = false;
        state.dragging = false;
        gallery.classList.remove("is-dragging");
        render(false);
      },
      destroy: stopSettling
    };
  }

  function createCardInteractions(story, card, galleryController, cleaners) {
    var root = document.documentElement;
    var ownerInitial = story.querySelector("[data-demo-owner-initial]");
    var ownerPhoto = story.querySelector("[data-demo-owner-photo]");
    var gallery = story.querySelector("[data-demo-gallery]");
    var countCurrent = story.querySelector("[data-demo-count-current]");
    var countTotal = story.querySelector("[data-demo-count-total]");
    var voteStatus = story.querySelector("[data-demo-vote-status]");
    var progress = story.querySelector("[data-demo-progress]");
    var voter = story.querySelector("[data-demo-voter]");
    var empty = story.querySelector("[data-demo-vote-empty]");
    var voteButton = story.querySelector("[data-demo-vote]");
    var voteButtonLabel = story.querySelector("[data-demo-vote-label]");
    var questionToggle = story.querySelector("[data-demo-question-toggle]");
    var questionPanel = story.querySelector("[data-demo-question-panel]");
    var questionSubmit = story.querySelector("[data-demo-question-submit]");
    var questionStatus = story.querySelector("[data-demo-question-status]");
    var questionInput = story.querySelector("#landing-demo-question-input");
    var voted = { friends: false, romantic: false };
    var skin = root.dataset.skin === "romantic" ? "romantic" : "friends";

    function pluralizedWaiting(remaining) {
      if (remaining <= 0) return "Все места выбраны";
      if (remaining === 1) return "Ждём ещё одного";
      return "Ждём ещё " + remaining + " человек";
    }

    function numberFromCard(name, fallback) {
      var value = Number(card.dataset[name]);
      return Number.isFinite(value) ? value : fallback;
    }

    function updateVote() {
      var suffix = skin === "romantic" ? "Romantic" : "Friends";
      var before = numberFromCard("voteBefore" + suffix, 0);
      var after = numberFromCard("voteAfter" + suffix, before + 1);
      var total = numberFromCard("voteTotal" + suffix, Math.max(after, 1));
      var current = voted[skin] ? after : before;
      var remaining = Math.max(0, total - current);

      countCurrent.textContent = String(current);
      countTotal.textContent = String(total);
      progress.style.setProperty("--demo-vote-progress", String(current / total));
      progress.setAttribute("aria-valuemax", String(total));
      progress.setAttribute("aria-valuenow", String(current));
      progress.setAttribute("aria-valuetext", current + " из " + total + " участников");
      voteButton.setAttribute("aria-pressed", voted[skin] ? "true" : "false");
      voteButton.classList.toggle("is-selected", voted[skin]);
      voteButtonLabel.textContent = voted[skin] ? "Выбрано" : "Выбрать";
      voter.hidden = !voted[skin];
      empty.hidden = skin !== "romantic" || current !== 0;

      if (skin === "romantic" && !voted[skin]) {
        voteStatus.textContent = "Ваш выбор решает";
      } else if (skin === "romantic" && current === total) {
        voteStatus.textContent = "Место выбрано";
      } else {
        voteStatus.textContent = pluralizedWaiting(remaining);
      }
    }

    function closeQuestion() {
      questionPanel.hidden = true;
      questionToggle.setAttribute("aria-expanded", "false");
    }

    function syncSkin(nextSkin) {
      skin = nextSkin === "romantic" ? "romantic" : "friends";
      card.dataset.demoSkin = skin;

      toArray(story.querySelectorAll("[data-friends-text][data-romantic-text]")).forEach(function (node) {
        node.textContent = skin === "romantic" ? node.dataset.romanticText : node.dataset.friendsText;
      });
      toArray(story.querySelectorAll("[data-friends-href][data-romantic-href]")).forEach(function (node) {
        node.setAttribute("href", skin === "romantic" ? node.dataset.romanticHref : node.dataset.friendsHref);
      });
      toArray(story.querySelectorAll("[data-friends-datetime][data-romantic-datetime]")).forEach(function (node) {
        node.setAttribute("datetime", skin === "romantic" ? node.dataset.romanticDatetime : node.dataset.friendsDatetime);
      });
      toArray(story.querySelectorAll("[data-participant-skin]")).forEach(function (node) {
        node.hidden = node.dataset.participantSkin !== skin;
      });

      ownerInitial.hidden = skin === "romantic";
      ownerPhoto.hidden = skin !== "romantic";
      gallery.setAttribute("aria-label", skin === "romantic"
        ? "Фотографии события «Кинопоказ на крыше»"
        : "Фотографии события «Антикафе на Невском»");
      galleryController.setSkin(skin);
      closeQuestion();
      updateVote();
    }

    function toggleVote() {
      voted[skin] = !voted[skin];
      updateVote();
    }

    function toggleQuestion() {
      var opening = questionPanel.hidden;
      questionPanel.hidden = !opening;
      questionToggle.setAttribute("aria-expanded", opening ? "true" : "false");
      if (opening) {
        questionStatus.hidden = true;
        window.requestAnimationFrame(function () { questionInput.focus(); });
      }
    }

    function submitQuestion(event) {
      if (event) event.preventDefault();
      questionStatus.textContent = questionInput.value.trim()
        ? "Вопрос сохранён в демонстрации"
        : "Напишите вопрос, чтобы отправить его";
      questionStatus.hidden = false;
    }

    listen(voteButton, "click", toggleVote, undefined, cleaners);
    listen(questionToggle, "click", toggleQuestion, undefined, cleaners);
    listen(questionSubmit, "click", submitQuestion, undefined, cleaners);
    listen(questionPanel, "submit", submitQuestion, undefined, cleaners);
    listen(document, "d4y:skinchange", function (event) {
      syncSkin(event.detail && event.detail.skin);
    }, undefined, cleaners);

    syncSkin(skin);
  }

  function buildStoryTimeline(story, pin, stage, camera, experience, card, gallery, gsap) {
    var material = story.querySelector("[data-card-surface]");
    var owner = story.querySelector("[data-card-owner]");
    var essentials = toArray(story.querySelectorAll("[data-card-essentials]"));
    var details = toArray(story.querySelectorAll("[data-card-details]"));
    var people = toArray(story.querySelectorAll("[data-card-people]"));
    var actions = toArray(story.querySelectorAll("[data-card-actions]"));
    var controls = toArray(story.querySelectorAll(
      "[data-demo-gallery-prev], [data-demo-gallery-next], .landing-demo-card__gallery-dots"
    ));
    var steps = toArray(story.querySelectorAll("[data-story-step]"));
    var counter = story.querySelector("[data-story-counter]");
    var body = story.querySelector("[data-card-body]");
    var autoSwipe = { progress: 0 };
    var galleryController = story._galleryController;
    var sceneNames = steps.map(function (step) { return step.dataset.scene; });
    var activeScene = -1;

    function sceneY(kind) {
      var mobile = window.matchMedia("(max-width: 760px)").matches;
      if (mobile) return kind === "focus" ? -58 : -48;
      if (window.innerHeight < 720) return kind === "focus" ? 4 : 8;
      return kind === "focus" ? 20 : 34;
    }

    function scales() {
      var stageRect = stage.getBoundingClientRect();
      var pinRect = pin.getBoundingClientRect();
      var toolbar = story.querySelector(".landing-story__toolbar");
      var narrative = story.querySelector(".landing-story__narrative");
      var naturalWidth = Math.max(1, experience.offsetWidth);
      var naturalHeight = Math.max(1, experience.offsetHeight);
      var mobile = window.matchMedia("(max-width: 760px)").matches;
      var stageTop = stageRect.top - pinRect.top;
      var stageBottom = stageTop + stageRect.height;
      var stageCenter = stageTop + (stageRect.height / 2);
      var safeTop = Math.max(stageTop + 8, 8);
      var safeBottom = Math.min(stageBottom - 8, window.innerHeight - 12);
      var largestSceneOffset = Math.max(
        Math.abs(sceneY("base")),
        Math.abs(sceneY("focus"))
      );
      var toolbarRect = toolbar && toolbar.getBoundingClientRect();
      var narrativeRect = narrative && narrative.getBoundingClientRect();
      safeTop = toolbarRect
        ? Math.max(safeTop, toolbarRect.bottom - pinRect.top + 10)
        : safeTop;
      safeBottom = mobile && narrativeRect
        ? Math.min(safeBottom, narrativeRect.top - pinRect.top - 10)
        : safeBottom;
      var safeHalfHeight = Math.max(1, Math.min(stageCenter - safeTop, safeBottom - stageCenter));
      var availableWidth = Math.max(1, Math.min(stageRect.width - 16, window.innerWidth - 20));
      var availableHeight = Math.max(1, (safeHalfHeight - largestSceneOffset) * 2);
      var fit = Math.min(availableWidth / naturalWidth, availableHeight / naturalHeight);
      fit = Math.min(fit * 0.98, mobile ? 1 : 1.14);
      return {
        photo: fit * 0.73,
        surface: fit * 0.84,
        assembled: fit * 0.90,
        focus: fit,
        returned: fit * 0.91
      };
    }

    function clipForBody(visibleBodyHeight) {
      var hiddenHeight = Math.max(
        0,
        card.offsetHeight - gallery.offsetHeight - visibleBodyHeight
      );
      return "inset(0px 0px " + hiddenHeight
        + "px 0px round 26px 26px 26px 26px)";
    }

    function photoClip() {
      return clipForBody(0);
    }

    function setActiveScene(index) {
      index = clamp(index, 0, steps.length - 1);
      if (index === activeScene) return;
      activeScene = index;
      story.dataset.activeScene = sceneNames[index];
      story.classList.toggle("is-card-interactive", index >= sceneNames.indexOf("interactive"));
      steps.forEach(function (step, stepIndex) {
        var active = stepIndex === index;
        step.classList.toggle("is-active", active);
        if (active) step.setAttribute("aria-current", "step");
        else step.removeAttribute("aria-current");
        gsap.set(step, { autoAlpha: active ? 1 : 0, y: active ? 0 : 14 });
      });
      if (counter) counter.textContent = String(index + 1).padStart(2, "0");
    }

    gsap.set(material, { autoAlpha: 0 });
    gsap.set(owner, { autoAlpha: 0, y: 8 });
    gsap.set(essentials, { autoAlpha: 0, y: 10 });
    gsap.set(details, { autoAlpha: 0, y: 10 });
    gsap.set(people, { autoAlpha: 0, y: 10 });
    gsap.set(actions, { autoAlpha: 0, y: 8 });
    gsap.set(controls, { autoAlpha: 0 });
    gsap.set(card, { clipPath: photoClip });
    gsap.set(experience, { rotationX: 0, rotationY: 0, rotationZ: 0, transformPerspective: 1500 });
    setActiveScene(0);

    var timeline = gsap.timeline({
      defaults: { ease: "power2.inOut" },
      scrollTrigger: {
        trigger: pin,
        start: "top top",
        end: function () { return "+=" + Math.max(6200, window.innerHeight * 8.6); },
        pin: true,
        pinSpacing: true,
        scrub: 0.62,
        anticipatePin: 1,
        invalidateOnRefresh: true,
        onUpdate: function () {
          var index = 0;
          sceneNames.forEach(function (name, sceneIndex) {
            if (timeline.time() + 0.001 >= timeline.labels[name]) index = sceneIndex;
          });
          setActiveScene(index);
        }
      }
    });

    timeline
      .addLabel("photo", 0)
      .fromTo(gallery,
        { autoAlpha: 0, scale: 0.94 },
        { autoAlpha: 1, scale: 1, duration: 1.05, ease: "power3.out" }, 0)
      .fromTo(camera,
        { scale: function () { return scales().photo * 0.94; }, y: function () { return sceneY("base") + 10; } },
        { scale: function () { return scales().photo; }, y: function () { return sceneY("base"); }, duration: 1.05, ease: "power3.out" }, 0)

      .addLabel("surface")
      .to(material, { autoAlpha: 1, duration: 0.7 }, "surface")
      .to(card, { clipPath: function () { return clipForBody(48); }, duration: 1.18, ease: "power3.inOut" }, "surface+=0.08")
      .to(camera, { scale: function () { return scales().surface; }, duration: 1.35, ease: "power3.inOut" }, "surface+=0.08")
      .to(body, { duration: 0.28 }, "surface+=1.18")

      .addLabel("essentials")
      .to(card, { clipPath: function () { return clipForBody(142); }, duration: 0.94, ease: "power3.inOut" }, "essentials")
      .to(camera, { scale: function () { return scales().assembled; }, duration: 1.05 }, "essentials")
      .to(experience, { rotationX: 1.2, rotationY: -1.7, y: -2, duration: 1.05 }, "essentials")
      .to(owner, { autoAlpha: 1, y: 0, duration: 0.48, ease: "power2.out" }, "essentials+=0.12")
      .to(essentials, { autoAlpha: 1, y: 0, duration: 0.56, stagger: 0.10, ease: "power2.out" }, "essentials+=0.24")

      .addLabel("details")
      .to(card, { clipPath: function () { return clipForBody(232); }, duration: 0.88, ease: "power3.inOut" }, "details")
      .to(details, { autoAlpha: 1, y: 0, duration: 0.62, stagger: 0.10, ease: "power2.out" }, "details+=0.08")
      .to(experience, { rotationX: 0.6, rotationY: -0.8, y: 0, duration: 0.85 }, "details")

      .addLabel("people")
      .to(card, { clipPath: "inset(0px 0px 0px 0px round 26px 26px 26px 26px)", duration: 0.92, ease: "power3.inOut" }, "people")
      .to(people, { autoAlpha: 1, y: 0, duration: 0.68, ease: "power2.out" }, "people+=0.08")
      .to(actions, { autoAlpha: 1, y: 0, duration: 0.58, ease: "power2.out" }, "people+=0.36")

      .addLabel("float")
      .to(experience, { rotationX: 1.15, rotationY: -1.55, rotationZ: 0.12, y: -4, duration: 0.78, ease: "sine.inOut" }, "float")
      .to(experience, { rotationX: -0.65, rotationY: 1.05, rotationZ: -0.10, y: 3, duration: 0.92, ease: "sine.inOut" })
      .to(experience, { rotationX: 0.25, rotationY: -0.45, rotationZ: 0, y: 0, duration: 0.68, ease: "sine.inOut" })

      .addLabel("focus")
      .to(camera, { scale: function () { return scales().focus; }, y: function () { return sceneY("focus"); }, duration: 1.25, ease: "power3.inOut" }, "focus")
      .to(experience, { rotationX: 0, rotationY: 0, rotationZ: 0, y: 0, duration: 1.05, ease: "power3.inOut" }, "focus")
      .to(controls, { autoAlpha: 1, duration: 0.42 }, "focus+=0.72")

      .addLabel("swipe")
      .to(autoSwipe, {
        progress: 1,
        duration: 1.35,
        ease: "power2.inOut",
        onUpdate: function () { galleryController.setAutoProgress(autoSwipe.progress); }
      }, "swipe+=0.12")

      .addLabel("interactive")
      .to(camera, { scale: function () { return scales().focus; }, duration: 1.55, ease: "none" }, "interactive")

      .addLabel("return")
      .to(camera, { scale: function () { return scales().returned; }, y: function () { return sceneY("base"); }, duration: 1.35, ease: "power3.inOut" }, "return")
      .to(experience, { rotationX: 0.2, rotationY: -0.35, rotationZ: 0, y: 0, duration: 1.25, ease: "power3.inOut" }, "return");

    return timeline;
  }

  function initLandingStory() {
    var story = document.querySelector("[data-landing-story]");
    if (!story) return function () {};

    var cleaners = [];
    var pin = story.querySelector("[data-story-pin]");
    var stage = story.querySelector("[data-story-stage]");
    var camera = story.querySelector("[data-story-camera]");
    var experience = story.querySelector("[data-demo-experience]");
    var card = story.querySelector("[data-demo-card]");
    var gallery = story.querySelector("[data-demo-gallery]");
    var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    var gsap = window.gsap;
    var ScrollTrigger = window.ScrollTrigger;
    var context = null;
    var timeline = null;

    if (!pin || !stage || !camera || !experience || !card || !gallery) {
      story.classList.add("is-story-ready");
      return function () {};
    }

    if (gsap && ScrollTrigger) gsap.registerPlugin(ScrollTrigger);

    var galleryController = createGallery(gallery, gsap, cleaners);
    story._galleryController = galleryController;
    createCardInteractions(story, card, galleryController, cleaners);

    function showStatic() {
      story.classList.add("is-story-ready", "is-card-interactive");
      story.dataset.activeScene = "interactive";
      card.style.clipPath = "none";
    }

    var staticMode = !gsap || !ScrollTrigger || reducedMotion.matches;
    story.classList.toggle("is-reduced-motion", staticMode);
    if (staticMode) {
      showStatic();
    } else {
      context = gsap.context(function () {
        timeline = buildStoryTimeline(story, pin, stage, camera, experience, card, gallery, gsap);
      }, story);
      story.classList.add("is-story-ready");
      window.requestAnimationFrame(function () { ScrollTrigger.refresh(); });
      if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(function () {
          if (story.isConnected) ScrollTrigger.refresh();
        });
      }
    }

    function onMotionChange() {
      init();
    }
    listen(reducedMotion, "change", onMotionChange, undefined, cleaners);

    return function () {
      cleaners.forEach(function (cleaner) { cleaner(); });
      galleryController.destroy();
      if (timeline && timeline.scrollTrigger) timeline.scrollTrigger.kill();
      if (timeline) timeline.kill();
      if (context) context.revert();
      delete story._galleryController;
    };
  }

  function init() {
    disposeCurrent();
    disposeCurrent = initLandingStory();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
  document.addEventListener("turbo:load", init);
  document.addEventListener("turbo:before-cache", function () { disposeCurrent(); });
})();
