// Cookie-уведомление: показываем один раз, отметку храним в localStorage.
(function () {
  "use strict";
  var bar = document.getElementById("cookie-bar");
  if (!bar) return;
  var KEY = "d4y_cookie_ok";
  try {
    if (localStorage.getItem(KEY)) return;
  } catch (e) { /* приватный режим — просто покажем баннер */ }
  bar.hidden = false;
  var ok = document.getElementById("cookie-ok");
  if (ok) ok.addEventListener("click", function () {
    bar.hidden = true;
    try { localStorage.setItem(KEY, "1"); } catch (e) { /* игнор */ }
  });
})();
