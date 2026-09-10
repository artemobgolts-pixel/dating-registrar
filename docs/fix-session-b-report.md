# Fix Session B — Broken User Flows & Regression Coverage

## 1. Итог этапа

**Session B завершена: все шесть findings — FIXED + VERIFIED.**

- Репозиторий: `E:/Storage/Projects/dating-registrar/boris-site`.
- Начальная ветка: `main`; SHA: `3b48bb2c583cb2b8766ec4b1b596b2b5cd8305b0`.
- Начальный `git status --short`: пусто. Изменения Session A присутствуют в этом коммите; отчёт: `docs/fix-session-a-report.md`.
- Родительский каталог является отдельным Git-репозиторием; его изменения не входят в Session B.
- Пользователь явно разрешил commit и push в `main`.
- Проверки выполняются только с временными SQLite, синтетическими аккаунтами и локальным backend. Внешние интеграции отключены.
- Проверена рабочая версия Session B поверх указанного SHA. Итоговый SHA commit/push указан в сообщении о завершении; этот отчёт входит в тот же коммит.
- Findings выполнялись последовательно: `FLOW-01 → TEST-01 → FLOW-02 → FLOW-06 → FLOW-04 → FLOW-05`.
- Итоговые результаты: **62 модуля, 528 unittest/regression tests, 85 smoke-блоков и отдельный screenshot regression чернил — PASS**. Один существующий browser test потребовал отдельного повторного запуска; подробности ниже. Test skips: **0**.
- Production data, рабочий `.env`, реальные интеграции и deployment не затрагивались.

## 2. Статус findings

| Finding | Статус | Проверка |
| --- | --- | --- |
| FLOW-01 | FIXED + VERIFIED | Все пять bulk actions, ownership/CSRF, дедупликация, повторы, пользовательский/IP limiter, rollback lifecycle-ошибки. |
| TEST-01 | FIXED + VERIFIED | 11 integration tests через реальный registry и HTTP middleware. Удаление только правила `datebulk` в отдельном процессе вызывает пять ожидаемых HTTP 500. Прежний narrow test с mock сохранён. |
| FLOW-02 | FIXED + VERIFIED | 9 integration tests: decline/retry/publish, очередь и отмена reminders, invalid/expired context, session/CSRF, два limiter buckets, native feedback и fetch errors. |
| FLOW-06 | FIXED + VERIFIED | 11 regressions: single/helper/bulk restore, атомарный отказ, квота владельца при operator action, exemptions, create/clone/shared copy и конкурирующие запросы. |
| FLOW-04 | FIXED + VERIFIED | 8 regressions всех числовых компонентов legacy/c1/r1/s1; границы int64, malformed и сверхдлинные значения, ranked/search pagination без потерь и дублей. |
| FLOW-05 | FIXED + VERIFIED | 6 Chromium → HTTP → SQLite tests: 500, network abort, initial failure, visible retry, сохранённый cursor, observer recovery и запоздавшие ответы при смене feed/search. |

## 3. Изменённые файлы

| Файл | Изменение и назначение |
| --- | --- |
| `app/ratelimit.py` | Настоящие правила `datebulk` и `review-decline`; limiter не обходится. |
| `app/admin_routes.py` | Общая quota check под SQLite writer lock; single/helper/bulk restore и предварительный расчёт всей selection; документирована семантика повторов. |
| `app/public_routes.py` | Предполагаемое восстановление считается через тот же predicate, что и текущая personal quota. |
| `app/main.py` | Узкий native review-decline error handler возвращает на страницу события с сообщением; fetch сохраняет HTTP status/JSON. |
| `app/community_feed.py` | Общий `_cursor_int` ограничивает числовые поля до SQLite binding и соблюдает более узкие offset limits. |
| `app/static/admin.js` | Явные feed states; сохранение sentinel/cursor при сбое, retry, остановка/возобновление observer, защита от старых responses/callbacks. |
| `app/templates/admin/dashboard.html` | Минимальные error message и retry button, независимо от наличия поиска. |
| `app/templates/public/share.html` | Экранированный native result с `role=status/alert`; прежде `msg` здесь не отображался. |
| `tests/test_bulk_http.py` | Реальные session/CSRF/limiter/endpoint/DB checks пяти bulk actions. |
| `tests/test_review_decline_http.py` | Review queue, reminders, retry/publish и защищённые error paths. |
| `tests/test_restore_quota_http.py` | Quota boundaries, atomicity, exceptions, ownership и конкурирующие HTTP mutations. |
| `tests/test_community_cursor_http.py` | Numeric bounds, malformed cursors, неизменность DB и pagination. |
| `tests/test_community_feed_e2e.py` | Browser failure/retry и смена feed/search с настоящими HTML responses и SQLite. |
| `docs/fix-session-b-report.md` | Результаты и handoff. |

Схема БД, migrations, dependencies и прежние тестовые модули не изменялись.

## 4. Behavioral verification

- Bulk archive/restore/privacy возвращают количество реально изменённых строк; повторное действие — `0`. Повторный delete отклоняет весь запрос при отсутствующей записи. Чужие ID и lifecycle-ошибка позднего элемента не оставляют частичных изменений в dates, bookings, review queue или outbox.
- Лимит bulk: 40 запросов пользователя за 3600 секунд, 120 на IP; до 100 уникальных событий в запросе. Ошибка возвращает пользователя в интерфейс с сообщением, после истечения окна операция снова доступна.
- Review decline: `review_queue.reason='declined'`, `dismissed_at=NULL`; matching review reminders получают `cancelled_at` и `last_error='review_declined'`. Дата и планы сохраняются; чужие reminders не изменяются. Повтор не создаёт дубликатов. Позднейшая публикация создаёт review и закрывает waiting queue.
- Review decline limiter: 10 действий участника и 30 на IP за 600 секунд. Native form показывает сообщение на странице события; fetch сохраняет HTTP 429 с JSON detail. Просроченная session, недоступный review и нарушение CSRF не меняют состояние.

### Restore quota

- Считаются только личные активные события: `owner_id`, `archived_at IS NULL`, `origin='admin'`, `source_date_id IS NULL`.
- При свободной квоте single/bulk restore проходят; на лимите или при превышении остатка показывается понятное сообщение. Ни counted, ни exempt события из отклонённого смешанного batch не восстанавливаются частично.
- Guest proposals и copies/shared imports остаются исключениями. Их restore и no-op restore разрешены даже после снижения квоты ниже текущего использования.
- Оператор использует квоту владельца события. Прямой вызов `_bulk_set_archived` не обходит правило. Ownership и CSRF сохраняются.
- Create/clone используют прежнюю продуктовую квоту; обычный clone считается личным событием, «Добавить себе» из share-ссылки — исключением.
- До исправления конкурирующие restore/restore, restore/create и restore/clone оставляли два активных личных события при лимите один. После исправления все три сценария оставляют **ровно одно**: count/check/update выполняются под SQLite writer lock до commit/rollback.

### Cursor parsing

- Общий parser обслуживает legacy numeric cursor, `c1.before_id/last_owner`, `r1.max_id/offset`, `s1.max_id/offset`. Timestamp сохраняет строгий datetime parser; search signature остаётся привязанной к запросу.
- Положительные ID ограничены `2^63−1`; отрицательные значения, включая `−2^63`, не соответствуют формату ID и безопасно сбрасывают pagination. Проверены обе int64 boundaries и непосредственно соседние значения.
- Проверены 10 000 цифр, большой Python integer, malformed composite, неверные даты/signatures, offset boundaries и superscript digits. Преобразование очень длинной строки в `int` не выполняется.
- Валидные форматы сохраняются. После invalid cursor начинается первая страница; дальнейшая pagination возвращает ожидаемые ID без потерь и дублей. GET не меняет DB.

### Feed failure/retry

- Состояния `data-feed-state`: `idle`, `loading`, `error`, `end`. Ошибка не показывает ложный end/empty state; `aria-busy` снимается.
- При 500/network abort сохраняются cards и исходный sentinel/cursor. Observer отключается до retry; кнопка загружает **точно тот же URL и cursor**.
- После recovery observer продолжает загрузку до конца: все 37 synthetic cards присутствуют ровно один раз. Search возвращает все 25 matching cards ровно один раз.
- Initial-page network failure тоже имеет retry. Ошибка поиска сохраняет query/cursor.
- Задержанный normal response не добавляется к search, а задержанная search error не портит normal feed после сброса поиска. Для этих проверок transport намеренно игнорирует abort, чтобы проверить generation guard; HTML responses получены с локального backend.
- Проверены desktop Friends и mobile Romantic; mobile retry отдельно пройден в dark theme. Полные DB snapshots после browser flows совпадают с исходными.

## 5. Tests

Каждый модуль запущен отдельным процессом, как в CI: временные DATA_DIR и module caches не разделяются. Окружение: Windows, Python 3.14, `.venv/Scripts/python.exe`, установленный Playwright Chromium, `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`; shell fixtures используют Git Bash.

| Команда / группа | Результат |
| --- | --- |
| `.venv/Scripts/python.exe tests/test_bulk_http.py` | PASS — 11 |
| `.venv/Scripts/python.exe tests/test_review_decline_http.py` | PASS — 9 |
| `.venv/Scripts/python.exe tests/test_restore_quota_http.py` | PASS — 11 |
| `.venv/Scripts/python.exe tests/test_community_cursor_http.py` | PASS — 8 |
| `.venv/Scripts/python.exe tests/test_community_feed_e2e.py` | PASS — 6 |
| `test_community_feed_e2e.py CommunityFeedBrowserTests.test_network_abort_recovers_on_mobile_romantic` после явного включения dark theme | PASS — 1 targeted rerun |
| Все 60 unittest-модулей `tests/test_*.py` | PASS — 528 tests, включая 45 новых Session B |
| `.venv/Scripts/python.exe tests/test_smoke.py` | PASS — 85 блоков |
| `.venv/Scripts/python.exe tests/test_ink.py` | PASS — screenshot regression, средняя Δ=0.122 при лимите 2.0 |
| `node --check app/static/admin.js`, Python compile изменённых модулей, `git diff --check` | PASS |
| Test SKIP | 0; неожиданных skips нет |
| Production, реальные интеграции, deployment; Firefox/WebKit; remote Linux CI до push | NOT RUN — за пределами локального Session B gate |

В полном прогоне прошли существующие auth/access/upload/session, profile/editor E2E, review/voting, UI/geometry/theme и backup fixtures. Логи и сводка: `artifacts/fix-b-gate/` (игнорируются Git).

Неуспешные попытки и их разрешение:

- **Baseline FAIL → PASS:** bulk request воспроизвёл `KeyError: 'datebulk'`; review decline — 500; single/bulk restore превысили квоту; три cursor formats дали 500; browser feed после 500 не показывал retry. После исправлений эти проверки проходят.
- **Fixture FAIL → PASS:** в начальном review reproducer отсутствовал обязательный `date_wants.updated_at` — исправлено до фиксации настоящего 500. В quota fixture были две copies одного source вопреки unique constraint — источники разделены.
- **Browser fixture FAIL → PASS:** IDs читаются из реального `data-widget`, а не отсутствующего `data-id`; ожидание delayed response переведено с `wait_for_function` на DOM assertion, совместимый с CSP. Production CSP не ослаблялся. Все шесть browser tests прошли.
- **Полный runner: FAIL → итоговые suite results PASS.** Выполнены 62 модуля. При трёх параллельных процессах существующий `test_guest_proposal_widget.py` один раз получил timeout ожидания `#propMedia`. Повтор модуля отдельно прошёл все 9 tests без изменения кода. Тайминг-зависимое место в неизменённых guest/UI fixtures за пределами Session B не переписывалось.
- Локальный runner завершился ошибкой Windows cp1251 при печати служебного smoke log со словом `skipped`, уже сохранив все 62 module logs. Это не test skip: строки относятся к неподдерживаемым map links. UTF-8 output исправлен в локальном runner; `summary.json` собрана из сохранённых результатов с указанием отдельного успешного повторного запуска. Smoke прошёл 85 блоков, настоящий test skip отсутствует.
- Существующие deprecation warnings Starlette/httpx и websockets сохранены; зависимости не обновлялись.

## 6. Remaining blockers

Незакрытых findings или blockers **Session B нет**.

## 7. Handoff to Session C

**Session B завершена.** Код и regression coverage готовы к следующему этапу. Session C deployment/recovery work не выполнялась. Push в `main` разрешён пользователем отдельно и не означает выполнение deployment-проверок Session C.
