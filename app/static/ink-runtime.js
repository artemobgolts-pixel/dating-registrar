/* date4you ink runtime: общий DOM-free WebGL2-движок для окна и Worker.
 *
 * Базовый этап компилирует один общий vertex shader и один фоновый fragment
 * shader. Float FBO, жидкость и кляксы создаются только по явному запросу,
 * когда cursor_effects включён и доступен точный указатель.
 */
(function (root) {
  "use strict";

  var MAX_CLICKS = 24;

  function create(canvas, options) {
    options = options || {};
    var state = {
      width: 1, height: 1, dpr: 1,
      dark: false, friends: false,
      interactive: false, fine: false,
    };
    var nowFn = options.now || function () {
      return root.performance && root.performance.now
        ? root.performance.now() : Date.now();
    };
    var requestFrame = options.requestFrame ||
      (root.requestAnimationFrame && root.requestAnimationFrame.bind(root)) ||
      function (cb) { return root.setTimeout(function () { cb(nowFn()); }, 16); };
    var cancelFrame = options.cancelFrame ||
      (root.cancelAnimationFrame && root.cancelAnimationFrame.bind(root)) ||
      function (id) { root.clearTimeout(id); };
    var reportFatal = options.onFatal || function () {};
    var reportFirstFrame = options.onFirstFrame || function () {};
    var reportInteractive = options.onInteractiveReady || function () {};
    var reportStats = options.onStats || function () {};

    var diagnostics = {
      mode: "base",
      vertexShaders: 0,
      fragmentShaders: 0,
      programs: 0,
      framebuffers: 0,
      textures: 0,
      firstFrameReady: false,
      interactiveAvailable: null,
      qualitySource: "cadence",
      scale: 1,
      inputFlushes: 0,
      pathSegments: 0,
    };

    var gl = canvas.getContext("webgl2", {
      alpha: true, antialias: false, depth: false, stencil: false,
      premultipliedAlpha: false, powerPreference: "low-power",
      preserveDrawingBuffer: !!options.preserveDrawingBuffer,
    });
    if (!gl) {
      reportFatal("webgl2-unavailable");
      return null;
    }

    // Параметры исходной анимации/жидкости. Значения сохранены.
    var SIM_RES = 150;
    var DYE_RES = 280;
    var DISP_RES = 220;
    var ITER = 20;
    var CURL = 0;
    var CLICK_CURL = 0.5;
    var VEL_DISS = 1.1;
    var PRESS_DISS = 0.8;
    var SPLAT_R = 0.00045;
    var FORCE = 6000;
    var STEP_SCALE = -0.016;
    var PERSIST = 0.999;
    var DISP_MAX = 0.22;
    var MASK_R = 0.003033;
    var IDLE_HOLD = 2.6;
    var CLICK_HOLD = 7.0;
    var DECAY_HOLD = 22;
    var DYE_DISS = 0.25;
    var DYE_FADE = 0.985;
    var DYE_SPREAD = 0.05;
    var DYE_R_MOVE = 0.0000396;
    var DYE_AMT = 0.3;

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
uniform vec2 point; uniform vec2 pointPrev; uniform float radius;
void main(){
  vec2 p = vUv; p.x *= aspect;
  vec2 a = pointPrev; a.x *= aspect;
  vec2 b = point; b.x *= aspect;
  vec2 ab = b - a;
  float h = clamp(dot(p-a,ab)/max(dot(ab,ab),1e-7),0.0,1.0);
  vec2 delta = p - (a + ab*h);
  vec3 splat = exp(-dot(delta,delta)/radius) * color;
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
uniform float uDark; uniform float uFriends; uniform float uInteractive;
// Кольцевой буфер кликов: каждый клик — центр (xy в uv), возраст(с) и сид формы.
// Неактивные слоты держат uClickAge < 0 и пропускаются в цикле (тяжёлый fbm не считается).
#define MAX_CLICKS ${MAX_CLICKS}
uniform vec2  uClickPos[MAX_CLICKS];
uniform float uClickAge[MAX_CLICKS];
uniform float uClickSeed[MAX_CLICKS];
const vec3 BG_L    = vec3(0.984, 0.949, 0.945);
const vec3 ROSE_L  = vec3(0.713, 0.372, 0.435);
const vec3 BERRY_L = vec3(0.560, 0.290, 0.345);
const vec3 PEACH_L = vec3(0.886, 0.690, 0.541);
const vec3 LILAC_L = vec3(0.808, 0.588, 0.784);
const vec3 EMBER_L = vec3(1.0, 0.38, 0.0);
const vec3 EMBHI_L = vec3(1.0, 0.78, 0.22);
const vec3 INK_L   = vec3(0.05, 0.04, 0.06);
// Та же палитра в ночной экспозиции: без чистого чёрного и кислотных бликов.
const vec3 BG_D    = vec3(0.090, 0.071, 0.090);
const vec3 ROSE_D  = vec3(0.380, 0.205, 0.265);
const vec3 BERRY_D = vec3(0.245, 0.128, 0.185);
const vec3 PEACH_D = vec3(0.405, 0.285, 0.235);
const vec3 LILAC_D = vec3(0.300, 0.220, 0.335);
const vec3 EMBER_D = vec3(0.730, 0.390, 0.245);
const vec3 EMBHI_D = vec3(0.845, 0.615, 0.390);
const vec3 INK_D   = vec3(0.835, 0.665, 0.720);
// Дружеская экспозиция: ivory + indigo / teal / amber. Геометрия и движение
// остаются теми же, поэтому оба оформления узнаются как один date4you.
const vec3 F_BG_L    = vec3(0.965, 0.953, 0.914);
const vec3 F_ROSE_L  = vec3(0.333, 0.306, 0.682);
const vec3 F_BERRY_L = vec3(0.220, 0.200, 0.500);
const vec3 F_PEACH_L = vec3(0.827, 0.604, 0.196);
const vec3 F_LILAC_L = vec3(0.067, 0.545, 0.525);
const vec3 F_INK_L   = vec3(0.055, 0.060, 0.125);
const vec3 F_BG_D    = vec3(0.071, 0.082, 0.137);
const vec3 F_ROSE_D  = vec3(0.310, 0.285, 0.600);
const vec3 F_BERRY_D = vec3(0.170, 0.155, 0.370);
const vec3 F_PEACH_D = vec3(0.480, 0.330, 0.115);
const vec3 F_LILAC_D = vec3(0.055, 0.330, 0.320);
const vec3 F_INK_D   = vec3(0.620, 0.610, 0.875);
float hash(vec2 p){ p = fract(p*vec2(123.34,456.21)); p += dot(p,p+45.32); return fract(p.x*p.y); }
float noise(vec2 p){
  vec2 i = floor(p), f = fract(p);
  float a = hash(i), b = hash(i+vec2(1.0,0.0));
  float c = hash(i+vec2(0.0,1.0)), d = hash(i+vec2(1.0,1.0));
  vec2 u = f*f*(3.0-2.0*f);
  return mix(mix(a,b,u.x), mix(c,d,u.x), u.y);
}
float fbm(vec2 p){ float s=0.0,a=0.5; for(int i=0;i<5;i++){ s+=a*noise(p); p*=2.02; a*=0.5; } return s; }
// Гребневой («ridged») fbm: пики шума → тонкие яркие жилы. Им рисуем ВЕТВИ туши.
float rfbm(vec2 p){ float s=0.0,a=0.5; for(int i=0;i<5;i++){ float n=1.0-abs(2.0*noise(p)-1.0); s+=a*n*n; p*=2.03; a*=0.5; } return s; }

// Грануляция и слои меняют только финальный цвет — движение q/r/f неизменно.
vec3 pigmentTexture(vec3 col, vec3 bg, vec2 uv, vec2 q, vec2 r, float f){
  float ink = smoothstep(0.025, 0.34, distance(col, bg));
  float quietFriendsLight = uFriends * (1.0 - uDark);
  float strata = 1.0 - smoothstep(0.025, 0.125,
    abs(fract((f + q.y*0.22 - r.x*0.13)*7.0) - 0.5));
  float gran = noise(uv*92.0 + q*2.1)*0.64
             + noise(uv*231.0 - r*2.7)*0.36;
  float fiber = noise(vec2(uv.x*37.0 + r.y, uv.y*315.0));
  float granStrength = mix(1.0, 0.42, quietFriendsLight);
  float fiberStrength = mix(1.0, 0.25, quietFriendsLight);
  col *= 0.972 + (gran - 0.5)*0.1125*ink*granStrength;
  vec3 layered = mix(col*0.74 + bg*0.08,
                     col*1.18 + vec3(0.012), uDark);
  col = mix(col, layered, strata*ink*0.34*(1.0 - uFriends));
  col += vec3((fiber - 0.5)*0.022*ink*(1.0 + 0.5*uDark)*fiberStrength);
  vec2 sp = uv*255.0;
  vec2 cell = floor(sp);
  vec2 local = fract(sp) - 0.5;
  float mineral = step(mix(0.982, 0.994, quietFriendsLight), hash(cell + 11.7))
    * (1.0 - smoothstep(0.035, 0.19, length(local)));
  float mineralStrength = mix(0.90, 0.34, quietFriendsLight);
  col = mix(col, vec3(0.985,0.968,0.945), mineral*ink*mineralStrength);
  return clamp(col, 0.0, 1.0);
}

// Плотность туши в нормированной точке p (тело капли ≈ радиус 1) и сид формы.
// Возвращает vec2(dens, veins): dens — плотность чернил, veins — жилы (для оттенка).
// Вынесено отдельно, чтобы inkAt мог усреднить НЕСКОЛЬКО подточек на пиксель
// (суперсэмплинг) — иначе субпиксельный шум нитей идёт «лесенкой».
// warp (0..1) — насколько развёрнут доменный варп: 0 = округлая капля (как в
// момент падения), 1 = полная форма с тендрилами. inkAt поднимает его с возрастом,
// поэтому клякса не «вылупляется» сразу изогнутой, а распускается из круглого пятна.
// evo — медленный «дрейф» шумового поля во времени: тендрилы сами расползаются и
// переплетаются за жизнь кляксы, как настоящая тушь, без сноса внешним течением.
vec2 inkDens(vec2 p, float sd, float warp, float evo){
  // РАЗНООБРАЗИЕ между кляксами: сид задаёт свой поворот, вытянутость по оси и
  // силу ветвления — силуэты получаются разными, а не «штампованными». Поворот+
  // анизотропия деформируют само тело капли, амплитуда варпа — густоту нитей.
  float ca = cos(sd), sa = sin(sd);
  vec2 ap = mat2(ca, -sa, sa, ca) * p;              // поворот тела на угол сида
  // вытянутость и сдвиг ОГРАНИЧЕНЫ: центр около 1.0, мягкий разброс — клякса
  // всегда читается как пятно, не вырождается в тонкий диагональный штрих
  // (раньше stretch уезжал в ~0.32 → растяжение ~10:1, получался «мазок»).
  float stretch = 1.0 + 0.16 * sin(sd * 1.7);       // ~0.84..1.16 — лёгкий овал
  ap.x *= stretch; ap.y /= stretch;
  ap.x += ap.y * 0.16 * sin(sd * 2.3);              // лёгкий сдвиг (shear), не штрих
  float br = 0.62 + 0.85 * fract(sd * 0.618);        // шире разброс силы ветвления у нитей
  // три прохода доменного варпа: всё сильнее закручиваем координату — прямые
  // радиальные линии изгибаются в блуждающие нити-тендрилы (как тушь в воде).
  // evo сдвигает аргумент шума → форма медленно «течёт» сама по себе во времени.
  vec2 wp = ap;
  wp += warp * br * 0.60 * (vec2(fbm(ap*1.4 + sd + evo),        fbm(ap*1.4 + sd + 7.0 - evo))  - 0.5);
  wp += warp * br * 0.45 * (vec2(fbm(wp*3.0 - sd*1.2 + evo*1.6), fbm(wp*3.0 + sd + 3.0 - evo*1.6)) - 0.5);
  wp += warp * br * 0.26 * (vec2(fbm(wp*6.5 + sd*2.0 + evo*2.4), fbm(wp*6.5 - sd + 9.0 + evo*2.4)) - 0.5);
  float rr = length(wp);
  // ДВА конверта на одной искажённой координате:
  //   coreEnv — тугое плотное тело (тает к ~1.3 радиуса)
  //   webEnv  — далеко тянущееся поле, в нём живут ТОНКИЕ нити-тендрилы
  float coreEnv = 1.0 - smoothstep(0.0, 1.3, rr);
  float webEnv  = 1.0 - smoothstep(0.0, 3.0, rr);
  if (webEnv <= 0.0) return vec2(0.0);
  // нити: гребневой шум → сеть жил; высокая степень заостряет их в волоски.
  float veins = rfbm(wp*2.1 + sd*0.6 + evo);
  veins = pow(clamp(veins, 0.0, 1.0), 2.3);         // острее → тоньше нити
  float core = pow(coreEnv, 2.0);                   // плотная сердцевина
  float dens = max(core, webEnv * veins * 1.7);     // тело + ветвящиеся нити
  return vec2(clamp(dens, 0.0, 1.0), veins);
}

// Одна клякса туши в воде вокруг точки cc (в тех же координатах, что uv).
// Тушь = связное пятно с мягкими вытянутыми нитями, не крапинки и не дым.
vec3 inkAt(vec3 col, vec2 uv, vec2 dsp, vec2 cc, float age, float sd){
  if (age < 0.0 || age >= 6.5) return col;
  vec2 d = uv - cc;
  float grow = smoothstep(0.0, 0.35, age);          // распускается (вдвое быстрее)
  float fade = 1.0 - smoothstep(2.5, 6.5, age);     // потом тает
  float life = grow * fade;
  // reach = радиус капли. Кроме раскрытия (grow) добавляем медленное РАСПЛЫВАНИЕ
  // по возрасту (spread): тушь со временем сама растекается и разрастается в воде,
  // а не держит фиксированный размер. Растёт до конца жизни кляксы.
  float spread = 1.0 + 0.55 * smoothstep(0.0, 6.5, age);
  // случайный размер ±15% от кляксы к кляксе (детерминированно от сида).
  float szJit = 1.0 + 0.15 * sin(sd * 3.13 + 1.7);
  float reach = (0.0035 + 0.0037 * grow) * spread * szJit;
  // доменный варп разворачиваем с возрастом: капля падает округлой, и лишь
  // за ~0.9 с распускается в форму с тендрилами — не «вылупляется» изогнутой.
  float warp = smoothstep(0.05, 0.95, age);
  // медленный дрейф формы за жизнь кляксы: тендрилы расползаются сами, не резко.
  float evo = age * 0.18;
  float dist0 = length(d);
  if (life <= 0.001 || dist0 >= reach * 4.6) return col;
  vec2 p = d / reach;                               // нормируем: тело капли ≈ радиус 1
  // ВЛИЯНИЕ ТЕЧЕНИЯ: ведём/мнём кляксу полем курсора, как фон. Работаем уже в
  // НОРМИРОВАННОЙ координате (p), поэтому усиление измеряется в радиусах капли и
  // не зависит от reach — раньше dsp подмешивался в d и делился на крошечный reach,
  // давая снос в ~десятки радиусов (тот самый излом-«банан»). Сила растёт с grow:
  // свежая капля держит округлую форму, раскрывшаяся — сильно деформируется потоком.
  p += dsp * (5.0 * grow);
  // СУПЕРСЭМПЛИНГ: пятно мелкое, шум нитей мельче пикселя → берём 4 подточки
  // по диагонали экранного следа пикселя (fwidth) и усредняем. Это убирает
  // «лесенки» на тонких нитях, не размывая саму форму (в отличие от fwidth-кромки).
  vec2 hp = fwidth(p) * 0.5;
  vec2 dn = vec2(0.0);
  dn += inkDens(p + vec2(-hp.x*0.5, -hp.y*0.5), sd, warp, evo);
  dn += inkDens(p + vec2( hp.x*0.5, -hp.y*0.5), sd, warp, evo);
  dn += inkDens(p + vec2(-hp.x*0.5,  hp.y*0.5), sd, warp, evo);
  dn += inkDens(p + vec2( hp.x*0.5,  hp.y*0.5), sd, warp, evo);
  float dens = dn.x * 0.25 * life;
  float veins = dn.y * 0.25;
  if (dens <= 0.004) return col;
  float dd = smoothstep(0.10, 0.72, dens);          // густота → к центру плотнее
  float a  = smoothstep(0.02, 0.18, dens);          // мягкая, но не размытая кромка
  vec3 inkBase = mix(INK_L, INK_D, uDark);
  if (uFriends > 0.5) inkBase = mix(F_INK_L, F_INK_D, uDark);
  vec3 inkc = mix(inkBase + vec3(0.14,0.115,0.15), inkBase, dd);
  inkc += (veins - 0.5) * 0.05;                     // лёгкая мраморность в жилах
  return mix(col, inkc, clamp(a * (0.34 + 0.50 * dd), 0.0, 0.86));
}
void main(){
  vec2 uv = vUv;
  uv.x *= res.x/res.y;
  float tt = t * 0.016;
  vec3 dsp = texture(uDisp, vUv).xyz * uInteractive;
  vec2 inkUv = uv;                       // НЕсмещённая координата для туши:
                                         // тушь стоит на месте, как в статич. эталоне
  uv += dsp.xy;                          // накопленное смещение — только для ФОНА
  vec2 mo = vec2(sin(tt*0.7), cos(tt*0.6)) * 0.7;
  vec2 q = vec2(fbm(uv*1.6 + mo + vec2(0.0, tt)), fbm(uv*1.6 - mo + vec2(5.2, -tt)));
  vec2 r = vec2(fbm(uv*1.6 + 4.0*q + vec2(1.7, 9.2) + tt*0.9),
                fbm(uv*1.6 + 4.0*q + vec2(8.3, 2.8) - tt*0.9));
  float f = fbm(uv*1.6 + 4.3*r + 0.35*sin(tt));
  vec3 bg = mix(BG_L, BG_D, uDark);
  vec3 rose = mix(ROSE_L, ROSE_D, uDark);
  vec3 berry = mix(BERRY_L, BERRY_D, uDark);
  vec3 peach = mix(PEACH_L, PEACH_D, uDark);
  vec3 lilac = mix(LILAC_L, LILAC_D, uDark);
  if (uFriends > 0.5) {
    bg = mix(F_BG_L, F_BG_D, uDark);
    rose = mix(F_ROSE_L, F_ROSE_D, uDark);
    berry = mix(F_BERRY_L, F_BERRY_D, uDark);
    peach = mix(F_PEACH_L, F_PEACH_D, uDark);
    lilac = mix(F_LILAC_L, F_LILAC_D, uDark);
  }
  vec3 col = bg;
  col = mix(col, lilac, smoothstep(0.42, 1.02, length(r)) * 0.42);
  col = mix(col, rose,  smoothstep(0.52, 1.10, f) * 0.62);
  col = mix(col, peach, smoothstep(0.45, 0.95, q.x*q.x) * 0.38);
  col = mix(col, berry, smoothstep(0.74, 1.12, f*1.1) * 0.5);
  col = mix(col, bg, smoothstep(0.55, 1.0, 1.0 - vUv.y*res.y/res.x) * 0.25);
  col = pigmentTexture(col, bg, uv, q, r, f);
  // тёплый след курсора (R-канал dye): оранжевая обводка + едва заметное ядро
  float warm = clamp(texture(uDye, vUv).x, 0.0, 1.4) * uInteractive;
  col = mix(col, mix(EMBER_L, EMBER_D, uDark), smoothstep(0.015, 0.35, warm) * 0.45);
  col = mix(col, mix(EMBHI_L, EMBHI_D, uDark), smoothstep(0.45, 1.15, warm) * 0.10);
  // ЧЁРНЫЕ КЛИКИ → тушь в воде. Кольцо клик-слотов: каждый рисуется тонкими
  // ветвящимися нитями (inkAt). Новый клик НЕ стирает прежние — складываем все
  // активные. Неактивные (age<0) inkAt отбрасывает сразу, fbm для них не считает.
  if (uInteractive > 0.5) {
    for (int i = 0; i < MAX_CLICKS; i++) {
      vec2 cc = uClickPos[i]; cc.x *= res.x/res.y;
      col = inkAt(col, inkUv, dsp.xy, cc, uClickAge[i], uClickSeed[i]);
    }
  }
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


  // Лёгкий фон: та же геометрия и скорость, но без samplers/FBO/click-кода.
  var BASE_DISPLAY_FS = `#version 300 es
precision highp float;
in vec2 vUv; out vec4 o;
uniform vec2 res; uniform float t; uniform float uDark; uniform float uFriends;
const vec3 BG_L=vec3(0.984,0.949,0.945), ROSE_L=vec3(0.713,0.372,0.435);
const vec3 BERRY_L=vec3(0.560,0.290,0.345), PEACH_L=vec3(0.886,0.690,0.541);
const vec3 LILAC_L=vec3(0.808,0.588,0.784);
const vec3 BG_D=vec3(0.090,0.071,0.090), ROSE_D=vec3(0.380,0.205,0.265);
const vec3 BERRY_D=vec3(0.245,0.128,0.185), PEACH_D=vec3(0.405,0.285,0.235);
const vec3 LILAC_D=vec3(0.300,0.220,0.335);
const vec3 F_BG_L=vec3(0.965,0.953,0.914), F_ROSE_L=vec3(0.333,0.306,0.682);
const vec3 F_BERRY_L=vec3(0.220,0.200,0.500), F_PEACH_L=vec3(0.827,0.604,0.196);
const vec3 F_LILAC_L=vec3(0.067,0.545,0.525);
const vec3 F_BG_D=vec3(0.071,0.082,0.137), F_ROSE_D=vec3(0.310,0.285,0.600);
const vec3 F_BERRY_D=vec3(0.170,0.155,0.370), F_PEACH_D=vec3(0.480,0.330,0.115);
const vec3 F_LILAC_D=vec3(0.055,0.330,0.320);
float hash(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p);float a=hash(i),b=hash(i+vec2(1,0));
float c=hash(i+vec2(0,1)),d=hash(i+vec2(1,1));vec2 u=f*f*(3.0-2.0*f);
return mix(mix(a,b,u.x),mix(c,d,u.x),u.y);}
float fbm(vec2 p){float s=0.0,a=0.5;for(int i=0;i<5;i++){s+=a*noise(p);p*=2.02;a*=0.5;}return s;}
vec3 pigmentTexture(vec3 col,vec3 bg,vec2 uv,vec2 q,vec2 r,float f){
float ink=smoothstep(0.025,0.34,distance(col,bg));
float quietFriendsLight=uFriends*(1.0-uDark);
float strata=1.0-smoothstep(0.025,0.125,abs(fract((f+q.y*.22-r.x*.13)*7.0)-.5));
float gran=noise(uv*92.0+q*2.1)*.64+noise(uv*231.0-r*2.7)*.36;
float fiber=noise(vec2(uv.x*37.0+r.y,uv.y*315.0));
float granStrength=mix(1.0,.42,quietFriendsLight);
float fiberStrength=mix(1.0,.25,quietFriendsLight);
col*=.972+(gran-.5)*.1125*ink*granStrength;
vec3 layered=mix(col*.74+bg*.08,col*1.18+vec3(.012),uDark);
col=mix(col,layered,strata*ink*.34*(1.0-uFriends));
col+=vec3((fiber-.5)*.022*ink*(1.0+.5*uDark)*fiberStrength);
vec2 sp=uv*255.0,cell=floor(sp),local=fract(sp)-.5;
float mineral=step(mix(.982,.994,quietFriendsLight),hash(cell+11.7))
  *(1.0-smoothstep(.035,.19,length(local)));
float mineralStrength=mix(.90,.34,quietFriendsLight);
col=mix(col,vec3(.985,.968,.945),mineral*ink*mineralStrength);
return clamp(col,0.0,1.0);}
void main(){
vec2 uv=vUv;uv.x*=res.x/res.y;
float tt=t*0.016;
vec2 mo=vec2(sin(tt*.7),cos(tt*.6))*.7;
vec2 q=vec2(fbm(uv*1.6+mo+vec2(0,tt)),fbm(uv*1.6-mo+vec2(5.2,-tt)));
vec2 r=vec2(fbm(uv*1.6+4.0*q+vec2(1.7,9.2)+tt*.9),
            fbm(uv*1.6+4.0*q+vec2(8.3,2.8)-tt*.9));
float f=fbm(uv*1.6+4.3*r+.35*sin(tt));
vec3 bg=mix(BG_L,BG_D,uDark),rose=mix(ROSE_L,ROSE_D,uDark);
vec3 berry=mix(BERRY_L,BERRY_D,uDark),peach=mix(PEACH_L,PEACH_D,uDark);
vec3 lilac=mix(LILAC_L,LILAC_D,uDark);
if(uFriends>.5){bg=mix(F_BG_L,F_BG_D,uDark);rose=mix(F_ROSE_L,F_ROSE_D,uDark);
berry=mix(F_BERRY_L,F_BERRY_D,uDark);peach=mix(F_PEACH_L,F_PEACH_D,uDark);
lilac=mix(F_LILAC_L,F_LILAC_D,uDark);}
vec3 col=bg;
col=mix(col,lilac,smoothstep(.42,1.02,length(r))*.42);
col=mix(col,rose,smoothstep(.52,1.10,f)*.62);
col=mix(col,peach,smoothstep(.45,.95,q.x*q.x)*.38);
col=mix(col,berry,smoothstep(.74,1.12,f*1.1)*.5);
col=mix(col,bg,smoothstep(.55,1.0,1.0-vUv.y*res.y/res.x)*.25);
col=pigmentTexture(col,bg,uv,q,r,f);
o=vec4(col,1.0);
}`;

  var sharedVertex = null;
  var progs = {};
  var quad = null;
  var effectsReady = false;
  var effectsUnavailable = false;
  var velocity, divergence, curlFbo, pressure, disp, dye;
  var LINEAR_OK = false;
  var simTexel, dyeTexel;

  function debug(message) {
    if (options.debug && root.console) root.console.warn("[ink]", message);
  }
  function compile(type, source) {
    var shader = gl.createShader(type);
    if (type === gl.VERTEX_SHADER) diagnostics.vertexShaders += 1;
    else diagnostics.fragmentShaders += 1;
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      debug(gl.getShaderInfoLog(shader));
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }
  function makeProgram(fragmentSource) {
    var fragment = compile(gl.FRAGMENT_SHADER, fragmentSource);
    if (!fragment) return null;
    var program = gl.createProgram();
    diagnostics.programs += 1;
    gl.attachShader(program, sharedVertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      debug(gl.getProgramInfoLog(program));
      gl.deleteProgram(program);
      return null;
    }
    var uniforms = {};
    var count = gl.getProgramParameter(program, gl.ACTIVE_UNIFORMS);
    for (var i = 0; i < count; i++) {
      var name = gl.getActiveUniform(program, i).name;
      uniforms[name] = gl.getUniformLocation(program, name);
    }
    return {prog: program, u: uniforms};
  }
  function blit(target) {
    gl.bindFramebuffer(gl.FRAMEBUFFER, target ? target.fbo : null);
    if (target) gl.viewport(0, 0, target.w, target.h);
    else gl.viewport(0, 0, canvas.width, canvas.height);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  sharedVertex = compile(gl.VERTEX_SHADER, BASE_VS);
  if (!sharedVertex) {
    reportFatal("base-vertex-compile");
    return null;
  }
  progs.base = makeProgram(BASE_DISPLAY_FS);
  if (!progs.base) {
    reportFatal("base-fragment-compile");
    return null;
  }
  quad = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, quad);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,3,-1,-1,3]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

  function makeFBO(w, h, internal, format, filter) {
    var tex = gl.createTexture();
    diagnostics.textures += 1;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, internal, w, h, 0, format, gl.HALF_FLOAT, null);
    var fbo = gl.createFramebuffer();
    diagnostics.framebuffers += 1;
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {
      throw new Error("incomplete-fbo");
    }
    gl.viewport(0, 0, w, h);
    gl.clear(gl.COLOR_BUFFER_BIT);
    return {tex:tex,fbo:fbo,w:w,h:h,attach:function(n){
      gl.activeTexture(gl.TEXTURE0+n);gl.bindTexture(gl.TEXTURE_2D,tex);return n;
    }};
  }
  function makeDouble(w, h, internal, format, filter) {
    var a=makeFBO(w,h,internal,format,filter);
    var b=makeFBO(w,h,internal,format,filter);
    return {w:w,h:h,read:a,write:b,swap:function(){
      var tmp=this.read;this.read=this.write;this.write=tmp;
    }};
  }

  function effectsEligible() {
    return !!(state.interactive && state.fine);
  }
  function ensureInteractive() {
    if (effectsReady) return true;
    if (effectsUnavailable || !effectsEligible()) return false;
    var colorFloat = gl.getExtension("EXT_color_buffer_float");
    if (!colorFloat) {
      effectsUnavailable = true;
      diagnostics.interactiveAvailable = false;
      reportInteractive(false);
      return false;
    }
    LINEAR_OK = !!gl.getExtension("OES_texture_float_linear");
    var optional = {
      clear:CLEAR_FS,splat:SPLAT_FS,adv:ADV_FS,div:DIV_FS,curl:CURL_FS,
      vort:VORT_FS,press:PRESS_FS,grad:GRAD_FS,disp:DISP_FS,
      accum:ACCUM_FS,diffuse:DIFFUSE_FS,
    };
    var made = {};
    for (var key in optional) {
      made[key] = makeProgram(optional[key]);
      if (!made[key]) {
        effectsUnavailable = true;
        diagnostics.interactiveAvailable = false;
        reportInteractive(false);
        return false;
      }
    }
    try {
      var small = Math.min(state.width, state.height) < 700;
      SIM_RES = small ? 110 : 150;
      DYE_RES = small ? 200 : 280;
      DISP_RES = small ? 160 : 220;
      var linear = LINEAR_OK ? gl.LINEAR : gl.NEAREST;
      velocity=makeDouble(SIM_RES,SIM_RES,gl.RG16F,gl.RG,linear);
      divergence=makeFBO(SIM_RES,SIM_RES,gl.R16F,gl.RED,gl.NEAREST);
      curlFbo=makeFBO(SIM_RES,SIM_RES,gl.R16F,gl.RED,gl.NEAREST);
      pressure=makeDouble(SIM_RES,SIM_RES,gl.R16F,gl.RED,gl.NEAREST);
      disp=makeDouble(DISP_RES,DISP_RES,gl.RG16F,gl.RG,linear);
      dye=makeDouble(DYE_RES,DYE_RES,gl.RG16F,gl.RG,linear);
    } catch (error) {
      debug(error && error.message);
      effectsUnavailable = true;
      diagnostics.interactiveAvailable = false;
      reportInteractive(false);
      return false;
    }
    for (var name in made) progs[name] = made[name];
    simTexel=[1/SIM_RES,1/SIM_RES];
    dyeTexel=[1/DYE_RES,1/DYE_RES];
    effectsReady=true;
    diagnostics.mode="interactive";
    diagnostics.interactiveAvailable=true;
    reportInteractive(true);
    reportStats(snapshot());
    return true;
  }

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
  function splat(x, y, px, py, dx, dy) {
    var aspect = canvas.width / canvas.height;
    var s = progs.splat;
    gl.useProgram(s.prog);
    gl.uniform1f(s.u.aspect, aspect);
    gl.uniform2f(s.u.point, x, y);
    gl.uniform2f(s.u.pointPrev, px, py);
    gl.uniform1f(s.u.radius, SPLAT_R);
    gl.uniform1i(s.u.uTarget, velocity.read.attach(0));
    gl.uniform3f(s.u.color, dx, dy, 0.0);
    blit(velocity.write); velocity.swap();
  }

  // капля ЧЕРНИЛ: добавляем плотность в каналы R (тёплый след) и/или G (чёрный клик).
  function dyeSplat(x, y, px, py, amtR, amtG, r) {
    var aspect = canvas.width / canvas.height;
    var s = progs.splat;
    gl.useProgram(s.prog);
    gl.uniform1f(s.u.aspect, aspect);
    gl.uniform2f(s.u.point, x, y);
    gl.uniform2f(s.u.pointPrev, px, py);
    gl.uniform1f(s.u.radius, r);
    gl.uniform1i(s.u.uTarget, dye.read.attach(0));
    gl.uniform3f(s.u.color, amtR, amtG, 0.0);
    blit(dye.write); dye.swap();
  }


  var pointer={x:.5,y:.5,px:.5,py:.5,moved:false};
  var activeUntil=0,decayUntil=0,curlUntil=0;
  var clickQueue=[];
  var clickPos=new Float32Array(MAX_CLICKS*2);
  var clickAge=new Float32Array(MAX_CLICKS);
  var clickSeed=new Float32Array(MAX_CLICKS);
  var clickStart=new Float32Array(MAX_CLICKS);
  var clickSlot=0,seedRot=0;
  for(var ci=0;ci<MAX_CLICKS;ci++) clickAge[ci]=-1;

  var SCALE=1,MIN_SCALE=.72,MAX_SCALE=1,slowFrames=0,fastFrames=0;
  var COARSE_PIXEL_FLOOR=600000;
  var SLOW_SAMPLE_LIMIT=12,FAST_SAMPLE_LIMIT=90;
  var SCALE_DOWN=.88,SCALE_UP=.06;
  var FRAME_MS=1000/30,EARLY=1;
  var raf=0,running=false,last=0,nextFrameAt=0,start=nowFn(),lastDrawAt=0;
  var timerExt=gl.getExtension("EXT_disjoint_timer_query_webgl2");
  var timerPending=[],timerCurrent=null,timerWaitFrames=0;
  diagnostics.qualitySource=timerExt?"gpu-timer":"cadence";

  function snapshot(){
    return {
      mode:diagnostics.mode,
      vertexShaders:diagnostics.vertexShaders,
      fragmentShaders:diagnostics.fragmentShaders,
      programs:diagnostics.programs,
      framebuffers:diagnostics.framebuffers,
      textures:diagnostics.textures,
      firstFrameReady:diagnostics.firstFrameReady,
      interactiveAvailable:diagnostics.interactiveAvailable,
      qualitySource:diagnostics.qualitySource,
      scale:SCALE,
      inputFlushes:diagnostics.inputFlushes,
      pathSegments:diagnostics.pathSegments,
    };
  }
  function updateScaleBounds(){
    var dpr=Math.min(state.dpr||1,2);
    var coarseBase=!state.fine;
    MIN_SCALE=.72;
    MAX_SCALE=1;
    if(coarseBase){
      var nativePixels=Math.max(1,(state.width||1)*(state.height||1)*dpr*dpr);
      // Стартуем в прежнем полном разрешении. До этого floor опускаемся
      // только после подтверждённых медленных GPU/cadence-сэмплов.
      MIN_SCALE=Math.max(.5,Math.min(.72,
        Math.sqrt(COARSE_PIXEL_FLOOR/nativePixels)));
    }
    if(!diagnostics.firstFrameReady||SCALE>MAX_SCALE)SCALE=MAX_SCALE;
    if(SCALE<MIN_SCALE)SCALE=MIN_SCALE;
    diagnostics.scale=SCALE;
  }
  function resize(){
    var dpr=Math.min(state.dpr||1,2);
    var w=Math.max(2,Math.floor((state.width||1)*dpr*SCALE));
    var h=Math.max(2,Math.floor((state.height||1)*dpr*SCALE));
    if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
  }
  function baseDraw(nowS){
    var base=progs.base;
    gl.disable(gl.BLEND);
    gl.useProgram(base.prog);
    gl.uniform2f(base.u.res,canvas.width,canvas.height);
    gl.uniform1f(base.u.t,nowS);
    gl.uniform1f(base.u.uDark,state.dark?1:0);
    gl.uniform1f(base.u.uFriends,state.friends?1:0);
    blit(null);
  }
  function effectDraw(nowS,dt){
    var interactive=effectsReady&&effectsEligible();
    var inject=0,prevX=pointer.x,prevY=pointer.y;
    if(interactive&&pointer.moved){
      var dx=(pointer.x-pointer.px)*FORCE;
      var dy=(pointer.y-pointer.py)*FORCE;
      splat(pointer.x,pointer.y,pointer.px,pointer.py,dx*dt,dy*dt);
      dyeSplat(pointer.x,pointer.y,pointer.px,pointer.py,DYE_AMT,0,DYE_R_MOVE);
      diagnostics.pathSegments+=1;
      reportStats(snapshot());
      prevX=pointer.px;prevY=pointer.py;
      pointer.px=pointer.x;pointer.py=pointer.y;pointer.moved=false;
      inject=1;activeUntil=nowS+IDLE_HOLD;decayUntil=nowS+DECAY_HOLD;
    }
    while(interactive&&clickQueue.length){
      var click=clickQueue.shift();
      clickPos[clickSlot*2]=click.x;clickPos[clickSlot*2+1]=click.y;
      clickStart[clickSlot]=nowS;
      seedRot=click.seed==null
        ?(seedRot+2.39996+Math.random()*3)%6.2832:Number(click.seed);
      clickSeed[clickSlot]=seedRot;
      clickSlot=(clickSlot+1)%MAX_CLICKS;
      activeUntil=Math.max(activeUntil,nowS+CLICK_HOLD);
      decayUntil=nowS+DECAY_HOLD;curlUntil=nowS+2.6;CURL=CLICK_CURL;
    }
    if(CURL>0&&nowS>=curlUntil)CURL=0;
    if(nowS<activeUntil)step(dt);
    if(nowS<decayUntil){
      var ac=progs.accum;
      gl.useProgram(ac.prog);
      gl.uniform1i(ac.u.uDisp,disp.read.attach(0));
      gl.uniform1i(ac.u.uVelocity,velocity.read.attach(1));
      gl.uniform2f(ac.u.ptr,pointer.x,pointer.y);
      gl.uniform2f(ac.u.prev,prevX,prevY);
      gl.uniform1f(ac.u.maskR,MASK_R);
      gl.uniform1f(ac.u.aspect,canvas.width/canvas.height);
      gl.uniform1f(ac.u.stepScale,STEP_SCALE);
      gl.uniform1f(ac.u.persist,PERSIST);
      gl.uniform1f(ac.u.dispMax,DISP_MAX);
      gl.uniform1f(ac.u.inject,inject);
      blit(disp.write);disp.swap();
      var df=progs.diffuse;
      gl.useProgram(df.prog);
      gl.uniform2f(df.u.texel,dyeTexel[0],dyeTexel[1]);
      gl.uniform1i(df.u.uTex,dye.read.attach(0));
      gl.uniform1f(df.u.spread,DYE_SPREAD);
      gl.uniform1f(df.u.fade,nowS>=activeUntil?DYE_FADE:1);
      blit(dye.write);dye.swap();
    }
    gl.disable(gl.BLEND);
    var dp=progs.disp;
    gl.useProgram(dp.prog);
    gl.uniform2f(dp.u.res,canvas.width,canvas.height);
    gl.uniform1f(dp.u.t,nowS);
    gl.uniform1f(dp.u.uDark,state.dark?1:0);
    gl.uniform1f(dp.u.uFriends,state.friends?1:0);
    gl.uniform1f(dp.u.uInteractive,interactive?1:0);
    gl.uniform1i(dp.u.uDisp,disp.read.attach(0));
    gl.uniform1i(dp.u.uDye,dye.read.attach(1));
    for(var s=0;s<MAX_CLICKS;s++){
      clickAge[s]=clickStart[s]>0?nowS-clickStart[s]:-1;
    }
    gl.uniform2fv(dp.u["uClickPos[0]"],clickPos);
    gl.uniform1fv(dp.u["uClickAge[0]"],clickAge);
    gl.uniform1fv(dp.u["uClickSeed[0]"],clickSeed);
    blit(null);
  }
  function beginTimer(){
    if(!timerExt||timerCurrent||timerPending.length>=4)return;
    try{
      timerCurrent=gl.createQuery();
      gl.beginQuery(timerExt.TIME_ELAPSED_EXT,timerCurrent);
    }catch(_){timerCurrent=null;timerExt=null;diagnostics.qualitySource="cadence";}
  }
  function endTimer(){
    if(!timerExt||!timerCurrent)return;
    gl.endQuery(timerExt.TIME_ELAPSED_EXT);
    timerPending.push(timerCurrent);timerCurrent=null;
  }
  function qualitySample(ms){
    if(ms>22){slowFrames++;fastFrames=0;}
    else if(ms<11){fastFrames++;slowFrames=Math.max(0,slowFrames-1);}
    else{slowFrames=Math.max(0,slowFrames-1);fastFrames=Math.max(0,fastFrames-1);}
    if(slowFrames>=SLOW_SAMPLE_LIMIT&&SCALE>MIN_SCALE){
      SCALE=Math.max(MIN_SCALE,SCALE*SCALE_DOWN);slowFrames=0;nextFrameAt=0;
    }else if(fastFrames>=FAST_SAMPLE_LIMIT&&SCALE<MAX_SCALE){
      SCALE=Math.min(MAX_SCALE,SCALE+SCALE_UP);fastFrames=0;nextFrameAt=0;
    }
    diagnostics.scale=SCALE;
  }
  function pollTimers(){
    if(!timerExt||!timerPending.length)return;
    var query=timerPending[0];
    var available=gl.getQueryParameter(query,gl.QUERY_RESULT_AVAILABLE);
    var disjoint=gl.getParameter(timerExt.GPU_DISJOINT_EXT);
    if(!available&&!disjoint){
      timerWaitFrames+=1;
      // Некоторые драйверы объявляют extension, но никогда не возвращают
      // результат. В этом случае не зависаем без адаптации — переходим к cadence.
      if(timerWaitFrames<120)return;
      while(timerPending.length)gl.deleteQuery(timerPending.shift());
      timerExt=null;timerCurrent=null;
      diagnostics.qualitySource="cadence";
      return;
    }
    timerWaitFrames=0;
    timerPending.shift();
    if(available&&!disjoint)qualitySample(gl.getQueryParameter(query,gl.QUERY_RESULT)/1000000);
    gl.deleteQuery(query);
  }
  function sampleCadence(now){
    if(timerExt||!lastDrawAt)return;
    var interval=now-lastDrawAt;
    if(interval>45){slowFrames++;fastFrames=0;}
    else if(interval<38){fastFrames++;slowFrames=Math.max(0,slowFrames-1);}
    else{slowFrames=Math.max(0,slowFrames-1);fastFrames=Math.max(0,fastFrames-1);}
    if(slowFrames>=SLOW_SAMPLE_LIMIT&&SCALE>MIN_SCALE){
      SCALE=Math.max(MIN_SCALE,SCALE*SCALE_DOWN);slowFrames=0;nextFrameAt=0;
    }else if(fastFrames>=FAST_SAMPLE_LIMIT&&SCALE<MAX_SCALE){
      SCALE=Math.min(MAX_SCALE,SCALE+SCALE_UP);fastFrames=0;nextFrameAt=0;
    }
    diagnostics.scale=SCALE;
  }
  function resetQualityEvidence(){
    slowFrames=0;fastFrames=0;lastDrawAt=0;timerWaitFrames=0;
    while(timerPending.length)gl.deleteQuery(timerPending.shift());
  }
  function render(now){
    raf=0;
    if(!running)return;
    if(nextFrameAt&&now+EARLY<nextFrameAt){raf=requestFrame(render);return;}
    if(!nextFrameAt)nextFrameAt=now;
    do{nextFrameAt+=FRAME_MS;}while(nextFrameAt<=now);
    if(!last)last=now;
    var dt=Math.min((now-last)/1000,.022);
    if(!isFinite(dt)||dt<=0)dt=.016;
    resize();
    sampleCadence(now);
    pollTimers();
    beginTimer();
    var nowS=(now-start)/1000;
    if(effectsReady&&effectsEligible())effectDraw(nowS,dt);else baseDraw(nowS);
    endTimer();
    last=now;lastDrawAt=now;
    if(!diagnostics.firstFrameReady){
      // Однократно подтверждаем, что первый кадр действительно закончен GPU.
      gl.finish();
      diagnostics.firstFrameReady=true;
      reportFirstFrame(snapshot());
      reportStats(snapshot());
    }
    if(running)raf=requestFrame(render);
  }
  function startLoop(){
    if(running)return;
    resetQualityEvidence();
    running=true;last=0;nextFrameAt=0;
    raf=requestFrame(render);
  }
  function pause(){
    running=false;
    if(raf){cancelFrame(raf);raf=0;}
    resetQualityEvidence();
  }
  function setState(next){
    next=next||{};
    var qualityBoundary=false;
    for(var key in state)if(next[key]!==undefined){
      if((key==="width"||key==="height"||key==="dpr"||key==="fine")&&
          state[key]!==next[key])qualityBoundary=true;
      state[key]=next[key];
    }
    if(qualityBoundary)resetQualityEvidence();
    if(!effectsEligible()){pointer.moved=false;clickQueue.length=0;}
    updateScaleBounds();
  }
  function input(payload){
    if(!effectsEligible())return false;
    if(!ensureInteractive())return false;
    payload=payload||{};
    diagnostics.inputFlushes+=1;
    if(payload.move){
      var move=payload.move;
      pointer.px=move.px==null?pointer.x:move.px;
      pointer.py=move.py==null?pointer.y:move.py;
      pointer.x=move.x;pointer.y=move.y;pointer.moved=true;
    }
    var clicks=payload.clicks||[];
    for(var i=0;i<clicks.length;i++)clickQueue.push(clicks[i]);
    reportStats(snapshot());
    return true;
  }
  function renderTest(clicks,bgTime){
    state.interactive=true;state.fine=true;
    if(!ensureInteractive())throw new Error("interactive pipeline unavailable");
    resize();
    clicks=clicks||[];
    for(var i=0;i<MAX_CLICKS;i++){
      var c=clicks[i];
      clickPos[i*2]=c?c.x:0;clickPos[i*2+1]=c?c.y:0;
      clickAge[i]=c?c.age:-1;clickSeed[i]=c?c.seed:0;
    }
    var d=progs.disp;
    gl.disable(gl.BLEND);gl.useProgram(d.prog);
    gl.uniform2f(d.u.res,canvas.width,canvas.height);
    gl.uniform1f(d.u.t,bgTime==null?8:bgTime);
    gl.uniform1f(d.u.uDark,state.dark?1:0);
    gl.uniform1f(d.u.uFriends,state.friends?1:0);
    gl.uniform1f(d.u.uInteractive,1);
    gl.uniform1i(d.u.uDisp,disp.read.attach(0));
    gl.uniform1i(d.u.uDye,dye.read.attach(1));
    gl.uniform2fv(d.u["uClickPos[0]"],clickPos);
    gl.uniform1fv(d.u["uClickAge[0]"],clickAge);
    gl.uniform1fv(d.u["uClickSeed[0]"],clickSeed);
    blit(null);gl.finish();
  }
  function destroy(){
    pause();
    while(timerPending.length)gl.deleteQuery(timerPending.pop());
    for(var key in progs)if(progs[key]&&progs[key].prog)gl.deleteProgram(progs[key].prog);
    if(sharedVertex)gl.deleteShader(sharedVertex);
    if(quad)gl.deleteBuffer(quad);
  }

  resize();
  return {
    start:startLoop,
    pause:pause,
    setState:setState,
    input:input,
    ensureInteractive:ensureInteractive,
    renderTest:renderTest,
    destroy:destroy,
    stats:snapshot,
  };
  }

  root.D4YInkRuntime={create:create};
})(typeof self!=="undefined"?self:this);
