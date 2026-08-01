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
       bookings: событие может выбрать только ОДИН человек —
       уникальный индекс по date_id с дедупликацией существующих строк;
  v6 — гость снова выбирает НЕСКОЛЬКО событий (bookings пересобрана без
       UNIQUE(категория, гость)); описание категории; видео у событий
       (date_videos); ручной порядок в категории (date_categories.position);
       модификатор оплаты «50/50» и распознанные ссылки на карты у событий.
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
       выбранного оформления.
  v15 — categories.og_image: своя картинка превью ссылки (WebP, как фото событий).
       NULL = дефолтная картинка выбранного оформления.
  v16 — dates.share_token: стабильная секретная ссылка на ОТДЕЛЬНОЕ событие
       (/d/<токен>). По ней другой залогиненный пользователь добавляет копию
       события себе в коллекцию. Уникальный индекс (несколько NULL допустимо);
       существующие события получают токен бэкофиллом.
  v18 — dates.is_public: событие попадает в общую ленту комьюнити на главной
       (по умолчанию 1 = публичное). Приватные (0) видны только владельцу и по
       секретным ссылкам, но не в ленте. Бэкофилл существующих = 1 (дефолт).
  v21 — categories.og_focus: точка фокуса своей картинки превью ссылки «X% Y%»
       (как date_images.focus). og:image кропается по ней в 1200×630 (WYSIWYG
       с редактором). NULL = центр.
  v22 — голосование с ручной настройкой категории: режим single/multiple,
       обязательный дедлайн, явный статус и зафиксированный результат;
       вместимость события 1..100 считается отдельно в каждой категории,
       глобальная уникальность брони по date_id снята. После результата участник
       может отказаться без удаления голоса. Также: настройка эффекта курсора
       в профиле и отдельный purpose у Telegram-кодов для безопасной привязки.
  v23 — надёжная очередь Telegram-уведомлений: дедупликация событий,
       отложенная отправка, срок жизни, повторные попытки и отмена.
  v24 — DB-инварианты голосования: single-режим не включается
        поверх несовместимых голосов, а дедлайн всегда раньше
        старта каждого активного кандидата, в том числе при гонках
        между параллельными запросами.
  v25 — одноразовая починка легаси-событий, которые оставались черновиками
        после привязки к категории; составные и частичные индексы для
        авторизации медиа, голосований, автоархива и непрочитанных вопросов.
  v26 — независимые оформления: category_skin управляет публичной страницей
        категории, admin_skin — кабинетом пользователя. Старые категории
        сохраняют романтическое оформление, новые по умолчанию дружеские.
  v27 — настройки типов Telegram-уведомлений и действия rich-карточек:
        inline-кнопка хранится вместе с отложенным сообщением outbox.
  v28 — социальные отметки событий и обзоры: независимое «Хочу сходить»,
        публичные отзывы с рейтингом и отдельная настройка уведомлений об
        обзорах.
  v29 — пользовательские события без категории больше не считаются
        «неактивными». Старые черновики владельцев становятся активными, но
        приватными, чтобы миграция сама не опубликовала ранее скрытый контент;
        гостевые предложения на модерации не затрагиваются.
  v30 — фиксированное стандартное превью категории и пользовательская очередь
        событий, которые ждут обзора после завершения, отказа или удаления
        ранее созданного обзора.

Свежая база создаётся сразу по последней схеме. Существующая —
докатывается миграциями при старте приложения.
"""

import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "app.db"

LATEST_VERSION = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,           -- NULL = аккаунт только через OAuth; 0 = служебный легаси-владелец
    tg_username TEXT,
    display_name TEXT,            -- отображаемое имя; из TG, редактируемо, можно русское
    avatar_path TEXT,             -- фото профиля, хранится локально (опционально)
    birth_date TEXT,              -- дата рождения (ISO yyyy-mm-dd)
    gender TEXT,                  -- 'm' | 'f'
    is_active INTEGER NOT NULL DEFAULT 1,    -- 0 = забанен
    is_operator INTEGER NOT NULL DEFAULT 0,  -- суперадмин (модерация/баны/лимиты)
    is_reviewed INTEGER NOT NULL DEFAULT 1,  -- 0 = новый, ждёт проверки админом (мягкая очередь)
    date_limit INTEGER NOT NULL DEFAULT 30,  -- квота событий; оператор поднимает вручную
    bot_linked INTEGER NOT NULL DEFAULT 0,   -- 1 = запускал бота → можно слать уведомления
    cursor_effects INTEGER NOT NULL DEFAULT 0, -- 1 = декоративные эффекты курсора включены
    admin_skin TEXT NOT NULL DEFAULT 'friends'
        CHECK(admin_skin IN ('friends', 'romantic')), -- оформление личного кабинета
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

-- Надёжная очередь пользовательских Telegram-уведомлений. chat_id намеренно
-- не сохраняется: он резолвится по user_id непосредственно перед отправкой,
-- поэтому сообщение дождётся поздней привязки Telegram к аккаунту.
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    action_url TEXT,
    action_label TEXT,
    send_at TEXT NOT NULL,
    expires_at TEXT,
    sent_at TEXT,
    cancelled_at TEXT,
    claimed_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
    ON notification_outbox(sent_at, cancelled_at, send_at);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_user
    ON notification_outbox(user_id, kind);

-- Пользователь управляет смысловыми группами Telegram-уведомлений. Отсутствие
-- строки означает безопасный обратносуместимый дефолт: все группы включены.
CREATE TABLE IF NOT EXISTS notification_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    votes INTEGER NOT NULL DEFAULT 1 CHECK(votes IN (0, 1)),
    questions INTEGER NOT NULL DEFAULT 1 CHECK(questions IN (0, 1)),
    proposals INTEGER NOT NULL DEFAULT 1 CHECK(proposals IN (0, 1)),
    updates INTEGER NOT NULL DEFAULT 1 CHECK(updates IN (0, 1)),
    reminders INTEGER NOT NULL DEFAULT 1 CHECK(reminders IN (0, 1)),
    reviews INTEGER NOT NULL DEFAULT 1 CHECK(reviews IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Привязки OAuth-провайдеров к аккаунту (Discord/Google/Yandex).
CREATE TABLE IF NOT EXISTS oauth_accounts (
    provider TEXT NOT NULL,               -- 'discord' | 'google' | 'yandex'
    provider_uid TEXT NOT NULL,           -- стабильный id пользователя у провайдера
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (provider, provider_uid)
);
CREATE INDEX IF NOT EXISTS idx_oauth_user ON oauth_accounts(user_id);

-- Глобальные флаги платформы (модерация и т.п.). Значения — строки '0'/'1'.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Одноразовые коды входа через бота (deep-link ?start=<code>). TTL чистится в коде.
CREATE TABLE IF NOT EXISTS login_codes (
    code TEXT PRIMARY KEY,
    telegram_id INTEGER,          -- проставляется ботом после /start <code>
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/awaiting_confirmation/processing/confirmed/conflict
    purpose TEXT NOT NULL DEFAULT 'login',   -- 'login' | 'link'
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, -- аккаунт для link-кода
    error TEXT,                   -- машинная причина неуспеха, напр. telegram_in_use
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- владелец категории
    name TEXT NOT NULL,
    category_skin TEXT NOT NULL DEFAULT 'friends'
        CHECK(category_skin IN ('friends', 'romantic')), -- оформление публичной ссылки
    link_token TEXT UNIQUE,
    link_enabled INTEGER NOT NULL DEFAULT 1,
    moderate_proposals INTEGER NOT NULL DEFAULT 0,
    is_reviewed INTEGER NOT NULL DEFAULT 1,  -- 0 = новая, ждёт проверки админом (мягкая очередь)
    description TEXT,          -- видно всем гостям под заголовком страницы
    og_title TEXT,             -- заголовок превью ссылки (NULL = дефолт)
    og_desc TEXT,              -- описание превью ссылки (NULL = дефолт)
    og_image TEXT,             -- картинка превью ссылки, WebP-файл (NULL = дефолт skin)
    og_focus TEXT,             -- точка фокуса своей картинки превью: «X% Y%» (NULL = центр)
    use_default_preview INTEGER NOT NULL DEFAULT 0
        CHECK(use_default_preview IN (0, 1)), -- 1 = не заменять дефолт авто-коллажем
    choice_mode TEXT CHECK(choice_mode IN ('single', 'multiple')),
    voting_deadline TEXT,      -- задаётся владельцем явно, время МСК
    voting_status TEXT NOT NULL DEFAULT 'unconfigured'
        CHECK(voting_status IN ('unconfigured', 'open', 'tie', 'resolved', 'no_winner')),
    closed_at TEXT,            -- момент заморозки бюллетеней после дедлайна
    resolved_at TEXT,          -- момент появления финального результата
    winner_date_id INTEGER REFERENCES dates(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- владелец события
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
    share_token TEXT,          -- секретная ссылка на это событие (/d/<токен>) для «добавить себе»
    is_public INTEGER NOT NULL DEFAULT 1,   -- 1 = видно в общей ленте комьюнити
    capacity INTEGER NOT NULL DEFAULT 1 CHECK(capacity BETWEEN 1 AND 100),
    archived_at TEXT,          -- NULL = активно
    created_at TEXT NOT NULL
);

-- Независимая от «добавить себе в коллекцию» связь с исходным событием.
-- Публичный профиль дополнительно проверяет публичность самого события: так
-- секретная /d-ссылка не может случайно раскрыться через чужой профиль.
CREATE TABLE IF NOT EXISTS date_wants (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    is_public INTEGER NOT NULL DEFAULT 1 CHECK(is_public IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, date_id)
);
CREATE INDEX IF NOT EXISTS idx_date_wants_profile
    ON date_wants(user_id, is_public, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_date_wants_date
    ON date_wants(date_id, user_id);

-- Один обзор пользователя на одно событие. Скрытие из профиля мягкое: текст и
-- рейтинг остаются владельцу для последующего редактирования/публикации.
CREATE TABLE IF NOT EXISTS date_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    text TEXT,
    is_public INTEGER NOT NULL DEFAULT 1 CHECK(is_public IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, date_id)
);
CREATE INDEX IF NOT EXISTS idx_date_reviews_profile
    ON date_reviews(user_id, is_public, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_date_reviews_date
    ON date_reviews(date_id, is_public, updated_at DESC);

-- Очередь центра уведомлений «Ждут отзыва». Строка появляется, когда обзор
-- становится уместен, пользователь откладывает его или удаляет уже созданный.
-- Удаление упоминания не затрагивает само событие и право оставить обзор позже.
CREATE TABLE IF NOT EXISTS review_queue (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    reason TEXT NOT NULL DEFAULT 'due'
        CHECK(reason IN ('due', 'declined', 'review_deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, date_id)
);
CREATE INDEX IF NOT EXISTS idx_review_queue_user
    ON review_queue(user_id, updated_at DESC);

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

-- Видео событий: mp4/webm как есть, отдаются с поддержкой Range
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

-- Голоса за события. Допустимое число вариантов у участника задаёт
-- categories.choice_mode, а dates.capacity ограничивает участников отдельно
-- в каждой категории (проверяется доменным модулем и триггерами ниже).
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    guest_token TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- залогиненный гость (для уведомлений)
    participation_withdrawn_at TEXT, -- отказ после результата; сам голос сохраняется
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
-- один участник голосует за конкретное событие в категории только один раз;
-- capacity применяется к каждой паре (date_id, category_id) независимо.
CREATE UNIQUE INDEX IF NOT EXISTS idx_book_vote
    ON bookings(date_id, category_id, guest_token);
CREATE INDEX IF NOT EXISTS idx_book_guest ON bookings(category_id, guest_token);
CREATE INDEX IF NOT EXISTS idx_book_cat_user
    ON bookings(category_id, user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dc_cat ON date_categories(category_id);
CREATE INDEX IF NOT EXISTS idx_q_read ON questions(is_read);
CREATE INDEX IF NOT EXISTS idx_q_date ON questions(date_id);
CREATE INDEX IF NOT EXISTS idx_q_date_read ON questions(date_id, is_read);
CREATE INDEX IF NOT EXISTS idx_di_date ON date_images(date_id);
CREATE INDEX IF NOT EXISTS idx_di_filename_date ON date_images(filename, date_id);
CREATE INDEX IF NOT EXISTS idx_dl_date ON date_links(date_id);
CREATE INDEX IF NOT EXISTS idx_dv_filename_date ON date_videos(filename, date_id);
CREATE INDEX IF NOT EXISTS idx_cat_owner ON categories(owner_id);
CREATE INDEX IF NOT EXISTS idx_categories_og_image
    ON categories(og_image, owner_id) WHERE og_image IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_categories_open_deadline
    ON categories(voting_deadline)
    WHERE voting_status='open' AND closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_categories_winner_date
    ON categories(winner_date_id)
    WHERE voting_status='resolved' AND winner_date_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dates_owner ON dates(owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dates_share ON dates(share_token);
-- лента комьюнити на главной: свежие публичные активные события
CREATE INDEX IF NOT EXISTS idx_dates_public ON dates(is_public, is_draft, archived_at, id);
-- Частичный покрывающий индекс: глобальный проход сканирует только активные
-- события с датой, а с owner_id тот же индекс обслуживает адресный проход.
CREATE INDEX IF NOT EXISTS idx_dates_autoarchive_active
    ON dates(owner_id, ends_at, starts_at, archived_at)
    WHERE archived_at IS NULL
      AND (starts_at IS NOT NULL OR ends_at IS NOT NULL);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at);

-- Даже прямые записи в bookings не могут переполнить событие. SQLite
-- сериализует пишущие транзакции, поэтому проверка COUNT и INSERT атомарны
-- относительно другого writer'а.
CREATE TRIGGER IF NOT EXISTS trg_bookings_capacity_insert
BEFORE INSERT ON bookings
WHEN (
    SELECT COUNT(*) FROM bookings
    WHERE date_id=NEW.date_id AND category_id=NEW.category_id
) >= COALESCE((SELECT capacity FROM dates WHERE id=NEW.date_id), 0)
BEGIN
    SELECT RAISE(ABORT, 'booking_capacity_reached');
END;

-- В single-режиме у участника не может быть двух вариантов в категории.
CREATE TRIGGER IF NOT EXISTS trg_bookings_single_insert
BEFORE INSERT ON bookings
WHEN (SELECT choice_mode FROM categories WHERE id=NEW.category_id)='single'
 AND EXISTS (
    SELECT 1 FROM bookings
    WHERE category_id=NEW.category_id AND guest_token=NEW.guest_token
 )
BEGIN
    SELECT RAISE(ABORT, 'single_choice_only');
END;

-- closed_at блокирует появление новых бюллетеней. Удаление/изменение старых
-- запрещает доменный модуль: DB-триггер на DELETE/UPDATE здесь намеренно не
-- ставим, иначе он также заблокирует штатные FK-каскады удаления аккаунта.
CREATE TRIGGER IF NOT EXISTS trg_bookings_closed_insert
BEFORE INSERT ON bookings
WHEN EXISTS (
    SELECT 1 FROM categories WHERE id=NEW.category_id AND closed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'voting_closed');
END;

-- Нельзя уменьшить capacity ниже уже набранного количества ни в одной из
-- категорий, где используется это событие.
CREATE TRIGGER IF NOT EXISTS trg_dates_capacity_update
BEFORE UPDATE OF capacity ON dates
WHEN EXISTS (
    SELECT 1 FROM bookings WHERE date_id=OLD.id
    GROUP BY category_id HAVING COUNT(*) > NEW.capacity
)
BEGIN
    SELECT RAISE(ABORT, 'capacity_below_existing_votes');
END;

-- Переключить категорию в single нельзя, если сохранённые бюллетени уже
-- содержат несколько вариантов одного участника. Это последний рубеж помимо
-- доменной проверки и остаётся атомарным при параллельных запросах.
CREATE TRIGGER IF NOT EXISTS trg_categories_single_update
BEFORE UPDATE OF choice_mode ON categories
WHEN NEW.choice_mode='single' AND EXISTS (
    SELECT 1 FROM bookings WHERE category_id=NEW.id
    GROUP BY guest_token HAVING COUNT(*) > 1
)
BEGIN
    SELECT RAISE(ABORT, 'existing_votes_incompatible');
END;

-- Открытое голосование обязано иметь полный конфиг, а его дедлайн должен быть
-- строго раньше старта каждого активного события.
CREATE TRIGGER IF NOT EXISTS trg_categories_voting_config_update
BEFORE UPDATE OF choice_mode, voting_deadline, voting_status ON categories
WHEN NEW.voting_status='open' AND (
    NEW.choice_mode IS NULL OR NEW.choice_mode NOT IN ('single', 'multiple')
    OR NEW.voting_deadline IS NULL OR TRIM(NEW.voting_deadline)=''
    OR datetime(NEW.voting_deadline) IS NULL
    OR EXISTS (
        SELECT 1 FROM date_categories dc JOIN dates d ON d.id=dc.date_id
        WHERE dc.category_id=NEW.id AND d.archived_at IS NULL AND d.is_draft=0
          AND d.starts_at IS NOT NULL AND NEW.voting_deadline>=d.starts_at
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid_voting_configuration');
END;

-- Нельзя незаметно добавить в открытый опрос вариант, который начинается до
-- дедлайна или одновременно с ним.
CREATE TRIGGER IF NOT EXISTS trg_date_categories_deadline_insert
BEFORE INSERT ON date_categories
WHEN EXISTS (
    SELECT 1 FROM categories c JOIN dates d ON d.id=NEW.date_id
    WHERE c.id=NEW.category_id AND c.voting_status='open'
      AND d.archived_at IS NULL AND d.is_draft=0 AND d.starts_at IS NOT NULL
      AND c.voting_deadline>=d.starts_at
)
BEGIN
    SELECT RAISE(ABORT, 'candidate_before_deadline');
END;

CREATE TRIGGER IF NOT EXISTS trg_date_categories_frozen_insert
BEFORE INSERT ON date_categories
WHEN EXISTS (
    SELECT 1 FROM categories c WHERE c.id=NEW.category_id
      AND (c.closed_at IS NOT NULL OR c.voting_status IN ('tie', 'resolved', 'no_winner'))
)
BEGIN
    SELECT RAISE(ABORT, 'category_composition_frozen');
END;

CREATE TRIGGER IF NOT EXISTS trg_date_categories_deadline_update
BEFORE UPDATE OF date_id, category_id ON date_categories
WHEN EXISTS (
    SELECT 1 FROM categories c JOIN dates d ON d.id=NEW.date_id
    WHERE c.id=NEW.category_id AND c.voting_status='open'
      AND d.archived_at IS NULL AND d.is_draft=0 AND d.starts_at IS NOT NULL
      AND c.voting_deadline>=d.starts_at
)
BEGIN
    SELECT RAISE(ABORT, 'candidate_before_deadline');
END;

CREATE TRIGGER IF NOT EXISTS trg_date_categories_frozen_update
BEFORE UPDATE ON date_categories
WHEN EXISTS (
    SELECT 1 FROM categories c WHERE c.id IN (OLD.category_id, NEW.category_id)
      AND (c.closed_at IS NOT NULL OR c.voting_status IN ('tie', 'resolved', 'no_winner'))
)
BEGIN
    SELECT RAISE(ABORT, 'category_composition_frozen');
END;

-- Аналогичная защита действует при редактировании времени, публикации и
-- возврате события из архива.
CREATE TRIGGER IF NOT EXISTS trg_dates_open_deadline_update
BEFORE UPDATE OF starts_at, archived_at, is_draft ON dates
WHEN NEW.archived_at IS NULL AND NEW.is_draft=0 AND NEW.starts_at IS NOT NULL
 AND EXISTS (
    SELECT 1 FROM date_categories dc JOIN categories c ON c.id=dc.category_id
    WHERE dc.date_id=NEW.id AND c.voting_status='open'
      AND c.voting_deadline>=NEW.starts_at
 )
BEGIN
    SELECT RAISE(ABORT, 'candidate_before_deadline');
END;

-- winner_date_id использует RESTRICT, чтобы победившее событие нельзя было
-- удалить отдельно. При удалении всего аккаунта сначала удаляем его категории:
-- затем штатный CASCADE users→dates не упирается в уже снятую ссылку результата.
CREATE TRIGGER IF NOT EXISTS trg_users_delete_owned_categories
BEFORE DELETE ON users
BEGIN
    DELETE FROM categories WHERE owner_id=OLD.id;
END;
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
        -- правило «одно событие — один человек»: перед уникальным индексом
        -- убираем возможные дубли, оставляя самую свежую бронь на событие
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
        -- гость снова может выбрать несколько событий: пересобираем bookings
        -- без UNIQUE(категория, гость). «Одно событие — один человек» остаётся
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
    15: """
        -- Своя картинка превью ссылки (WebP, как фото событий). NULL = дефолт
        -- /static/og.png. Отдаётся публично через /c/<токен>/og-image.
        ALTER TABLE categories ADD COLUMN og_image TEXT;
    """,
    16: """
        -- Секретная ссылка на ОТДЕЛЬНОЕ событие (/d/<токен>): по ней другой
        -- залогиненный пользователь добавляет копию события себе в коллекцию.
        -- Бэкофилл уникальных токенов средствами SQLite (16 байт hex), чтобы у
        -- всех существующих событий тоже была ссылка. Уникальный индекс
        -- допускает несколько NULL, но после бэкофилла их не остаётся.
        ALTER TABLE dates ADD COLUMN share_token TEXT;
        UPDATE dates SET share_token = lower(hex(randomblob(16))) WHERE share_token IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_dates_share ON dates(share_token);
    """,
    17: """
        -- Сброс ложного флага модерации: из-за бага в category_create все
        -- категории создавались с moderate_proposals=1, и на каждой висел бейдж
        -- «модерация». Возвращаем к дефолту 0 (выключено) — оператор включает
        -- модерацию осознанно на нужной категории.
        UPDATE categories SET moderate_proposals=0;
    """,
    18: """
        -- Публичность события для общей ленты комьюнити на главной. По умолчанию
        -- 1 (публичное) — существующие события попадают в ленту. Владелец может
        -- сделать приватным в редакторе события (тумблер в блоке «Категории»).
        ALTER TABLE dates ADD COLUMN is_public INTEGER NOT NULL DEFAULT 1;
        CREATE INDEX IF NOT EXISTS idx_dates_public ON dates(is_public, is_draft, archived_at, id);
    """,
    19: """
        -- OAuth-вход (Discord/Google/Yandex). Аккаунт больше НЕ обязан иметь
        -- telegram_id: пользователь может завестись через соцсеть. Делаем
        -- telegram_id NULLABLE (UNIQUE допускает несколько NULL в SQLite) —
        -- пересобираем users (SQLite не умеет ослабить NOT NULL на месте).
        -- FK=OFF на время миграций (см. init_db), id сохраняются 1:1.
        CREATE TABLE users_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,          -- NULL = аккаунт только через OAuth
            tg_username TEXT,
            display_name TEXT,
            avatar_path TEXT,
            birth_date TEXT,
            gender TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_operator INTEGER NOT NULL DEFAULT 0,
            is_reviewed INTEGER NOT NULL DEFAULT 1,
            date_limit INTEGER NOT NULL DEFAULT 30,
            bot_linked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        );
        INSERT INTO users_new SELECT id, telegram_id, tg_username, display_name,
            avatar_path, birth_date, gender, is_active, is_operator, is_reviewed,
            date_limit, bot_linked, created_at, last_login_at FROM users;
        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;

        -- Привязки OAuth: один пользователь может иметь несколько (Google+Discord).
        -- provider_uid — стабильный id пользователя у провайдера (sub/id).
        CREATE TABLE IF NOT EXISTS oauth_accounts (
            provider TEXT NOT NULL,              -- 'discord' | 'google' | 'yandex'
            provider_uid TEXT NOT NULL,          -- id пользователя у провайдера
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            email TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (provider, provider_uid)
        );
        CREATE INDEX IF NOT EXISTS idx_oauth_user ON oauth_accounts(user_id);
    """,
    20: """
        -- Индекс на date_id вопросов: подсчёт непрочитанных в actx() крутится на
        -- КАЖДОЙ странице кабинета (questions есть с v1 — создаём тут явно).
        -- Индексы idx_di_date/idx_dl_date (date_images/date_links) добавлены в
        -- SCHEMA: эти таблицы живут только в SCHEMA и создаются её идемпотентным
        -- пост-проходом в конце init_db, туда же попадут и их индексы — на старой
        -- базе к моменту миграций таблицы date_links может ещё не быть.
        CREATE INDEX IF NOT EXISTS idx_q_date ON questions(date_id);
    """,
    21: """
        -- Точка фокуса своей картинки превью ссылки: «X% Y%» (как date_images.focus).
        -- Владелец двигает картинку в редакторе категории, чтобы выбрать кадр 1200×630;
        -- og:image кропается по этой точке (WYSIWYG). NULL = центр (50% 50%).
        ALTER TABLE categories ADD COLUMN og_focus TEXT;
    """,
    22: """
        -- Настройки профиля и безопасное разделение Telegram-кодов входа/привязки.
        ALTER TABLE users ADD COLUMN cursor_effects INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE login_codes ADD COLUMN purpose TEXT NOT NULL DEFAULT 'login';
        ALTER TABLE login_codes ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
        ALTER TABLE login_codes ADD COLUMN error TEXT;

        -- Категории до v22 намеренно остаются unconfigured: старые голоса
        -- сохраняются, но новые нельзя принимать, пока владелец явно не задаст
        -- режим и дедлайн.
        ALTER TABLE categories ADD COLUMN choice_mode TEXT
            CHECK(choice_mode IN ('single', 'multiple'));
        ALTER TABLE categories ADD COLUMN voting_deadline TEXT;
        ALTER TABLE categories ADD COLUMN voting_status TEXT NOT NULL DEFAULT 'unconfigured'
            CHECK(voting_status IN ('unconfigured', 'open', 'tie', 'resolved', 'no_winner'));
        ALTER TABLE categories ADD COLUMN closed_at TEXT;
        ALTER TABLE categories ADD COLUMN resolved_at TEXT;
        ALTER TABLE categories ADD COLUMN winner_date_id INTEGER
            REFERENCES dates(id) ON DELETE RESTRICT;

        -- capacity одинакова у самого события, но счётчик набирается отдельно
        -- для каждой категории. Старые события сохраняют прежний максимум 1.
        ALTER TABLE dates ADD COLUMN capacity INTEGER NOT NULL DEFAULT 1
            CHECK(capacity BETWEEN 1 AND 100);
        ALTER TABLE bookings ADD COLUMN participation_withdrawn_at TEXT;

        -- Снимаем глобальную блокировку date_id: одно событие теперь независимо
        -- набирает людей в каждой категории. Повтор одного участника защищён
        -- составным уникальным индексом.
        DROP INDEX IF EXISTS idx_book_date;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_book_vote
            ON bookings(date_id, category_id, guest_token);

        CREATE TRIGGER IF NOT EXISTS trg_bookings_capacity_insert
        BEFORE INSERT ON bookings
        WHEN (
            SELECT COUNT(*) FROM bookings
            WHERE date_id=NEW.date_id AND category_id=NEW.category_id
        ) >= COALESCE((SELECT capacity FROM dates WHERE id=NEW.date_id), 0)
        BEGIN
            SELECT RAISE(ABORT, 'booking_capacity_reached');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_bookings_single_insert
        BEFORE INSERT ON bookings
        WHEN (SELECT choice_mode FROM categories WHERE id=NEW.category_id)='single'
         AND EXISTS (
            SELECT 1 FROM bookings
            WHERE category_id=NEW.category_id AND guest_token=NEW.guest_token
         )
        BEGIN
            SELECT RAISE(ABORT, 'single_choice_only');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_bookings_closed_insert
        BEFORE INSERT ON bookings
        WHEN EXISTS (
            SELECT 1 FROM categories WHERE id=NEW.category_id AND closed_at IS NOT NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'voting_closed');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_dates_capacity_update
        BEFORE UPDATE OF capacity ON dates
        WHEN EXISTS (
            SELECT 1 FROM bookings WHERE date_id=OLD.id
            GROUP BY category_id HAVING COUNT(*) > NEW.capacity
        )
        BEGIN
            SELECT RAISE(ABORT, 'capacity_below_existing_votes');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_users_delete_owned_categories
        BEFORE DELETE ON users
        BEGIN
            DELETE FROM categories WHERE owner_id=OLD.id;
        END;
    """,
    23: """
        -- Пользовательская очередь Telegram. chat_id здесь нет намеренно:
        -- он определяется по users непосредственно перед реальной отправкой.
        CREATE TABLE notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            event_key TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            send_at TEXT NOT NULL,
            expires_at TEXT,
            sent_at TEXT,
            cancelled_at TEXT,
            claimed_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_notification_outbox_due
            ON notification_outbox(sent_at, cancelled_at, send_at);
        CREATE INDEX idx_notification_outbox_user
            ON notification_outbox(user_id, kind);
    """,
    24: """
        CREATE TRIGGER IF NOT EXISTS trg_categories_single_update
        BEFORE UPDATE OF choice_mode ON categories
        WHEN NEW.choice_mode='single' AND EXISTS (
            SELECT 1 FROM bookings WHERE category_id=NEW.id
            GROUP BY guest_token HAVING COUNT(*) > 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'existing_votes_incompatible');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_categories_voting_config_update
        BEFORE UPDATE OF choice_mode, voting_deadline, voting_status ON categories
        WHEN NEW.voting_status='open' AND (
            NEW.choice_mode IS NULL OR NEW.choice_mode NOT IN ('single', 'multiple')
            OR NEW.voting_deadline IS NULL OR TRIM(NEW.voting_deadline)=''
            OR datetime(NEW.voting_deadline) IS NULL
            OR EXISTS (
                SELECT 1 FROM date_categories dc JOIN dates d ON d.id=dc.date_id
                WHERE dc.category_id=NEW.id AND d.archived_at IS NULL AND d.is_draft=0
                  AND d.starts_at IS NOT NULL AND NEW.voting_deadline>=d.starts_at
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid_voting_configuration');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_date_categories_deadline_insert
        BEFORE INSERT ON date_categories
        WHEN EXISTS (
            SELECT 1 FROM categories c JOIN dates d ON d.id=NEW.date_id
            WHERE c.id=NEW.category_id AND c.voting_status='open'
              AND d.archived_at IS NULL AND d.is_draft=0 AND d.starts_at IS NOT NULL
              AND c.voting_deadline>=d.starts_at
        )
        BEGIN
            SELECT RAISE(ABORT, 'candidate_before_deadline');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_date_categories_frozen_insert
        BEFORE INSERT ON date_categories
        WHEN EXISTS (
            SELECT 1 FROM categories c WHERE c.id=NEW.category_id
              AND (c.closed_at IS NOT NULL OR c.voting_status IN ('tie', 'resolved', 'no_winner'))
        )
        BEGIN
            SELECT RAISE(ABORT, 'category_composition_frozen');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_date_categories_deadline_update
        BEFORE UPDATE OF date_id, category_id ON date_categories
        WHEN EXISTS (
            SELECT 1 FROM categories c JOIN dates d ON d.id=NEW.date_id
            WHERE c.id=NEW.category_id AND c.voting_status='open'
              AND d.archived_at IS NULL AND d.is_draft=0 AND d.starts_at IS NOT NULL
              AND c.voting_deadline>=d.starts_at
        )
        BEGIN
            SELECT RAISE(ABORT, 'candidate_before_deadline');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_date_categories_frozen_update
        BEFORE UPDATE ON date_categories
        WHEN EXISTS (
            SELECT 1 FROM categories c WHERE c.id IN (OLD.category_id, NEW.category_id)
              AND (c.closed_at IS NOT NULL OR c.voting_status IN ('tie', 'resolved', 'no_winner'))
        )
        BEGIN
            SELECT RAISE(ABORT, 'category_composition_frozen');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_dates_open_deadline_update
        BEFORE UPDATE OF starts_at, archived_at, is_draft ON dates
        WHEN NEW.archived_at IS NULL AND NEW.is_draft=0 AND NEW.starts_at IS NOT NULL
         AND EXISTS (
            SELECT 1 FROM date_categories dc JOIN categories c ON c.id=dc.category_id
            WHERE dc.date_id=NEW.id AND c.voting_status='open'
              AND c.voting_deadline>=NEW.starts_at
         )
        BEGIN
            SELECT RAISE(ABORT, 'candidate_before_deadline');
        END;
    """,
    25: """
        -- Исторический repair из GET /admin/dates. Админское событие, уже
        -- привязанное хотя бы к одной категории, не должно оставаться
        -- черновиком. Гостевые предложения и архив намеренно не затрагиваем.
        UPDATE dates SET is_draft=0
        WHERE is_draft=1
          AND origin<>'guest'
          AND archived_at IS NULL
          AND EXISTS (
              SELECT 1 FROM date_categories dc WHERE dc.date_id=dates.id
          )
          -- Не публикуем строку, которую справедливо отверг бы v24-триггер:
          -- один некорректный легаси-черновик не должен сорвать весь startup.
          AND NOT EXISTS (
              SELECT 1
              FROM date_categories dc
              JOIN categories c ON c.id=dc.category_id
              WHERE dc.date_id=dates.id
                AND c.voting_status='open'
                AND dates.starts_at IS NOT NULL
                AND c.voting_deadline>=dates.starts_at
          );

        -- Проверка доступа к файлу начинается с имени, а затем соединяется с
        -- событием; date_id вторым полем делает эти lookup-запросы покрывающими.
        CREATE INDEX IF NOT EXISTS idx_di_filename_date
            ON date_images(filename, date_id);
        CREATE INDEX IF NOT EXISTS idx_dv_filename_date
            ON date_videos(filename, date_id);
        CREATE INDEX IF NOT EXISTS idx_categories_og_image
            ON categories(og_image, owner_id) WHERE og_image IS NOT NULL;

        -- Адресные проверки участия и выборка получателей уведомлений.
        CREATE INDEX IF NOT EXISTS idx_book_cat_user
            ON bookings(category_id, user_id) WHERE user_id IS NOT NULL;

        -- Фоновое закрытие читает только открытые и ещё не закрытые опросы,
        -- отсортированные SQLite по ближайшему дедлайну.
        CREATE INDEX IF NOT EXISTS idx_categories_open_deadline
            ON categories(voting_deadline)
            WHERE voting_status='open' AND closed_at IS NULL;

        -- Частичный покрывающий индекс не содержит архив и события без даты.
        -- owner_id первым сохраняет быстрый адресный проход для одного аккаунта.
        CREATE INDEX IF NOT EXISTS idx_dates_autoarchive_active
            ON dates(owner_id, ends_at, starts_at, archived_at)
            WHERE archived_at IS NULL
              AND (starts_at IS NOT NULL OR ends_at IS NOT NULL);

        CREATE INDEX IF NOT EXISTS idx_q_date_read
            ON questions(date_id, is_read);
    """,
    26: """
        -- Два независимых оформления. Схемный DEFAULT новых записей —
        -- нейтральный friends. Существующие категории после добавления колонки
        -- явно возвращаем в прежний romantic, чтобы миграция не меняла их вид.
        ALTER TABLE categories ADD COLUMN category_skin TEXT NOT NULL DEFAULT 'friends'
            CHECK(category_skin IN ('friends', 'romantic'));
        UPDATE categories SET category_skin='romantic';

        -- Общий вид кабинета больше не предполагает романтический сценарий:
        -- существующие и будущие пользователи начинают с friends.
        ALTER TABLE users ADD COLUMN admin_skin TEXT NOT NULL DEFAULT 'friends'
            CHECK(admin_skin IN ('friends', 'romantic'));
    """,
    27: """
        -- Inline-кнопка Telegram должна переживать отложенную доставку вместе
        -- с текстом сообщения, а не собираться из уже изменившихся данных.
        ALTER TABLE notification_outbox ADD COLUMN action_url TEXT;
        ALTER TABLE notification_outbox ADD COLUMN action_label TEXT;

        -- Строку создаём только после первого сохранения настроек. Для всех
        -- существующих пользователей отсутствие строки означает «всё включено».
        CREATE TABLE IF NOT EXISTS notification_preferences (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            votes INTEGER NOT NULL DEFAULT 1 CHECK(votes IN (0, 1)),
            questions INTEGER NOT NULL DEFAULT 1 CHECK(questions IN (0, 1)),
            proposals INTEGER NOT NULL DEFAULT 1 CHECK(proposals IN (0, 1)),
            updates INTEGER NOT NULL DEFAULT 1 CHECK(updates IN (0, 1)),
            reminders INTEGER NOT NULL DEFAULT 1 CHECK(reminders IN (0, 1)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """,
    28: """
        -- Отзывы и приглашения оставить отзыв управляются отдельно от общих
        -- напоминаний: пользователь может оставить дедлайны, но выключить
        -- социальные сообщения.
        ALTER TABLE notification_preferences ADD COLUMN reviews INTEGER NOT NULL
            DEFAULT 1 CHECK(reviews IN (0, 1));

        -- v22 намеренно оставляла старые категории ненастроенными. Теперь
        -- дедлайн обязателен ещё и как страховка review-prompt для события без
        -- времени: legacy-категории получают нейтральный режим и момент
        -- миграции по Москве. Статус остаётся unconfigured — это не открывает
        -- голосование без явного решения владельца.
        UPDATE categories
        SET choice_mode=COALESCE(NULLIF(TRIM(choice_mode), ''), 'multiple'),
            voting_deadline=COALESCE(
                NULLIF(TRIM(voting_deadline), ''),
                strftime('%Y-%m-%dT%H:%M:%S', 'now', '+3 hours')
            )
        WHERE choice_mode IS NULL OR TRIM(choice_mode)=''
           OR voting_deadline IS NULL OR TRIM(voting_deadline)='';

        CREATE TABLE IF NOT EXISTS date_wants (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
            is_public INTEGER NOT NULL DEFAULT 1 CHECK(is_public IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, date_id)
        );
        CREATE INDEX IF NOT EXISTS idx_date_wants_profile
            ON date_wants(user_id, is_public, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_date_wants_date
            ON date_wants(date_id, user_id);

        CREATE TABLE IF NOT EXISTS date_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            text TEXT,
            is_public INTEGER NOT NULL DEFAULT 1 CHECK(is_public IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (user_id, date_id)
        );
        CREATE INDEX IF NOT EXISTS idx_date_reviews_profile
            ON date_reviews(user_id, is_public, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_date_reviews_date
            ON date_reviews(date_id, is_public, updated_at DESC);
    """,
    29: """
        -- is_draft остаётся только техническим флагом модерации гостевых
        -- предложений (и редких легаси-конфликтов с уже открытым голосованием).
        -- Ранее скрытые owner-created события сначала делаем приватными: иначе
        -- DEFAULT is_public=1 внезапно раскрыл бы их в общей ленте при startup.
        UPDATE dates SET is_public=0
        WHERE is_draft=1 AND origin<>'guest' AND archived_at IS NULL;

        UPDATE dates SET is_draft=0
        WHERE is_draft=1
          AND origin<>'guest'
          AND archived_at IS NULL
          -- Не обходим DB-инвариант v24. Такая строка остаётся видна владельцу
          -- в общем списке с пометкой «нужно исправить», но не публикуется.
          AND NOT EXISTS (
              SELECT 1
              FROM date_categories dc
              JOIN categories c ON c.id=dc.category_id
              WHERE dc.date_id=dates.id
                AND c.voting_status='open'
                AND dates.starts_at IS NOT NULL
                AND c.voting_deadline>=dates.starts_at
          );

        CREATE INDEX IF NOT EXISTS idx_categories_winner_date
            ON categories(winner_date_id)
            WHERE voting_status='resolved' AND winner_date_id IS NOT NULL;
    """,
    30: """
        -- Явный opt-in фиксирует фирменную картинку и запрещает новым событиям
        -- автоматически заменять её коллажем.
        ALTER TABLE categories ADD COLUMN use_default_preview INTEGER NOT NULL DEFAULT 0
            CHECK(use_default_preview IN (0, 1));

        CREATE TABLE IF NOT EXISTS review_queue (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            date_id INTEGER NOT NULL REFERENCES dates(id) ON DELETE CASCADE,
            reason TEXT NOT NULL DEFAULT 'due'
                CHECK(reason IN ('due', 'declined', 'review_deleted')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, date_id)
        );
        CREATE INDEX IF NOT EXISTS idx_review_queue_user
            ON review_queue(user_id, updated_at DESC);
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
