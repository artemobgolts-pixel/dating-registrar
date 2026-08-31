/* WebGL2 вне основного потока. Только same-origin classic worker: без blob/eval. */
(function () {
  "use strict";

  var renderer = null;

  function send(type, detail) {
    self.postMessage({type: type, detail: detail || null});
  }

  self.onmessage = function (event) {
    var message = event.data || {};
    try {
      if (message.type === "init") {
        importScripts(message.runtimeUrl);
        if (!self.D4YInkRuntime || !self.D4YInkRuntime.create) {
          throw new Error("ink runtime unavailable");
        }
        renderer = self.D4YInkRuntime.create(message.canvas, {
          preserveDrawingBuffer: !!message.preserveDrawingBuffer,
          debug: !!message.debug,
          onFirstFrame: function (stats) { send("first-frame", stats); },
          onInteractiveReady: function (ready) {
            send(ready ? "interactive-ready" : "interactive-unavailable");
          },
          onFatal: function (reason) { send("fatal", reason); },
          onStats: function (stats) { send("stats", stats); },
        });
        if (!renderer) return;
        renderer.setState(message.state || {});
        renderer.start();
        return;
      }
      if (!renderer) return;
      if (message.type === "state" || message.type === "resize") {
        renderer.setState(message.state || {});
      } else if (message.type === "input") {
        renderer.input(message.payload || {});
      } else if (message.type === "ensure-interactive") {
        renderer.ensureInteractive();
      } else if (message.type === "pause") {
        renderer.pause();
      } else if (message.type === "resume") {
        renderer.start();
      } else if (message.type === "destroy") {
        renderer.destroy();
        renderer = null;
        self.close();
      }
    } catch (error) {
      send("fatal", error && error.message ? error.message : "worker-error");
    }
  };

  self.onmessageerror = function () {
    send("fatal", "worker-message-error");
  };
})();
