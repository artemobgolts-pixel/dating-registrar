/* date4you ink controller: policy, progressive OffscreenCanvas and DOM lifecycle. */
(function () {
  "use strict";

  var host = document.querySelector(".bg-smoke");
  if (!host) return;

  var script = document.currentScript ||
    document.querySelector('script[src*="/ink.js"]');
  var assets = script ? script.dataset : {};
  var controller = host.__d4yInkController;
  if (controller) {
    controller.refresh();
    return;
  }

  var reduceQuery = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)");
  var reduceMotion = !!(reduceQuery && reduceQuery.matches);
  var connection = navigator.connection ||
    navigator.mozConnection || navigator.webkitConnection;
  var saveData = !!(connection && connection.saveData);
  var memory = Number(navigator.deviceMemory);
  var cores = Number(navigator.hardwareConcurrency);
  var veryWeak = (memory > 0 && memory <= 1) ||
    (memory > 1 && memory <= 2 && cores > 0 && cores <= 2);
  var deviceStaticPolicy = saveData || veryWeak;
  var staticPolicy = reduceMotion || deviceStaticPolicy;

  var canvas = null;
  var worker = null;
  var renderer = null;
  var backendReady = false;
  var firstFrameReady = false;
  var workerTimer = 0;
  var revealRaf = 0;
  var backendGeneration = 0;
  var failedOver = false;
  var runtimeLoading = false;
  var runtimeWaiters = [];
  var latestRuntimeStats = {};
  var controllerStats = {
    backend: staticPolicy ? "poster" : "pending",
    worker: false,
    pointerFlushes: 0,
    firstFrameReady: false,
  };

  var pendingMove = null;
  var pendingClicks = [];
  var inputRaf = 0;
  var lastPointer = {x: 0.5, y: 0.5};
  var hasPointerSample = false;
  var stateCache = null;
  var posterLoader = null;
  var posterPreloader = null;
  var posterGeneration = 0;
  var pageStopped = false;

  function assetFallback(name) {
    if (!script || !script.src) return "";
    try {
      var url = new URL(script.src, window.location.href);
      url.pathname = url.pathname.replace(/ink\.js$/, name);
      return url.toString();
    } catch (_) {
      return "";
    }
  }

  var runtimeSrc = assets.runtimeSrc || assetFallback("ink-runtime.js");
  var workerSrc = assets.workerSrc || assetFallback("ink-worker.js");

  function darkTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark";
  }
  function friendsSkin() {
    var bodySkin = document.body && document.body.getAttribute("data-skin");
    return (bodySkin || document.documentElement.getAttribute("data-skin")) === "friends";
  }
  function cursorEffects() {
    return document.documentElement.getAttribute("data-ink-interactive") === "1" ||
      (!!document.body && document.body.getAttribute("data-ink-interactive") === "1");
  }
  function phoneLike() {
    if (!window.matchMedia) return Math.min(window.innerWidth, window.innerHeight) < 700;
    return window.matchMedia("(pointer: coarse)").matches ||
      window.matchMedia("(max-width: 700px)").matches;
  }
  function finePointer() {
    return !phoneLike() && (!window.matchMedia ||
      window.matchMedia("(pointer: fine)").matches);
  }
  function state() {
    stateCache = {
      width: Math.max(1, window.innerWidth),
      height: Math.max(1, window.innerHeight),
      dpr: Math.min(window.devicePixelRatio || 1, 2),
      dark: darkTheme(),
      friends: friendsSkin(),
      interactive: cursorEffects(),
      fine: finePointer(),
    };
    return stateCache;
  }
  function eligible() {
    var next = stateCache || state();
    return !staticPolicy && next.interactive && next.fine;
  }

  function stats() {
    var result = {};
    var key;
    for (key in latestRuntimeStats) result[key] = latestRuntimeStats[key];
    for (key in controllerStats) result[key] = controllerStats[key];
    return result;
  }
  window.__inkStats = stats;

  function resetReveal() {
    if (revealRaf) window.cancelAnimationFrame(revealRaf);
    revealRaf = 0;
    firstFrameReady = false;
    controllerStats.firstFrameReady = false;
    host.classList.remove("has-ink");
  }

  function beginBackend() {
    backendGeneration += 1;
    resetReveal();
    return backendGeneration;
  }

  function reveal(expectedCanvas, generation) {
    if (generation !== backendGeneration || expectedCanvas !== canvas) return;
    if (firstFrameReady) return;
    firstFrameReady = true;
    controllerStats.firstFrameReady = true;
    revealRaf = window.requestAnimationFrame(function () {
      revealRaf = 0;
      if (generation === backendGeneration && expectedCanvas === canvas &&
          expectedCanvas.parentNode === host) {
        host.classList.add("has-ink");
      }
    });
  }

  function makeCanvas() {
    var node = document.createElement("canvas");
    node.className = "ink-canvas";
    node.setAttribute("aria-hidden", "true");
    host.appendChild(node);
    return node;
  }

  function removeCanvas() {
    if (canvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
    canvas = null;
  }

  function removePoster() {
    posterGeneration += 1;
    host.querySelectorAll(".ink-static-frame").forEach(function (image) {
      image.onload = null;
      image.onerror = null;
      image.remove();
    });
    posterPreloader = null;
    posterLoader = null;
  }

  function updateRuntimeStats(next) {
    latestRuntimeStats = next || latestRuntimeStats;
  }

  function posterUrl() {
    var skin = friendsSkin() ? "Friends" : "Romantic";
    var theme = darkTheme() ? "Dark" : "Light";
    var key = "static" + skin + theme;
    if (key === "staticFriendsLight" && window.innerHeight > window.innerWidth &&
        assets.staticFriendsLightPortrait) {
      return assets.staticFriendsLightPortrait;
    }
    return assets[key] || assetFallback(
      "ink-static-" + skin.toLowerCase() + "-" + theme.toLowerCase() + ".webp");
  }

  function startPoster() {
    document.documentElement.classList.add("ink-static");
    controllerStats.backend = "poster";
    controllerStats.worker = false;
    host.querySelectorAll("animate, animateTransform").forEach(function (node) {
      node.remove();
    });
    function loadPoster() {
      var url = posterUrl();
      if (!url) return;
      var current = host.querySelector(".ink-static-frame:not(.is-pending)");
      if (current && current.getAttribute("src") === url &&
          current.complete && current.naturalWidth > 0) {
        if (posterPreloader) {
          posterPreloader.onload = null;
          posterPreloader.onerror = null;
          posterPreloader.remove();
          posterPreloader = null;
        }
        if (staticPolicy && current.parentNode === host) host.classList.add("has-ink");
        return;
      }
      if (posterPreloader && posterPreloader.getAttribute("src") === url) return;
      if (posterPreloader) {
        posterPreloader.onload = null;
        posterPreloader.onerror = null;
        posterPreloader.remove();
      }
      var generation = ++posterGeneration;
      var nextImage = document.createElement("img");
      posterPreloader = nextImage;
      nextImage.className = "ink-static-frame is-pending";
      nextImage.alt = "";
      nextImage.setAttribute("aria-hidden", "true");
      nextImage.setAttribute("draggable", "false");
      nextImage.decoding = "async";
      nextImage.onload = function () {
        if (generation !== posterGeneration || !staticPolicy ||
            nextImage.parentNode !== host) return;
        var decoded = nextImage.decode
          ? nextImage.decode().catch(function () {}) : Promise.resolve();
        decoded.then(function () {
          if (generation === posterGeneration && staticPolicy &&
              nextImage.parentNode === host && nextImage.complete &&
              nextImage.naturalWidth > 0) {
            var previous = host.querySelector(
              ".ink-static-frame:not(.is-pending)");
            if (previous && previous !== nextImage) {
              previous.onload = null;
              previous.onerror = null;
              previous.remove();
            }
            nextImage.classList.remove("is-pending");
            posterPreloader = null;
            firstFrameReady = true;
            controllerStats.firstFrameReady = true;
            host.classList.add("has-ink");
          }
        });
      };
      nextImage.onerror = function () {
        if (generation === posterGeneration && staticPolicy &&
            nextImage.parentNode === host) {
          nextImage.remove();
          posterPreloader = null;
          if (!host.querySelector(".ink-static-frame:not(.is-pending)")) {
            host.classList.remove("has-ink");
          }
        }
      };
      host.appendChild(nextImage);
      nextImage.src = url;
    }
    posterLoader = loadPoster;
    loadPoster();
  }

  function ensureRuntime(callback, errorCallback) {
    if (window.D4YInkRuntime && window.D4YInkRuntime.create) {
      callback();
      return;
    }
    runtimeWaiters.push({ready: callback, error: errorCallback});
    if (runtimeLoading) return;
    runtimeLoading = true;
    if (!runtimeSrc) {
      runtimeLoading = false;
      var missing = runtimeWaiters.splice(0);
      for (var m = 0; m < missing.length; m++) {
        if (missing[m].error) missing[m].error("runtime-url-missing");
      }
      return;
    }
    var tag = document.createElement("script");
    tag.src = runtimeSrc;
    tag.async = true;
    tag.onload = function () {
      runtimeLoading = false;
      var waiters = runtimeWaiters.splice(0);
      for (var i = 0; i < waiters.length; i++) waiters[i].ready();
    };
    tag.onerror = function () {
      runtimeLoading = false;
      var waiters = runtimeWaiters.splice(0);
      for (var i = 0; i < waiters.length; i++) {
        if (waiters[i].error) waiters[i].error("runtime-load-error");
      }
    };
    document.head.appendChild(tag);
  }

  function mainFatal(expectedCanvas, generation) {
    if (generation !== backendGeneration || expectedCanvas !== canvas) return;
    backendGeneration += 1;
    resetReveal();
    backendReady = false;
    if (renderer) {
      renderer.destroy();
      renderer = null;
    }
    removeCanvas();
    controllerStats.backend = "css-fallback";
  }

  function startMain(useExistingCanvas) {
    if (!useExistingCanvas || !canvas) {
      removeCanvas();
      canvas = makeCanvas();
    }
    var mainCanvas = canvas;
    var generation = beginBackend();
    controllerStats.backend = "main";
    controllerStats.worker = false;
    ensureRuntime(function () {
      if (staticPolicy || generation !== backendGeneration ||
          mainCanvas !== canvas || !window.D4YInkRuntime) return;
      var created = window.D4YInkRuntime.create(mainCanvas, {
        preserveDrawingBuffer: !!window.__INK_PRESERVE,
        debug: !!window.__INK_DEBUG,
        onFirstFrame: function (runtimeStats) {
          if (generation !== backendGeneration || mainCanvas !== canvas) return;
          updateRuntimeStats(runtimeStats);
          reveal(mainCanvas, generation);
          scheduleInteractiveUpgrade();
        },
        onInteractiveReady: function () {},
        onFatal: function () { mainFatal(mainCanvas, generation); },
        onStats: function (runtimeStats) {
          if (generation === backendGeneration && mainCanvas === canvas) {
            updateRuntimeStats(runtimeStats);
          }
        },
      });
      if (!created) {
        mainFatal(mainCanvas, generation);
        return;
      }
      if (staticPolicy || generation !== backendGeneration || mainCanvas !== canvas) {
        created.destroy();
        return;
      }
      renderer = created;
      created.setState(state());
      backendReady = true;

      if (window.__INK_TEST) {
        window.__inkRenderClicks = function (clicks, bgTime) {
          if (generation !== backendGeneration || created !== renderer) return;
          created.setState({
            width: window.innerWidth,
            height: window.innerHeight,
            dpr: window.devicePixelRatio || 1,
            dark: darkTheme(),
            friends: friendsSkin(),
            interactive: true,
            fine: true,
          });
          created.renderTest(clicks || [], bgTime);
          latestRuntimeStats = created.stats();
          firstFrameReady = true;
          controllerStats.firstFrameReady = true;
          host.classList.add("has-ink");
        };
        window.__inkReady = true;
      } else {
        created.start();
      }
      flushInput();
    }, function () { mainFatal(mainCanvas, generation); });
  }

  function fallbackFromWorker(expectedWorker, generation) {
    if (generation !== backendGeneration || worker !== expectedWorker || failedOver) return;
    failedOver = true;
    window.clearTimeout(workerTimer);
    if (expectedWorker) expectedWorker.terminate();
    worker = null;
    renderer = null;
    backendReady = false;
    resetReveal();
    // Transferred canvas is one-way; main fallback always receives a fresh node.
    removeCanvas();
    canvas = makeCanvas();
    startMain(true);
  }

  function startWorker() {
    failedOver = false;
    removeCanvas();
    canvas = makeCanvas();
    var workerCanvas = canvas;
    var generation = beginBackend();
    controllerStats.backend = "worker";
    controllerStats.worker = true;
    var startedWorker = null;
    try {
      startedWorker = new Worker(workerSrc);
      worker = startedWorker;
      startedWorker.onerror = function () {
        fallbackFromWorker(startedWorker, generation);
      };
      startedWorker.onmessageerror = function () {
        fallbackFromWorker(startedWorker, generation);
      };
      startedWorker.onmessage = function (event) {
        if (generation !== backendGeneration || worker !== startedWorker ||
            canvas !== workerCanvas) return;
        var message = event.data || {};
        if (message.type === "first-frame") {
          window.clearTimeout(workerTimer);
          updateRuntimeStats(message.detail);
          reveal(workerCanvas, generation);
          scheduleInteractiveUpgrade();
        } else if (message.type === "stats") {
          updateRuntimeStats(message.detail);
        } else if (message.type === "fatal") {
          fallbackFromWorker(startedWorker, generation);
        }
      };
      var offscreen = workerCanvas.transferControlToOffscreen();
      startedWorker.postMessage({
        type: "init",
        canvas: offscreen,
        runtimeUrl: runtimeSrc,
        state: state(),
        preserveDrawingBuffer: false,
        debug: !!window.__INK_DEBUG,
      }, [offscreen]);
      backendReady = true;
      workerTimer = window.setTimeout(function () {
        fallbackFromWorker(startedWorker, generation);
      }, 5000);
    } catch (_) {
      fallbackFromWorker(startedWorker, generation);
    }
  }

  function scheduleInteractiveUpgrade() {
    if (!eligible()) return;
    var upgrade = function () {
      if (!eligible()) return;
      if (worker) worker.postMessage({type: "ensure-interactive"});
      else if (renderer) renderer.ensureInteractive();
    };
    if (window.requestIdleCallback) {
      window.requestIdleCallback(upgrade, {timeout: 1800});
    } else {
      window.setTimeout(upgrade, 250);
    }
  }

  function sendState() {
    var next = state();
    if (worker) worker.postMessage({type: "state", state: next});
    else if (renderer) renderer.setState(next);
  }

  function resetPointerSample() {
    hasPointerSample = false;
    pendingMove = null;
  }

  function flushInput() {
    inputRaf = 0;
    if (!pendingMove && !pendingClicks.length) return;
    if (!backendReady || !eligible()) {
      if (!eligible()) {
        resetPointerSample();
        pendingClicks.length = 0;
      } else {
        inputRaf = window.requestAnimationFrame(flushInput);
      }
      return;
    }
    var payload = {move: pendingMove, clicks: pendingClicks.splice(0)};
    if (pendingMove) {
      lastPointer.x = pendingMove.x;
      lastPointer.y = pendingMove.y;
    }
    pendingMove = null;
    controllerStats.pointerFlushes += 1;
    if (worker) worker.postMessage({type: "input", payload: payload});
    else if (renderer) renderer.input(payload);
  }

  function scheduleInput() {
    if (!inputRaf) inputRaf = window.requestAnimationFrame(flushInput);
  }

  function onMove(event) {
    if (!eligible()) return;
    var x = event.clientX / Math.max(1, window.innerWidth);
    var y = 1 - event.clientY / Math.max(1, window.innerHeight);
    var firstSample = !hasPointerSample;
    var px = firstSample ? x : lastPointer.x;
    var py = firstSample ? y : lastPointer.y;
    if (firstSample) {
      lastPointer.x = x;
      lastPointer.y = y;
      hasPointerSample = true;
    }
    pendingMove = {x: x, y: y, px: px, py: py};
    scheduleInput();
  }

  function onClick(event) {
    if (!eligible()) return;
    if (event.button != null && event.button !== 0) return;
    pendingClicks.push({
      x: event.clientX / Math.max(1, window.innerWidth),
      y: 1 - event.clientY / Math.max(1, window.innerHeight),
    });
    scheduleInput();
  }

  var refreshRaf = 0;
  function refresh() {
    // Любая граница состояния (тема, Turbo-навигация, cursor_effects,
    // resize) начинает новый жест. Иначе segment-splat соединит первую
    // новую координату со старой точкой до выключения эффекта.
    resetPointerSample();
    if (pageStopped) return;
    if (staticPolicy) {
      if (posterLoader) posterLoader();
      return;
    }
    if (refreshRaf) return;
    refreshRaf = window.requestAnimationFrame(function () {
      refreshRaf = 0;
      if (staticPolicy) return;
      stateCache = null;
      sendState();
      scheduleInteractiveUpgrade();
    });
  }

  function pause() {
    resetPointerSample();
    if (staticPolicy) return;
    if (worker) worker.postMessage({type: "pause"});
    else if (renderer) renderer.pause();
  }
  function resume() {
    if (pageStopped) return;
    if (staticPolicy) {
      if (posterLoader) posterLoader();
      return;
    }
    sendState();
    if (worker) worker.postMessage({type: "resume"});
    else if (renderer) renderer.start();
  }

  function canUseOffscreen() {
    return !window.__INK_FORCE_MAIN && !window.__INK_TEST &&
      !!window.Worker && !!workerSrc && !!runtimeSrc &&
      "transferControlToOffscreen" in HTMLCanvasElement.prototype;
  }

  function startBackend() {
    pageStopped = false;
    document.documentElement.classList.remove("ink-static");
    removePoster();
    latestRuntimeStats = {};
    failedOver = false;
    backendReady = false;
    if (canUseOffscreen()) startWorker();
    else {
      removeCanvas();
      canvas = makeCanvas();
      startMain(true);
    }
  }

  function enterStaticMode() {
    if (staticPolicy && posterLoader) {
      posterLoader();
      return;
    }
    staticPolicy = true;
    backendGeneration += 1;
    resetReveal();
    failedOver = true;
    window.clearTimeout(workerTimer);
    workerTimer = 0;
    if (inputRaf) window.cancelAnimationFrame(inputRaf);
    inputRaf = 0;
    resetPointerSample();
    pendingClicks.length = 0;
    if (worker) {
      worker.terminate();
      worker = null;
    }
    if (renderer) {
      renderer.destroy();
      renderer = null;
    }
    backendReady = false;
    controllerStats.backend = "poster";
    controllerStats.worker = false;
    window.__inkReady = false;
    try { delete window.__inkRenderClicks; } catch (_) {
      window.__inkRenderClicks = undefined;
    }
    removeCanvas();
    startPoster();
  }

  function exitStaticMode() {
    if (!staticPolicy || deviceStaticPolicy || reduceMotion) return;
    staticPolicy = false;
    backendGeneration += 1;
    resetReveal();
    removePoster();
    stateCache = null;
    startBackend();
  }

  function stopBackend() {
    if (pageStopped || staticPolicy) return;
    pageStopped = true;
    backendGeneration += 1;
    window.clearTimeout(workerTimer);
    workerTimer = 0;
    if (inputRaf) window.cancelAnimationFrame(inputRaf);
    if (refreshRaf) window.cancelAnimationFrame(refreshRaf);
    if (revealRaf) window.cancelAnimationFrame(revealRaf);
    inputRaf = 0;
    refreshRaf = 0;
    revealRaf = 0;
    resetPointerSample();
    pendingClicks.length = 0;
    if (worker) {
      try { worker.postMessage({type: "destroy"}); } catch (_) {}
      worker.terminate();
      worker = null;
    }
    if (renderer) {
      renderer.destroy();
      renderer = null;
    }
    backendReady = false;
    controllerStats.backend = "stopped";
    controllerStats.worker = false;
  }

  function onPageHide(event) {
    pause();
    if (!event.persisted) stopBackend();
  }

  function onPageShow() {
    if (pageStopped && !staticPolicy) {
      stateCache = null;
      startBackend();
      return;
    }
    resume();
  }

  controller = {refresh: refresh, pause: pause, resume: resume};
  host.__d4yInkController = controller;
  host.classList.remove("has-ink");

  var moveEvent = window.PointerEvent ? "pointermove" : "mousemove";
  var clickEvent = window.PointerEvent ? "pointerdown" : "mousedown";
  var leaveEvent = window.PointerEvent ? "pointerleave" : "mouseleave";
  window.addEventListener(moveEvent, onMove, {passive: true});
  window.addEventListener(clickEvent, onClick, {passive: true});
  window.addEventListener(leaveEvent, resetPointerSample, {passive: true});
  window.addEventListener("resize", refresh, {passive: true});
  document.addEventListener("d4y:themechange", refresh);
  document.addEventListener("d4y:skinchange", refresh);
  document.addEventListener("turbo:load", refresh);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) pause(); else resume();
  });
  window.addEventListener("pagehide", onPageHide);
  window.addEventListener("pageshow", onPageShow);
  if (reduceQuery && reduceQuery.addEventListener) {
    reduceQuery.addEventListener("change", function (event) {
      reduceMotion = !!event.matches;
      if (reduceMotion) enterStaticMode();
      else exitStaticMode();
    });
  }

  // Настройка cursor_effects может измениться без навигации.
  new MutationObserver(refresh).observe(document.documentElement, {
    attributes: true,
    subtree: true,
    attributeFilter: ["data-ink-interactive", "data-skin", "data-theme"],
  });

  if (staticPolicy) enterStaticMode();
  else startBackend();
})();
