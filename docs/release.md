# Выпуск и восстановление beta

Единственная процедура обновления — `scripts/release.py`. `update.sh <полный SHA>`
только активирует заранее проверенный artifact. Он не выполняет `git pull`, build,
commit или push. Все команды ниже — инструкции оператору; Phase C не выполняет
production deployment.

## 1. Проверка и подготовка

Хост: Linux, Python 3.12+, Git, Docker Engine + Compose v2. Для release gate нужны
именно Python 3.12/Linux, зависимости приложения/тестов, Chromium, bash/sh, ffmpeg.
Windows developer gate отдельно показывает отсутствие production runtime и не
выдаёт разрешение на release. Все тесты запускаются в отдельных процессах из
уникальной копии исходников, с временными DATA_DIR/TMP и отключёнными интеграциями.
Любой test skip или отсутствие обязательного инструмента закрывает release gate.

```bash
# В чистом checkout нужного полного SHA (не переключайте работающий image):
git fetch origin
git switch --detach <40-символьный-SHA>
python3.12 -m venv .venv
.venv/bin/pip install -c app/constraints.txt -r app/requirements.txt -r app/requirements-test.txt
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/python scripts/release_gate.py --sha <SHA> --mode release --output artifacts/release-gate

# Точный путь receipt.json печатает gate. Допустим доверенный receipt из CI того же SHA.
python3 scripts/release.py prepare --sha <SHA> --receipt <путь/receipt.json>
```

`prepare` проверяет полный SHA, tree, чистоту checkout и полный PASS release receipt.
Receipt — доверенное свидетельство CI/оператора, не криптографическая подпись.
Код для сборки извлекается из `git archive <SHA>`. Единственный собранный app image
проходит проверку реального старта, migrations, `/ready`, `/login`, ffmpeg и backup
на tmpfs без сети/production env. Caddyfile валидируется сохранённым proxy image.

`.release/artifacts/<SHA>/` содержит app/Caddy image IDs, `docker save` архивы,
SHA256 файлов, gate receipt, Caddyfile и извлечённую из image статику. Один SHA
нельзя повторно пересобрать поверх существующего artifact. CI сохраняет artifacts
ограниченное время; перед release перенесите весь каталог в защищённое постоянное
хранилище сервера. Потерянный image загружается из retained tar, не пересобирается.

Транзитивные constraints сохраняют версии проверенного окружения A/B, включая
Starlette 1.3.1. Это не полный portable/hash lock: `python:3.12-slim`, Caddy tag,
apt, pip и Linux-only uvloop ещё разрешаются при **первой** сборке. Сохранённый
artifact воспроизводит исполнение/откат без нового разрешения зависимостей;
побитовая воспроизводимость повторной сборки из исходников не заявляется.

## 2. Активация

Оператор заранее проверяет `.env`, наличие места для двух образов и полного
DB/uploads recovery point, совместимость изменений настроек и выбранный SHA.
Приложение остаётся в одном uvicorn worker. `.release/` содержит закрытые снимки
данных; права каталога 0700, доступ и удалённое хранилище — только оператору.

```bash
python3 scripts/release.py deploy --sha <SHA>
# Только для первого запуска с совершенно пустым data и без текущего сервиса:
python3 scripts/release.py deploy --sha <SHA> --first-install
python3 scripts/release.py status
```

При первом переходе со старого deployment сохраняется **реальный работающий** app
image и исходный Caddy image. Импортированный `legacy-<id>` не получает выдуманного
PASS старого gate. Его rollback использует сохранённый app image и новый проверенный
proxy с content-addressed static. Первую миграцию старой установки нужно отрепетировать
на копии данных до production.

Дальнейшие шаги выполняются под одной exclusive lock с backup:

1. Сохранить previous artifact; записать `maintenance` в `.release/state.json`.
2. Остановить Caddy (входящий трафик), затем app (HTTP и фоновые writers).
3. Создать и проверить отдельный DB+uploads bundle **до** старта candidate/migrations.
4. Установить конфигурацию с точными image IDs. Запустить app с `--no-build --pull never`.
5. Проверять `/ready`, release identity и получение SQLite writer lock: deadline 60 s,
   timeout отдельной Docker-команды не более 5 s и не больше остатка общего deadline.
6. Записать `opening` и факт возможного traffic **до** старта Caddy; затем `active`.

Короткий downtime здесь намеренный: SQLite и uploads нельзя согласованно остановить
одним DB lock. Ошибка оставляет понятную незавершённую запись; автоматического DB
rollback нет. При остановленном Caddy снаружи возможен отказ соединения, а не красивая
maintenance page. Прямого публичного порта app нет; внутренним writers/ручным скриптам
тоже нельзя менять data во время операции.

Не запускайте `docker compose up --build`, `docker system prune -a` или ручной restart
другого image поверх управляемого релиза. Для logs/exec используйте:

```bash
python3 scripts/release.py compose logs --tail 100 app
python3 scripts/release.py compose ps
```

`.env` остаётся операторской конфигурацией и применяется при recreation контейнера.
Code rollback не возвращает старые секреты/настройки: их совместимость проверяется
отдельно; сохраните закрытую версию конфигурации у оператора. `setup-backup.sh`
сохраняет выбранный `TG_BACKUP_CHAT_ID`, включая отключение, и применяет текущий `.env`
к **тому же retained image** перед контрольным backup.

Профиль monitoring в исходном `docker-compose.yml` сохранён. Управляемый compose
переключает только app/Caddy и не удаляет уже работающие Prometheus/Grafana. Для
отдельного запуска/обновления monitoring задайте `APP_IMAGE` равным active image ID
из artifact manifest и запускайте только `prometheus grafana` с `--no-deps`;
не запускайте всю исходную конфигурацию поверх managed app/Caddy.

## 3. Static и health

Версионированные `/static/file?v=<sha256[:12]>` обслуживаются Caddy из append-only
`.release/assets/<hash>/static/file`, заполненного из сохранённых images. Build
failure не меняет байты старых URL. Rollback сохраняет и старые, и новые адреса;
не удаляйте опубликованные assets в течение срока immutable-cache (год), для beta
автоматическая очистка assets отключена. Рабочее дерево `app/static` не монтируется
в Caddy. Неверный/неизвестный hash возвращает 404. Без `v` запрос идёт в backend
image; такие URL не имеют immutable-контракта. Прямой backend проверяет совпадение
версии с фактическим содержимым и не выдаёт изменённый файл под старым hash.

`/health` означает, что процесс обслуживает запросы; Docker healthcheck использует
его. `/ready` выполняет `BEGIN IMMEDIATE` + `ROLLBACK` отдельным соединением с timeout
100 ms, доступен только внутри Docker-сети (Caddy закрывает его снаружи), проверяет schema и возвращает `database_busy`/`database_unavailable`/schema
mismatch с 503. Короткая конкуренция writer не должна вести к рестартам. Проверка
не пишет пользовательских строк и не измеряет, сколько будущих uploads поместится
на диск. Свободное место, длительное ожидание SQLite и ошибки транзакций требуют
отдельного наблюдения; зелёный `/health` не является допуском релиза.

Повторяющиеся 500 группируются по method, route template и типу исключения.
Request ID остаётся в диагностике; секретные tokens/сырые URL не входят в group key.

## 4. Отказ обновления и code rollback

```bash
python3 scripts/release.py status
# После устранения причины readiness: повторить candidate, не меняя DB.
python3 scripts/release.py resume
# Явно подтверждённая семантическая совместимость; только recorded previous.
python3 scripts/release.py rollback --release <previous-ID> --schema-compatible
```

`resume` из `maintenance` возвращает прежний artifact, поскольку candidate ещё не
стартовал. Из `candidate/opening` продолжает проверку target. Code rollback допускается
только при совпадении **фактической** schema с ожидаемой предыдущим image и явном
решении оператора о совместимости данных/конфигурации. Перед откатом снова сохраняется
текущая DB+media; новые пользовательские данные не заменяются старым snapshot.

Одинаковый `user_version` не доказывает семантическую совместимость. При изменении
schema или несовместимой миграции оставьте traffic закрытым и используйте отдельное
восстановление. Phase C не изменяет schema v38. Фаза A требует повторного входа для
старых cookies без registry session; это ожидаемое изменение, не повод откатывать A.

После crash `operation.lock` может остаться. Удалять её можно только после проверки,
что прежний процесс release/backup завершён; не обходите работающий lock. Сначала
сохраните `state.json` и посмотрите status/current/target/previous/recovery.

## 5. Резервирование и полное восстановление

Автоматические SQLite-only `data/backups/app-*.db` публикуются через validated
temporary file + fsync + atomic rename. Частичный или старый legacy-файл не подавляет
retry; retention последних 14 новых snapshots выполняется только после успеха.
**Эти копии не гарантируют восстановление media**, в том числе DB-only Telegram copy.

Полная точка — `app.db + uploads/ + manifest.json` в `.release/recovery/<id>/`.
Manifest связывает schema, release/image, hashes/размеры и ссылки БД на оригинальные
файлы. Отсутствующий оригинал блокирует publication. Responsive/OG caches восстанавливаются
приложением. Create требует остановки всех writers; один SQLite backup не защищает
файловую систему. Restore проверяет hashes/inventory/SQLite integrity/ссылки и
публикует **только новый** каталог, никогда не перезаписывает существующий DATA_DIR.

```bash
# Локальный полный bundle с короткой остановкой app/Caddy:
python3 scripts/release.py backup
# Полный bundle -> удалённое хранилище; daily cron, 30 завершённых bundles:
RCLONE_REMOTE=backup:date4you KEEP_REMOTE=30 ./scripts/backup.sh
# Например 00:00 МСК на сервере UTC:
# 0 21 * * * cd /opt/date4you && ./scripts/backup.sh >> /var/log/date4you-backup.log 2>&1

# Отдельный restore-drill: никакого запуска восстановленной копии в production.
python3 app/recovery.py verify /safe/recovery/<id>
python3 app/recovery.py restore /safe/recovery/<id> --destination /safe/restored-new
```

Remote publication копирует DB+media, проверяет bytes через `rclone check --download`,
и только затем загружает manifest как commit marker. Retention после успешной
публикации удаляет только **целые завершённые bundles**, сохраняя последние 30.
Незавершённый remote каталог без manifest не является backup. Старые DB-only remote
snapshots/uploads-trash остаются для ручного разбора; новый процесс не продлевает
их прежние гарантии. Новые bundles не зависят от прежней семидневной media trash.
Локальные полные bundles/images автоматически не удаляются: следите за местом,
удаляйте целую точку только после off-host verification и проверки ссылок state.

Для реального полного restore сначала закройте traffic и остановите все writers,
сохраните **текущее** состояние отдельным forensic bundle. Восстановите выбранную
точку в новый каталог, проверьте её с соответствующим retained image без внешних
интеграций. Решение заменить live data принимает оператор после явного согласования
потери всех изменений после выбранной точки; скрипт этого переключения не делает.
Не копируйте один старый `app.db` поверх текущих WAL/SHM или uploads.

Плановый RPO при успешном ежедневном off-host backup — до 24 h; при пропусках он
увеличивается до возраста последнего проверенного bundle. Pre-migration RPO — момент
остановки перед запуском candidate. RTO не измерен на production объёме: включает
полное копирование/проверку DB+media, загрузку image и readiness. Ориентир для beta —
десятки минут с измерением на реальном объёме в staging, не обещание SLA. Remote restore,
TLS и Linux/Docker failed-update drill должны пройти до production launch.

Семантика команд сверена с [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)
и [Caddy rewrite](https://caddyserver.com/docs/caddyfile/directives/rewrite).
