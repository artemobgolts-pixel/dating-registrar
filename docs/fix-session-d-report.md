# Fix Session D — UI, Accessibility, Mobile Performance & Cleanup

## 1. Итог этапа

Выполнены точечные изменения интерфейса, доступности, загрузки логотипов и cleanup.
Уменьшена повторная работа геометрии лендинга; заметное улучшение общей холодной
загрузки **не подтверждено**. Полная готовность к beta не заявляется.

- Репозиторий: `E:/Storage/Projects/dating-registrar/boris-site`.
- Начальная ветка: `main`; HEAD и обновлённый `origin/main`:
  `35816ad6f059513ede284e39b72e07f25fb7e811`. Начальный `git status --porcelain` пуст.
- Авторитетный контекст: `../fix-session-a-report.md`, `docs/fix-session-b-report.md`,
  `docs/fix-session-c-report.md`; границы: `../04_fix_session_D_ui_performance_cleanup.md`.
- На старте точный SHA имел красный CI в существующем guest-proposal browser test.
  Повтор **без изменения исходников**, attempt 2, прошёл весь release gate и
  disposable image drill. Только после этого начаты изменения Session D.
  [CI исходного SHA](https://github.com/artemobgolts-pixel/dating-registrar/actions/runs/34592794237/attempts/2).
- Windows, Python 3.12.3 в `tmp/phase-c-py312`, Playwright 1.61.0,
  Chromium 149.0.7827.55; Git Bash/sh и локальный ffmpeg. Также существует `.venv`
  Python 3.14, но итоговые проверки выполнялись Python 3.12.
- Firefox/WebKit не установлены: **NOT RUN**. Docker/Linux локально недоступны;
  новый exact-SHA Linux/image gate выполняется GitHub Actions после push.
- Изменения шли в порядке UI-01 → UI-02 → UI-03 → A11Y-01 → PERF-01 → PERF-02 → cleanup.
- Во время работы в общем checkout появилась посторонняя правка `update.sh`,
  заменяющая проверенную процедуру Phase C. Она оставлена пользователю, исключена
  из Session D и commit. Полный gate перенесён в `tmp/session-d-verification`:
  detached checkout исходного SHA с копией только файлов Session D и исходным
  `update.sh`. Первый gate со смешанным checkout не считается итоговой проверкой.
- Производственные данные, `.env`, реальные Telegram/OAuth/S3 и deployment не
  использовались. Схема, registry сессий, upload limits, восстановление черновиков,
  bulk/feed/review/quota и release/backup-процедуры A/B/C сохранены.
- Commit + push в `main` прямо разрешены пользователем. Итоговый SHA и результат
  CI этого SHA указываются в сообщении завершения; отчёт входит в тот же commit.

## 2. Статус findings

| Finding | Статус | Результат |
| --- | --- | --- |
| UI-01 | **FIXED + VERIFIED** | Grid/flex и native selects ограничены собственным контейнером; короткие/200-символьные названия, шесть ширин, QR/share проверены. |
| UI-02 | **FIXED + VERIFIED** | Имя в отдельном flex child с `overflow-wrap:anywhere`; выбранный, невыбранный, disabled/locked и keyboard states сохранены. |
| UI-03 | **FIXED + VERIFIED** | Перенос 200-символьного заголовка публичной карточки; меню и доступность voting controls сохранены; оба skin. |
| A11Y-01 | **FIXED + VERIFIED** | Видимые labels связаны с profile/dashboard controls, фильтры имеют имена назначения, gender group назван. AX, targeted axe и keyboard/autosubmit PASS. |
| PERF-01 | **FIXED BUT NOT FULLY VERIFIED** | Повторные geometry reads и refresh сокращены, lifecycle/resize проверены. Снижение общего cold blocking/LCP не доказано; длинные задачи остаются. |
| PERF-02 | **FIXED + VERIFIED** | Закрытый диалог: 383 459 → 0 байт логотипов. При открытии загружается только активный skin; оба skin, light/dark, focus/close проверены. |
| DEAD-01 | **NO FURTHER ACTION** | Phase A уже удалила ordinary-user export и связанные единственные helpers/UI. |
| TEST-02 | **NO FURTHER ACTION** | Действуют negative ordinary-user tests и положительные operator export tests из A. |
| DEAD-02 | **FIXED + VERIFIED** | Удалены `editorPreview`, его export и приватные `RU_MONTHS`, `fmtPoint`, `fmtWhen`, `hostOf` из `ui.js`. |
| DEAD-03 | **FIXED + VERIFIED** | Удалены неиспользуемые `set_guest_cookie`, `require_name` и ставшие лишними imports. Legacy reads сохранены. |
| DEAD-04 | **DEFERRED** | `og-default.jpg` используется home/about; исторические `og-friends.jpg`/`og.png` сохранены из-за неизвестных внешних URL. |
| DEAD-05 | **DEFERRED** | Старые `landing-video.js` и оба `landing-brand-loop*.mp4` не участвуют в актуальной home; сохранены для возможных старых URL/HTML. |
| DEAD-06 | **FIXED + VERIFIED** | Обновлены устаревшие route/auth/test notes в `CLAUDE.md`, описание legacy guests и комментарий про бывшую video-сцену. |
| A11Y-02 | **DEFERRED** | Необязательная коррекция контраста не выполнялась; цветовые токены не менялись. |

## 3. UI before/after

В `/admin/` select теперь ограничен шириной своей колонки, grid children могут
сжиматься, а мобильная колонка использует `minmax(0,1fr)`. В `/admin/dates` контейнер
фильтров и selects ограничены по ширине; flex basis не зависит от длинного option.
На узком экране фильтры переносятся. Глобальный `overflow-x:clip` не добавлялся:
тест проверяет реальные bounding boxes компонентов, а не только `scrollWidth` страницы.
У native select внутренний `scrollWidth` текста option не равен ширине самого control;
декоративные слои QR сохраняют намеренное clipping.

В editor текст collection chip помещён в `.chip-cat-name`. Он переносится внутри
ограниченного flex item; checkbox, selected styling, lock icon, disabled state,
hidden value закрытой подборки и Space/Tab продолжают работать. У публичной
`.card .title` переносится длинное слово; media/decorative overflow не изменён.

`Имя`, `Дата рождения` и dashboard `Подборка` используют `label[for]`/уникальный `id`.
Сортировка, фильтр событий и подборка в списке получили `aria-label` с назначением.
`Пол` именует radio group; оформление подписи сохранено. Нажатие видимых labels,
keyboard focus и autosubmit проверены через реальный локальный HTTP backend.

Геометрия проверена на 320/390/430/768/1280/1440 px, editor и public card — на
390/1280 px. Отдельно выполнена touch/mobile emulation 390×844 с обоими skin и темами.
Это выборочная regression matrix, не полный browser audit или WCAG certification.

## 4. Performance before/after

Использовался локальный synthetic FastAPI с настоящими hashed assets. Без Caddy,
HTTP compression и TLS: это воспроизводимый frontend lab, а не production network.
Chromium 149, viewport 390×844, mobile/touch, DPR 1, CPU×4, latency 150 ms,
download 1.6 Mbit/s, upload 750 Kbit/s. Окно: navigation → load + 8 s.
Blocking — сумма `max(longTask.duration - 50,0)` в этом окне, **не field INP**
и не заявление о стандартном Lighthouse TBT.

Первичный baseline: cold LCP 2536 ms, blocking 395 ms; warm 384/35 ms (медианы 3).
После последовательных экспериментов выполнен более строгий финальный paired run:
оригинальный `git archive` исходного SHA и кандидат чередовались три раза,
по cold + warm на каждый вариант. Все cold runs имели CDP trace; фоновый полный
test suite во время измерений не выполнялся. Cold — friends/light, warm —
friends/dark после предыдущего реального переключения темы; состояния одинаковы
между before/after внутри каждого режима. Между cold и warm метрики не смешиваются.

| Метрика, медиана | Before | After | Интерпретация |
| --- | ---: | ---: | --- |
| Cold LCP | 2544 ms | 2564 ms | Улучшение не подтверждено. Диапазоны 2540–2552 / 2552–2572. |
| Cold observed blocking | 411 ms | 424 ms | Общая блокировка остаётся. Диапазоны 409–423 / 412–425. |
| Cold максимальная long task | 392 ms | 405 ms | По две long tasks в каждом запуске. |
| Warm LCP | 412 ms | 412 ms | Диапазоны 340–452 / 388–440. |
| Warm observed blocking | 32 ms | 31 ms | Малая разница, не доказательство пользовательского ускорения. |
| Cold click → два animation frames | 34.8 ms | 58.3 ms | Диапазоны 34–65.5 / 57–58.9; улучшение responsiveness не заявляется. |
| Warm click → два animation frames | 30.6 ms | 37.2 ms | Диапазоны 30.3–34.6 / 29.9–39.7. |
| Layout events в trace | 73 | 57 | Меньше повторных layout passes. |
| UpdateLayoutTree events | 238 | 170 | Меньше style recalculation passes. |
| UpdateLayoutTree суммарно | 193.4 ms | 153.7 ms | Уменьшена измеренная работа стилей. |
| Refresh + story callbacks, суммарно | 134.8 ms | 92.8 ms | Примерно −31%; это часть startup work, не вся блокировка. |

`scales()` и narrative placement используют общую геометрию внутри одного refresh;
`onRefreshInit` сбрасывает caches. Удалены дополнительные startup/resize refresh,
дублировавшие штатный ScrollTrigger lifecycle. Незавершённая загрузка шрифтов
сохраняет отдельный refresh, а callback старой уничтоженной сцены игнорируется.
Ink runtime/worker, saveData и weak-device policies не переписывались.
Пять сцен, gallery, повторные refresh, native resize, fonts readiness,
reduced-motion и Turbo disposal проверены. Полного устранения PERF-01 эти результаты
не доказывают; оставшийся cold bottleneck не приписывается целиком GSAP/`scales()`.

Для login на cold `/c/session-d-0` до изменения загружались оба PNG:
172 395 + 211 064 байт. После — **0 requests** при закрытом dialog; при первом
открытии friends загружается только 211 064 байта. Диалог появляется примерно
за 52 ms в обоих вариантах; готовность логотипа после открытия — 62.6 → 1385.4 ms
при указанном throttling. Это явный обмен фонового трафика на отложенную загрузку
декоративной картинки; текст и способы входа доступны сразу. Размер logo slot
сохранён. Переключение skin загружает второй логотип только при необходимости.

Raw evidence: ignored `artifacts/fix-d-perf/paired-*/`, `paired-summary.json`,
`artifacts/fix-d-ui/before-login/`, `after-login/`. Каждый performance JSON содержит
browser version, параметры, resource sizes, long tasks и hash `landing-story.js`.
Небольшие изменения комментариев между измерениями меняли content hash, но не runtime.
Воспроизведение: `scripts/probe_session_d_performance.py --output <dir>`;
для baseline — `--source-root <extracted-original-source>`. Axe/AX/network probe:
`scripts/probe_session_d_accessibility.py --output <dir> --axe <local-axe.min.js>`.
Использовался axe-core 4.10.3 из npm tarball, только в ignored artifacts, без изменения
production/test dependencies; SHA-256 `axe.min.js`:
`880970c081707360e64f34cea25ff91892f5bc95675b0776925b9709dd8a68bb`.

## 5. Cleanup

Поиск включал app/templates/frontend, Python imports, tests и docs. У удалённого
`editorPreview` не было caller; четыре JS formatters использовались только им.
Сохранены `escapeHTML`, `renderMarkup`, `richEditor` и действующие `data-preview`
pay hooks редактора. Серверный `helpers.RU_MONTHS` не затронут.
В `test_price_scroll_feed_regressions.py` убрана только проверка исходника
удалённого `UI.editorPreview`: серверные значения, шаблоны, оба действующих
редактора (`admin.js`, `guest.js`) и цвет бесплатного события проверяются как прежде.

`get_guest`, `get_guest_name`, `gname` сохранены: `public_routes.py` использует
legacy cookie для claim, `admin_routes.py` — имя старого автора предложения.
Ordinary-user export endpoint/UI отсутствуют; operator CSV/JSON/platform ZIP и
совместимый operator alias остаются. Новые маршруты экспорта не создавались.

Ни один asset не удалён. `og-default.jpg` живой; отсутствие внутренних references
на другие исторические OG/video файлы не доказывает отсутствие внешних потребителей.
`CLAUDE.md` больше не направляет все правки в `main.py` и не описывает старый
guest-name/412 flow как актуальный вход.

## 6. Tests

Локальный полный developer gate — **FAIL**, `release_qualified=false`:
существующий `test_guest_proposal_widget.py` теряет fixture после отложенного
reload и не находит `#propDlg`/`#propBar` либо `#voteCta`. Тот же тест отдельно
воспроизведён с **неизменённым git archive исходного SHA**; это не регрессия D.
Последний receipt: ignored
`artifacts/fix-d-gate-final/35816ad6f059-developer-bxfcg1gj/receipt.json`.
Предыдущие неуспешные receipts также сохранены.

Чтобы остановка gate не оставила остальные модули непроверенными, они выполнены
отдельно из изолированного snapshot с тем же `safe_environment` и `_module` runner.
Инвентарь: **72 модуля, 617 unittest tests, 85 smoke blocks, 0 skips**.
После исправления двух D-related test failures и адресного повторного запуска:
**71 модуль PASS, один существующий guest-proposal модуль FAIL**; smoke PASS.
Это совокупное покрытие модулей, а не PASS полного release gate. Raw results:
`artifacts/fix-d-gate-isolated/35816ad6f059-developer-ylc7yzfi/remaining-modules.json`
и `artifacts/fix-d-final-targeted/results.json`.

| Проверка | Результат |
| --- | --- |
| `test_session_d_ui.py` | **PASS**, 6 browser tests с subcases ширин/состояний |
| `test_landing_refresh_e2e.py` | **PASS**, 3 lifecycle/geometry browser tests |
| `test_landing_experience.py` | **PASS**, 21 existing contract tests |
| `test_price_scroll_feed_regressions.py` | **PASS**, 6 tests; ожидание удалённого preview исключено, рабочие редакторы сохранены |
| Existing ink/runtime, A/B/C, smoke | **PASS**, кроме явно указанного guest-proposal module; ink screenshot baseline сохранён |
| Полный локальный developer gate | **FAIL**, existing guest-proposal fixture; все остальные модули проверены отдельно |
| Exact pushed SHA Linux/image CI | Выполняется после push; итог и ссылка фиксируются в сообщении завершения |
| Targeted axe + AX | **PASS**, 5 поверхностей; violations 0, incomplete 0 для выбранных naming/ARIA rules |
| Performance | **RUN**, 3+3 исходных, два промежуточных варианта и финальные 3 paired cold/warm before/after; page errors 0 |
| JS syntax / `git diff --check` | **PASS** |
| Firefox/WebKit / real screen reader / production | **NOT RUN** |

Первые новые UI fixtures исправлены: связи создаются до заморозки категории,
SQL setup явно commit-ится, editor использует `/edit`, accessible name проверяется
через AX (декоративная required mark исключена). Geometry assertion различает
native option text и намеренно обрезанный QR decor от размеров control.
Сравнение landing resize/fresh сначала попадало в отложенный setup refresh и
промежуточные кадры CSS smooth scroll. Тест ждёт штатного lifecycle и задаёт
`behavior:'instant'` при позиционировании сцены. После этого три browser tests
прошли. Отдельное before/after сравнение всех пяти сцен × двух skin × 390/1280 px
дало нулевую разницу bounding boxes. Независимый проход со сменой skin до первого
просмотра последующих сцен также дал нулевую разницу.
Неуспешные промежуточные попытки не выдаются за PASS. Существующие поведенческие
тесты A/B/C сохранены; удалено только ожидание текста в мёртвом preview-коде,
описанное выше. Screenshot baseline ink не менялся.

## 7. Remaining risks before final beta verification

1. PERF-01 остаётся частично подтверждённым: локальная работа refresh уменьшилась,
   но общий cold blocking/LCP и post-load responsiveness заметно не улучшились.
   Нужна независимая оценка на целевом устройстве/production-like asset serving.
2. Firefox/WebKit и настоящий screen reader не проверены. Targeted axe не заменяет
   ручную проверку доступности всего продукта; P3 contrast не закрывался.
3. Исторические OG/video assets намеренно сохранены. Для их удаления нужны данные
   о внешних URL/cache consumers.
4. Существующий guest-proposal browser test уже требовал unchanged-SHA CI rerun;
   его нестабильность не скрывается и не устранялась за пределами Session D.
5. Ограничения реального remote restore, production volumes/TLS и launch-процедур
   из отчёта C сохраняются. Эта сессия не выполняла production deployment.
6. Посторонняя локальная правка `update.sh` не проверена и не входит в результат.
   Pushed Session D сохраняет исходную Phase C процедуру.

## 8. Ready for independent GO/NO-GO verification?

**YES — для независимой проверки данного набора изменений**, с явно открытым
ограничением PERF-01 и перечисленными coverage gaps. Это не GO для запуска beta:
решение по общей производительности и оставшимся launch risks должен принять
следующий независимый этап. Финальный GO/NO-GO audit здесь не выполнялся.
