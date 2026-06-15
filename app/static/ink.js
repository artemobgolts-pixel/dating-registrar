/* Фон «растекающиеся чернила» (вариант A) + локальная деформация-жидкость.
 *
 * Видимый фон — наш прежний доменно-искажённый fbm: чернильные пятна нашей
 * палитры медленно перетекают (анимация не изменилась). Поверх него работает
 * GPU-решатель Навье–Стокса (стабильные жидкости): курсор вливает скорость,
 * она течёт и завихряется по законам жидкости — и этим полем скоростей мы
 * ЛОКАЛЬНО смещаем фон у курсора. Краску (dye) не рисуем: фон остаётся нашим,
 * а деформация живёт только маленьким пятном под курсором и плавно тает.
 *
 * Самохостинг, без зависимостей и Three.js — под нашу строгую CSP.
 * Нет WebGL2 / float-рендера → тихий выход, остаётся CSS-дым (.bg-smoke).
 * prefers-reduced-motion → один статичный кадр.
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
    premultipliedAlpha: false, powerPreference: "low-power",
  });
  if (!gl) return;
  if (!gl.getExtension("EXT_color_buffer_float")) return;  // нет float-рендера
  var LINEAR_OK = !!gl.getExtension("OES_texture_float_linear");

  host.appendChild(canvas);
  host.classList.add("has-ink");

  // --- параметры (можно крутить) ------------------------------------------
  var SMALL = Math.min(window.innerWidth, window.innerHeight) < 700;
  var SIM_RES    = SMALL ? 110 : 150;  // сетка скоростей/давления
  var DYE_RES    = SMALL ? 200 : 280;  // сетка краски (чернил) — повыше, для тендрилов
  var ITER       = 20;                 // итерации давления (несжимаемость)
  var CURL       = 0;                  // завихрённость ВЫКЛ в покое — давала зерно по экрану
  var CLICK_CURL = 2.4;                // на время клика включаем вихри: струи закручиваются спиралями
  var VEL_DISS   = 1.3;                 // затухание скорости (ниже — течение дольше несёт чернила в тендрилы)
  var PRESS_DISS = 0.8;
  var SPLAT_R    = 0.00045;            // размер зоны скорости у курсора (uv²)
  var FORCE      = 6000;               // сила толчка течения
  var STEP_SCALE = -0.016;             // вклад скорости в накопление (знак «-» = расталкивание)
  var PERSIST    = 0.999;              // деформация почти не возвращается (медленно зарастает ~20с)
  var DISP_MAX   = 0.22;               // максимум перекоса фона (защита от «взрыва»)
  var MASK_R     = 0.003033;            // радиус² пятна деформации у курсора (+5% радиус: ×1.05²)
  var IDLE_HOLD  = 2.6;                // сек: сколько ещё считать жидкость после движения
  var CLICK_HOLD = 7.0;                // сек: считать жидкость+адвекцию после клика (чернила раскручиваются в тендрилы)
  var DECAY_HOLD = 22;                 // сек: сколько крутить затухание следа (до полного зарастания)
  // --- чернила (dye): реальная краска, которую несёт и закручивает течение ---
  var DYE_DISS    = 0.55;              // затухание чернил при активной симуляции (ниже=живут дольше, успевают расплыться)
  var DYE_FADE    = 0.985;             // дотаивание чернил в покое (×за кадр; меньше=быстрее)
  var DYE_SPREAD  = 0.14;              // диффузия чернил в покое (расползаются как в воде)
  var DYE_R_MOVE  = 0.0000396;         // радиус² капли-следа от движения (толщина −20%: ×0.8²)
  var DYE_R_CLICK = 0.0006;            // радиус² ядра-капли по клику — компактнее, чтобы не было «диска»
  var DYE_AMT     = 0.3;               // насыщенность тёплой капли от движения (тоньше=прозрачнее)
  var DYE_CLICK   = 0.85;              // насыщенность ядра чернил по клику
  var BURST_N     = 17;                // сколько вихревых струй по клику (нечётно → нет зеркальной симметрии)
  var BURST_FORCE = 6.5;               // сила струй клика (раскручивает чернила в тендрилы)


  // --- шейдеры -------------------------------------------------------------
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

  // ДИФФУЗИЯ чернил: лёгкое расползание к соседям (как капля в воде растекается),
  // + общее дотаивание (fade). Работает и без поля скоростей, в покое.
  var DIFFUSE_FS = `#version 300 es
precision highp float; in vec2 vUv; in vec2 vL; in vec2 vR; in vec2 vT; in vec2 vB;
out vec4 o; uniform sampler2D uTex; uniform float spread; uniform float fade;
void main(){
  vec2 c = texture(uTex,vUv).xy;
  vec2 n = texture(uTex,vL).xy + texture(uTex,vR).xy
         + texture(uTex,vT).xy + texture(uTex,vB).xy;
  vec2 v = mix(c, n*0.25, spread);   // тянемся к среднему по соседям → размытие/расплыв
  o = vec4(v*fade, 0.0, 1.0);
}`;

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

  // ВЫВОД: наш прежний fbm-фон, смещённый НАКОПЛЕННЫМ полем (uDisp), плюс
  // настоящие ЧЕРНИЛА (uDye) — краска, которую несёт и закручивает течение.
  var DISP_FS = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 o;
uniform sampler2D uDisp; uniform sampler2D uDye; uniform vec2 res; uniform float t;
const vec3 BG    = vec3(0.984, 0.949, 0.945);
const vec3 ROSE  = vec3(0.713, 0.372, 0.435);
const vec3 BERRY = vec3(0.560, 0.290, 0.345);
const vec3 PEACH = vec3(0.886, 0.690, 0.541);
const vec3 LILAC = vec3(0.808, 0.588, 0.784);
const vec3 EMBER = vec3(1.0, 0.38, 0.0);   // выжигающий оранжевый — обводка следа
const vec3 EMBHI = vec3(1.0, 0.78, 0.22);  // светящееся ядро капли (делаем прозрачнее)
const vec3 INKBLK = vec3(0.05, 0.04, 0.06); // чёрные чернила по клику
float hash(vec2 p){ p = fract(p*vec2(123.34,456.21)); p += dot(p,p+45.32); return fract(p.x*p.y); }
float noise(vec2 p){
  vec2 i = floor(p), f = fract(p);
  float a = hash(i), b = hash(i+vec2(1.0,0.0));
  float c = hash(i+vec2(0.0,1.0)), d = hash(i+vec2(1.0,1.0));
  vec2 u = f*f*(3.0-2.0*f);
  return mix(mix(a,b,u.x), mix(c,d,u.x), u.y);
}
float fbm(vec2 p){ float s=0.0,a=0.5; for(int i=0;i<5;i++){ s+=a*noise(p); p*=2.02; a*=0.5; } return s; }
void main(){
  vec2 uv = vUv;
  uv.x *= res.x/res.y;
  float tt = t * 0.016;
  vec3 dsp = texture(uDisp, vUv).xyz;
  uv += dsp.xy;                          // накопленное смещение фона
  vec2 mo = vec2(sin(tt*0.7), cos(tt*0.6)) * 0.7;
  vec2 q = vec2(fbm(uv*1.6 + mo + vec2(0.0, tt)), fbm(uv*1.6 - mo + vec2(5.2, -tt)));
  vec2 r = vec2(fbm(uv*1.6 + 4.0*q + vec2(1.7, 9.2) + tt*0.9),
                fbm(uv*1.6 + 4.0*q + vec2(8.3, 2.8) - tt*0.9));
  float f = fbm(uv*1.6 + 4.3*r + 0.35*sin(tt));
  vec3 col = BG;
  col = mix(col, LILAC, smoothstep(0.42, 1.02, length(r)) * 0.42);
  col = mix(col, ROSE,  smoothstep(0.52, 1.10, f) * 0.62);
  col = mix(col, PEACH, smoothstep(0.45, 0.95, q.x*q.x) * 0.38);
  col = mix(col, BERRY, smoothstep(0.74, 1.12, f*1.1) * 0.5);
  col = mix(col, BG, smoothstep(0.55, 1.0, 1.0 - vUv.y*res.y/res.x) * 0.25);
  // чернила: тёплый след (R) = оранжевая обводка + светлое ядро (ядро намного
  // прозрачнее, чтоб не било в глаза). Чёрный впрыск (G) по клику — заметный.
  float warm = clamp(texture(uDye, vUv).x, 0.0, 1.4);
  float blk  = clamp(texture(uDye, vUv).y, 0.0, 2.0);
  col = mix(col, EMBER, smoothstep(0.015, 0.35, warm) * 0.45);   // оранжевая обводка (ощутимо прозрачнее)
  col = mix(col, EMBHI, smoothstep(0.45, 1.15, warm) * 0.10);    // светлое ядро — едва уловимое
  // чёрный клик-впрыск: рваный край через fbm → читается как чернила в воде, а не круг
  float inkEdge = 0.45 + 0.7 * fbm(uv*3.4 + 2.0*r);
  col = mix(col, INKBLK, smoothstep(0.02, 0.7, blk) * 0.62 * clamp(inkEdge, 0.0, 1.15));
  o = vec4(col, 1.0);
}`;

  // НАКОПЛЕНИЕ смещения. Затухание (persist) идёт КАЖДЫЙ кадр → след всегда
  // дотаивает и зарастает фоном, не «замерзает». Вклад скорости добавляем только
  // во время движения (inject=1) и по маске вдоль ОТРЕЗКА пути курсора (prev→ptr)
  // — получается непрерывный хвост-след, а не круг под точкой.
  // Канал z — оранжевая подкраска того же следа, тает быстрее (свой persist).
  var ACCUM_FS = `#version 300 es
precision highp float; in vec2 vUv; out vec4 o;
uniform sampler2D uDisp; uniform sampler2D uVelocity;
uniform vec2 ptr; uniform vec2 prev; uniform float maskR; uniform float aspect;
uniform float stepScale; uniform float persist; uniform float dispMax;
uniform float inject;
void main(){
  vec2 old = texture(uDisp, vUv).xy;
  vec2 p = vUv; p.x *= aspect;
  vec2 a = prev; a.x *= aspect;
  vec2 b = ptr;  b.x *= aspect;
  vec2 ab = b - a; float len2 = max(dot(ab,ab), 1e-7);
  float h = clamp(dot(p - a, ab) / len2, 0.0, 1.0);   // проекция на отрезок пути
  vec2 nearest = a + ab * h;
  vec2 dp = p - nearest;
  float mask = exp(-dot(dp,dp)/max(maskR,1e-4)) * inject;
  vec2 nd = old * persist + texture(uVelocity, vUv).xy * stepScale * mask;
  float L = length(nd);
  if (L > dispMax) nd *= dispMax / L;    // ограничиваем перекос, без «взрыва»
  o = vec4(nd, 0.0, 1.0);
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
    var u = {}, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (var i = 0; i < n; i++) { var nm = gl.getActiveUniform(p, i).name; u[nm] = gl.getUniformLocation(p, nm); }
    return { prog: p, u: u };
  }

  var progs = {
    clear: makeProg(BASE_VS, CLEAR_FS), splat: makeProg(BASE_VS, SPLAT_FS),
    adv: makeProg(BASE_VS, ADV_FS), div: makeProg(BASE_VS, DIV_FS),
    curl: makeProg(BASE_VS, CURL_FS), vort: makeProg(BASE_VS, VORT_FS),
    press: makeProg(BASE_VS, PRESS_FS), grad: makeProg(BASE_VS, GRAD_FS),
    disp: makeProg(BASE_VS, DISP_FS), accum: makeProg(BASE_VS, ACCUM_FS),
    diffuse: makeProg(BASE_VS, DIFFUSE_FS),
  };
  for (var k in progs) { if (!progs[k]) { teardown(); return; } }

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

  // --- буферы кадра (FBO): скорость, давление, смещение, чернила -----------
  var RG = gl.RG16F, R = gl.R16F;
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
    return { w: w, h: h, read: a, write: b,
      swap: function () { var t = this.read; this.read = this.write; this.write = t; } };
  }

  var LIN = LINEAR_OK ? gl.LINEAR : gl.NEAREST;
  var velocity = makeDouble(SIM_RES, SIM_RES, RG, gl.RG, LIN);
  var divergence = makeFBO(SIM_RES, SIM_RES, R, gl.RED, gl.NEAREST);
  var curlFbo = makeFBO(SIM_RES, SIM_RES, R, gl.RED, gl.NEAREST);
  var pressure = makeDouble(SIM_RES, SIM_RES, R, gl.RED, gl.NEAREST);
  // накопленное смещение фона (xy) — «расталкивание дыма». Своя сетка.
  var DISP_RES = SMALL ? 160 : 220;
  var disp = makeDouble(DISP_RES, DISP_RES, RG, gl.RG, LIN);
  // ЧЕРНИЛА (dye): два канала — R тёплый след от движения, G чёрный впрыск по клику.
  var dye = makeDouble(DYE_RES, DYE_RES, RG, gl.RG, LIN);

  // --- один шаг симуляции скорости (без краски) ---------------------------
  var simTexel = [1 / SIM_RES, 1 / SIM_RES];
  var dyeTexel = [1 / DYE_RES, 1 / DYE_RES];

  function step(dt) {
    gl.disable(gl.BLEND);

    // завихрённость (vorticity confinement) — только если включена; на грубой
    // сетке она шумит, поэтому по умолчанию CURL=0 и блок пропускается
    if (CURL > 0) {
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
    }

    var d = progs.div;
    gl.useProgram(d.prog);
    gl.uniform2f(d.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(d.u.uVelocity, velocity.read.attach(0));
    blit(divergence);

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

    var g = progs.grad;
    gl.useProgram(g.prog);
    gl.uniform2f(g.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(g.u.uPressure, pressure.read.attach(0));
    gl.uniform1i(g.u.uVelocity, velocity.read.attach(1));
    blit(velocity.write); velocity.swap();

    var a = progs.adv;
    gl.useProgram(a.prog);
    gl.uniform2f(a.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(a.u.uVelocity, velocity.read.attach(0));
    gl.uniform1i(a.u.uSource, velocity.read.attach(0));
    gl.uniform1f(a.u.dt, dt);
    gl.uniform1f(a.u.diss, VEL_DISS);
    blit(velocity.write); velocity.swap();

    // адвекция ЧЕРНИЛ тем же полем скоростей — краску несёт и закручивает.
    // texel = simTexel (скорость в единицах своей сетки); своё затухание DYE_DISS.
    var ad = progs.adv;
    gl.useProgram(ad.prog);
    gl.uniform2f(ad.u.texel, simTexel[0], simTexel[1]);
    gl.uniform1i(ad.u.uVelocity, velocity.read.attach(0));
    gl.uniform1i(ad.u.uSource, dye.read.attach(1));
    gl.uniform1f(ad.u.dt, dt);
    gl.uniform1f(ad.u.diss, DYE_DISS);
    blit(dye.write); dye.swap();
  }

  // толчок течения в точку (uv 0..1), направление dx/dy
  function splat(x, y, dx, dy) {
    var aspect = canvas.width / canvas.height;
    var s = progs.splat;
    gl.useProgram(s.prog);
    gl.uniform1f(s.u.aspect, aspect);
    gl.uniform2f(s.u.point, x, y);
    gl.uniform1f(s.u.radius, SPLAT_R);
    gl.uniform1i(s.u.uTarget, velocity.read.attach(0));
    gl.uniform3f(s.u.color, dx, dy, 0.0);
    blit(velocity.write); velocity.swap();
  }

  // капля ЧЕРНИЛ: добавляем плотность в каналы R (тёплый след) и/или G (чёрный клик).
  function dyeSplat(x, y, amtR, amtG, r) {
    var aspect = canvas.width / canvas.height;
    var s = progs.splat;
    gl.useProgram(s.prog);
    gl.uniform1f(s.u.aspect, aspect);
    gl.uniform2f(s.u.point, x, y);
    gl.uniform1f(s.u.radius, r);
    gl.uniform1i(s.u.uTarget, dye.read.attach(0));
    gl.uniform3f(s.u.color, amtR, amtG, 0.0);
    blit(dye.write); dye.swap();
  }

  // --- ввод курсора --------------------------------------------------------
  var pointer = { x: 0.5, y: 0.5, px: 0.5, py: 0.5, moved: false };
  var activeUntil = 0;     // до какого времени гоняем симуляцию после движения
  var decayUntil = 0;      // до какого времени крутим затухание накопл. поля
  var curlUntil = 0;       // до какого времени держим вихри включёнными (после клика)
  var clickX = 0, clickY = 0, clickPending = false;  // впрыск чернил по клику

  function onMove(cx, cy) {
    pointer.px = pointer.x; pointer.py = pointer.y;
    pointer.x = cx / window.innerWidth;
    pointer.y = 1.0 - cy / window.innerHeight;
    pointer.moved = true;
    play();
  }
  function onClick(cx, cy) {
    clickX = cx / window.innerWidth;
    clickY = 1.0 - cy / window.innerHeight;
    clickPending = true;
    activeUntil = (performance.now() - start) / 1000 + CLICK_HOLD;
    play();
  }
  if (!reduce) {
    window.addEventListener("mousemove", function (e) { onMove(e.clientX, e.clientY); }, { passive: true });
    window.addEventListener("mousedown", function (e) { onClick(e.clientX, e.clientY); }, { passive: true });
    window.addEventListener("touchmove", function (e) {
      var t = e.touches[0]; if (t) onMove(t.clientX, t.clientY);
    }, { passive: true });
    window.addEventListener("touchstart", function (e) {
      var t = e.touches[0];
      if (t) { pointer.x = t.clientX / window.innerWidth; pointer.y = 1 - t.clientY / window.innerHeight; pointer.px = pointer.x; pointer.py = pointer.y; onClick(t.clientX, t.clientY); }
    }, { passive: true });
  }

  // рендерим в пониженном разрешении: фон мягкий, детали не нужны
  var SCALE = 0.5;
  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    var w = Math.max(2, Math.floor(window.innerWidth * dpr * SCALE));
    var h = Math.max(2, Math.floor(window.innerHeight * dpr * SCALE));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  }

  // --- цикл ----------------------------------------------------------------
  var raf = 0, last = 0, start = performance.now();

  function render(now) {
    raf = 0;
    if (!last) last = now;
    var dt = Math.min((now - last) / 1000, 0.022);
    last = now;
    resize();
    var nowS = (now - start) / 1000;

    // ввод курсора → толчок течения; держим симуляцию активной ещё IDLE_HOLD сек.
    // prevX/Y — начало отрезка пути для непрерывного следа в accum.
    var inject = 0.0, prevX = pointer.x, prevY = pointer.y;
    if (pointer.moved) {
      var dx = (pointer.x - pointer.px) * FORCE;
      var dy = (pointer.y - pointer.py) * FORCE;
      splat(pointer.x, pointer.y, dx * dt, dy * dt);
      dyeSplat(pointer.x, pointer.y, DYE_AMT, 0.0, DYE_R_MOVE);   // тёплая капля (R) у курсора
      prevX = pointer.px; prevY = pointer.py;
      pointer.px = pointer.x; pointer.py = pointer.y;
      pointer.moved = false;
      inject = 1.0;
      activeUntil = nowS + IDLE_HOLD;
      decayUntil = nowS + DECAY_HOLD;
    }
    // клик = капля упала в воду. Чтобы это были ЧЕРНИЛА, а не диск:
    //  • плотное, но компактное ядро в центре;
    //  • венок из BURST_N НЕровных струй (нечётное число + джиттер угла/силы →
    //    нет зеркальной симметрии, нет ровного кольца);
    //  • вдоль каждой струи впрыскиваем НЕСКОЛЬКО капелек на разном радиусе —
    //    течение растаскивает их в вытянутые тендрилы (как тушь в воде);
    //  • на время клика включаем завихрённость (CLICK_CURL) → струи
    //    закручиваются спиралями, а не расходятся радиусами.
    if (clickPending) {
      var aspect = canvas.width / canvas.height;
      CURL = CLICK_CURL;                                       // включаем вихри на время клика
      dyeSplat(clickX, clickY, 0.0, DYE_CLICK, DYE_R_CLICK);   // компактное ядро чёрных чернил (G)
      for (var bi = 0; bi < BURST_N; bi++) {
        // угол с детерминированным джиттером (без Math.random — он запрещён в скриптах,
        // но здесь обычный браузер; всё равно держим воспроизводимость кадра)
        var ang = (bi / BURST_N) * 6.2832 + 0.9 * Math.sin(bi * 12.9898) + nowS * 0.15;
        var jit = 0.45 + 0.95 * (0.5 + 0.5 * Math.sin(bi * 78.233 + 1.7));  // неровная сила/длина спицы
        var ca = Math.cos(ang), sa = Math.sin(ang);
        // толкаем из нескольких точек по длине спицы → струя, а не точка
        for (var sj = 1; sj <= 3; sj++) {
          var rad = 0.006 * sj * (0.7 + 0.6 * jit);
          var ox = clickX + ca * rad / aspect, oy = clickY + sa * rad;
          splat(ox, oy, ca * BURST_FORCE * jit, sa * BURST_FORCE * jit);
          // капельки чернил вдоль струи, всё тоньше к концу — течение вытянет их в нить
          dyeSplat(ox, oy, 0.0, DYE_CLICK * (0.55 / sj), DYE_R_CLICK * (0.6 / sj));
        }
      }
      clickPending = false;
      activeUntil = Math.max(activeUntil, nowS + CLICK_HOLD);
      decayUntil = nowS + DECAY_HOLD;
      curlUntil = nowS + 1.2;                                  // вихри живут ~1.2с, потом снова спокойно
    }
    // вихри только короткое время после клика — в покое CURL=0 (иначе зерно по экрану)
    if (CURL > 0 && nowS >= curlUntil) CURL = 0;
    // жидкость + чернила считаем пока активна симуляция (дёшево). Накопительное
    // затухание (accum) и дотаивание чернил крутим дольше — пока след не зарастёт,
    // иначе деформация «замерзает» на полпути (это и были визуальные баги).
    if (nowS < activeUntil) step(dt);
    if (nowS < decayUntil) {
      var ac = progs.accum;
      gl.useProgram(ac.prog);
      gl.uniform1i(ac.u.uDisp, disp.read.attach(0));
      gl.uniform1i(ac.u.uVelocity, velocity.read.attach(1));
      gl.uniform2f(ac.u.ptr, pointer.x, pointer.y);
      gl.uniform2f(ac.u.prev, prevX, prevY);
      gl.uniform1f(ac.u.maskR, MASK_R);
      gl.uniform1f(ac.u.aspect, canvas.width / canvas.height);
      gl.uniform1f(ac.u.stepScale, STEP_SCALE);
      gl.uniform1f(ac.u.persist, PERSIST);
      gl.uniform1f(ac.u.dispMax, DISP_MAX);
      gl.uniform1f(ac.u.inject, inject);
      blit(disp.write); disp.swap();

      // расползание + дотаивание чернил: всегда (и в движении, и в покое), чтобы
      // и след, и клик-капля мягко растекались по воде, а не стояли чётким пятном.
      var df = progs.diffuse;
      gl.useProgram(df.prog);
      gl.uniform2f(df.u.texel, dyeTexel[0], dyeTexel[1]);
      gl.uniform1i(df.u.uTex, dye.read.attach(0));
      gl.uniform1f(df.u.spread, DYE_SPREAD);
      gl.uniform1f(df.u.fade, nowS >= activeUntil ? DYE_FADE : 1.0);
      blit(dye.write); dye.swap();
    }

    // вывод: наш fbm-фон, смещённый накопл. полем + настоящие чернила (uDye)
    gl.disable(gl.BLEND);
    var dp = progs.disp;
    gl.useProgram(dp.prog);
    gl.uniform2f(dp.u.res, canvas.width, canvas.height);
    gl.uniform1f(dp.u.t, nowS);
    gl.uniform1i(dp.u.uDisp, disp.read.attach(0));
    gl.uniform1i(dp.u.uDye, dye.read.attach(1));
    blit(null);

    // фон сам по себе анимирован всегда; даже без курсора крутим цикл,
    // чтобы fbm жил. (раньше при затухании жидкости цикл засыпал)
    if (!document.hidden) raf = requestAnimationFrame(render);
  }

  function play() { if (!raf && !document.hidden) { last = 0; raf = requestAnimationFrame(render); } }
  function pause() { if (raf) { cancelAnimationFrame(raf); raf = 0; } }

  function teardown() {
    pause();
    if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
  }

  resize();

  if (reduce) {
    // статичный «красивый» кадр без анимации и без жидкости (uDisp = 0)
    var dpr2 = progs.disp;
    gl.useProgram(dpr2.prog);
    gl.uniform2f(dpr2.u.res, canvas.width, canvas.height);
    gl.uniform1f(dpr2.u.t, 8.0);
    gl.uniform1i(dpr2.u.uDisp, disp.read.attach(0));
    gl.uniform1i(dpr2.u.uDye, dye.read.attach(1));   // пустые чернила на статичном кадре
    blit(null);
  } else {
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) pause(); else play();
    });
    window.addEventListener("pagehide", pause);
    play();
  }
})();
