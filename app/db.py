"""SQLite: подключение, актуальная схема и миграции.

Версия схемы хранится в PRAGMA user_version:
  v1 — исходная схема;
  v2 — questions: + answer, answered_at (ответы на вопросы);
  v3 — dates: + is_draft (черновики), categories: + moderate_proposals (модерация);
  v4 — guests (имена гостей) и bookings (одна именная бронь на гостя
       в категории) вместо votes; старые голоса переносятся: по одному
       последнему на гостя в категории;
  v5 — questions: + suggest_starts/suggest_ends (гостевые предложения
       времени, которые админ принимает одной кнопкой);
       bookings: свидание может выбрать только ОДИН человек —
       уникальный индекс по date_id с дедупликацией существующих строк;
  v6 — гость снова выбирает НЕСКОЛЬКО свиданий (bookings пересобрана без
       UNIQUE(категория, гость)); описание категории; видео у свиданий
       (date_videos); ручной порядок в категории (date_categories.position);
       модификатор оплаты «50/50» и распознанные ссылки на карты у свиданий.
  v7 — date_images.focus: точка фокуса фото (object-position) для обрезки
       в карточке; NULL = центр.
  v8 — dates: дроп мёртвой колонки is_chosen. Историческая, логика брони
       давно живёт в таблице bookings; колонка только путала.
  v9 — мультитенантность (фундамент): таблица users (вход через Telegram,
       профиль, роли, квота), login_codes (одноразовые коды входа через бота),
       owner_id у categories и dates (NULLABLE на этом этапе — скоупинг ручек
       идёт отдельно). Бэкофилл: все существующие данные отходят служебному
       «легаси-владельцу» (telegram_id=0, is_operator=1), которого при первом
       входе оператора можно «забрать» (сменить telegram_id на реальный).
  v10 — owner_id у categories/dates переведён в NOT NULL (скоупинг ручек
       завершён, осиротевших строк не осталось).
  v11 — reports: жалобы гостей на контент (очередь модерации оператора).
  v12 — users.bot_linked: 1 = запускал бота (можно слать уведомления);
       вход через виджет оставляет 0 до подключения бота. Бэкофилл = 1.
  v13 — вход обязателен для гостевых действий + per-user уведомления + модерация:
       bookings.user_id, questions.user_id, dates.proposed_by (кто совершил
       действие — для уведомлений автору); users.is_reviewed, categories.is_reviewed
       (мягкая очередь модерации, по умолчанию 1 = одобрено); таблица settings
       (глобальные флаги moderate_users/moderate_categories, по умолчанию выкл).
  v14 — categories.og_title/og_desc: редактируемый текст превью секретной ссылки
       (как она выглядит при отправке в мессенджере). NULL = дефолтный текст
       «Тебя ждёт сюрприз ♥ / Открой — внутри кое-что приятное».

Свежая база создаётся сразу по последней схеме. Существующая —
докатывается миграциями при старте приложения.
"""

import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"

LATEST_VERSION = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE NOT NULL,  -- 0 = служебный легаси-владелец
    tg_username TEXT,
    display_name TEXT,            -- отображаемое имя; из TG, редактируемо, можно русское
    avatar_path TEXT,             -- фото профиля, хранится локально (опционально)
    birth_date TEXT,              -- дата рождения (ISO yyyy-mm-dd)
    gender TEXT,                  -- 'm' | 'f'
    is_active INTEGER NOT NULL DEFAULT 1,    -- 0 = забанен
    is_operator INTEGER NOT NULL DEFAULT 0,  -- суперадмин (модерация/баны/лимиты)
    is_reviewed INTEGER NOT NULL DEFAULT 1,  -- 0 = новый, ждёт проверки админом (мягкая очередь)
    date_limit INTEGER NOT NULL DEFAULT 30,  -- квота свиданий; оператор поднимает вручную
    bot_linked INTEGER NOT NULL DEFAULT 0,   -- 1 = запускал бота → можно слать уведомления
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

-- Глобальные флаги платформы (модерация и т.п.). Значения — строки '0'/'1'.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Одноразовые коды входа через бота (deep-link ?start=<code>). TTL чистится в коде.
CREATE TABLE IF NOT EXISTS login_codes (
    code TEXT PRIMARY KEY,
    telegram_id INTEGER,          -- проставляется ботом после /start <code>
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'confirmed'
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- владелец категории
    name TEXT NOT NULL,
    link_token TEXT UNIQUE,
    link_enabled INTEGER NOT NULL DEFAULT 1,
    moderate_proposals INTEGER NOT NULL DEFAULT 0,
    is_reviewed INTEGER NOT NULL DEFAULT 1,  -- 0 = новая, ждёт проверки админом (мягкая очередь)
    description TEXT,          -- видно всем гостям под заголовком страницы
    og_title TEXT,             -- заголовок превью ссылки (NULL = дефолт)
    og_desc TEXT,              -- описание превью ссылки (NULL = дефолт)
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- владелец свидания
    name TEXT NOT NULL,
    place TEXT,
    starts_at TEXT,            -- "YYYY-MM-DDTHH:MM", время МСК
    ends_at TEXT,              -- "YYYY-MM-DDTHH:MM", время МСК (конец диапазона)
    comment TEXT,
    origin TEXT NOT NULL DEFAULT 'admin',   -- 'admin' | 'guest'
    guest_token TEXT,          -- кто предложил (если гость)
    proposed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- автор предложения (для уведомления о публикации)
    is_draft INTEGER NOT NULL DEFAULT 0,    -- черновик / на модерации: гостям не виден
    pay_split INTEGER NOT NULL DEFAULT 0,   -- бейдж «оплата 50/50»
    place_url TEXT,            -- если «место» вставили ссылкой на карты
    archived_at TEXT,          -- NULL = активно
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS date_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS date_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    focus TEXT          -- точка фокуса для обрезки в карточке, напр. "50% 30%"
);

CREATE TABLE IF NOT EXISTS date_categories (
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,    -- ручной порядок внутри категории
    PRIMARY KEY (date_id, category_id)
);

-- Видео свиданий: mp4/webm как есть, отдаются с поддержкой Range
CREATE TABLE IF NOT EXISTS date_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dv_date ON date_videos(date_id);

-- Имя гостя: спрашивается перед первым действием, привязано к cookie-токену
CREATE TABLE IF NOT EXISTS guests (
    token TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Выбор свиданий гостем. Гость может выбрать НЕСКОЛЬКО свиданий в категории,
-- но одно свидание может выбрать только ОДИН человек (уникальный idx_book_date).
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    guest_token TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- залогиненный гость (для уведомлений)
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    guest_token TEXT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- автор вопроса (уведомить при ответе)
    text TEXT NOT NULL,
    answer TEXT,               -- ответ админа: виден автору вопроса
    answered_at TEXT,
    suggest_starts TEXT,       -- если это предложение времени: начало (МСК)
    suggest_ends TEXT,         --   …и конец; админ может «принять» одной кнопкой
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Жалобы гостей на контент. target_id НЕ внешний ключ: при takedown контент
-- удаляется, а жалоба остаётся в очереди для разбора (помечается обработанной).
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,            -- 'date' | 'category'
    target_id INTEGER NOT NULL,
    reporter TEXT,                        -- guest_token пожаловавшегося
    reason TEXT,                          -- текст жалобы (опционально)
    status TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'resolved' | 'dismissed'
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_book_cat ON bookings(category_id);
-- свидание выбирает максимум один человек
CREATE UNIQUE INDEX IF NOT EXISTS idx_book_date ON bookings(date_id);
CREATE INDEX IF NOT EXISTS idx_book_guest ON bookings(category_id, guest_token);
CREATE INDEX IF NOT EXISTS idx_dc_cat ON date_categories(category_id);
CREATE INDEX IF NOT EXISTS idx_q_read ON questions(is_read);
CREATE INDEX IF NOT EXISTS idx_cat_owner ON categories(owner_id);
CREATE INDEX IF NOT EXISTS idx_dates_owner ON dates(owner_id);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at);
"""

# Миграции: ключ — целевая версия, значение — SQL, который к ней приводит.
MIGRATIONS: dict[int, str] = {
    2: """
        ALTER TABLE questions ADD COLUMN answer TEXT;
        ALTER TABLE questions ADD COLUMN answered_at TEXT;
    """,
    3: """
        ALTER TABLE dates ADD COLUMN is_draft INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE categories ADD COLUMN moderate_proposals INTEGER NOT NULL DEFAULT 0;
    """,
    4: """
        CREATE TABLE guests (
            token TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            guest_token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (category_id, guest_token)
        );
        -- из старых множественных голосов оставляем по одному, самому свежему,
        -- на пару (категория, гость) — это и становится бронью.
        -- ROW_NUMBER вместо MAX() с «голыми» колонками: порядок явный
        -- и детерминированный (при равном времени побеждает больший id)
        INSERT INTO bookings(date_id, category_id, guest_token, created_at)
            SELECT date_id, category_id, guest_token, created_at FROM (
                SELECT v.*, ROW_NUMBER() OVER (
                    PARTITION BY category_id, guest_token
                    ORDER BY created_at DESC, id DESC
                ) AS rn FROM votes v
            ) WHERE rn = 1;
        DROP TABLE votes;
        CREATE INDEX IF NOT EXISTS idx_book_cat ON bookings(category_id);
    """,
    5: """
        ALTER TABLE questions ADD COLUMN suggest_starts TEXT;
        ALTER TABLE questions ADD COLUMN suggest_ends TEXT;
        -- правило «одно свидание — один человек»: перед уникальным индексом
        -- убираем возможные дубли, оставляя самую свежую бронь на свидание
        DELETE FROM bookings WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY date_id
                    ORDER BY created_at DESC, id DESC
                ) AS rn FROM bookings
            ) WHERE rn = 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_book_date ON bookings(date_id);
    """,
    6: """
        ALTER TABLE categories ADD COLUMN description TEXT;
        ALTER TABLE dates ADD COLUMN pay_split INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE dates ADD COLUMN place_url TEXT;
        -- date_categories исторически появлялась только из idempotent-прогона
        -- SCHEMA в конце init_db, а не из миграции, — поэтому к моменту v6
        -- её может не быть. Создаём при необходимости, затем добавляем position.
        CREATE TABLE IF NOT EXISTS date_categories (
            date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            PRIMARY KEY (date_id, category_id)
        );
        ALTER TABLE date_categories ADD COLUMN position INTEGER NOT NULL DEFAULT 0;
        CREATE TABLE date_videos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_dv_date ON date_videos(date_id);
        -- bookings тоже могло не быть до v4 в очень старых базах
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            guest_token TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        -- гость снова может выбрать несколько свиданий: пересобираем bookings
        -- без UNIQUE(категория, гость). «Одно свидание — один человек» остаётся
        -- уникальным индексом по date_id.
        CREATE TABLE bookings_v6(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            guest_token TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO bookings_v6(id, date_id, category_id, guest_token, created_at)
            SELECT id, date_id, category_id, guest_token, created_at FROM bookings;
        DROP TABLE bookings;
        ALTER TABLE bookings_v6 RENAME TO bookings;
        CREATE UNIQUE INDEX idx_book_date ON bookings(date_id);
        CREATE INDEX idx_book_cat ON bookings(category_id);
        CREATE INDEX idx_book_guest ON bookings(category_id, guest_token);
    """,
    7: """
        -- точка фокуса фото для обрезки в карточке (object-position), напр. "50% 30%".
        -- NULL = центр (как было раньше). date_images в очень старых базах могла
        -- создаваться только idempotent-прогоном SCHEMA в конце init_db, а не
        -- миграцией, — поэтому к моменту v7 её может не быть. Создаём при
        -- необходимости, затем добавляем колонку.
        CREATE TABLE IF NOT EXISTS date_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0
        );
        ALTER TABLE date_images ADD COLUMN focus TEXT;
    """,
    8: """
        -- Дроп мёртвой колонки is_chosen: логика брони давно в таблице bookings.
        -- DROP COLUMN поддержан в SQLite ≥ 3.35 (на проде python:3.12-slim — есть).
        ALTER TABLE dates DROP COLUMN is_chosen;
    """,
    9: """
        -- Мультитенантность: пользователи, коды входа, владельцы у корневых сущностей.
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            tg_username TEXT,
            display_name TEXT,
            avatar_path TEXT,
            birth_date TEXT,
            gender TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_operator INTEGER NOT NULL DEFAULT 0,
            date_limit INTEGER NOT NULL DEFAULT 30,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );
        CREATE TABLE login_codes (
            code TEXT PRIMARY KEY,
            telegram_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        -- owner_id NULLABLE: на этом этапе скоупинг ручек ещё не сделан, жёсткий
        -- NOT NULL сломал бы текущие INSERT'ы. Ужесточим, когда переведём ручки.
        ALTER TABLE categories ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
        ALTER TABLE dates ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
        -- Служебный «легаси-владелец»: на него отходят все существующие данные.
        -- При первом входе оператора (OPERATOR_TG_IDS) его telegram_id меняется
        -- на реальный — и легаси-данные автоматически становятся данными оператора.
        INSERT INTO users(telegram_id, display_name, is_operator, created_at)
            VALUES(0, 'Легаси-владелец', 1, strftime('%Y-%m-%dT%H:%M:%S','now'));
        UPDATE categories SET owner_id = (SELECT id FROM users WHERE telegram_id=0)
            WHERE owner_id IS NULL;
        UPDATE dates SET owner_id = (SELECT id FROM users WHERE telegram_id=0)
            WHERE owner_id IS NULL;
        CREATE INDEX idx_cat_owner ON categories(owner_id);
        CREATE INDEX idx_dates_owner ON dates(owner_id);
    """,
    10: """
        -- Ужесточаем owner_id до NOT NULL: к этому моменту все ручки штампуют
        -- владельца, а v9 backfill'нул легаси-данные. SQLite не умеет ADD/ALTER
        -- NOT NULL — пересобираем таблицы (12-шаговая процедура из доки SQLite).
        -- ВАЖНО: миграции идут с foreign_keys=OFF (см. init_db), иначе DROP TABLE
        -- dates каскадом снёс бы все дочерние строки (links/images/bookings/...).
        -- id сохраняются 1:1, поэтому дочерние FK остаются валидными.

        -- Подстраховка: если вдруг остались NULL — отдать легаси-владельцу.
        UPDATE categories SET owner_id=(SELECT id FROM users WHERE telegram_id=0)
            WHERE owner_id IS NULL;
        UPDATE dates SET owner_id=(SELECT id FROM users WHERE telegram_id=0)
            WHERE owner_id IS NULL;

        CREATE TABLE categories_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            link_token TEXT UNIQUE,
            link_enabled INTEGER NOT NULL DEFAULT 1,
            moderate_proposals INTEGER NOT NULL DEFAULT 0,
            description TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO categories_new SELECT id, owner_id, name, link_token,
            link_enabled, moderate_proposals, description, created_at FROM categories;
        DROP TABLE categories;
        ALTER TABLE categories_new RENAME TO categories;

        CREATE TABLE dates_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            place TEXT,
            starts_at TEXT,
            ends_at TEXT,
            comment TEXT,
            origin TEXT NOT NULL DEFAULT 'admin',
            guest_token TEXT,
            is_draft INTEGER NOT NULL DEFAULT 0,
            pay_split INTEGER NOT NULL DEFAULT 0,
            place_url TEXT,
            archived_at TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO dates_new SELECT id, owner_id, name, place, starts_at, ends_at,
            comment, origin, guest_token, is_draft, pay_split, place_url,
            archived_at, created_at FROM dates;
        DROP TABLE dates;
        ALTER TABLE dates_new RENAME TO dates;

        CREATE INDEX idx_cat_owner ON categories(owner_id);
        CREATE INDEX idx_dates_owner ON dates(owner_id);
    """,
    11: """
        CREATE TABLE reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_type TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            reporter TEXT,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        );
        CREATE INDEX idx_reports_status ON reports(status, created_at);
    """,
    12: """
        -- bot_linked: 1 = пользователь запускал бота (deeplink-вход) → ему можно
        -- слать уведомления. Вход через Telegram-виджет НЕ запускает бота, поэтому
        -- такие аккаунты остаются с 0, пока не подключат бота. Бэкофилл: все, кто
        -- уже есть, входили только через deeplink — значит бот у них подключён.
        ALTER TABLE users ADD COLUMN bot_linked INTEGER NOT NULL DEFAULT 0;
        UPDATE users SET bot_linked=1 WHERE telegram_id <> 0;
    """,
    13: """
        -- Вход обязателен для гостевых действий + per-user уведомления + модерация.
        -- Кто совершил действие (для уведомлений автору): nullable, старые записи
        -- остаются с NULL (показ по guests.name, как раньше). REFERENCES без
        -- проверки FK: миграции идут с foreign_keys=OFF, целостность не нарушается
        -- (новые колонки пустые).
        ALTER TABLE bookings ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
        ALTER TABLE questions ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
        ALTER TABLE dates ADD COLUMN proposed_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
        -- Мягкая очередь модерации: 1 = одобрено (дефолт для всех существующих),
        -- 0 проставляется только новым, когда соответствующий флаг включён.
        ALTER TABLE users ADD COLUMN is_reviewed INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE categories ADD COLUMN is_reviewed INTEGER NOT NULL DEFAULT 1;
        -- Глобальные флаги платформы (moderate_users/moderate_categories).
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """,
    14: """
        -- Редактируемое превью секретной ссылки (как она выглядит при отправке).
        -- NULL = дефолтный текст «Тебя ждёт сюрприз ♥ / Открой — внутри кое-что
        -- приятное» (рендерится в шаблоне).
        ALTER TABLE categories ADD COLUMN og_title TEXT;
        ALTER TABLE categories ADD COLUMN og_desc TEXT;
    """,
}


def connect() -> sqlite3.Connection:
    # check_same_thread=False обязателен: FastAPI открывает sync-зависимость,
    # выполняет эндпоинт и закрывает соединение в РАЗНЫХ потоках тредпула.
    # Само соединение живёт строго внутри одного запроса (последовательно),
    # а sqlite3 в CPython собран в serialized-режиме — это безопасно.
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='categories'"
    ).fetchone()
    ver = conn.execute("PRAGMA user_version").fetchone()[0]

    if not has_tables:
        # Свежая база — сразу последняя схема (одной транзакцией)
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{SCHEMA}\nPRAGMA user_version={LATEST_VERSION};\nCOMMIT;")
        ver = LATEST_VERSION
    elif ver == 0:
        # База, созданная до появления миграций, — это v1
        ver = 1
        conn.execute("PRAGMA user_version=1")

    while ver < LATEST_VERSION:
        ver += 1
        # Каждая миграция атомарна: схема + bump user_version коммитятся вместе,
        # поэтому падение в любой момент не оставит «полуприменённую» версию.
        # foreign_keys ВЫКЛючаем на время миграций: некоторые из них пересобирают
        # таблицы (DROP+RENAME), а с FK=ON это каскадом снесло бы дочерние строки.
        # PRAGMA вне транзакции — поэтому ставим до BEGIN, проверяем целостность
        # после COMMIT через foreign_key_check (вернёт строки при нарушении).
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{MIGRATIONS[ver]}\nPRAGMA user_version={ver};\nCOMMIT;")
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.execute("PRAGMA foreign_keys=ON")
        if broken:
            raise RuntimeError(f"Миграция v{ver} нарушила ссылочную целостность: {broken[:5]}")

    # Страховка: SCHEMA целиком написана через IF NOT EXISTS, поэтому прогоняем
    # её и после миграций — если в старой базе исторически не хватало какой-то
    # служебной таблицы или индекса, они появятся здесь (idempotent, мгновенно).
    conn.executescript(SCHEMA)

    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
