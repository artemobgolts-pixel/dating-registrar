/* Фон «растекающиеся чернила» (墨流し) — настоящая жидкостная симуляция.
 * GPU-решатель Навье–Стокса (стабильные жидкости, Jos Stam): поле скоростей
 * + краска; адвекция, проекция давления (Якоби), завихрённость. Курсор вливает
 * чернила и тянет течение — они растекаются, смешиваются и медленно растворяются,
 * как тушь в воде. В покое мягкое «авто-течение» само рисует разводы.
 *
 * Самохостинг, без зависимостей и Three.js — под нашу строгую CSP.
 * Нужен WebGL2 + рендер в half-float. Нет поддержки → тихий выход, остаётся
 * CSS-дым (.bg-smoke). prefers-reduced-motion → один статичный кадр.
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
    alpha: true, antialias: false, depth: false, stencil: false,
    premultipliedAlpha: false, powerPreference: "high-performance",
  });
  if (!gl) return;
  if (!gl.getExtension("EXT_color_buffer_float")) return;  // нет float-рендера
  gl.getExtension("OES_texture_float_linear");

  host.appendChild(canvas);
  host.classList.add("has-ink");

  // --- параметры симуляции (можно крутить) --------------------------------
  var SMALL = Math.min(window.innerWidth, window.innerHeight) < 700;
  var SIM_RES    = SMALL ? 100 : 140;  // сетка скоростей/давления
  var DYE_RES    = SMALL ? 420 : 640;  // сетка краски (детальность разводов)
  var ITER       = 22;                 // итерации давления (несжимаемость)
  var CURL       = 26;                 // завихрённость — «жилки» в чернилах
  var VEL_DISS   = 0.30;               // затухание скорости (выше — спокойнее)
  var DEN_DISS   = 0.60;               // растворение краски (выше — быстрее тает)
  var PRESS_DISS = 0.80;
  var SPLAT_R    = 0.0022;             // размер чернильного пятна (uv²)
  var FORCE      = 6200;               // сила толчка течения от курсора

  // палитра чернил (sRGB 0..1): роза, малина, персик, сирень, мягкий индиго
  var INKS = [
    [0.713, 0.372, 0.435],
    [0.560, 0.290, 0.345],
    [0.886, 0.690, 0.541],
    [0.808, 0.588, 0.784],
    [0.392, 0.345, 0.560],
  ];

  // --- исходники шейдеров (GLSL ES 3.00) ----------------------------------
  var BASE_VS = `#version 300 es
precision highp float;
layout(location=0) in vec2 aPos;
out vec2 vUv; out vec2 vL; out vec2 vR; out vec2 vT; out vec2 vB;
uniform vec2 texel;
void main(){
  vUv = aPos*0.5+0.5;
  vL = vUv - vec2(texel.x,0.0); vR = vUv + vec2(texel.x,0.0);
  vT = vUv + vec2(0.0,texel.y); vB = vUv - vec2(0.0,texel.y);
  gl_Position = vec4(aPos,0.0,1.0);
}`;

  var CLEAR_FS = `#version 300 es
precision highp float; in vec2 vUv; out vec4 o; uniform sampler2D uTex; uniform float val;
void main(){ o = val*texture(uTex,vUv); }`;

  var SPLAT_FS = `#version 300 es
precision highp float; in vec2 vUv; out vec4 o;
uniform sampler2D uTarget; uniform float aspect; uniform vec3 color;
uniform vec2 point; uniform float radius;
void main(){
  vec2 p = vUv - point; p.x *= aspect;
  vec3 splat = exp(-dot(p,p)/radius) * color;
  o = vec4(texture(uTarget,vUv).rgb + splat, 1.0);
}`;

  var ADV_FS = `#version 300 es
precision highp float; in vec2 vUv; out vec4 o;
uniform sampler2D uVelocity; uniform sampler2D uSource;
uniform vec2 texel; uniform float dt; uniform float diss;
void main(){
  vec2 coord = vUv - dt * texture(uVelocity,vUv).xy * texel;
  o = texture(uSource,coord) / (1.0 + diss*dt);
}`;

  var DIV_FS = `#version 300 es
precision highp float; in vec2 vUv; in vec2 vL; in vec2 vR; in vec2 vT; in vec2 vB;
out vec4 o; uniform sampler2D uVelocity;
void main(){
  float l = texture(uVelocity,vL).x; float r = texture(uVelocity,vR).x;
  float t = texture(uVelocity,vT).y; float b = texture(uVelocity,vB).y;
  vec2 c = texture(uVelocity,vUv).xy;
  if(vL.x<0.0) l=-c.x; if(vR.x>1.0) r=-c.x;
  if(vT.y>1.0) t=-c.y; if(vB.y<0.0) b=-c.y;
  o = vec4(0.5*(r-l+t-b),0.0,0.0,1.0);
}`;

  var CURL_FS = `#version 300 es
precision highp float; in vec2 vUv; in vec2 vL; in vec2 vR; in vec2 vT; in vec2 vB;
out vec4 o; uniform sampler2D uVelocity;
void main(){
  float l = texture(uVelocity,vL).y; float r = texture(uVelocity,vR).y;
  float t = texture(uVelocity,vT).x; float b = texture(uVelocity,vB).x;
  o = vec4(0.5*(r-l-(t-b)),0.0,0.0,1.0);
}`;

  var VORT_FS = `#version 300 es
precision highp float; in vec2 vUv; in vec2 vL; in vec2 vR; in vec2 vT; in vec2 vB;
out vec4 o; uniform sampler2D uVelocity; uniform sampler2D uCurl;
uniform float curl; uniform float dt;
void main(){
  float l = texture(uCurl,vL).x; float r = texture(uCurl,vR).x;
  float t = texture(uCurl,vT).x; float b = texture(uCurl,vB).x;
  float c = texture(uCurl,vUv).x;
  vec2 force = 0.5*vec2(abs(t)-abs(b), abs(r)-abs(l));
  force /= length(force)+1e-4; force *= curl*c; force.y *= -1.0;
  vec2 vel = texture(uVelocity,vUv).xy;
  o = vec4(vel + force*dt, 0.0, 1.0);
}`;

  var PRESS_FS = `#version 300 es
precision highp float; in vec2 vUv; in vec2 vL; in vec2 vR; in vec2 vT; in vec2 vB;
out vec4 o; uniform sampler2D uPressure; uniform sampler2D uDivergence;
void main(){
  float l = texture(uPressure,vL).x; float r = texture(uPressure,vR).x;
  float t = texture(uPressure,vT).x; float b = texture(uPressure,vB).x;
  float div = texture(uDivergence,vUv).x;
  o = vec4((l+r+t+b-div)*0.25,0.0,0.0,1.0);
}`;

  var GRAD_FS = `#version 300 es
precision highp float; in vec2 vUv; in vec2 vL; in vec2 vR; in vec2 vT; in vec2 vB;
out vec4 o; uniform sampler2D uPressure; uniform sampler2D uVelocity;
void main(){
  float l = texture(uPressure,vL).x; float r = texture(uPressure,vR).x;
  float t = texture(uPressure,vT).x; float b = texture(uPressure,vB).x;
  vec2 vel = texture(uVelocity,vUv).xy;
  vel -= vec2(r-l,t-b);
  o = vec4(vel,0.0,1.0);
}`;

  // вывод: краску кладём поверх кремового фона, лёгкий свет сверху — как в референсе
  var DISP_FS = `#version 300 es
precision highp float; in vec2 vUv; out vec4 o; uniform sampler2D uTex; uniform vec2 uAspect;
const vec3 BG = vec3(0.984,0.949,0.945);
void main(){
  vec3 ink = texture(uTex,vUv).rgb;
  float a = clamp(max(ink.r,max(ink.g,ink.b)),0.0,1.0);
  vec3 col = mix(BG, ink/max(a,1e-3), smoothstep(0.0,0.25,a));
  col = mix(col, BG, smoothstep(0.55,1.0,1.0-vUv.y)*0.18);
  o = vec4(col,1.0);
}`;

  // --- компиляция/линковка -------------------------------------------------
  function compile(type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { gl.deleteShader(s); return null; }
    return s;
  }
  function makeProg(vsSrc, fsSrc) {
    var vs = compile(gl.VERTEX_SHADER, vsSrc), fs = compile(gl.FRAGMENT_SHADER, fsSrc);
    if (!vs || !fs) return null;
    var p = gl.createProgram();
    gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) return null;
    // карта uniform-локаций по имени
    var u = {}, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (var i = 0; i < n; i++) { var nm = gl.getActiveUniform(p, i).name; u[nm] = gl.getUniformLocation(p, nm); }
    return { prog: p, u: u };
  }

  var progs = {
    clear: makeProg(BASE_VS, CLEAR_FS), splat: makeProg(BASE_VS, SPLAT_FS),
    adv: makeProg(BASE_VS, ADV_FS), div: makeProg(BASE_VS, DIV_FS),
    curl: makeProg(BASE_VS, CURL_FS), vort: makeProg(BASE_VS, VORT_FS),
    press: makeProg(BASE_VS, PRESS_FS), grad: makeProg(BASE_VS, GRAD_FS),
    disp: makeProg(BASE_VS, DISP_FS),
  };
  for (var k in progs) { if (!progs[k]) { teardown(); return; } }

  // полноэкранный треугольник
  var quad = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, quad);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 3,-1, -1,3]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

  function blit(target) {
    gl.bindFramebuffer(gl.FRAMEBUFFER, target ? target.fbo : null);
    if (target) gl.viewport(0, 0, target.w, target.h);
    else gl.viewport(0, 0, canvas.width, canvas.height);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  // --- буферы кадра (FBO) --------------------------------------------------
  var RG = gl.RG16F, RGBA = gl.RGBA16F, R = gl.R16F;
  function makeFBO(w, h, internal, format, filter) {
    var tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, internal, w, h, 0, format, gl.HALF_FLOAT, null);
    var fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    gl.viewport(0, 0, w, h); gl.clear(gl.COLOR_BUFFER_BIT);
    return { tex: tex, fbo: fbo, w: w, h: h,
      attach: function (n) { gl.activeTexture(gl.TEXTURE0 + n); gl.bindTexture(gl.TEXTURE_2D, tex); return n; } };
  }
  function makeDouble(w, h, internal, format, filter) {
    var a = makeFBO(w, h, internal, format, filter), b = makeFBO(w, h, internal, format, filter);
    return {
      w: w, h: h,
      read: a, write: b,
      swap: function () { var t = this.read; this.read = this.write; this.write = t; },
    };
  }

  var LIN = gl.getExtension("OES_texture_float_linear") ? gl.LINEAR : gl.NEAREST;
  var velocity, density, divergence, curlFbo, pressure;

  function initFBOs() {
    velocity   = makeDouble(SIM_RES, SIM_RES, RG, gl.RG, LIN);
    density    = makeDouble(DYE_RES, DYE_RES, RGBA, gl.RGBA, LIN);
    divergence = makeFBO(SIM_RES, SIM_RES, R, gl.RED, gl.NEAREST);
    curlFbo    = makeFBO(SIM_RES, SIM_RES, R, gl.RED, gl.NEAREST);
    pressure   = makeDouble(SIM_RES, SIM_RES, R, gl.RED, gl.NEAREST);
  }
  initFBOs();

  // --- один шаг симуляции --------------------------------------------------
  var simTexel = [1 / SIM_RES, 1 / SIM_RES];

  function step(dt) {
    gl.disable(gl.BLEND);

    // завихрённость → подкручиваем скорость (живые «жилки» в чернилах)
    var c = progs.curl;
    gl.useProgram(c.prog);
    gl.uniform2f(c.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(c.u.uVelocity, velocity.read.attach(0));
    blit(curlFbo);

    var v = progs.vort;
    gl.useProgram(v.prog);
    gl.uniform2f(v.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(v.u.uVelocity, velocity.read.attach(0));
    gl.uniform1i(v.u.uCurl, curlFbo.attach(1));
    gl.uniform1f(v.u.curl, CURL);
    gl.uniform1f(v.u.dt, dt);
    blit(velocity.write); velocity.swap();

    // дивергенция поля скоростей
    var d = progs.div;
    gl.useProgram(d.prog);
    gl.uniform2f(d.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(d.u.uVelocity, velocity.read.attach(0));
    blit(divergence);

    // затухание давления + итерации Якоби (несжимаемость)
    var cl = progs.clear;
    gl.useProgram(cl.prog);
    gl.uniform1i(cl.u.uTex, pressure.read.attach(0));
    gl.uniform1f(cl.u.val, PRESS_DISS);
    blit(pressure.write); pressure.swap();

    var pr = progs.press;
    gl.useProgram(pr.prog);
    gl.uniform2f(pr.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(pr.u.uDivergence, divergence.attach(0));
    for (var i = 0; i < ITER; i++) {
      gl.uniform1i(pr.u.uPressure, pressure.read.attach(1));
      blit(pressure.write); pressure.swap();
    }

    // вычитаем градиент давления → поле снова несжимаемо
    var g = progs.grad;
    gl.useProgram(g.prog);
    gl.uniform2f(g.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(g.u.uPressure, pressure.read.attach(0));
    gl.uniform1i(g.u.uVelocity, velocity.read.attach(1));
    blit(velocity.write); velocity.swap();

    // адвекция скорости по самой себе
    var a = progs.adv;
    gl.useProgram(a.prog);
    gl.uniform2f(a.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(a.u.uVelocity, velocity.read.attach(0));
    gl.uniform1i(a.u.uSource, velocity.read.attach(0));
    gl.uniform1f(a.u.dt, dt);
    gl.uniform1f(a.u.diss, VEL_DISS);
    blit(velocity.write); velocity.swap();

    // адвекция краски (своя сетка, выше разрешением)
    gl.uniform1i(a.u.uVelocity, velocity.read.attach(0));
    gl.uniform1i(a.u.uSource, density.read.attach(1));
    gl.uniform1f(a.u.diss, DEN_DISS);
    blit(density.write); density.swap();
  }

  // вливаем чернила + толчок течения в точку (uv 0..1, dx/dy — направление)
  function splat(x, y, dx, dy, color) {
    var aspect = canvas.width / canvas.height;
    var s = progs.splat;
    gl.useProgram(s.prog);
    gl.uniform1f(s.u.aspect, aspect);
    gl.uniform2f(s.u.point, x, y);
    gl.uniform1f(s.u.radius, SPLAT_R);
    // в скорость — направление движения
    gl.uniform1i(s.u.uTarget, velocity.read.attach(0));
    gl.uniform3f(s.u.color, dx, dy, 0.0);
    blit(velocity.write); velocity.swap();
    // в краску — цвет чернил
    gl.uniform1i(s.u.uTarget, density.read.attach(0));
    gl.uniform3f(s.u.color, color[0], color[1], color[2]);
    blit(density.write); density.swap();
  }

  // --- ввод и авто-течение -------------------------------------------------
  var pointer = { x: 0.5, y: 0.5, px: 0.5, py: 0.5, down: false, moved: false };
  var colorIdx = 0, colorT = 0;
  function nextColor() {
    colorIdx = (colorIdx + 1) % INKS.length;
    return INKS[colorIdx];
  }

  function onMove(cx, cy) {
    pointer.px = pointer.x; pointer.py = pointer.y;
    pointer.x = cx / window.innerWidth;
    pointer.y = 1.0 - cy / window.innerHeight;
    pointer.moved = true;
    play();
  }
  if (!reduce) {
    window.addEventListener("mousemove", function (e) { onMove(e.clientX, e.clientY); }, { passive: true });
    window.addEventListener("touchmove", function (e) {
      var t = e.touches[0]; if (t) onMove(t.clientX, t.clientY);
    }, { passive: true });
    window.addEventListener("touchstart", function (e) {
      var t = e.touches[0]; if (t) { pointer.x = t.clientX / window.innerWidth; pointer.y = 1 - t.clientY / window.innerHeight; pointer.px = pointer.x; pointer.py = pointer.y; }
    }, { passive: true });
  }

  // мягкие авто-вливания, когда курсор спит — фон сам живёт и течёт
  var idleT = 0, idleX = 0.5, idleY = 0.5, idleVX = 0, idleVY = 0, seedColor = INKS[0];
  function autoFlow(dt) {
    idleT -= dt;
    if (idleT <= 0) {
      // новая «капля» в случайной точке, лёгкий дрейф; интервал варьируем
      idleX = 0.18 + 0.64 * fract(idleX * 9.137 + 0.371);
      idleY = 0.18 + 0.64 * fract(idleY * 7.331 + 0.613);
      var ang = 6.2831 * fract(idleX * idleY * 13.7);
      idleVX = Math.cos(ang); idleVY = Math.sin(ang);
      seedColor = nextColor();
      idleT = 0.9 + fract(idleX + idleY) * 1.4;
    }
    splat(idleX, idleY, idleVX * FORCE * 0.20 * dt, idleVY * FORCE * 0.20 * dt, mul(seedColor, 0.85));
  }
  function fract(v) { return v - Math.floor(v); }
  function mul(c, k) { return [c[0] * k, c[1] * k, c[2] * k]; }

  // --- размеры вывода (краска и скорость в фикс. сетке, тут — только канвас) -
  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    var w = Math.max(2, Math.floor(window.innerWidth * dpr));
    var h = Math.max(2, Math.floor(window.innerHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w; canvas.height = h;
    }
  }

  // --- цикл ----------------------------------------------------------------
  var raf = 0, last = 0;

  function render(now) {
    raf = 0;
    if (!last) last = now;
    var dt = Math.min((now - last) / 1000, 0.022); // клампим, чтобы не «взрывалось»
    last = now;
    resize();

    // ввод от курсора → вливание чернил и толчок течения
    if (pointer.moved) {
      var dx = (pointer.x - pointer.px) * FORCE;
      var dy = (pointer.y - pointer.py) * FORCE;
      colorT -= 1;
      if (colorT <= 0) { seedColor = nextColor(); colorT = 8; }
      splat(pointer.x, pointer.y, dx * dt, dy * dt, seedColor);
      pointer.px = pointer.x; pointer.py = pointer.y;
      pointer.moved = false;
    } else {
      autoFlow(dt);                 // курсор спит — фон течёт сам
    }

    step(dt);

    // вывод краски на экран
    gl.disable(gl.BLEND);
    var dp = progs.disp;
    gl.useProgram(dp.prog);
    gl.uniform1i(dp.u.uTex, density.read.attach(0));
    blit(null);

    if (!document.hidden) raf = requestAnimationFrame(render);
  }

  function play() { if (!raf && !document.hidden) { last = 0; raf = requestAnimationFrame(render); } }
  function pause() { if (raf) { cancelAnimationFrame(raf); raf = 0; } }

  function teardown() {
    pause();
    if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
  }

  resize();

  // несколько стартовых капель — чтобы фон не был пустым на первом кадре
  (function seed() {
    for (var i = 0; i < 5; i++) {
      var sx = 0.2 + 0.15 * i, sy = 0.35 + 0.1 * ((i * 7) % 5);
      var ang = i * 1.7;
      splat(sx, sy, Math.cos(ang) * FORCE * 0.5 * 0.016, Math.sin(ang) * FORCE * 0.5 * 0.016, INKS[i % INKS.length]);
    }
  })();

  if (reduce) {
    // один прогон + статичный кадр, без анимации
    for (var s = 0; s < 40; s++) step(0.016);
    gl.useProgram(progs.disp.prog);
    gl.uniform1i(progs.disp.u.uTex, density.read.attach(0));
    blit(null);
  } else {
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) pause(); else play();
    });
    window.addEventListener("pagehide", pause);
    play();
  }
})();
