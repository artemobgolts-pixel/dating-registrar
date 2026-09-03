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

  function hydrateImage(image) {
    if (!image || !image.dataset.deferredSrc) return;
    image.src = image.dataset.deferredSrc;
    delete image.dataset.deferredSrc;
  }

  function initDeferredMedia(cleaners) {
    var images = toArray(document.querySelectorAll("#possibilities img[data-deferred-src]"));
    if (!images.length) return;
    if (!("IntersectionObserver" in window)) {
      images.forEach(hydrateImage);
      return;
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        hydrateImage(entry.target);
        observer.unobserve(entry.target);
      });
    }, { rootMargin: "360px 0px" });
    images.forEach(function (image) { observer.observe(image); });
    cleaners.push(function () { observer.disconnect(); });
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
      startY: 0,
      startProgress: 0,
      startedAt: 0,
      pendingGesture: false,
      timelineProgress: 0
    };

    function activeSlides() {
      return slides.filter(function (slide) {
        return slide.dataset.demoSlideSkin === state.skin;
      });
    }

    function hydrateSlides(indexes) {
      activeSlides().forEach(function (slide) {
        if (indexes.indexOf(Number(slide.dataset.slideIndex || 0)) === -1) return;
        hydrateImage(slide.querySelector("img"));
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
      var target = Math.round(state.progress) + direction;
      hydrateSlides([target]);
      settle(target, true);
    }

    function onPointerDown(event) {
      if (event.button !== undefined && event.button !== 0) return;
      if (event.target.closest && event.target.closest("button, a")) return;
      state.pendingGesture = true;
      state.pointerId = event.pointerId;
      state.startX = event.clientX;
      state.startY = event.clientY;
      state.startProgress = state.progress;
      state.startedAt = performance.now();
    }

    function onPointerMove(event) {
      if (event.pointerId !== state.pointerId) return;
      if (state.pendingGesture) {
        var pendingX = event.clientX - state.startX;
        var pendingY = event.clientY - state.startY;
        if (Math.abs(pendingX) < 8 && Math.abs(pendingY) < 8) return;
        if (Math.abs(pendingX) <= Math.abs(pendingY) * 1.15) {
          state.pendingGesture = false;
          state.pointerId = null;
          return;
        }
        takeControl();
        hydrateSlides([0, 1, 2]);
        state.pendingGesture = false;
        state.dragging = true;
        gallery.classList.add("is-dragging");
        if (gallery.setPointerCapture) gallery.setPointerCapture(event.pointerId);
      }
      if (!state.dragging) return;
      var width = Math.max(1, gallery.getBoundingClientRect().width);
      var maximum = Math.max(0, activeSlides().length - 1);
      var raw = state.startProgress - ((event.clientX - state.startX) / width);
      state.progress = clamp(raw, 0, maximum);
      render(false);
    }

    function finishPointer(event) {
      if (event.pointerId !== state.pointerId) return;
      if (state.pendingGesture) {
        state.pendingGesture = false;
        state.pointerId = null;
        return;
      }
      if (!state.dragging) return;
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
    hydrateSlides([0, 1]);

    return {
      setAutoProgress: function (progress) {
        state.timelineProgress = clamp(progress, 0, 1);
        if (state.userControlled || state.dragging) return;
        state.progress = state.timelineProgress;
        render(false);
      },
      releaseToTimeline: function (progress) {
        stopSettling();
        state.timelineProgress = clamp(progress, 0, 1);
        state.progress = state.timelineProgress;
        state.userControlled = false;
        state.dragging = false;
        state.pendingGesture = false;
        state.pointerId = null;
        gallery.classList.remove("is-dragging");
        render(false);
      },
      setSkin: function (skin) {
        stopSettling();
        state.skin = skin === "romantic" ? "romantic" : "friends";
        state.progress = state.timelineProgress;
        state.userControlled = false;
        state.dragging = false;
        state.pendingGesture = false;
        state.pointerId = null;
        gallery.classList.remove("is-dragging");
        hydrateSlides([0, 1]);
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
      if (skin === "romantic") hydrateImage(ownerPhoto);
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
      if (questionInput.value.trim()) {
        questionStatus.textContent = "Вопрос сохранён в демонстрации";
        questionStatus.hidden = false;
        closeQuestion();
        questionInput.value = "";
        questionToggle.focus();
        return;
      }
      questionStatus.textContent = "Напишите вопрос, чтобы отправить его";
      questionStatus.hidden = false;
    }

    listen(voteButton, "click", toggleVote, undefined, cleaners);
    listen(questionToggle, "click", toggleQuestion, undefined, cleaners);
    listen(questionPanel, "submit", submitQuestion, undefined, cleaners);
    listen(document, "d4y:skinchange", function (event) {
      syncSkin(event.detail && event.detail.skin);
    }, undefined, cleaners);

    syncSkin(skin);
  }

  function initProfilePreview(cleaners) {
    toArray(document.querySelectorAll("[data-profile-preview]")).forEach(function (preview) {
      var tabs = toArray(preview.querySelectorAll("[data-profile-tab]"));
      var panels = toArray(preview.querySelectorAll("[data-profile-panel]"));
      if (!tabs.length || tabs.length !== panels.length) return;

      function activate(name, moveFocus) {
        tabs.forEach(function (tab) {
          var active = tab.dataset.profileTab === name;
          tab.classList.toggle("is-active", active);
          tab.setAttribute("aria-selected", active ? "true" : "false");
          tab.tabIndex = active ? 0 : -1;
          if (active && moveFocus) tab.focus();
        });
        panels.forEach(function (panel) {
          var active = panel.dataset.profilePanel === name;
          panel.hidden = !active;
          if (active) toArray(panel.querySelectorAll("img[data-deferred-src]")).forEach(hydrateImage);
        });
      }

      tabs.forEach(function (tab, index) {
        listen(tab, "click", function () {
          activate(tab.dataset.profileTab, false);
        }, undefined, cleaners);
        listen(tab, "keydown", function (event) {
          var nextIndex = null;
          if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
          if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
          if (event.key === "Home") nextIndex = 0;
          if (event.key === "End") nextIndex = tabs.length - 1;
          if (nextIndex === null) return;
          event.preventDefault();
          activate(tabs[nextIndex].dataset.profileTab, true);
        }, undefined, cleaners);
      });
    });
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
    var body = story.querySelector("[data-card-body]");
    var toolbar = story.querySelector(".landing-story__toolbar");
    var narrative = story.querySelector(".landing-story__narrative");
    var autoSwipe = { progress: 0 };
    var galleryController = story._galleryController;
    var sceneNames = steps.map(function (step) { return step.dataset.scene; });
    var interactiveIndex = Math.max(0, sceneNames.indexOf("float"));
    var activeScene = -1;
    var timeline;

    function sceneY(kind) {
      if (window.matchMedia("(max-width: 760px)").matches) return kind === "focus" ? -18 : -16;
      if (window.innerHeight < 720) return kind === "focus" ? 8 : 12;
      return kind === "focus" ? 20 : 30;
    }

    function scales() {
      var stageRect = stage.getBoundingClientRect();
      var pinRect = pin.getBoundingClientRect();
      var naturalWidth = Math.max(1, experience.offsetWidth);
      var naturalHeight = Math.max(1, experience.offsetHeight);
      var stageTop = stageRect.top - pinRect.top;
      var stageBottom = stageTop + stageRect.height;
      var stageCenter = stageTop + (stageRect.height / 2);
      var safeTop = Math.max(stageTop + 6, 6);
      var safeBottom = Math.min(stageBottom - 6, pinRect.height - 8);
      var toolbarRect = toolbar && toolbar.getBoundingClientRect();
      if (toolbarRect) safeTop = Math.max(safeTop, toolbarRect.bottom - pinRect.top + 6);
      var availableWidth = Math.max(1, Math.min(stageRect.width - 8, window.innerWidth - 12));

      function fitAt(offset) {
        var center = stageCenter + offset;
        var availableHeight = Math.max(1, 2 * Math.min(center - safeTop, safeBottom - center));
        return Math.min(1, availableWidth / naturalWidth, availableHeight / naturalHeight);
      }

      var widthFit = Math.min(1, availableWidth / naturalWidth);
      var baseFit = fitAt(sceneY("base"));
      return {
        photo: Math.min(widthFit, 0.82),
        surface: Math.min(widthFit, 0.88),
        essentials: Math.min(widthFit, 0.92),
        details: Math.min(widthFit, 0.95),
        full: baseFit * 0.97,
        focus: fitAt(sceneY("focus"))
      };
    }

    function narrativeStartY() {
      if (!narrative || !window.matchMedia("(max-width: 760px)").matches) return 0;
      var pinRect = pin.getBoundingClientRect();
      var stageRect = stage.getBoundingClientRect();
      var toolbarRect = toolbar && toolbar.getBoundingClientRect();
      var narrativeRect = narrative.getBoundingClientRect();
      var photoScale = scales().photo;
      var photoTop = stageRect.top - pinRect.top
        + (stageRect.height - (gallery.offsetHeight * photoScale)) / 2;
      var switchBottom = toolbarRect ? toolbarRect.bottom - pinRect.top : 120;
      var centeredTop = switchBottom + (photoTop - switchBottom - narrativeRect.height) / 2;
      return Math.max(0, centeredTop - narrative.offsetTop);
    }

    function materialClipForBottom(revealedBottom) {
      var clipY = Math.max(0, Math.min(material.offsetHeight, revealedBottom));
      var radius = Math.min(26, clipY / 2, material.offsetWidth / 2);
      var width = material.offsetWidth;
      var shoulderY = clipY - radius;
      var shallowY = clipY - radius * 0.61732;
      var diagonalY = clipY - radius * 0.29289;
      var deepY = clipY - radius * 0.07612;
      var shallowX = radius * 0.07612;
      var diagonalX = radius * 0.29289;
      var deepX = radius * 0.61732;
      return "polygon(0px 0px, 100% 0px, 100% " + shoulderY + "px, "
        + (width - shallowX) + "px " + shallowY + "px, "
        + (width - diagonalX) + "px " + diagonalY + "px, "
        + (width - deepX) + "px " + deepY + "px, "
        + (width - radius) + "px " + clipY + "px, "
        + radius + "px " + clipY + "px, "
        + deepX + "px " + deepY + "px, "
        + diagonalX + "px " + diagonalY + "px, "
        + shallowX + "px " + shallowY + "px, 0px " + shoulderY + "px)";
    }

    function revealedThrough(nodes, extra) {
      var revealedBottom = gallery.offsetHeight + 44;
      nodes.forEach(function (node) {
        if (!body.contains(node)) return;
        revealedBottom = Math.max(
          revealedBottom,
          body.offsetTop + node.offsetTop + node.offsetHeight + (extra || 0)
        );
      });
      return revealedBottom;
    }

    function clipThrough(nodes, extra) {
      return materialClipForBottom(revealedThrough(nodes, extra));
    }

    function centeredY(revealedBottom, scale, includeOwner) {
      var visualTop = includeOwner ? owner.offsetTop : card.offsetTop;
      var visualBottom = card.offsetTop + revealedBottom;
      var visualCenter = (visualTop + visualBottom) / 2;
      return ((experience.offsetHeight / 2) - visualCenter) * scale;
    }

    function photoClip() {
      return materialClipForBottom(gallery.offsetHeight);
    }

    function setActiveScene(index) {
      index = clamp(index, 0, steps.length - 1);
      if (index === activeScene) return;
      activeScene = index;
      story.dataset.activeScene = sceneNames[index];
      story.classList.toggle("is-card-interactive", index >= interactiveIndex);
      if (index < interactiveIndex) galleryController.releaseToTimeline(autoSwipe.progress);
      steps.forEach(function (step, stepIndex) {
        var active = stepIndex === index;
        step.classList.toggle("is-active", active);
        if (active) step.setAttribute("aria-current", "step");
        else step.removeAttribute("aria-current");
        gsap.set(step, { autoAlpha: active ? 1 : 0, y: active ? 0 : 12 });
      });
    }

    function syncScene(trigger) {
      if (trigger && trigger.direction < 0 && activeScene >= interactiveIndex) {
        galleryController.releaseToTimeline(autoSwipe.progress);
      }
      var index = 0;
      sceneNames.forEach(function (name, sceneIndex) {
        if (timeline.time() + 0.001 >= timeline.labels[name]) index = sceneIndex;
      });
      setActiveScene(index);
    }

    gsap.set(material, { autoAlpha: 0, clipPath: photoClip });
    gsap.set(owner, { autoAlpha: 0, y: 8 });
    gsap.set(essentials, { autoAlpha: 0, y: 9 });
    gsap.set(details, { autoAlpha: 0, y: 9 });
    gsap.set(people, { autoAlpha: 0, y: 9 });
    gsap.set(actions, { autoAlpha: 0, y: 7 });
    gsap.set(controls, { autoAlpha: 0 });
    gsap.set(gallery, { borderRadius: "26px" });
    gsap.set(card, { clipPath: "none" });
    gsap.set(experience, { clearProps: "transform" });
    gsap.set(camera, { force3D: false });
    setActiveScene(0);

    timeline = gsap.timeline({
      defaults: { ease: "none" },
      scrollTrigger: {
        trigger: pin,
        start: "top top",
        end: function () { return "+=" + Math.max(2800, window.innerHeight * 3.9); },
        pin: true,
        pinSpacing: true,
        scrub: true,
        anticipatePin: 1,
        invalidateOnRefresh: true,
        onUpdate: syncScene,
        onLeaveBack: function () {
          galleryController.releaseToTimeline(0);
          setActiveScene(0);
        }
      }
    });

    timeline
      .addLabel("photo", 0)
      .fromTo(narrative,
        { y: narrativeStartY },
        { y: 0, duration: 4.10 }, 0.82)
      .fromTo(steps[0],
        { autoAlpha: 0, y: 12 },
        { autoAlpha: 1, y: 0, duration: 0.82 }, 0)
      .fromTo(gallery,
        { autoAlpha: 0, scale: 0.97 },
        { autoAlpha: 1, scale: 1, duration: 0.82 }, 0)
      .fromTo(camera,
        {
          scale: function () { return scales().photo * 0.96; },
          y: function () {
            var scale = scales().photo * 0.96;
            return centeredY(gallery.offsetHeight, scale, false) + 5;
          }
        },
        {
          scale: function () { return scales().photo; },
          y: function () {
            var scale = scales().photo;
            return centeredY(gallery.offsetHeight, scale, false);
          },
          force3D: false,
          duration: 0.82
        }, 0)

      .to(material, { autoAlpha: 1, duration: 0.50 }, 0.82)
      .to(material, { clipPath: function () { return materialClipForBottom(gallery.offsetHeight + 44); }, duration: 0.82 }, 0.82)
      .to(gallery, { borderRadius: "26px 26px 0px 0px", duration: 0.82 }, 0.82)
      .to(camera, {
        scale: function () { return scales().surface; },
        y: function () {
          var scale = scales().surface;
          return centeredY(gallery.offsetHeight + 44, scale, false);
        },
        force3D: false,
        duration: 0.82
      }, 0.82)

      .addLabel("essentials", 1.66)
      .to(material, { clipPath: function () { return clipThrough(essentials, 12); }, duration: 0.82 }, "essentials")
      .to(camera, {
        scale: function () { return scales().essentials; },
        y: function () {
          var scale = scales().essentials;
          return centeredY(revealedThrough(essentials, 12), scale, true);
        },
        force3D: false,
        duration: 0.82
      }, "essentials")
      .to(owner, { autoAlpha: 1, y: 0, duration: 0.42 }, "essentials+=0.08")
      .to(essentials, { autoAlpha: 1, y: 0, duration: 0.50, stagger: 0.08 }, "essentials+=0.16")

      .addLabel("details", 2.74)
      .to(material, { clipPath: function () { return clipThrough(details, 12); }, duration: 0.82 }, "details")
      .to(camera, {
        scale: function () { return scales().details; },
        y: function () {
          var scale = scales().details;
          return centeredY(revealedThrough(details, 12), scale, true);
        },
        force3D: false,
        duration: 0.82
      }, "details")
      .to(details, { autoAlpha: 1, y: 0, duration: 0.54, stagger: 0.08 }, "details+=0.10")

      .addLabel("people", 3.82)
      .to(material, {
        clipPath: function () { return materialClipForBottom(material.offsetHeight); },
        duration: 0.86
      }, "people")
      .to(camera, {
        scale: function () { return scales().full; },
        y: function () { return sceneY("base"); },
        force3D: false,
        duration: 0.86
      }, "people")
      .to(people, { autoAlpha: 1, y: 0, duration: 0.55 }, "people+=0.08")
      .to(actions, { autoAlpha: 1, y: 0, duration: 0.48 }, "people+=0.30")

      .addLabel("float", 4.92)
      .to(camera, {
        scale: function () { return scales().focus; },
        y: function () { return sceneY("focus"); },
        force3D: false,
        duration: 0.72
      }, "float")
      .to(controls, { autoAlpha: 1, duration: 0.30 }, "float+=0.30")
      .to(autoSwipe, {
        progress: 1,
        duration: 0.82,
        onUpdate: function () { galleryController.setAutoProgress(autoSwipe.progress); }
      }, "float+=0.02")
      .to(autoSwipe, {
        progress: 1,
        duration: 0.72,
        onUpdate: function () { galleryController.setAutoProgress(1); }
      }, "float+=0.84");

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

    if (gsap && ScrollTrigger) {
      gsap.registerPlugin(ScrollTrigger);
      if (window.matchMedia("(max-width: 760px), (pointer: coarse)").matches) {
        ScrollTrigger.config({ ignoreMobileResize: true });
      }
    }

    initDeferredMedia(cleaners);
    initProfilePreview(cleaners);
    var galleryController = createGallery(gallery, gsap, cleaners);
    story._galleryController = galleryController;
    createCardInteractions(story, card, galleryController, cleaners);

    function showStatic() {
      story.classList.add("is-story-ready", "is-card-interactive");
      story.dataset.activeScene = "float";
      card.style.clipPath = "none";
      var material = story.querySelector("[data-card-surface]");
      if (material) material.style.clipPath = "none";
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
      var viewportWidth = window.innerWidth;
      listen(window, "resize", function () {
        if (Math.abs(window.innerWidth - viewportWidth) < 2) return;
        viewportWidth = window.innerWidth;
        window.requestAnimationFrame(function () {
          if (story.isConnected) ScrollTrigger.refresh();
        });
      }, { passive: true }, cleaners);
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
