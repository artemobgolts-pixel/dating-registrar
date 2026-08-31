# date4you ♥

Сервис для организации встреч и голосования за события. В кабинете ты создаёшь события
и собираешь их в категории, у каждой категории — секретная ссылка. Тот, кому ты её
отправишь, открывает страницу, входит удобным способом и может:

- **голосовать за события** — организатор разрешает выбрать один или несколько
  вариантов; за одно событие могут проголосовать несколько участников, а его
  вместимость настраивается от 1 до 100 человек; после дедлайна сервис определяет
  победителя или фиксирует ничью;
- **предлагать свои** события — сразу или через твою модерацию (настраивается на категории),
  с фото и видео, которые можно перетаскивать и менять местами;
- **править и удалять свои предложения** в любой момент;
- **задавать вопросы** к событию — их видишь только ты, а твой ответ — только автор вопроса;
- открыть **место на Яндекс.Картах** (или вставить готовую ссылку на карты — название
  места подтянется само), добавить событие в **Google Календарь** в один тап
  (или скачать .ics для Apple/Outlook);
- листать раздел **архива** — события с истёкшей датой автоматически уходят туда
  (штамп «Архив», ч/б фото), но остаются на странице на память.

У событий есть необязательный модификатор оплаты **«50/50»** (зелёная плашка),
у категорий — **описание**, видимое всем гостям. В описаниях и комментариях работает
лёгкая разметка: `**жирный**`, `*курсив*`, `__подчёркнутый__`, `~~зачёркнутый~~`
и `[ссылки](https://…)`.

Для каждой категории можно выбрать оформление публичной ссылки: **«Стандартный»**
с индиго-бирюзовой палитрой и нейтральными иконками или сохранённый авторский
**«Романтический»** дизайн. Новые категории создаются со стандартным оформлением,
а существующие после обновления сохраняют прежний вид. Оформление личного
кабинета выбирается отдельно в профиле и не влияет на гостевые ссылки;
светлая/тёмная тема остаётся настройкой конкретного браузера.

Все действия гостей приходят тебе в Telegram (если настроить бота) и видны в админке —
с именами, а не безликими токенами.

---

## Стек

- **Python + FastAPI + SQLite** — само приложение (один контейнер, процесс работает не от root);
- **Caddy 2** — веб-сервер, который **сам получает и продлевает** сертификат Let's Encrypt;
- **Prometheus + Grafana** — необязательный внутренний профиль `monitoring` с готовым dashboard;
- **Docker Compose** — всё поднимается одной командой.

Все данные (база + фото + автоснимки базы) живут в папке `./data` рядом с проектом.
`./data` и `.env` добавлены в `.gitignore` — в репозиторий они не попадают.

---

## Шаг 1. DNS на Reg.ru

В панели Reg.ru для домена `date4you.online` создай **A-запись**:

| Имя | Тип | Значение |
|-----|-----|----------|
| `@` | A   | IP твоего сервера |

Поддомены не нужны. (Если хочешь, чтобы работал и `www.date4you.online`, —
добавь вторую A-запись `www` на тот же IP и раскомментируй www-блок в `Caddyfile`.)

DNS обновляется от пары минут до пары часов. Проверить:

```bash
dig +short date4you.online
```

Когда команда возвращает IP сервера — можно запускать.

## Шаг 2. Docker на Debian 12

На чистом сервере (от root или через sudo):

```bash
apt update && apt install -y curl git
curl -fsSL https://get.docker.com | sh
```

Проверка: `docker compose version` должна вывести версию.

Если включён файрвол ufw — открой 80 и 443:

```bash
ufw allow 80/tcp
ufw allow 443/tcp
```

## Шаг 3. Запуск сайта

```bash
git clone https://github.com/artemobgolts-pixel/dating-registrar.git /opt/date4you
cd /opt/date4you
cp .env.example .env
nano .env
```

В `.env` обязательно заполни:

- `SECRET_KEY` — сгенерируй: `openssl rand -hex 32`
- `DOMAIN` — уже стоит `date4you.online`
- `TG_BOT_TOKEN`, `TG_BOT_USERNAME`, `TG_WEBHOOK_SECRET` — для входа через Telegram (см. ниже)
- `OPERATOR_TG_IDS` — твой telegram_id (узнать у [@userinfobot](https://t.me/userinfobot))

Без `SECRET_KEY` приложение **не запустится** — упадёт с понятной ошибкой.

Дальше:

```bash
docker compose up -d --build
```

Минуту-две Caddy получает сертификат, после этого:

- сайт: `https://date4you.online`
- вход: `https://date4you.online/login`

Права на папку `data/` контейнер **выравнивает сам при каждом старте**
(см. `app/docker-entrypoint.sh`) — никаких ручных `chown` не нужно,
в том числе при обновлении старой установки.

### Вход через Telegram

Пароля нет: вход — через бота или Telegram Mini App. Кнопка на `/login` открывает
бота с одноразовым кодом, человек жмёт **Start** и подтверждает вход. Обычный
`/start` без сайта создаёт новый аккаунт и показывает кнопку Mini App; внутри
Telegram профиль проверяется по подписанному `WebApp.initData` и логинится сам.
Настройка:

1. [@BotFather](https://t.me/BotFather) → `/newbot` → токен `1234567:AAH...` это `TG_BOT_TOKEN`.
2. Username бота (без `@`) впиши в `TG_BOT_USERNAME`.
3. Придумай длинную случайную строку для `TG_WEBHOOK_SECRET` (`openssl rand -hex 32`).
4. Свой `telegram_id` (у [@userinfobot](https://t.me/userinfobot)) впиши в `OPERATOR_TG_IDS`.
   При первом входе ты автоматически станешь владельцем всех существующих данных.
5. `docker compose up -d` — приложение само зарегистрирует вебхук входа в Telegram
   и глобальную кнопку меню Mini App при старте (ручные `setWebhook` и
   `setChatMenuButton` не нужны).
6. После того как HTTPS-сертификат домена уже работает, в @BotFather обязательно
   включи **Main Mini App** и укажи `https://<DOMAIN>/tg/app` — тогда большая
   кнопка запуска появится в профиле бота на телефоне и Desktop. Для отдельного
   staging-домена задай полный HTTPS URL в `TG_MINI_APP_URL`. Кнопку меню личного
   чата приложение настраивает само через `setChatMenuButton`.

### Telegram-уведомления

Тот же бот шлёт уведомления о голосах/вопросах и алёрты о сбоях. Пользовательские
уведомления отправляются через Bot API Rich Messages с primary-кнопкой, которая
открывает нужный экран в Mini App; для старого Bot API есть fallback `sendMessage`.
Для операторских алёртов нужен `TG_CHAT_ID`:
напиши боту любое сообщение, открой `https://api.telegram.org/bot<ТОКЕН>/getUpdates`,
найди `"chat":{"id":123456789,...}` — это число и есть `TG_CHAT_ID`.

---

## Обновление сайта

Если проект на сервере склонирован из git (шаг 3):

```bash
cd /opt/date4you
git pull
docker compose up -d --build
```

`data/` и `.env` в `.gitignore`, так что `git pull` их не трогает.
После обновления админка один раз попросит войти заново — cookie сессии
переехала на защищённое имя `__Host-…`. Это разовое и ожидаемое.
Миграции базы применяются автоматически при старте (схема хранит версию
в `PRAGMA user_version` — старые установки докатываются сами, включая
перенос старых «выборов» в новые именные голоса).

### Если на сервере старая копия без git

```bash
cd /opt
mv date4you date4you-old
git clone https://github.com/artemobgolts-pixel/dating-registrar.git date4you
cp date4you-old/.env date4you/
mv date4you-old/data date4you/
cd date4you
docker compose up -d --build
# когда убедишься, что всё работает:
# rm -rf /opt/date4you-old
```

---

## Разработка и Git

### Первый раз на компьютере

```bash
git clone https://github.com/artemobgolts-pixel/dating-registrar.git
cd dating-registrar
```

### Локальный запуск без Docker

```bash
cd app
pip install -r requirements.txt
DATA_DIR=../data-dev COOKIE_SECURE=false SECRET_KEY=dev \
  TG_BOT_USERNAME=dev_bot TG_WEBHOOK_SECRET=dev OPERATOR_TG_IDS=1 \
  uvicorn main:app --reload
# сайт: http://127.0.0.1:8000  · вход: /login
# Локально без настоящего бота вход не завершить — для отладки кабинета
# можно подтвердить код вручную, дёрнув /tg/webhook с заголовком секрета.
```

### Цикл правок

```bash
git pull                      # забрать свежие изменения
# ...правишь код...
python tests/test_smoke.py    # прогнать тесты (нужны зависимости из requirements.txt)
git add -A
git commit -m "что сделано"
git push
```

При первом `git push` GitHub попросит авторизацию. Пароль от аккаунта не подойдёт —
нужен **Personal Access Token**: GitHub → Settings → Developer settings →
Personal access tokens → Generate new token (classic, права `repo`).
Вставляешь токен вместо пароля. Либо один раз: `gh auth login` (через GitHub CLI).

После `git push` обнови сервер: `cd /opt/date4you && git pull && docker compose up -d --build`.

### Claude Code в VS Code

1. Установи расширение **Claude Code** из маркетплейса VS Code и открой папку проекта.
2. В корне лежит **`CLAUDE.md`** — Claude читает его автоматически: там структура проекта,
   команды запуска/тестов и правила (миграции, один воркер, русский интерфейс).
3. Типичный запрос: *«поменяй X в app/main.py, прогони `python tests/test_smoke.py`
   и сделай коммит»* — Claude сам отредактирует файлы, запустит тесты и закоммитит.
4. Перед пушем полезно попросить: *«покажи git diff»* — и просмотреть изменения глазами.
5. `git push` и обновление сервера остаются за тобой (или попроси Claude — он умеет).

---

## Бэкапы

Три уровня, от простого к параноидальному:

1. **Автоматический** — приложение само раз в сутки кладёт консистентный снимок базы
   в `data/backups/` и хранит последние 14. Ничего настраивать не нужно.
2. **Ручной** — `docker compose exec app python backup.py` (снимок через sqlite backup API).
3. **Внешний** — добавь в cron на сервере копирование `data/` куда-то ещё, например:

   ```bash
   0 4 * * * tar -czf /root/date4you-$(date +\%F).tar.gz -C /opt/date4you data/backups data/uploads
   ```

   ⚠ Не архивируй «живой» `data/app.db` напрямую (база в режиме WAL) —
   бери снимки из `data/backups/`, они для этого и существуют.

4. **В облако (рекомендуется для прода)** — `scripts/backup.sh`: делает свежий снимок
   внутри контейнера и заливает его в облако через [rclone](https://rclone.org)
   (S3 / Я.Диск / R2), храня последние 30 копий. Настрой `rclone config` (ремоут
   по умолчанию `backup`), затем добавь в cron:

   ```bash
   0 21 * * *  cd /opt/date4you && ./scripts/backup.sh >> /var/log/date4you-backup.log 2>&1
   ```
   На сервере с часовым поясом UTC это 00:00 по Москве. Telegram-отправка
   выполняется только этим cron; встроенный цикл приложения хранит лишь
   локальный свежий снимок и не дублирует сообщение.

   Параметры (`RCLONE_REMOTE`, `KEEP_REMOTE`, `SERVICE`, …) переопределяются
   переменными окружения — см. шапку скрипта. **Проверь восстановление** из
   облачной копии хотя бы раз, прежде чем полагаться на бэкап.

### Восстановление из снимка

```bash
cd /opt/date4you
docker compose stop app
cp data/backups/app-ГГГГММДД-ЧЧММСС.db data/app.db
rm -f data/app.db-wal data/app.db-shm
docker compose start app   # права на файлы entrypoint поправит сам
```

Полный архив (база + все фото + JSON) можно скачать кнопкой в админке → Главная → Экспорт.

---

## Наблюдаемость

### JSON-логи, request ID и Sentry

Приложение пишет в stdout по одному JSON-объекту на строку. Основные поля:
`event`, `request_id`, `route`, `status_class`, `duration_ms`, `environment` и
`release`. Сырые query string, cookies, токены, email и секретные части URL в
телеметрию не попадают. На публичном HTTPS edge Caddy всегда заменяет входящий
`X-Request-ID` своим значением: присланному клиентом ID мы не доверяем. Возьми
фактический `X-Request-ID` из заголовков ответа и найди его в логах приложения
(для фильтра нужен `jq`, на Debian: `apt install -y jq`):

```bash
curl -sS -D - -o /dev/null https://date4you.online/health
# скопируй значение X-Request-ID из ответа, например edge-generated-id
docker compose logs --no-log-prefix app \
  | jq -R 'fromjson? | select(.request_id == "edge-generated-id")'
```

Для Sentry заполни в `.env`:

```dotenv
APP_ENV=production
APP_RELEASE=<полный git SHA развёрнутого коммита>
SENTRY_DSN=<DSN проекта Sentry>
SENTRY_TRACES_SAMPLE_RATE=0.05
```

Пустой `SENTRY_DSN` полностью отключает Sentry. `SENTRY_TRACES_SAMPLE_RATE=0`
отключает performance traces, но оставляет сбор необработанных ошибок. Перед
отправкой наружу приложение удаляет request body, query, cookies, заголовки и PII;
`request_id` остаётся безопасным ключом корреляции. После изменения переменных
пересоздай контейнер: `docker compose up -d --build app`.

### Локальные Prometheus и Grafana

Monitoring-стек является opt-in и без профиля не запускается. Его настройки
хранятся отдельно от переменных приложения, чтобы пароль Grafana не попадал в
app-контейнер. Создай игнорируемый `.env.monitoring` и задай стойкий пароль:

```bash
cp .env.monitoring.example .env.monitoring
openssl rand -hex 24
nano .env.monitoring  # вставь результат в GRAFANA_ADMIN_PASSWORD=...
docker compose --env-file .env --env-file .env.monitoring \
  --profile monitoring up -d --build
docker compose --env-file .env --env-file .env.monitoring \
  --profile monitoring ps
```

Compose создаёт password secret из `GRAFANA_ADMIN_PASSWORD`, а Grafana читает
его из `/run/secrets/grafana_admin_password`; пароль не передаётся обычной
переменной окружения ни в Grafana, ни в приложение. Срок хранения метрик задаёт
`PROMETHEUS_RETENTION` (по умолчанию `30d`).

Prometheus опрашивает `http://app:8000/metrics` внутри Compose-сети и не имеет
опубликованного host-порта. Публичный `https://<DOMAIN>/metrics` намеренно
заблокирован Caddy. Grafana также не выставлена в интернет: порт привязан только
к `127.0.0.1:3000` сервера. Для доступа открой SSH-туннель на своём компьютере:

```bash
ssh -N -L 3000:127.0.0.1:3000 root@<IP_СЕРВЕРА>
```

Затем открой `http://127.0.0.1:3000` и войди как `admin` с паролем из
`.env.monitoring`.
Открывать порт 3000 в `ufw` не нужно. Datasource и dashboard **Date4you —
Production overview** создаются автоматически и показывают request rate, долю
5xx, p50/p95/p99 HTTP latency, разбивку route/status, ошибки и latency внешних
зависимостей, результаты входа, backlog/возраст outbox и состояние фоновых задач.

Готовых alert rules намеренно нет. До настройки алёртов нужно собрать
репрезентативный baseline обычной и пиковой нагрузки, согласовать пользовательские
SLO, затем вывести из них пороги и длительности. У каждого будущего алёрта должны
быть понятное действие и runbook; иначе он создаст шум вместо сигнала.

Остановить только monitoring-контейнеры, сохранив данные в именованных volumes:

```bash
docker compose --env-file .env --env-file .env.monitoring \
  --profile monitoring stop grafana prometheus
```

---

## Лимиты и защита

| Что | Лимит |
|-----|-------|
| Голоса | 30 действий в минуту на участника (120 на IP) |
| Вопросы | 10 за 10 минут на гостя (30 на IP) |
| Предложения и их правки | 5 за 10 минут на гостя (15 на IP) |
| Смена имени | 10 за 10 минут на гостя |
| Вход в админку | 10 неудачных попыток за 15 минут с IP |
| Фото | до 5 на событие, до 10 МБ каждое, максимум 8000×8000 |

Плюс: CSRF-токены во всех формах админки, фото доступны только по активной секретной
ссылке, секретные ссылки не индексируются (robots + noindex), HSTS и другие
заголовки в Caddy, CSP с одноразовым nonce (без `unsafe-inline` для скриптов),
cookie с префиксом `__Host-`. IP клиента для лимитов берётся из `X-Real-IP`,
который Caddy перезаписывает сам, — подделать его заголовком нельзя.

---

## Если что-то пошло не так

- **Логи приложения:** `docker compose logs -f app` · **Caddy:** `docker compose logs -f caddy`
- **Статус:** `docker compose ps` — у `app` должно быть `(healthy)`.
- **Приложение не стартует и пишет про SECRET_KEY** — заполни `.env`.
- **PermissionError на /data** — не должно случаться: entrypoint чинит права при старте.
  Если всё же случилось, перезапусти контейнер: `docker compose restart app`.
- **Несколько воркеров uvicorn** — нельзя: лимиты живут в памяти процесса,
  SQLite комфортнее с одним писателем. В Dockerfile уже стоит `--workers 1`.
