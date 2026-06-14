/* Фон «растекающиеся чернила» (вариант A).
 *
 * Один полноэкранный WebGL2-шейдер: фрактальный шум с доменным искажением
 * медленно эволюционирует во времени — чернильные пятна нашей палитры
 * расплываются и перетекают друг в друга, как тушь в воде.
 *
 * Бережно к телефону:
 *   • разрешение капается (DPR ≤ 1.5, рендер в пониженном масштабе);
 *   • при скрытой вкладке цикл встаёт на паузу;
 *   • prefers-reduced-motion → один статичный кадр, без анимации;
 *   • нет WebGL2 → молча выходим, остаётся CSS-дым (.bg-smoke) как фон.
 *
 * Самохостинг, без внешних зависимостей и Three.js — под нашу строгую CSP.
 */
(function () {
  "use strict";

  var host = document.querySelector(".bg-smoke");
  if (!host) return;

  var reduce = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var canvas = document.createElement("canvas");
  canvas.className = "ink-canvas";
  canvas.setAttribute("aria-hidden", "true");

  var gl = canvas.getContext("webgl2", {
    alpha: true, antialias: false, depth: false,
    stencil: false, premultipliedAlpha: false, powerPreference: "low-power",
  });
  if (!gl) return;                       // нет WebGL2 → остаётся CSS-дым

  // Шейдер виден поверх CSS-дыма, но под всем контентом
  host.appendChild(canvas);
  host.classList.add("has-ink");

  var VERT = "#version 300 es\n" +
    "in vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }";

  // Доменно-искажённый fbm: чернила в нашей палитре, медленно перетекают.
  var FRAG = "#version 300 es\n" +
    "precision highp float;\n" +
    "out vec4 o;\n" +
    "uniform vec2 res; uniform float t; uniform vec3 mouse; uniform vec2 mdir;\n" +
    // палитра (sRGB 0..1): крем-фон, роза, малина, персик, сирень
    "const vec3 BG    = vec3(0.984, 0.949, 0.945);\n" +
    "const vec3 ROSE  = vec3(0.713, 0.372, 0.435);\n" +
    "const vec3 BERRY = vec3(0.560, 0.290, 0.345);\n" +
    "const vec3 PEACH = vec3(0.886, 0.690, 0.541);\n" +
    "const vec3 LILAC = vec3(0.808, 0.588, 0.784);\n" +
    "float hash(vec2 p){ p = fract(p*vec2(123.34, 456.21)); p += dot(p, p+45.32); return fract(p.x*p.y); }\n" +
    "float noise(vec2 p){\n" +
    "  vec2 i = floor(p), f = fract(p);\n" +
    "  float a = hash(i), b = hash(i+vec2(1.0,0.0));\n" +
    "  float c = hash(i+vec2(0.0,1.0)), d = hash(i+vec2(1.0,1.0));\n" +
    "  vec2 u = f*f*(3.0-2.0*f);\n" +
    "  return mix(mix(a,b,u.x), mix(c,d,u.x), u.y);\n" +
    "}\n" +
    "float fbm(vec2 p){\n" +
    "  float s = 0.0, a = 0.5;\n" +
    "  for(int i=0;i<5;i++){ s += a*noise(p); p *= 2.02; a *= 0.5; }\n" +
    "  return s;\n" +
    "}\n" +
    "void main(){\n" +
    "  vec2 uv = gl_FragCoord.xy / res;\n" +
    "  uv.x *= res.x / res.y;\n" +
    "  float tt = t * 0.016;\n" +
    // --- интерактив: «палка в воде» — след-волна ИДЁТ ЗА движением -----------
    // позиция касания в той же аспект-коррекции, что и uv
    "  vec2 m = mouse.xy; m.x *= res.x / res.y;\n" +
    "  vec2 dm = uv - m;\n" +
    // направление движения (сглажено на JS, не дёргается); вода толкается вперёд,
    // а возмущение/волны остаются ПОЗАДИ — как настоящий след от палки
    "  vec2 dir = (dot(mdir, mdir) > 1e-6) ? normalize(vec2(mdir.x * res.x / res.y, mdir.y)) : vec2(0.0, 0.0);\n" +
    "  float along  = dot(dm, dir);\n" +        // >0 впереди по ходу, <0 — позади (хвост)
    "  float side   = length(dm - dir * along);\n" +  // поперечное расстояние от линии хода
    // зона у острия компактная; хвост позади растянут (along<0 → мягче спад вдоль)
    "  float tail = along < 0.0 ? 60.0 : 220.0;\n" +
    "  float env = exp(-(along*along * tail + side*side * 320.0));\n" +
    // дуги-волны изгибаются назад от острия (фронт по ходу, гребни сходят в хвост)
    "  float phase = along * 90.0 - side * side * 60.0 - t * 9.0;\n" +
    "  float wake = sin(phase) * smoothstep(0.0, 0.18, -along + 0.05);\n" +  // только позади
    // вперёд — короткий «нос» (вода раздвигается), позади — расходящийся след
    "  float bow = exp(-along*along * 600.0 - side*side * 320.0) * step(0.0, along);\n" +
    "  float disp = wake * 0.8 + bow * 0.5;\n" +
    "  vec2 push = dir * (along < 0.0 ? -1.0 : 1.0);\n" +  // вдоль линии хода
    "  uv += (push * disp + dm / (length(dm) + 1e-4) * wake * 0.25) * env * mouse.z * 0.09;\n" +
    // орбитальное смещение доменов → поле не просто плывёт, а перетекает/морфит
    "  vec2 mo = vec2(sin(tt*0.7), cos(tt*0.6)) * 0.7;\n" +
    "  vec2 q = vec2(fbm(uv*1.6 + mo + vec2(0.0, tt)), fbm(uv*1.6 - mo + vec2(5.2, -tt)));\n" +
    "  vec2 r = vec2(fbm(uv*1.6 + 4.0*q + vec2(1.7, 9.2) + tt*0.9),\n" +
    "                fbm(uv*1.6 + 4.0*q + vec2(8.3, 2.8) - tt*0.9));\n" +
    "  float f = fbm(uv*1.6 + 4.3*r + 0.35*sin(tt));\n" +
    "  vec3 col = BG;\n" +
    // послойно подмешиваем чернила: более высокие пороги → больше крема между пятнами
    "  col = mix(col, LILAC, smoothstep(0.42, 1.02, length(r)) * 0.42);\n" +
    "  col = mix(col, ROSE,  smoothstep(0.52, 1.10, f) * 0.62);\n" +
    "  col = mix(col, PEACH, smoothstep(0.45, 0.95, q.x*q.x) * 0.38);\n" +
    "  col = mix(col, BERRY, smoothstep(0.74, 1.12, f*1.1) * 0.5);\n" +
    // лёгкое осветление к верху — «свет сверху», как в референсе
    "  col = mix(col, BG, smoothstep(0.55, 1.0, 1.0 - uv.y*res.y/res.x) * 0.25);\n" +
    "  o = vec4(col, 1.0);\n" +
    "}";

  function compile(type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      gl.deleteShader(s);
      return null;
    }
    return s;
  }

  var vs = compile(gl.VERTEX_SHADER, VERT);
  var fs = compile(gl.FRAGMENT_SHADER, FRAG);
  if (!vs || !fs) { teardown(); return; }

  var prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { teardown(); return; }
  gl.useProgram(prog);

  // полноэкранный треугольник
  var buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  var loc = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

  var uRes = gl.getUniformLocation(prog, "res");
  var uT = gl.getUniformLocation(prog, "t");
  var uMouse = gl.getUniformLocation(prog, "mouse");
  var uMdir = gl.getUniformLocation(prog, "mdir");

  // Сглаженная позиция указателя (mx,my) тянется к цели (tx,ty) внутри
  // кадрового цикла — поэтому след не дёргается при неравномерных событиях.
  // (dirx,diry) — направление хода (тоже плавно сглажено), mStrength — сила следа.
  var mx = 0.5, my = 0.5, tx = 0.5, ty = 0.5;
  var dirx = 0.0, diry = 0.0, mStrength = 0.0, tStrength = 0.0;

  // рендерим в пониженном разрешении: чернила мягкие, детали не нужны,
  // зато ощутимо легче для GPU телефона
  var SCALE = 0.5;

  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    var w = Math.max(2, Math.floor(window.innerWidth * dpr * SCALE));
    var h = Math.max(2, Math.floor(window.innerHeight * dpr * SCALE));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
    }
  }

  var raf = 0;
  var start = performance.now();

  function frame(now) {
    raf = 0;
    resize();
    // скорость следа = насколько позиция тянется к цели; из неё берём
    // направление хода и плавно (медленно!) его сглаживаем — без рывков
    var vx = tx - mx, vy = ty - my;
    var vlen = Math.sqrt(vx * vx + vy * vy);
    if (vlen > 1e-4) {
      dirx += (vx / vlen - dirx) * 0.08;   // медленное сглаживание оси → нет дрожи
      diry += (vy / vlen - diry) * 0.08;
    }
    // плавно подтягиваем позицию следа к цели — гасит рывки от событий мыши
    mx += (tx - mx) * 0.12;
    my += (ty - my) * 0.12;
    // силу тоже ведём через цель: события мыши лишь поднимают tStrength,
    // а видимая mStrength плавно тянется к ней — без скачков на каждый event
    mStrength += (tStrength - mStrength) * 0.18;
    gl.uniform2f(uRes, canvas.width, canvas.height);
    gl.uniform1f(uT, (now - start) / 1000);
    gl.uniform3f(uMouse, mx, my, mStrength);
    gl.uniform2f(uMdir, dirx, diry);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    tStrength *= 0.92;                    // палка вынута — след затухает плавно
    if (tStrength < 0.002) tStrength = 0;
    if (mStrength < 0.002 && tStrength === 0) mStrength = 0;
    if (!document.hidden) raf = requestAnimationFrame(frame);
  }

  function play() {
    if (!raf && !document.hidden) raf = requestAnimationFrame(frame);
  }
  function pause() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
  }

  function teardown() {
    pause();
    if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
  }

  // первый кадр всегда рисуем; анимируем только если движение не отключено
  resize();
  gl.uniform2f(uRes, canvas.width, canvas.height);
  gl.uniform1f(uT, reduce ? 8.0 : 0.0);   // для статики берём «красивый» момент
  gl.drawArrays(gl.TRIANGLES, 0, 3);

  if (!reduce) {
    // указатель: переводим в UV (0..1), y переворачиваем под gl_FragCoord.
    // Пишем ЦЕЛЬ (tx,ty) — позиция ряби подтягивается к ней в цикле плавно.
    function point(cx, cy) {
      var nx = cx / window.innerWidth;
      var ny = 1.0 - cy / window.innerHeight;     // WebGL: 0 снизу
      var dx = nx - tx, dy = ny - ty;
      var speed = Math.sqrt(dx * dx + dy * dy);
      tx = nx; ty = ny;
      // быстрее ведёшь — сильнее «булькает»; стоишь на месте — рябь тихо живёт
      tStrength = Math.min(1.0, tStrength + 0.12 + speed * 3.0);
      play();                                       // оживляем цикл, если стоял
    }
    window.addEventListener("mousemove", function (e) {
      point(e.clientX, e.clientY);
    }, { passive: true });
    window.addEventListener("touchmove", function (e) {
      var tch = e.touches[0];
      if (tch) point(tch.clientX, tch.clientY);
    }, { passive: true });
    window.addEventListener("touchstart", function (e) {
      var tch = e.touches[0];
      if (tch) point(tch.clientX, tch.clientY);
    }, { passive: true });

    document.addEventListener("visibilitychange", function () {
      if (document.hidden) pause(); else play();
    });
    window.addEventListener("pagehide", pause);
    play();
  }
})();
