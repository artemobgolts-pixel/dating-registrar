/* Адаптивная загрузка фонового бренд-ролика публичного лендинга. */
(function () {
  "use strict";

  var mobileViewport = window.matchMedia("(max-width: 640px)");
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  var saveData = Boolean(connection && connection.saveData);

  function listen(mediaQuery, handler) {
    if (typeof mediaQuery.addEventListener === "function") {
      mediaQuery.addEventListener("change", handler);
    } else if (typeof mediaQuery.addListener === "function") {
      mediaQuery.addListener(handler);
    }
  }

  function setup() {
    var motion = document.querySelector("[data-brand-motion]");
    var video = motion && motion.querySelector("[data-brand-video]");
    if (!motion || !video || motion.dataset.videoInitialized === "1") return;

    motion.dataset.videoInitialized = "1";
    video.muted = true;
    video.defaultMuted = true;
    var resumeAt = 0;

    function motionDisabled() {
      return reducedMotion.matches || saveData;
    }

    function selectedSource() {
      return mobileViewport.matches
        ? video.dataset.srcMobile
        : video.dataset.srcDesktop;
    }

    function tryToPlay() {
      if (!video.isConnected || motionDisabled() || document.hidden) return;
      var attempt = video.play();
      if (attempt && typeof attempt.catch === "function") {
        attempt.catch(function () {
          motion.classList.remove("is-video-ready");
        });
      }
    }

    function loadSelectedSource() {
      if (!video.isConnected) return;
      if (motionDisabled()) {
        motion.classList.add("is-motion-static");
        motion.classList.remove("is-video-ready");
        video.pause();
        return;
      }

      motion.classList.remove("is-motion-static", "has-video-error");
      var source = selectedSource();
      if (!source) return;
      if (video.dataset.activeSource === source) {
        tryToPlay();
        return;
      }

      resumeAt = Number.isFinite(video.currentTime) ? video.currentTime : 0;
      motion.classList.remove("is-video-ready");
      video.dataset.activeSource = source;
      video.src = source;
      video.load();
    }

    video.addEventListener("loadedmetadata", function () {
      if (resumeAt > 0 && Number.isFinite(video.duration)) {
        video.currentTime = Math.min(resumeAt, Math.max(0, video.duration - 0.05));
      }
      resumeAt = 0;
      tryToPlay();
    });
    video.addEventListener("canplay", tryToPlay);
    video.addEventListener("playing", function () {
      motion.classList.add("is-video-ready");
      motion.classList.remove("has-video-error");
    });
    video.addEventListener("error", function () {
      motion.classList.add("has-video-error");
      motion.classList.remove("is-video-ready");
    });

    listen(mobileViewport, loadSelectedSource);
    listen(reducedMotion, loadSelectedSource);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) video.pause();
      else tryToPlay();
    });
    document.addEventListener("turbo:before-cache", function () {
      video.pause();
      motion.classList.remove("is-video-ready");
      motion.removeAttribute("data-video-initialized");
    });
    document.addEventListener("turbo:before-render", function () {
      video.pause();
    });

    loadSelectedSource();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup, { once: true });
  } else {
    setup();
  }
  document.addEventListener("turbo:load", setup);
})();
