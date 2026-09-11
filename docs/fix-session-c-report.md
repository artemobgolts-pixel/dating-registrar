# Fix Session C — Release, Backup, Deployment & Rollback Safety

## 1. Итог этапа

Реализована процедура выпуска точного artifact, атомарные snapshots, согласованное
восстановление DB+media и явный release gate. Production deployment не выполнялся.

- Репозиторий: `E:/Storage/Projects/dating-registrar/boris-site`; ветка `main`.
- Начальный pushed SHA: `12ac41d95c0274c9e2fee333a11b7c45183e78ca`; после `git fetch origin`
  он совпадал с `origin/main`. Начальный `git status --short` пуст.
- Родительский каталог — другой Git-репозиторий; его файлы и изменения не затрагивались.
- Авторитетный контекст: `../fix-session-a-report.md` и `docs/fix-session-b-report.md`.
  Исправления A/B сохранены; schema/migrations v38, продуктовые потоки, UI и зависимости
  верхнего уровня не изменялись. Из существующих тестов менялись только smoke-контракт
  health/temporary path и проверка managed-ветки backup setup.
- Пользователь явно разрешил commit + push в `main`. Финальный SHA фиксируется в
  сообщении завершения и в clean-SHA receipt, создаваемом после commit, до push.
- Работа выполнялась 10–11 сентября 2026 года. Реальные DB/media, `.env`, Telegram,
  S3/rclone remotes и production сервер не использовались.

| Инструмент | Фактически доступно |
| --- | --- |
| ОС | Windows; Git Bash 5.2.26 — shell fixtures, не Linux runtime |
| Python | Изначально `.venv` Python 3.14; отдельно создан `tmp/phase-c-py312`, Python 3.12.3 |
| Docker / Compose | Локально отсутствуют — runtime drill **BLOCKED** |
| Linux / WSL | Локально отсутствуют — production-like runtime **BLOCKED** |
| Caddy | Изначально отсутствовал; официальный Windows binary 2.11.4 в ignored artifacts, adapt/HTTP drill выполнены |
| ffmpeg | Изначально отсутствовал в PATH; найден существующий imageio-ffmpeg binary 7.1, использован из ignored artifacts |
| Browser | Playwright 1.61.0 + установленный Chromium; Firefox/WebKit **NOT TESTED** |
| GitHub Actions | Read-only доступ проверен; новый workflow выполняется только после push, не выдаётся за локальное свидетельство |

## 2. Статус findings

| Finding | Статус | Доказательства и границы |
| --- | --- | --- |
| DEPLOY-01 | **FIXED BUT NOT FULLY VERIFIED** | Full SHA + clean gate receipt; сохранённые app/Caddy image IDs и tar; previous artifact, журнал состояний, pre-migration DB/media point, bounded readiness, explicit compatible code rollback. Fault injection state machine выполнен; настоящий Docker deploy/rollback локально BLOCKED. |
| DEPLOY-02 | **FIXED + VERIFIED** | 7 tests: SQLite backup в unpublished temporary, integrity_check/fsync/rename, настоящий mid-copy failure через callback, rename failure, retry, retention, live WAL, missing source. |
| DEPLOY-03 | **FIXED BUT NOT FULLY VERIFIED** | Статика извлекается из image; append-only content hashes сохраняются при update/rollback/переносе artifact. Caddy Windows HTTP drill и 7 backend tests PASS; Docker/Caddy switch целиком локально BLOCKED. |
| DEPLOY-06 | **FIXED + VERIFIED** | 10 synthetic recovery tests + 5 remote-command fixtures: связанные DB/images/video/avatar/OG, mutation/delete, verify/restore в новый каталог, проверка ссылок/hashes. Реальный remote upload/download **NOT TESTED**. |
| BUILD-01 | **FIXED BUT NOT FULLY VERIFIED** | Rebuild-on-rollback удалён; retained tar/IDs проверяются и загружаются. Constraints фиксируют транзитивные версии проверенного A/B окружения. Реальная первая Docker сборка локально BLOCKED; битовая воспроизводимость новой сборки не заявляется. |
| TEST-04 | **FIXED + VERIFIED** | 12 gate tests: exact SHA/tree, dirty rejection, unique source/TMP/DATA/output, synthetic env, обязательный Chromium, skips/custom runners, developer PASS не разрешает release. CI отделяет Linux/3.12 gate и image drill. |
| DEPLOY-04 | **FIXED + VERIFIED** | 6 HTTP/SQLite tests: normal, writer lock → 503 при живом /health, recovery, missing DB, schema mismatch, незаблокированный event loop и отсутствие mutations. |
| DEPLOY-05 | **FIXED + VERIFIED** | 6 tests: стабильная группа method/route/type, разные причины, sample request IDs и полные IDs в логах, истечение окна, конкурентные вызовы, legacy callers; только local notify stub. |

## 3. Новый release procedure

Подробный runbook: [release.md](release.md).

1. Чистый checkout полного SHA → Linux/Python 3.12 release gate без skips.
2. `prepare` принимает receipt этого SHA, собирает image из `git archive`, проверяет
   synthetic startup/migrations/HTTP/backup без сети, сохраняет app/Caddy artifacts,
   image IDs, checksums, Caddyfile и static. CI upload имеет retention 14 дней;
   release artifacts нужно переносить в постоянное закрытое хранилище.
3. `deploy` сохраняет previous, закрывает ingress и останавливает app/background writers,
   создаёт **парный** recovery point до миграций; включает точный image без build/pull.
4. `/ready` + release identity проверяются в общем deadline 60 s; timeout команды
   ограничен оставшимся временем. Caddy открывается только после readiness.
5. Ошибка/interrupt остаётся в `state.json`; `resume` и явный code rollback используют
   сохранённые artifacts. Failed candidate не становится previous known-good.
   Actual schema и `--schema-compatible` обязательны для code rollback. DB автоматически
   не заменяется, новые пользовательские данные сохраняются.

Начальный legacy image сохраняется по фактическому Docker ID без выдуманного старого
gate; recovery helper связан с подготовленным candidate artifact. Managed compose
сохраняет один worker и VIDEO_FASTSTART default. Старый host `app/static` не монтируется.
Опубликованные immutable assets автоматически не очищаются; unversioned assets
обслуживает backend image с явными WebP/font MIME types. Monitoring-профиль сохранён.

## 4. Backup/recovery evidence

- Routine snapshots публикуются только после успешной SQLite copy и integrity_check.
  Partial/legacy файлы не подавляют retry; предыдущая успешная копия остаётся при сбое.
- Полная точка: `app.db`, `uploads/`, `manifest.json`; manifest связывает schema,
  release/image, hashes/размеры и ссылки на оригинальные media. Responsive/OG caches
  исключены как восстанавливаемые. Restore проверяет всё и создаёт только **новый** каталог.
- Реальный CLI create → verify → restore и linked synthetic DB/media roundtrip выполнены.
  Проверены missing media, повреждение bundle, копирование с ошибкой, чужие пути,
  существующий destination, WAL и отсутствие side effects исходного состояния.
- Remote-command fixtures проверяют: payload copy/check → manifest last → final check;
  при неудаче нет retention предыдущих good bundles; при failed final check marker
  удаляется; retention удаляет целые завершённые bundles. Реальный rclone/S3 **NOT TESTED**.
- Backup и release используют общий lock; backup journal позволяет `resume` после
  interrupted stop. Phase A opt-in Telegram сохранён, включая отключение получателя
  и применение актуального `.env` к retained image перед контрольным backup.
- Полные remote bundles хранятся комплектами (default 30), независимо от прежней
  семидневной media trash. Routine 14 DB-only snapshots и Telegram копии **не дают**
  гарантии media recovery. Локальные bundles/images требуют контроля места и ручной
  очистки целыми точками после off-host verification.
- Плановый RPO до 24 h только при успешном ежедневном off-host backup; пропуски его
  увеличивают. Pre-migration point соответствует моменту остановки. Production RTO
  не измерен и не обещается; нужен staging drill на реальном объёме.

## 5. Release gate results

Локальный gate использует Python 3.12.3, UTF-8, Chromium, Git Bash/sh, ffmpeg,
синтетические credentials, DOMAIN=localhost и отдельные source/data/tmp/output roots.
Все модули выполняются отдельными процессами; runtime/dependency inventory сохраняется
в receipt. Подробные логи — ignored `artifacts/fix-c-gate/`.

| Проверка | Результат |
| --- | --- |
| Локальные unittest/browser regressions | **PASS — 598 tests, 68 unittest-модулей** |
| Smoke | **PASS — 85 блоков** |
| Screenshot regression ink | **PASS**; эталон не изменялся |
| Общий inventory | **70 модулей; test SKIP = 0** |
| Phase C targeted | Atomic 7; recovery 10; remote 5; release gate 12; procedure 16; health 6; grouping 6; static 7 — PASS |
| Phase A setup/delivery | Setup 6 (добавлена managed-ветка), delivery 2 — PASS |
| Caddy | Adapt/validate и реальный локальный HTTP old/new immutable/invalid hash drill — PASS |
| Shell syntax / git diff whitespace | PASS |
| Exact committed SHA | Полный clean-SHA gate запускается после commit; SHA, tree, source hash и результаты фиксируются отдельным receipt до push |
| Linux/Python 3.12 + Docker image build/start/rollback | Локально **BLOCKED**; workflow проверяет это на GitHub runner после push |
| Production deployment, реальные Telegram/S3/TLS/remote restore | **NOT TESTED** — за границами безопасной локальной проверки |

Неуспешные попытки и исправления:

- Первый полный gate: 59 модулей PASS, smoke остановился на новой опечатке
  `db.LATEST_VERSION` вместо существующего `dbm.LATEST_VERSION`. Исправлена Phase C fixture.
- Следующий smoke выявил отсутствие WebP/WOFF2 в MIME database Python 3.12/Windows
  (Python 3.14 распознаёт их). Это важно для нового backend static path: типы WebP и
  fonts заданы явно, добавлена проверка с полностью отсутствующей MIME database.
  После исправления smoke прошёл все 85 блоков.
- Review исправил publication static после переноса CI artifact, сохранение previous
  known-good при failed-candidate rollback, общий readiness deadline, backup journal,
  legacy recovery helper и сохранение VIDEO_FASTSTART default. Есть fault-injection tests.
- До финального clean-SHA запуска результаты собирались из первого прогона и отдельных
  успешных повторов smoke/изменённых и оставшихся модулей; этот составной результат не
  выдаётся за release-qualified Linux gate.
- Read-only проверка уже существовавшего CI baseline `12ac41d` обнаружила failure
  в `test_guest_proposal_widget.py` (timeout ожидания `[data-proposal-empty-cta]`).
  Это предшествует Phase C; соответствующий fixture/UI не переписывался. Новый gate
  также остановит release, если проблема повторится. Локально модуль проходит 9 tests.
- Предыдущие interrupted tool sessions без завершённого receipt не считаются PASS.
  Существующие Starlette/httpx deprecation warnings сохранены, зависимости не обновлялись.

## 6. Remaining launch risks

1. До production нужен полный Docker/Linux disposable deployment/failed-update/rollback
   drill, включая первый переход с legacy image, Caddy/TLS и реальные тома/права.
   Локальные shell fixtures и Windows Caddy этого не доказывают.
2. CI baseline имеет отмеченный выше старый browser timeout. Результат нового CI
   нужно проверять по **точному pushed SHA**, а не по последнему зелёному запуску.
3. Remote upload/download и restore выбранного полного bundle не выполнялись на
   настоящем хранилище. Проверены алгоритм/CLI/local references и заглушки команд.
4. Полный snapshot требует краткой остановки всех writers. Размер media определяет
   downtime и место для backups; disk-full воспроизводился через безопасные ошибки,
   host disk намеренно не заполнялся. `/ready` не обещает запас места для новых uploads.
5. Первая сборка ещё зависит от base/tag/apt/pip и Linux-only resolution. Rollback
   воспроизводим через retained image, повторная сборка из исходников — не bit-for-bit.
6. Откат schema/данных с потерей изменений требует отдельного решения оператора.
   Скрипт никогда автоматически не подменяет live DB/media прошлым snapshot.

## 7. Handoff to Session D

**Реализация Phase C завершена; инфраструктурная верификация ограничена указанными
BLOCKED/NOT TESTED шагами. Полная production readiness не заявляется.**
Все verified fixes A/B сохранены. Session D, UI cleanup и performance work не запускались.
Следующий release должен пройти exact-SHA Linux gate и реальный disposable recovery drill;
оставшиеся инфраструктурные проверки нельзя заменить зелёным `/health`.
