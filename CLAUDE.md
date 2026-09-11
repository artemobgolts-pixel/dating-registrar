# CLAUDE.md — памятка для Claude Code

Сервис для организации встреч и голосования за события. Production: https://date4you.online
Язык интерфейса, комментариев и коммитов — **русский**.

## Стек и структура

FastAPI + SQLite (WAL) + Jinja2 + Pillow. Деплой: Docker Compose + Caddy 2 (TLS сам).

```
app/
  main.py        — сборка приложения, middleware, health/readiness и подключение роутов
  admin_routes.py / public_routes.py / auth_routes.py — кабинет, публичные страницы, вход
  sessions.py    — серверный registry сессий и отзыв при выходе
  db.py          — схема и миграции (PRAGMA user_version)
  images.py      — приём фото (WebP, лимиты), backup.py — снимки базы, notify.py — Telegram
  docker-entrypoint.sh — чинит права на /data и понижает привилегии до appuser
  static/        — public.css (гостевая), admin.css, ui.js (sortable/uploader/чипы/конфетти)
  templates/     — public/ и admin/ (Jinja2)
tests/test_*.py  — unit/HTTP/browser regression; test_smoke.py — смоук-проверки
scripts/release_gate.py — изолированный полный gate, привязанный к SHA
data/            — база, фото, бэкапы; НЕ в гите, никогда не трогать и не коммитить
```

## Команды

```bash
# локальный запуск (из app/)
DATA_DIR=../data-dev COOKIE_SECURE=false SECRET_KEY=dev uvicorn main:app --reload

# тесты (из корня репозитория)
python tests/test_smoke.py
```

Перед коммитом тесты должны быть зелёные. Для browser suites нужны зависимости
из `app/requirements-test.txt` и Chromium (`python -m playwright install chromium`).
Полный gate и выпуск точного SHA описаны в [docs/release.md](docs/release.md);
один smoke не заменяет этот gate. Локальные browser fixtures используют временную
SQLite и отключённые интеграции (`tests/live_backend.py`).

## Правила проекта

- **Миграции.** Любое изменение схемы = новая запись в `MIGRATIONS` в `db.py`
  + увеличить `LATEST_VERSION` + продублировать изменение в `SCHEMA` (для свежих баз).
  Старые базы докатываются автоматически при старте — миграции не редактировать задним числом.
- **Один воркер uvicorn.** Лимиты запросов и троттлинг входа живут в памяти процесса.
  Не добавлять `--workers N`.
- **Фото** не отдаются напрямую: только через `/c/<токен>/image/<файл>` (с проверками)
  и `/admin/uploads/<файл>`. Не монтировать `/uploads` в StaticFiles.
- **CSRF**: каждая POST-форма админки несёт `<input type="hidden" name="csrf" value="{{ csrf }}">`;
  fetch-запросы админки берут токен из `document.body.dataset.csrf`.
- **Участники**: гостевые действия требуют входа в аккаунт. Legacy guest cookie
  и имена читаются для старых записей и переноса голосов после входа; старый
  диалог имени и выпуск guest cookie не используются. Правила количества
  голосов и дедлайна задаёт `voting.py`; режим категории ограничивает выбор
  одним или несколькими событиями.
- **Оформления** независимы от светлой/тёмной темы: `category_skin` задаёт
  `friends|romantic` для публичной ссылки категории, `admin_skin` — для кабинета
  пользователя. `data-skin` и `data-theme` не объединять. Romantic сохраняет
  прежний кремово-розовый авторский вид; friends использует ivory, индиго, teal,
  amber и семантические SVG-иконки. Анимации уважают `prefers-reduced-motion`.
- **CSP без `unsafe-inline` для скриптов.** Любой инлайновый `<script>` обязан нести
  `nonce="{{ csp_nonce }}"`. Атрибуты `onclick`/`onsubmit`/`onchange` запрещены —
  есть `data-confirm`, `data-copy`, `data-autosubmit` (делегирование в `admin/base.html`)
  или `addEventListener`.
- **IP клиента** для лимитов — только через `client_ip()` (читает `X-Real-IP`,
  который перезаписывает Caddy). `request.client` напрямую не использовать.
- Правки делай точечными и минимальными; не переименовывай публичные URL без необходимости.
- **Версии интерфейса.** Перед изменением компонента или функции найди все её
  пользовательские варианты: карточка/список/модалка, кабинет/гостевая страница,
  desktop/mobile, light/dark и friends/romantic. Общие формулировки и состояния
  держи в одном компоненте или обновляй синхронно; на расхождения добавляй
  регрессионную проверку.
- **Безопасность UI-правок.** Не делай предположений в неоднозначном месте, если
  они могут изменить поведение или сломать геометрию. Сначала проверь связанные
  шаблоны, CSS/JS и тесты; если безопасный вариант всё ещё неочевиден — уточни у
  владельца до изменения. После правок проверяй затронутые варианты интерфейса.
- Версии зависимостей в `requirements.txt` запинены — обновлять осознанно, прогоняя тесты.
