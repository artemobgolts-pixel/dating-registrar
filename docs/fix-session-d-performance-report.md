# Session D — Performance Deep-Dive без визуальной регрессии

## A. Root cause

Авторитетный handoff — `docs/fix-session-d-report.md`. Исходная ветка `main`,
SHA `5e6884c571ba5fb46fbd69cdcbdbf800e75fc251`; `origin/main` совпадает. В начале
изменён только пользовательский `update.sh`: он не относится к этой работе,
не редактируется и не включается в commit. Исходный код сохранён в отдельном
detached checkout `tmp/session-d-performance-base`. CI исходного SHA остановился
на известном `test_guest_proposal_widget.py`; unchanged-SHA attempt 2 прошёл полный
release gate и disposable image drill. Продукт и тест ради него не менялись.
[CI исходного D, attempt 2](https://github.com/artemobgolts-pixel/dating-registrar/actions/runs/34629694410/attempts/2).

До изменения production code разобраны три traces предыдущего D, снят новый
подробный trace и создана таблица `artifacts/fix-d-deep-tools/bottleneck-table.md`.
Крупнейшая задача — браузерный `ScheduledActionSendBeginMainFrame`, а не выполнение
`scales()` или Ink на main thread. В инспекционном trace она занимает **414.684 ms**,
из них **395.836 ms Style/Layout**. Два Layout: **240.427 + 130.425 ms**.
Оба относятся к document root, без JS-инициатора. Между ними `computeIntersections`
увеличивает число layout objects с 245 до 462.

Основные причины разделены диагностикой на свежих browser processes с CPU×4:

- Первый расчёт системного шрифта/видимой разметки дорог. Предварительный рендер
  того же system-font текста в отдельном диагностическом документе уменьшил
  крупнейшую задачу с 424 до 171 ms. Это **не оптимизация продукта** и не методика
  итогового benchmark: такой prewarm не используется в before/after.
- Ранний расчёт невидимого содержимого `#possibilities` даёт значимый лишний расход.
  Его временное диагностическое исключение уменьшило крупнейшую задачу 424 → 273 ms.
  Исключение контента не сохранялось. Существующий `content-visibility:auto` на
  всей секции позволяет браузеру рано раскрыть сразу все её карточки.
- Отдельная JS-задача **155.262 ms**: `landing-story.js` EvaluateScript 74.250 ms
  плюс DOMContentLoaded 80.231 ms → ScrollTrigger `_refreshAll` 78.985 ms.
  Это другая задача; перенос всей сцены менял бы pin spacing и scroll mapping.
- Третья задача **51.853 ms**: load → `_refreshAll` 48.647 ms; Ink `onPageShow`
  занимает около 1.424 ms. Длинная инициализация Ink ~0.9 s относится к Worker,
  не к main-thread blocking. Runtime/shaders/качество эффекта не менялись.

LCP — текст `h2#product-preview-title` («Встреча собирается на ваших глазах»),
а не картинка. Web-font requests отсутствуют; используется неизменённый системный
font stack. Важного main-thread image decode в trace не обнаружено. Галерея уже
откладывает неактивный skin и третью фотографию; скрытые login logos остаются
исправленными в D. Дублирующие мелкие gallery/theme обновления не выбраны целью.

## B. Changes made

**Отклонён и полностью отменён CSS-кандидат:** `content-visibility:auto` и
`contain-intrinsic-size:auto 400px` на отдельных `.possibility-story`.
Диагностически largest task уменьшилась 435 → 267 ms, blocking 456 → 276 ms,
но поведение прокрутки изменилось. На 390 px первоначальный document height
8410 → 6948 px; после Ctrl+End фактическая высота уже 8410, но scrollY остаётся
6104 вместо 7566, footer ниже экрана. Фокус профиля тоже попадает за экран:
scrollY 5068 вместо 6120. На 1280 px Ctrl+End недокручивает 369 px.
Причина — неточная резервная высота карточек (на mobile `min-height` снят;
естественные высоты feed/profile/settings: 1614.31 / 536.75 / 679.16 px).
Ускорение с такой регрессией неприемлемо. Raw frames/screenshots:
`artifacts/fix-d-deep/containment-entry/`.

**Второй кандидат также отклонён и отменён:** отключение
ScrollTrigger auto-refresh по `DOMContentLoaded` только когда deferred init
видит уже разобранный DOM (`readyState === 'interactive'`). Сохранены `load`,
`visibilitychange`, `resize`, native queued pin refresh и callback шрифтов.
Три парных замера не дали существенного улучшения, cold blocking вырос на 7.5%.
Важная оговорка: vendor отменяет queued refresh после DCL refresh через счётчик;
поэтому отсутствие DCL callback само по себе не доказывает устранение работы.
Считаются суммарные refresh callbacks, а не только имя исчезнувшего события.

В `scripts/probe_session_d_performance.py` добавлены LCP element, paint/navigation
timings, font/Ink state, resource chronology и optional `--detailed-trace`.
Добавлен `scripts/analyze_frontend_trace.py`: clocks, long-task stacks и категории
без двойного счёта. Оба инструмента не загружаются сайтом.
**Финальные `app/`, templates, CSS, JS и media побайтно совпадают с Session D.**
Ни один неуспешный кандидат не включён в commit.

## C. Performance before/after

Оба варианта измерены одним инструментом: Chromium 149.0.7827.55, Python 3.12.3,
Playwright 1.61.0, Windows, 390×844, mobile/touch, DPR1, CPU×4, 150 ms latency,
1.6 Mbit/s download, 750 Kbit/s upload. Три чередующихся пары before/after;
cold и warm отдельно; browser tests не выполнялись параллельно. Local FastAPI с настоящими hashed
assets, без Caddy/compression/TLS. Cold friends/light; warm friends/dark после
одинакового реального переключения темы. Ни fonts, ни cache заранее не прогреваются.

Observed blocking — сумма `max(longtask.duration−50,0)` от navigation до load+8s,
не field INP и не стандартный Lighthouse TBT. Подробный tracing добавляет overhead;
его результаты сопоставляются только с таким же tracing у baseline.

Финальный frontend идентичен D: отдельного изменённого runtime «Final» нет.
Не приписываем исходному коду искусственную разницу измерений:

| Metric | Session D baseline, медиана [min–max] | Final | Delta |
| --- | ---: | --- | --- |
| Cold LCP | 2568 ms [2564–2760] | Тот же измеренный frontend D | — |
| Cold observed blocking | 456 ms [443–759] | Тот же frontend | — |
| Cold largest long task | 405 ms [390–560] | Тот же frontend | — |
| Cold long-task count | 2 [2–3] | Тот же frontend | — |
| Warm LCP / blocking | 360 ms [356–456] / 39 ms [35–40] | Тот же frontend | — |

Следующая таблица показывает **отклонённый scheduling-кандидат, не поставляемый
результат**. Это три независимых before/after пары; выбросы не исключались:

| Metric | D baseline | Кандидат (отменён) | Delta медиан |
| --- | ---: | ---: | ---: |
| Cold LCP | 2568 [2564–2760] ms | 2576 [2564–2696] ms | +8 ms |
| Cold blocking | 456 [443–759] ms | 490 [442–659] ms | +34 ms / +7.5% |
| Cold largest task | 405 [390–560] ms | 400 [400–529] ms | −5 ms / −1.2% |
| Cold task count | 2 [2–3] | 3 [3–4] | +1 |
| Warm LCP | 360 [356–456] ms | 416 [396–464] ms | +56 ms |
| Warm blocking | 39 [35–40] ms | 34 [23–42] ms | −5 ms |
| Warm largest task | 89 [85–90] ms | 84 [73–92] ms | −5 ms |
| Warm task count | 1 | 1 | 0 |
| Cold click → two frames | 78.7 [75.5–79.7] ms | 60.6 [59.8–70.1] ms | −18.1 ms |
| Warm click → two frames | 36.3 [35.7–42.5] ms | 39.3 [31.9–50.7] ms | +3 ms |
| Cold document resource bytes / requests | 629861 / 11 | 630124 / 11 | +263 bytes / 0 |
| Warm document resource transfer bytes | 0 | 0 | 0 |

Улучшение одного post-load click не компенсирует ухудшение целевого cold blocking.
Разброс велик, но ни largest task, ни LCP не дают основания сохранять изменение.
ResourceTiming здесь относится к document resources, не к импортам внутри Worker.
В проверенном cold run изображения галереи начинали загрузку после LCP:
примерно 2689 ms против LCP 2568 ms. Ink stats подтверждают `backend:worker`,
`mode:base`, один shader program, ноль интерактивных FBO/textures, `firstFrameReady:true`.

## D. Long-task breakdown

Все шесть rich traces разобраны; ниже D и **отменённый** scheduling-кандидат.
Категории вычисляются как
неперекрывающиеся интервалы вложенных trace slices: Script Evaluation, Function
Call, Style/Layout, Paint/Composite, Parse HTML, Image decode, GC, JS compile.
Неизмеренная работа и idle не выдаются за CPU cost.

| Startup metric | D, медиана [min–max] | Отменённый кандидат |
| --- | ---: | ---: |
| Layout count | 48 [47–50] | 57 [56–57] |
| Layout inclusive time | 391.6 [381.9–556.1] ms | 403.1 [396.1–514.6] ms |
| UpdateLayoutTree count | 125 [124–127] | 148 [147–148] |
| UpdateLayoutTree time | 99.3 [91.2–156.4] ms | 124.8 [107.9–160.7] ms |
| Script Evaluation, exclusive | 104.2 [76.3–144.9] ms | 89.1 [81.1–102.0] ms |
| Function Call, exclusive | 172.9 [158.7–288.9] ms | 201.5 [175.8–222.1] ms |
| Style/Layout, exclusive | 484.8 [483.5–715.2] ms | 530.1 [506.4–679.7] ms |
| Paint/Composite, exclusive | 159.3 [137.6–183.7] ms | 201.4 [125.5–216.7] ms |
| Parse HTML | 18.2 [16.7–18.6] ms | 17.4 [16.1–17.4] ms |
| JS compile/parse | 1.62 [1.12–2.17] ms | 1.59 [1.59–1.82] ms |
| GC | 1.65 [1.58–1.78] ms | 1.63 [1.58–1.76] ms |
| Main-thread image decode | Не обнаружен | Не обнаружен |
| Union callbacks с refresh | 120.4 [111.2–207.5] ms | 185.1 [149.0–213.5] ms |

Именованных `_refreshAll` стало 1 вместо 2, но deferred wrapper
`_queueRefreshAll → requestAnimationFrame → kt(true)` активировался:
0.020–0.198 ms в D → 38.989–58.471 ms у кандидата. Две дорогие ветки global refresh
сохранились; дополнительно выполнялся GSAP ticker
`zl → updateRoot → Te.update → Te.refresh → _getBounds`.
Union стоимости callbacks с refresh **вырос**, поэтому перенос части работы
за LCP не считается устранением cold blocking.

Все 17 main-thread long tasks (>50 ms), времена от navigation. Durations в этой
таблице — LongTask API; точные RunTask boundaries, вложенные slices и sampled stacks
сохранены отдельно в JSON. «Layout» здесь native renderer без установленного JS caller;
«init» — `landing-story.js` evaluation + lifecycle; «refresh» — указанные выше
ScrollTrigger/GSAP callbacks.

| Запуск | До LCP: native Layout | Пересекает LCP: init | После LCP: refresh |
| --- | --- | --- | --- |
| D-1 | 2072.1 ms → **405 ms** | 2514.8 → 138 ms | — |
| D-2 | 2070.6 → **390 ms** | 2515.0 → 166 ms | — |
| D-3 | 2086.7 → **560 ms** | 2714.4 → 272 ms | 3002.8 → 77 ms |
| candidate-1 | 2075.6 → **529 ms** | 2650.4 → 106 ms | 2756.9 → 153 ms; 2913.9 → 71 ms |
| candidate-2 | 2077.4 → **400 ms** | 2516.9 → 77 ms | 2594.3 → 115 ms |
| candidate-3 | 2080.7 → **400 ms** | 2522.2 → 90 ms | 2613.0 → 150 ms |

Задача около 400 ms никуда не исчезла. В отдельном инспекционном trace FP=LCP;
до этой точки категории составляли Style/Layout 418.2, Script Evaluation 70.2,
Function Call 1.2, Paint 14.9, Parse HTML 19.6 ms. После LCP до load+8s:
Style/Layout 85.5, Script Evaluation 14.5, Function Call 198.6, Paint 115.4 ms.
Это temporal breakdown одного trace, не медианы из таблицы выше.
Детали: `artifacts/fix-d-deep-tools/scheduling-startup-summary.json`.

Уточнение handoff D: прежние 57 Layout / 170 UpdateLayoutTree включали тестовый
theme click после окна загрузки. Здесь startup ограничен load+8s, interaction
рассматривается отдельно; числа разных окон не сравниваются напрямую.

## E. Visual verification

Исходный D зафиксирован: **208 snapshots, 88 PNG, page errors 0**. Матрица: 390×844 и
1280×844, friends/romantic, light/dark, все пять сцен с промежуточными и конечными
состояниями, reverse scroll, gallery, карточки «Возможности», typography, stacking,
декоративные слои, scroll/trigger positions. Screenshot baselines не обновлялись.
Измерения offscreen-потомков не раскрывали принудительно проверяемую границу;
содержимое карточки проверялось при появлении.

CSS-кандидат прошёл отдельное сравнение реальных кадров End / anchor / focus:
регрессия из раздела B подтверждена, дальнейшая полная матрица для него остановлена.
Scheduling-кандидат прошёл 3 существующих lifecycle/geometry tests, но не прошёл
performance acceptance. Подготовленный дополнительный pre-load/restore probe для
него **NOT RUN**, поскольку кандидат уже отклонён по метрикам. Не заявляется
полное визуальное одобрение неудачного кандидата. Для поставляемого результата
паритет подтверждён отсутствием любых изменений application assets/templates.

## F. Tests

- `test_landing_experience.py`: PASS, 21 tests.
- `test_landing_refresh_e2e.py`: PASS, 3 tests на scheduling-кандидате.
- Baseline visual capture: PASS, 208 состояний; CSS entry parity: FAIL, кандидат отменён.
- Три paired cold/warm замера: RUN, 12 навигаций, page errors 0; acceptance FAIL.
- Trace analyzer: PASS, новый CLI в точности воспроизвёл весь первоначальный разбор.
- Полный exact-SHA CI неизменённого frontend D: PASS, 72 модуля / 617 unittest tests,
  85 smoke blocks, 0 skips, retained image + disposable drill PASS (attempt 2).
- Firefox/WebKit не установлены; production и внешние интеграции не используются.
- Existing guest-proposal: FAIL в CI attempt 1, PASS в unchanged-SHA attempt 2.
  Не маскируется и не исправляется изменением постороннего поведения.
- Новый commit содержит только инструменты и отчёт; результат exact-SHA CI после
  push фиксируется в сообщении завершения.

## G. Remaining bottlenecks

Первый native font/layout cost видимой страницы остаётся. Полного устранения
этой нативной стоимости проведённые эксперименты не доказывают. Перенос видимого
Ink или всего story timeline ради цифры не выполнялся.
Не начиналась новая cleanup wave и не менялись A/B/C security/business/release flows.

## H. Verdict

**PERF-01 NO MATERIAL IMPROVEMENT.** Оставшиеся задержки локализованы;
сохранить безопасное ускорение по проведённым экспериментам не удалось.
Вариант с заметным выигрышем ломал навигацию, вариант изменения scheduling не
улучшил целевой cold blocking. Оба отменены. Это срабатывание заданных stop
conditions, а не заявление, что любые будущие оптимизации невозможны.

Воспроизведение: `scripts/probe_session_d_performance.py --runs 1 --detailed-trace
--source-root <checkout> --output <dir>`; затем `scripts/analyze_frontend_trace.py
<dir> --output <analysis.json>`. Для парных запусков чередовать checkout D и
кандидата минимум три раза. Raw local evidence остаётся в ignored
`artifacts/fix-d-deep/`; rejected patch и harnesses — `artifacts/fix-d-deep-tools/`.
Новые зависимости, production deployment, изменения A/B/C/D и пользовательского
`update.sh` не выполнялись. Commit + push в `main` разрешены пользователем.
