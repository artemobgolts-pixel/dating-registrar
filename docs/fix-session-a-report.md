# Fix Session A — Security, Access Control & Data Integrity

## 1. Итог этапа

- Репозиторий: `E:/Storage/Projects/dating-registrar/boris-site`.
- Начальная ветка: `main`; SHA: `637e4e7bc4616f79f752081751f0973c94f84075` — совпадает с аудитом.
- Начальный `git status --short`: пусто, рабочее дерево было чистым.
- Финальный regression gate: **PASS**, 57 модулей, 483 unittest tests и 85 smoke-блоков. Все семь findings исправлены и проверены.
- Проверки используют временные SQLite и каталоги, синтетические аккаунты и заглушки интеграций. Production, реальные данные и рабочий `.env` не изменялись; реальные Telegram-сообщения не отправлялись.
- Пользователь отдельно разрешил commit и push в `main`. Развёртывание не входит в этап.

## 2. Статус findings

| Finding | Статус | Доказательство |
| --- | --- | --- |
| SEC-01 | FIXED + VERIFIED | `/admin/export/account-archive` удалён вместе с эксклюзивными helpers; HTTP 404 для обеих ролей; обычному пользователю запрещены остальные exports; операторские CSV, JSON и оба URL platform ZIP работают. 5 targeted tests. |
| SEC-02 | FIXED + VERIFIED | `category_detach` проверяет владельца события и существование связи до mutation/review/outbox; чужие, отсутствующие и повторные связи безопасно отклоняются. 9 targeted tests, включая сравнение состояния БД и pending/cancelled outbox. |
| SEC-03 | FIXED + VERIFIED | Серверный registry, миграция v38, случайный токен с SHA-256 в БД; logout удаляет запись, replay не восстанавливает вход. Все четыре login-пути выдают registry session. 9 targeted tests: независимые сессии, all-session revoke, деактивация, два уровня expiry, legacy cookie, OAuth link state, миграция. |
| SEC-04 | FIXED + VERIFIED | Авторизация multipart до spool; ограниченный Starlette parser считает реальные байты/части/заголовки; размеры Pillow проверяются до decode; cleanup при ошибках и disconnect. 18 HTTP upload tests + 16 image tests, включая допустимую пачку 5 фото + 2 видео. |
| CFG-01 | FIXED + VERIFIED | Получателя по умолчанию нет; существующая настройка, включая отключение, сохраняется; контрольный backup предварительно применяет `.env` к контейнеру. 5 shell fixture tests + 2 delivery tests с mock отправки. |
| FLOW-03 | FIXED + VERIFIED | Fetch получает явный JSON с persisted profile, frontend сверяет отправленные значения и revision; ошибка/сбой сети сохраняет dirty state. 3 Chromium → HTTP → SQLite tests: validation, correction, network/retry и запоздавший success. |
| FLOW-07 | FIXED + VERIFIED | 4 Chromium → HTTP → SQLite tests: overlong title (client + native server fallback), framework 422, неверная дата, сохранение описания/фото при fetch-ошибке, correction + create/edit, Back/Forward. После HTML-rerender явно предлагается повторный выбор файлов; orphan uploads не остаются. |

Прежний finding утечки через ordinary-user legacy export: **NO LONGER APPLICABLE** вследствие удаления самой возможности. Эквивалентного ordinary-user archive пути в проверенной поверхности не осталось. Операторские backup/export сохранены.

## 3. Что изменено

| Файл | Назначение |
| --- | --- |
| `app/admin_routes.py` | Удаление личного archive; ownership detach; отзыв при logout; JSON-контракты профиля/редактора и восстановление editor draft. |
| `app/auth_routes.py` | Выдача серверных сессий для widget, Mini App, bot poll и OAuth. |
| `app/db.py` | Registry `auth_sessions`, индексы и миграция v38 с зеркалом в SCHEMA. |
| `app/sessions.py` | Выдача, проверка, отзыв текущей/всех сессий; middleware. |
| `app/uploads.py` | Авторизация до разбора multipart; ограничения и закрытие временных файлов. |
| `app/public_routes.py` | Подключение защищённого UploadRoute к гостевым upload-путям. |
| `app/images.py` | Проверка размеров до decode; закрытие изображений и cleanup незавершённых файлов/пачек. |
| `app/main.py` | Session revocation middleware; узкие обработчики ошибок fetch/editor без изменения Referrer-Policy. |
| `app/static/admin.js` | Правдивые состояния автосохранения; сохранение editor draft при ошибках и навигации. |
| `app/templates/admin/date_form.html` | Отображение введённых значений и связанного с формой сообщения об ошибке. |
| `scripts/setup-backup.sh` | Явный opt-in Telegram backup, сохранение выбора и применение актуальной конфигурации. |
| `tests/test_export_archives.py` | Замена obsolete positive personal-export контракта на отсутствие endpoint и проверку operator exports. |
| `tests/test_category_detach_security.py` | Ownership/relationship/no-side-effect regression. |
| `tests/test_session_revocation.py` | HTTP replay, expiry, deactivation, OAuth state и migration regression. |
| `tests/test_upload_security.py` | Реальные ограниченные multipart-запросы и cleanup. |
| `tests/test_image_upload_security.py` | Изображения неверного размера, отказ до decode, batch cleanup. |
| `tests/test_backup_setup.py` | Настоящий shell script с временным `.env` и заглушками системных/сетевых команд. |
| `tests/test_backup_delivery.py` | Отключённая/явная доставка снимка и cleanup gzip; сеть заменена mock. |
| `tests/live_backend.py` | Временный локальный HTTP backend для browser → DB проверок, lifespan/integrations выключены. |
| `tests/test_profile_autosave_e2e.py` | Сохранение профиля, ошибки, исправление, network retry, stale response. |
| `tests/test_editor_draft_e2e.py` | Overlong title, server/framework validation, draft/media retention, correction, create/edit, Back/Forward. |
| `tests/test_apple_privacy.py` | Request fixture с headers и проверка public default через draft-контекст. |
| `tests/test_smoke.py` | Проверка HTML 422, сохранённого draft и отсутствия orphan data вместо obsolete redirect на ошибках редактора. |
| `tests/test_theme_data.py` | Уточнение request fixture (`headers={}`) под fetch-контракт профиля. |
| `docs/fix-session-a-report.md` | Этот отчёт и результаты regression gate. |

## 4. Regression tests

Все команды выполняются из корня репозитория через `.venv/Scripts/python.exe`, каждый `tests/test_*.py` в отдельном процессе, как в CI. Окружение процесса: `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`; в Windows для smoke/setup доступен Git Bash. Локально Python 3.14 и установленный Playwright Chromium; CI использует Python 3.12/Linux.

| Команда / suite | Статус | Количество |
| --- | --- | --- |
| `.venv/Scripts/python.exe tests/test_admin_search_privacy_quota.py` | PASS | 5 |
| `.venv/Scripts/python.exe tests/test_apple_accessibility.py` | PASS | 22 |
| `.venv/Scripts/python.exe tests/test_apple_privacy.py` | PASS | 8 |
| `.venv/Scripts/python.exe tests/test_backend_regressions_v30.py` | PASS | 21 |
| `.venv/Scripts/python.exe tests/test_backup_delivery.py` | PASS | 2 |
| `.venv/Scripts/python.exe tests/test_backup_setup.py` | PASS | 5 |
| `.venv/Scripts/python.exe tests/test_category_card_geometry.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_category_detach_security.py` | PASS | 9 |
| `.venv/Scripts/python.exe tests/test_community_feed.py` | PASS | 14 |
| `.venv/Scripts/python.exe tests/test_confirm_dialog.py` | PASS | 3 |
| `.venv/Scripts/python.exe tests/test_copy_actions.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_db_performance.py` | PASS | 2 |
| `.venv/Scripts/python.exe tests/test_editor_back_position.py` | PASS | 1 |
| `.venv/Scripts/python.exe tests/test_editor_draft_e2e.py` | PASS | 4 |
| `.venv/Scripts/python.exe tests/test_editor_return_safety.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_event_card_geometry.py` | PASS | 11 |
| `.venv/Scripts/python.exe tests/test_event_lifecycle.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_export_archives.py` | PASS | 5 |
| `.venv/Scripts/python.exe tests/test_guest_pin_webview_csrf.py` | PASS | 4 |
| `.venv/Scripts/python.exe tests/test_guest_proposal_backend.py` | PASS | 5 |
| `.venv/Scripts/python.exe tests/test_guest_proposal_widget.py` | PASS | 9 |
| `.venv/Scripts/python.exe tests/test_image_upload_security.py` | PASS | 16 |
| `.venv/Scripts/python.exe tests/test_ink.py` | PASS | 85 smoke-блоков |
| `.venv/Scripts/python.exe tests/test_ink_runtime.py` | PASS | 19 |
| `.venv/Scripts/python.exe tests/test_landing_experience.py` | PASS | 21 |
| `.venv/Scripts/python.exe tests/test_legacy_vote_claim.py` | PASS | 8 |
| `.venv/Scripts/python.exe tests/test_login_module.py` | PASS | 10 |
| `.venv/Scripts/python.exe tests/test_media_optimization.py` | PASS | 10 |
| `.venv/Scripts/python.exe tests/test_metrics.py` | PASS | 12 |
| `.venv/Scripts/python.exe tests/test_notification_outbox.py` | PASS | 14 |
| `.venv/Scripts/python.exe tests/test_notification_ui.py` | PASS | 10 |
| `.venv/Scripts/python.exe tests/test_observability.py` | PASS | 15 |
| `.venv/Scripts/python.exe tests/test_operator_csrf.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_operator_user_card.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_price_scroll_feed_regressions.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_profile_autosave_e2e.py` | PASS | 3 |
| `.venv/Scripts/python.exe tests/test_profile_editor_tour_regressions.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_public_csrf.py` | PASS | 7 |
| `.venv/Scripts/python.exe tests/test_public_privacy_copy_regressions.py` | PASS | 7 |
| `.venv/Scripts/python.exe tests/test_public_profile_tabs.py` | PASS | 4 |
| `.venv/Scripts/python.exe tests/test_public_quality.py` | PASS | 12 |
| `.venv/Scripts/python.exe tests/test_public_review_page_ui.py` | PASS | 4 |
| `.venv/Scripts/python.exe tests/test_review_collection_ui.py` | PASS | 5 |
| `.venv/Scripts/python.exe tests/test_session_revocation.py` | PASS | 9 |
| `.venv/Scripts/python.exe tests/test_shadow_safe_scroll_rows.py` | PASS | 3 |
| `.venv/Scripts/python.exe tests/test_skin_geometry_contract.py` | PASS | 5 |
| `.venv/Scripts/python.exe tests/test_smoke.py` | PASS | 85 smoke-блоков |
| `.venv/Scripts/python.exe tests/test_social_events.py` | PASS | 7 |
| `.venv/Scripts/python.exe tests/test_suspicious_moderation.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_telegram_miniapp.py` | PASS | 13 |
| `.venv/Scripts/python.exe tests/test_telegram_profile.py` | PASS | 6 |
| `.venv/Scripts/python.exe tests/test_theme_data.py` | PASS | 24 |
| `.venv/Scripts/python.exe tests/test_time_deadline_ui.py` | PASS | 9 |
| `.venv/Scripts/python.exe tests/test_ui_polish_followup.py` | PASS | 4 |
| `.venv/Scripts/python.exe tests/test_upload_security.py` | PASS | 18 |
| `.venv/Scripts/python.exe tests/test_voting.py` | PASS | 16 |
| `.venv/Scripts/python.exe tests/test_voting_events.py` | PASS | 8 |

Пропуски: **SKIP: 0**, неожиданных пропусков нет.

`bash -n scripts/setup-backup.sh`, Python compile, `node --check app/static/admin.js`, `git diff --cached --check`: **PASS**. Полный прогон повторяет все добавленные/изменённые targeted suites и существующие auth/access/browser regressions. Локальные подробные логи: `artifacts/fix-a-gate/` (игнорируются Git).

Предыдущие неуспешные попытки и их разрешение:

- **FAIL → PASS:** первый editor E2E ожидал accessible name `Сохранить`, но CSS добавляет `💾` в имя кнопки. Fixture использует однозначную связь `button[form="dateForm"]`; все 4 теста прошли.

- **FAIL → PASS:** полный gate выявил старые expectations в `test_apple_privacy.py` (request headers, template default) и `test_smoke.py` (redirect на validation error). Fixtures обновлены под новый контракт; повторно прошли privacy 8 и smoke 85. Остальные 55 модулей прошли первый полный прогон; production-код после него не менялся.

- **FAIL → PASS:** первый smoke без `PYTHONUTF8=1` упал на Windows cp1251 при чтении `public.css`; с UTF-8 прошли все 85 блоков.
- **FAIL → PASS:** дополнительные upload fixtures сначала ожидали прямой RuntimeError вместо AnyIO ExceptionGroup и только 413 вместо более раннего native parser 400 на большом заголовке. Ожидания уточнены; все 18 тестов прошли.
- **FAIL → PASS:** тест задержанного profile response снимал Playwright route внутри активного callback (`RouteAlreadyHandled`). Fixture исправлен; все 3 browser tests прошли.
- **Review → исправлено и PASS:** восстановлено применение `.env` перед контрольным backup, чтобы уже работающий контейнер не использовал старого получателя после отключения/смены. Повторно прошли setup 5 и delivery 2.

## 5. Regression risks

- После миграции v38 старые cookie без registry token требуют повторного входа. Срок сессии абсолютный — 30 дней; переподпись cookie его не продлевает.
- Деактивация блокирует вход, пока аккаунт неактивен. Если его снова активировать, существующая неистёкшая registry session может работать; для постоянного отзыва предусмотрен `revoke_user_sessions`.
- Multipart-проверки используют интерфейсы закреплённой версии Starlette; при обновлении parser/framework нужны upload regressions. Рабочий лимит Caddy сохранён.
- Бинарные файлы сохраняются в открытой форме после fetch-ошибки. После полной навигации/серверного HTML-rerender браузер требует выбрать их заново; файлы не сериализуются в историю и не сохраняются сервером как бесхозный draft.
- Изменение `.env` применяется работающему контейнеру при запуске setup script перед контрольным backup; тесты выполняли только заглушки Docker/интеграций.
- Проверка настоящих Telegram/OAuth провайдеров, production migration и deployment: **NOT RUN** по границам задания. Login/revocation проверены локально; внешние вызовы подменены.

## 6. Remaining beta blockers from this phase

Неразрешённых beta blockers из перечисленных семи findings не осталось.

## 7. Handoff to Session B

Phase A можно считать завершённой: все семь findings имеют статус FIXED + VERIFIED, полный локальный gate прошёл. Session B не запускалась. При последующем развёртывании учитывать миграцию v38 и повторный вход пользователей; production deployment в этой сессии не выполнялся.


Финальный `git diff --cached --stat` перед commit:

```text
 app/admin_routes.py                    | 168 +++++++++++++--------
 app/auth_routes.py                     |  17 +--
 app/db.py                              |  20 ++-
 app/images.py                          | 102 +++++++------
 app/main.py                            |  49 +++++-
 app/public_routes.py                   |   3 +-
 app/sessions.py                        |  99 +++++++++++++
 app/static/admin.js                    | 177 +++++++++++++++++++++-
 app/templates/admin/date_form.html     |  51 ++++---
 app/uploads.py                         | 213 ++++++++++++++++++++++++++
 docs/fix-session-a-report.md           | 182 +++++++++++++++++++++++
 scripts/setup-backup.sh                |  24 ++-
 tests/live_backend.py                  |  81 ++++++++++
 tests/test_apple_privacy.py            |   8 +-
 tests/test_backup_delivery.py          |  50 +++++++
 tests/test_backup_setup.py             |  85 +++++++++++
 tests/test_category_detach_security.py | 179 ++++++++++++++++++++++
 tests/test_editor_draft_e2e.py         | 156 +++++++++++++++++++
 tests/test_export_archives.py          | 117 ++++++++-------
 tests/test_image_upload_security.py    | 242 ++++++++++++++++++++++++++++++
 tests/test_profile_autosave_e2e.py     | 106 +++++++++++++
 tests/test_session_revocation.py       | 168 +++++++++++++++++++++
 tests/test_smoke.py                    |  27 ++--
 tests/test_theme_data.py               |   1 +
 tests/test_upload_security.py          | 264 +++++++++++++++++++++++++++++++++
 25 files changed, 2359 insertions(+), 230 deletions(-)
```
