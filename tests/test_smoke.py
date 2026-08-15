#!/usr/bin/env python3
"""Smoke-тест date4you (итерация 3: именные брони, архив на странице, DnD).

Запуск из корня репозитория:  python tests/test_smoke.py
Зависимости: pip install -r app/requirements.txt
"""

import html
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from urllib.parse import unquote
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
DATA = Path(tempfile.gettempdir()) / f"date4you-smoke-{os.getpid()}"
FAILFAST_DATA = Path(tempfile.gettempdir()) / f"date4you-smoke-ff-{os.getpid()}"

ENV = {
    "DATA_DIR": str(DATA),
    "COOKIE_SECURE": "false",
    "DOMAIN": "t.local",
    "SECRET_KEY": "test-secret",
    "TG_BOT_TOKEN": "",
    "TG_CHAT_ID": "",
    "TG_BOT_USERNAME": "date4you_test_bot",
    "TG_WEBHOOK_SECRET": "hook-secret",
    "OPERATOR_TG_IDS": "555001",
    "SUPPORT_CONTACT": "@date4you_support",
    "AUTHOR_PROJECTS": "Мой VPN|https://vpn.example.com;Блог|https://blog.example.com",
    "ABOUT_TEXT": "Тестовое описание проекта.",
}

OK = 0


def step(msg: str) -> None:
    global OK
    OK += 1
    print(f"  ✓ {msg}")


# ---------- 0. fail-fast: обязательные переменные окружения ----------

def check_failfast(missing: str) -> None:
    env = {**os.environ, **ENV}
    env.pop(missing, None)
    env["DATA_DIR"] = str(FAILFAST_DATA)
    r = subprocess.run([sys.executable, "-c", "import main"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode != 0, f"без {missing} приложение обязано падать"
    assert missing in r.stderr, r.stderr


check_failfast("SECRET_KEY")
step("fail-fast: без SECRET_KEY приложение не стартует")

# ---------- 0.1 entrypoint синтаксически корректен ----------

r = subprocess.run(["sh", "-n", str(ROOT / "docker-entrypoint.sh")],
                   capture_output=True, text=True)
assert r.returncode == 0, r.stderr
step("docker-entrypoint.sh: синтаксис sh корректен")

# ---------- подготовка ----------

shutil.rmtree(DATA, ignore_errors=True)
shutil.rmtree(FAILFAST_DATA, ignore_errors=True)
os.environ.update(ENV)
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import backup as bk  # noqa: E402
import db as dbm  # noqa: E402
import main  # noqa: E402
import social_events as social  # noqa: E402


def png(color=(180, 90, 110), size=(640, 480)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


# алиас: в одном из поздних блоков локальная переменная `png` затеняет эту
# функцию (png = cs.get(...)), а она нужна ниже — держим стабильную ссылку.
make_png = png


CSRF = {"v": ""}


def refresh_csrf(c) -> None:
    page = c.get("/admin/categories")
    CSRF["v"] = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


TG_WEBHOOK_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}


def tg_open_login(c, code, telegram_id, username="user", first_name="Тест"):
    """Имитирует /start: код привязан к Telegram, но вход ещё не подтверждён."""
    person = {"id": telegram_id, "username": username, "first_name": first_name}
    wh = c.post("/tg/webhook", headers=TG_WEBHOOK_HEADERS,
                json={"message": {"text": f"/start {code}", "from": person}})
    assert wh.status_code == 200, wh.status_code
    return person


def tg_confirm_login(c, code, telegram_id, username="user", first_name="Тест",
                     callback_id=None):
    """Имитирует явное нажатие inline-кнопки «Подтвердить» в Telegram."""
    person = {"id": telegram_id, "username": username, "first_name": first_name}
    callback_id = callback_id or f"confirm-{telegram_id}-{code}"
    wh = c.post("/tg/webhook", headers=TG_WEBHOOK_HEADERS,
                json={"callback_query": {
                    "id": callback_id,
                    "data": f"auth_confirm:{code}",
                    "from": person,
                }})
    assert wh.status_code == 200, wh.status_code
    return wh


def tg_login(c, telegram_id, username="user", first_name="Тест"):
    """Полный поток: start → /start → pending → inline callback → poll.

    Возвращает финальный ответ poll. Сессия логина оседает в куках клиента c.
    """
    r = c.post("/auth/start")
    assert r.status_code == 200, r.status_code
    code = r.json()["code"]
    tg_open_login(c, code, telegram_id, username, first_name)
    pending = c.get(f"/auth/poll?code={code}")
    assert pending.status_code == 200 and pending.json()["status"] == "pending"
    tg_confirm_login(c, code, telegram_id, username, first_name)
    result = c.get(f"/auth/poll?code={code}")
    if result.status_code == 200 and result.json().get("status") == "ok":
        page = c.get("/admin/categories")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        c.headers["X-CSRF-Token"] = csrf
    return result


def apost(c, url, data=None, files=None):
    d = dict(data or {})
    d["csrf"] = CSRF["v"]
    return c.post(url, data=d, files=files)


def db_one(sql, args=()):
    conn = dbm.connect()
    row = conn.execute(sql, args).fetchone()
    conn.close()
    return row


def db_all(sql, args=()):
    conn = dbm.connect()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def configure_voting(cid, mode="multiple"):
    """Явно открывает голосование для legacy-сценариев бронирования.

    Новая продуктовая модель намеренно оставляет свежую категорию в состоянии
    ``unconfigured``. Старые smoke-сценарии ниже тестируют другие подсистемы,
    поэтому дают им динамический будущий дедлайн и сохраняют прежнюю семантику
    нескольких вариантов на одного гостя через режим ``multiple``.
    """
    import voting

    conn = dbm.connect()
    owner_id = conn.execute(
        "SELECT owner_id FROM categories WHERE id=?", (cid,)
    ).fetchone()[0]
    deadline = (datetime.now() + timedelta(days=30)).replace(
        second=0, microsecond=0
    ).isoformat(timespec="minutes")
    voting.configure_category(conn, cid, owner_id, mode, deadline)
    conn.commit()
    conn.close()


def category_data(name: str, mode: str = "multiple") -> dict[str, str]:
    """Обязательные поля новой категории из актуальной UI-модели."""
    deadline = (datetime.now() + timedelta(days=2)).replace(
        second=0, microsecond=0
    ).isoformat(timespec="minutes")
    return {"name": name, "choice_mode": mode, "voting_deadline": deadline}


def set_moderation(cid, on):
    """Прямое переключение модерации категории в БД. Раньше это делалось POST'ом
    владельца, но теперь режим модерации — операторская настройка (404 для
    обычного пользователя), а тестам разных блоков нужно лишь задать состояние."""
    conn = dbm.connect()
    conn.execute("UPDATE categories SET moderate_proposals=? WHERE id=?",
                 (1 if on else 0, cid))
    conn.commit()
    conn.close()


def set_name(client, tok, name):
    """Гость теперь = залогиненный пользователь. Ставим его display_name (оно же
    будет именем рядом с бронью/предложением) и регистрируем на странице
    категории (создаётся строка guests с токеном u<id>). Клиент должен быть
    уже залогинен через tg_login."""
    pc = re.search(r'name="csrf" value="([^"]+)"',
                   client.get("/admin/profile").text).group(1)
    r = client.post("/admin/profile",
                    data={"csrf": pc, "display_name": name, "birth_date": "1990-01-01"})
    assert r.status_code == 303, r.text
    client.get(f"/c/{tok}")          # регистрирует guests-строку u<id> с этим именем
    return r


def guest_client(tg_id, tok, name):
    """Создаёт нового залогиненного «гостя»: отдельный клиент, вход через бота,
    имя в профиле, заход на категорию. Возвращает клиент с живой сессией."""
    gc = TestClient(main.app, follow_redirects=False)
    assert tg_login(gc, tg_id, username=f"u{tg_id}").json()["status"] == "ok"
    set_name(gc, tok, name)
    return gc


with TestClient(main.app, follow_redirects=False) as c:

    # ---------- health ----------
    r = c.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert (DATA / ".health").exists()           # проба записи на диск прошла
    assert c.get("/favicon.ico").status_code == 200
    step("/health отвечает и проверяет чтение базы + запись на диск; favicon на месте")

    holder = {}
    t = threading.Thread(target=lambda: holder.update(c=dbm.connect()))
    t.start(); t.join()
    holder["c"].execute("SELECT 1").fetchone()
    holder["c"].close()                          # раньше тут падал ProgrammingError
    step("SQLite-коннект переживает смену потока тредпула (фикс прод-500 на фото)")

    # ---------- вход через Telegram-бота ----------
    r = c.get("/admin/")
    assert r.status_code == 303 and "/login" in r.headers["location"]
    # страница входа отдаёт способы входа (Telegram Login Widget + OAuth-иконки)
    # сразу, а условия показывает пассивным текстом без обязательной галочки
    lp = c.get("/login")
    assert lp.status_code == 200 and "data-tg-widget" in lp.text
    assert "tg-consent" not in lp.text and "Продолжая вход" in lp.text

    # вебхук без секрета — 403 (иначе любой подтвердит чужой код)
    r0 = c.post("/auth/start")
    bad = c.post("/tg/webhook",
                 json={"message": {"text": f"/start {r0.json()['code']}",
                                   "from": {"id": 555001}}})
    assert bad.status_code == 403
    # незалогинены: кабинет недоступен
    assert c.get("/admin/").status_code == 303

    # полный поток: оператор (есть в OPERATOR_TG_IDS) входит через поллинг
    poll = tg_login(c, 555001, username="boss", first_name="Шеф")
    assert poll.status_code == 200 and poll.json()["status"] == "ok"
    assert c.get("/admin/").status_code == 200
    me = db_one("SELECT id, is_operator, telegram_id FROM users WHERE telegram_id=555001")
    assert me["is_operator"] == 1
    # на свежей базе служебного легаси-владельца (telegram_id=0) нет
    assert not db_one("SELECT 1 FROM users WHERE telegram_id=0")
    step("вход через Telegram: webhook без секрета → 403, поллинг логинит оператора")

    # На странице уведомлений состояние канала не дублируется текстом. Если бот
    # ещё не связан, единственный CTA стоит справа от заголовка; общий баннер на
    # этой странице скрыт. После подключения CTA исчезает целиком.
    _bot = dbm.connect()
    _bot.execute("UPDATE users SET bot_linked=0 WHERE id=?", (me["id"],))
    _bot.commit(); _bot.close()
    q_unlinked = c.get("/admin/questions").text
    assert "Уведомления в Telegram" in q_unlinked
    assert q_unlinked.count("Подключить уведомления") == 1
    assert "Бот подключён" not in q_unlinked and "Бот не подключён" not in q_unlinked
    assert '<div class="flash tg-connect">' not in q_unlinked
    _bot = dbm.connect()
    _bot.execute("UPDATE users SET bot_linked=1 WHERE id=?", (me["id"],))
    _bot.commit(); _bot.close()
    q_linked = c.get("/admin/questions").text
    assert "Уведомления в Telegram" in q_linked
    assert "Подключить уведомления" not in q_linked
    assert "Бот подключён" not in q_linked and "Бот не подключён" not in q_linked
    step("Telegram-настройки: без статусной подписи, CTA только для неподключённого бота")

    # анти-спам /auth/start: 10 кодов на IP за окно, 11-й → 429
    sc = TestClient(main.app, follow_redirects=False)
    for _ in range(10):
        assert sc.post("/auth/start", headers={"X-Real-IP": "9.9.9.9"}).status_code == 200
    assert sc.post("/auth/start", headers={"X-Real-IP": "9.9.9.9"}).status_code == 429
    main._rates.clear()
    step("анти-спам входа: не больше 10 кодов с одного IP за окно")

    # TTL-чистка кодов: сравнение ISO-строк (МСК), не зависит от TZ сервера.
    # Регресс на баг, когда mktime читал МСК-метку как UTC и раздувал TTL до ~3ч.
    import auth_routes  # noqa: E402
    from datetime import timedelta as _td  # noqa: E402
    cc = dbm.connect()
    old = (main.now_naive() - _td(seconds=auth_routes.TTL_SECONDS + 60)) \
        .isoformat(sep="T")
    fresh = main.now_iso()
    cc.execute("INSERT INTO login_codes(code, status, created_at) VALUES('old','pending',?)",
               (old,))
    cc.execute("INSERT INTO login_codes(code, status, created_at) VALUES('fresh','pending',?)",
               (fresh,))
    cc.commit()
    auth_routes._gc_codes(cc)
    assert not cc.execute("SELECT 1 FROM login_codes WHERE code='old'").fetchone()
    assert cc.execute("SELECT 1 FROM login_codes WHERE code='fresh'").fetchone()
    cc.close()
    step("TTL-чистка кодов входа: протухший снесён, свежий жив (не зависит от TZ)")

    # Смягчение фишинга: /start только показывает inline-подтверждение, а аккаунт
    # создаётся после callback. Оба сообщения уходят на telegram_id пользователя.
    captured = []
    def _cap(url, json=None, timeout=None):
        captured.append((url.rsplit("/", 1)[-1], dict(json or {})))
        class _R: status_code = 200; text = "ok"
        return _R()
    real_post = main.notify.httpx.post
    real_token = main.notify.TOKEN
    main.notify.httpx.post = _cap
    main.notify.TOKEN = "t"
    try:
        sc2 = TestClient(main.app, follow_redirects=False)
        code2 = sc2.post("/auth/start").json()["code"]
        tg_open_login(sc2, code2, 660002)
        with TestClient(main.app, follow_redirects=False) as code_thief:
            stolen = code_thief.get(f"/auth/poll?code={code2}")
            assert stolen.status_code == 403 and stolen.json()["status"] == "forbidden"
        assert sc2.get(f"/auth/poll?code={code2}").json()["status"] == "pending"
        callback_id = "callback-login-660002"
        tg_confirm_login(sc2, code2, 660002, callback_id=callback_id)
        assert sc2.get(f"/auth/poll?code={code2}").json()["status"] == "ok"
    finally:
        main.notify.TOKEN = real_token
        main.notify.httpx.post = real_post
    sent = [payload for method, payload in captured if method == "sendRichMessage"]
    answered = [payload for method, payload in captured
                if method == "answerCallbackQuery"]
    assert len(sent) == 2 and all(p["chat_id"] == 660002 for p in sent), captured
    buttons = sent[0]["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"auth_confirm:{code2}"
    assert buttons[1]["callback_data"] == f"auth_cancel:{code2}"
    assert "войти" in sent[0]["rich_message"]["html"].lower()
    assert "подтверждён" in sent[1]["rich_message"]["html"].lower()
    assert answered == [{"callback_query_id": callback_id, "text": "Подтверждено"}]
    step("вход: /start оставляет pending, inline callback подтверждает привязанную браузерную сессию")

    refresh_csrf(c)
    assert CSRF["v"]

    # ---------- CSRF ----------
    r = c.post("/admin/categories/create", data={"name": "Без токена"})
    assert r.status_code == 303
    fr = c.get(r.headers["location"])
    assert "Сессия устарела" in fr.text and "⚠" in fr.text
    assert not db_one("SELECT 1 FROM categories WHERE name='Без токена'")
    step("CSRF: POST без токена отклоняется с дружелюбным flash")

    missing_deadline = apost(c, "/admin/categories/create", {"name": "Без дедлайна"})
    assert missing_deadline.status_code == 303
    assert "дедлайн" in unquote(missing_deadline.headers["location"]).lower()
    assert not db_one("SELECT 1 FROM categories WHERE name='Без дедлайна'")
    step("категория не создаётся без обязательного дедлайна")

    # ---------- профиль кабинета ----------
    # форма открывается, в шапке — ссылка на профиль
    pf = c.get("/admin/profile")
    assert pf.status_code == 200 and "Мой профиль" in pf.text
    assert "Открыть публичный профиль" not in pf.text
    assert all(label in pf.text for label in ("Публичные события", "Хочу сходить", "Обзоры"))
    assert "/admin/profile" in c.get("/admin/categories").text
    # сохранение имени/ДР/пола + аватара одним POST
    r = apost(c, "/admin/profile",
              {"display_name": "Артём", "birth_date": "1990-05-01", "gender": "m"},
              files={"avatar": ("me.png", png(), "image/png")})
    assert r.status_code == 303
    me = db_one("SELECT display_name, birth_date, gender, avatar_path "
                "FROM users WHERE telegram_id=555001")
    assert me["display_name"] == "Артём" and me["birth_date"] == "1990-05-01"
    assert me["gender"] == "m" and me["avatar_path"]
    av = me["avatar_path"]
    # имя владельца появилось в шапке
    profile_with_avatar = c.get("/admin/profile").text
    assert "Артём" in profile_with_avatar
    assert 'class="avatar-delete"' in profile_with_avatar
    assert ">Удалить фото</button>" not in profile_with_avatar
    # свой аватар отдаётся
    assert c.get(f"/admin/avatar/{av}").status_code == 200
    # чужой клиент (Боб из HTTP-изоляции ещё не залогинен здесь) — проверим, что
    # неавторизованный не получит аватар: новый клиент без сессии
    anon = TestClient(main.app, follow_redirects=False)
    assert anon.get(f"/admin/avatar/{av}").status_code == 303  # → /login
    # дата рождения в будущем отклоняется (возрастного гейта 18+ больше нет)
    bad = apost(c, "/admin/profile",
                {"display_name": "Х", "birth_date": "2999-01-01", "gender": ""})
    assert bad.status_code == 303 and "будущ" in c.get(bad.headers["location"]).text
    # пустое имя отклоняется
    bad2 = apost(c, "/admin/profile", {"display_name": "  ", "birth_date": ""})
    assert bad2.status_code == 303
    # старый аватар не затёрт неудачными сохранениями
    assert db_one("SELECT avatar_path FROM users WHERE telegram_id=555001")["avatar_path"] == av
    # удаление аватара
    r = apost(c, "/admin/profile/avatar/delete", {})
    assert r.status_code == 303
    assert not db_one("SELECT avatar_path FROM users WHERE telegram_id=555001")["avatar_path"]
    assert c.get(f"/admin/avatar/{av}").status_code == 404  # уже не его
    step("профиль: имя/ДР/пол/аватар сохраняются, будущая ДР и пустое имя отклоняются, аватар приватный")

    # ---------- категория и секретная ссылка ----------
    r = apost(c, "/admin/categories/create", category_data("Лето"))
    assert r.status_code == 303
    page = c.get("/admin/categories").text
    cid = int(re.search(r"/admin/categories/(\d+)", page).group(1))
    detail = c.get(f"/admin/categories/{cid}").text
    tok = re.search(r"https://t\.local/c/([A-Za-z0-9_-]+)", detail).group(1)
    assert len(tok) == 11, "новые capability-ссылки должны иметь 64 бита энтропии"
    # модерация предложений по умолчанию ВЫКЛючена — оператор включает её осознанно;
    # иначе бейдж «модерация» висел на каждой новой категории (баг)
    assert db_one("SELECT moderate_proposals FROM categories WHERE id=?", (cid,))[0] == 0
    step("категория создана, секретная ссылка получена")

    r = c.get(f"/c/{tok}")
    assert r.status_code == 200 and "пусто" in r.text
    assert 'data-skin="friends"' in r.text
    assert "bg-gather" in r.text
    assert f"/c/{tok}/og-image?skin=friends&amp;v=" in r.text
    assert "Собираемся вместе" in r.text
    category_editor = c.get(f"/admin/categories/{cid}").text
    assert 'name="category_skin"' in category_editor
    assert "Стандартный" in category_editor and "Романтический" in category_editor
    assert "Для друзей, семьи и общих планов" not in category_editor
    assert "Тёплое авторское оформление для двоих" not in category_editor

    # Авторский романтический дизайн остаётся доступен и возвращается тем же
    # переключателем; затем продолжаем smoke на новом дружеском оформлении.
    rr = apost(c, f"/admin/categories/{cid}/rename", {
        "name": "Лето", "category_skin": "romantic",
    })
    assert rr.status_code == 303
    romantic_page = c.get(f"/c/{tok}").text
    assert 'data-skin="romantic"' in romantic_page
    assert "bg-hearts" in romantic_page
    assert f"/c/{tok}/og-image?skin=romantic&amp;v=" in romantic_page
    rr = apost(c, f"/admin/categories/{cid}/rename", {
        "name": "Лето", "category_skin": "friends",
    })
    assert rr.status_code == 303
    assert db_one("SELECT category_skin FROM categories WHERE id=?", (cid,))[0] == "friends"
    step("пустая категория показывает дружеский дизайн; романтический можно вернуть")

    # ---------- событие от админа (+фото) ----------
    r = apost(c, "/admin/dates/new", {
        "name": "Ужин на крыше",
        "place": "Крыша, СПб",
        "starts_at": "2030-07-01T18:00",
        "ends_at": "2030-07-01T21:00",
        "links": "ya.ru\nhttps://example.com/menu",
        "comment": "Тёплый плед прилагается",
        "categories": str(cid),
    }, files=[("images", ("a.png", png(), "image/png"))])
    assert r.status_code == 303, r.text
    did = db_one("SELECT id FROM dates WHERE name='Ужин на крыше'")["id"]
    configure_voting(cid, "multiple")
    step("событие с фото создано из админки")

    # ---------- дружелюбные ошибки админки ----------
    r = apost(c, "/admin/dates/new", {"name": "Перебор"},
              files=[("images", (f"p{i}.png", png((i * 30, 80, 80)), "image/png"))
                     for i in range(6)])
    assert r.status_code == 303
    fr = c.get(r.headers["location"])
    assert "⚠" in fr.text and "Максимум" in fr.text
    assert not db_one("SELECT 1 FROM dates WHERE name='Перебор'")

    r = apost(c, "/admin/dates/new", {"name": "Битый файл"},
              files=[("images", ("x.png", b"definitely not an image", "image/png"))])
    fr = c.get(r.headers["location"])
    assert "не похож" in fr.text
    assert not db_one("SELECT 1 FROM dates WHERE name='Битый файл'")

    r = apost(c, "/admin/dates/new", {"name": "Кривая дата", "starts_at": "lol"})
    fr = c.get(r.headers["location"])
    assert "Неверный формат" in fr.text
    step("ошибки форм админки превращаются в flash-сообщения, мусор не создаётся")

    # ---------- защита фото ----------
    page = c.get(f"/c/{tok}").text
    assert "Ужин на крыше" in page
    fn_did = re.search(rf"/c/{tok}/image/([A-Za-z0-9_-]+\.webp)", page).group(1)
    r = c.get(f"/c/{tok}/image/{fn_did}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/webp")
    assert "max-age=604800" in r.headers["cache-control"]   # 7 дней, а не год
    thumb = c.get(f"/c/{tok}/image/{fn_did}?w=480")
    assert thumb.status_code == 200
    with Image.open(io.BytesIO(thumb.content)) as thumb_im:
        assert thumb_im.size == (480, 360)
    thumb_path = main.images.RESPONSIVE_DIR / f"{Path(fn_did).stem}.w480.webp"
    assert thumb_path.exists()
    assert c.get(f"/c/{tok}/image/{fn_did}?w=777").status_code == 404
    assert c.get(f"/uploads/{fn_did}").status_code == 404
    assert c.get(f"/admin/uploads/{fn_did}?w=480").status_code == 200
    anon = TestClient(main.app, follow_redirects=False)
    assert anon.get(f"/admin/uploads/{fn_did}").status_code == 303
    step("фото защищены; responsive-копия 480 px создаётся лениво и кэшируется")

    # ---------- CSP с nonce, без inline-обработчиков ----------
    rr = c.get(f"/c/{tok}")
    csp = rr.headers.get("content-security-policy", "")
    m = re.search(r"'nonce-([^']+)'", csp)
    assert m, csp
    assert "script-src 'self' 'nonce-" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert "object-src 'none'" in csp and "base-uri 'none'" in csp
    assert "/static/guest.js" in rr.text          # весь гостевой JS вынесен
    assert "<script nonce=" not in rr.text        # инлайна на гостевой больше нет
    rr2 = c.get("/admin/")
    m2 = re.search(r"'nonce-([^']+)'", rr2.headers.get("content-security-policy", ""))
    assert m2, "у /admin/ должен быть per-request nonce в CSP"
    # инициализация кабинета вынесена во внешний admin.js (под Turbo+CSP инлайн с
    # per-request nonce не переживает подмену <body>); сам admin.js подключён
    assert "/static/admin.js" in rr2.text and "/static/vendor/turbo.min.js" in rr2.text
    # любой инлайн-скрипт на админ-странице обязан нести актуальный nonce
    for mscript in re.finditer(r"<script(?![^>]*\bsrc=)([^>]*)>", rr2.text):
        assert f'nonce="{m2.group(1)}"' in mscript.group(0), "инлайн-скрипт без nonce"
    assert "content-security-policy" not in c.get(f"/c/{tok}/image/{fn_did}").headers
    for html_page in (rr.text,
                      c.get("/admin/dates").text,
                      c.get("/admin/categories").text,
                      c.get(f"/admin/categories/{cid}").text,
                      c.get(f"/admin/dates/{did}/edit").text,
                      c.get("/admin/questions").text):
        assert not re.search(r"\son(click|submit|change|load)=", html_page)
    assert 'id="lbPrev"' in rr.text and 'id="lbNext"' in rr.text and "gal-wrap" in rr.text
    step("CSP: per-request nonce вместо unsafe-inline; инлайн-обработчиков нет")

    # ---------- HEIC с айфона и octet-stream-подсказка ----------
    heic_data = None
    try:
        b = io.BytesIO()
        Image.new("RGB", (320, 240), (130, 90, 160)).save(b, "HEIF", quality=80)
        heic_data = b.getvalue()
    except Exception as e:                     # нет кодека — мягкий скип HEIC-части
        print(f"  ~ HEIC-энкодер недоступен ({e!r}) — проверяю только octet-stream")
    files = [("images", ("blob.png", png((90, 90, 90)), "application/octet-stream"))]
    if heic_data:
        files.insert(0, ("images", ("photo.heic", heic_data, "image/heic")))
    r = apost(c, "/admin/dates/new", {"name": "Айфонное", "categories": str(cid)},
              files=files)
    assert r.status_code == 303, r.text
    hid = db_one("SELECT id FROM dates WHERE name='Айфонное'")["id"]
    assert db_one("SELECT COUNT(*) AS n FROM date_images WHERE date_id=?",
                  (hid,))["n"] == len(files)
    apost(c, f"/admin/dates/{hid}/delete", {})
    step("HEIC (если есть кодек) и octet-stream принимаются: решает декодер, не mime")

    # ---------- вход обязателен для гостевых действий ----------
    # Аноним (без сессии) не может ничего: 401 с флагом need_login.
    anon_g = TestClient(main.app, follow_redirects=False)
    r = anon_g.post(f"/c/{tok}/book", data={"date_id": did})
    assert r.status_code == 401 and r.json()["detail"]["need_login"] is True
    r = anon_g.post(f"/c/{tok}/question", data={"date_id": did, "text": "Эй?"})
    assert r.status_code == 401 and "Войди" in r.json()["detail"]["msg"]
    main._rates.clear()
    r = anon_g.post(f"/c/{tok}/propose", data={"name": "Аноним"})
    assert r.status_code == 401
    assert not db_one("SELECT 1 FROM dates WHERE name='Аноним'")
    # на гостевой странице анонима — кнопка «Войти», без пилюли с именем
    apage = anon_g.get(f"/c/{tok}").text
    assert ">Войти<" in apage and 'data-auth=""' in apage

    # «Аня» — отдельный залогиненный гость; её display_name станет именем у брони
    ga = guest_client(700101, tok, "Аня")
    page = ga.get(f"/c/{tok}").text
    assert 'id="greetName">Аня<' in page
    assert 'data-auth="1"' in page               # залогинен — кнопки активны
    assert "куда нам отправиться" not in page    # подзаголовок убран
    assert "(мск)" not in page                   # пояснение времени убрано
    empty_single = re.search(
        r'<article[^>]*id="date-%d".*?</article>' % did, page, re.S,
    ).group(0)
    assert re.search(r'class="vote-progress-head"[^>]*\bhidden\b', empty_single)
    assert re.search(r'class="vote-progress-track"[^>]*\bhidden\b', empty_single)
    step("вход обязателен: аноним → 401 need_login и кнопка «Войти»; залогиненный гость — с именем")

    # ---------- бронь: toggle и /vote больше нет ----------
    r = ga.post(f"/c/{tok}/book", data={"date_id": did})
    vote_json = r.json()
    assert vote_json["booked"] is True
    assert vote_json["updates"] == [{
        "date_id": did, "mine": True, "vote_count": 1, "capacity": 1,
        "is_full": True,
        "participants": [{
            "name": "Аня", "user_id": db_one(
                "SELECT id FROM users WHERE telegram_id=700101")["id"],
            "has_avatar": False, "is_me": True, "withdrawn": False,
        }],
        "hidden_count": 0,
    }]
    r = ga.post(f"/c/{tok}/book", data={"date_id": did})
    vote_json = r.json()
    assert vote_json["booked"] is False
    assert vote_json["updates"][0]["vote_count"] == 0
    assert vote_json["updates"][0]["participants"] == []
    r = ga.post(f"/c/{tok}/book", data={"date_id": did})
    assert r.json()["booked"] is True
    page = ga.get(f"/c/{tok}").text
    assert 'class="btn book on"' in page and "Выбрано" in page
    mycard = re.search(r'<article[^>]*id="date-%d".*?</article>' % did, page, re.S).group(0)
    assert "booked-me" in mycard                        # карточка помечена выбором
    assert "vote-progress" in mycard and "1/1" in mycard
    assert '<div class="seal">' in mycard and "ui-icon-check" in mycard
    assert "Аня" in mycard and "· ты" in mycard        # участники видны во время голосования
    assert ga.post(f"/c/{tok}/vote", data={"date_id": did}).status_code == 404
    step("голос работает как переключатель; видны прогресс и участники; /vote удалён")

    # ---------- вопрос и ответ ----------
    r = ga.post(f"/c/{tok}/question", data={"date_id": did, "text": "Можно прийти позже?"})
    assert r.status_code == 200
    r = ga.post(f"/c/{tok}/question", data={"date_id": did, "text": "   "})
    assert r.status_code == 400 and "обязательно" in r.json()["detail"]
    page = ga.get(f"/c/{tok}").text
    assert "Можно прийти позже?" in page and "пока без ответа" in page

    qpage = c.get("/admin/questions").text
    assert "Аня" in qpage
    qid = int(re.search(r"/admin/questions/(\d+)/answer", qpage).group(1))
    r = apost(c, f"/admin/questions/{qid}/answer",
              {"text": "Конечно, жду тебя!", "next": "/admin/questions"})
    assert r.status_code == 303
    assert "Конечно, жду тебя!" in ga.get(f"/c/{tok}").text
    assert "отвечено" in c.get("/admin/questions?f=all").text
    r = apost(c, f"/admin/questions/{qid}/answer", {"text": "", "next": "/admin/questions"})
    assert "пока без ответа" in ga.get(f"/c/{tok}").text
    apost(c, f"/admin/questions/{qid}/answer",
          {"text": "Конечно!", "next": "/admin/questions"})
    step("вопрос гостя подписан именем; ответ админа виден автору, бейдж «отвечено»")

    # ---------- календарь: gcal-ссылка, .ics, Яндекс.Карты ----------
    page = ga.get(f"/c/{tok}").text
    assert "calendar.google.com/calendar/render" in page
    assert "dates=20300701T150000Z%2F20300701T180000Z" in page  # 18:00 МСК = 15:00 UTC
    r = ga.get(f"/c/{tok}/ics/{did}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "DTSTART:20300701T150000Z" in r.text
    assert "SUMMARY:" in r.text and "LOCATION:" in r.text
    assert "yandex.ru/maps/?text=" in page
    r = apost(c, "/admin/dates/new", {"name": "Без даты", "categories": str(cid)})
    did2 = db_one("SELECT id FROM dates WHERE name='Без даты'")["id"]
    assert ga.get(f"/c/{tok}/ics/{did2}").status_code == 404
    page2 = ga.get(f"/c/{tok}").text
    assert page2.count("calendar.google.com") == 1  # у события без даты gcal нет
    step("ссылка в Google Календарь с верным UTC; .ics работает; без даты — ничего")

    # ---------- «Назначить дату»: гость предлагает время ----------
    card2 = re.search(r'id="date-%d".*?</article>' % did2, page2, re.S).group(0)
    assert "предложить дату" in card2 and "chip-suggest" in card2
    card1 = re.search(r'id="date-%d".*?</article>' % did, page2, re.S).group(0)
    assert "предложить дату" not in card1        # у события со временем чипа нет
    r = ga.post(f"/c/{tok}/suggest_time",
               data={"date_id": did, "starts_at": "2030-08-01T19:00"})
    assert r.status_code == 400 and "уже назначено" in r.json()["detail"]
    r = ga.post(f"/c/{tok}/suggest_time", data={"date_id": did2, "starts_at": ""})
    assert r.status_code == 400
    r = ga.post(f"/c/{tok}/suggest_time",
               data={"date_id": did2, "starts_at": "2030-08-01T19:00",
                     "ends_at": "2030-08-01T21:00"})
    assert r.status_code == 200, r.text
    page = ga.get(f"/c/{tok}").text
    assert "Предлагаю назначить" in page and "1 августа 2030, 19:00–21:00" in page
    assert "Предлагаю назначить" in c.get("/admin/questions").text
    step("у события без даты — чип «предложить дату»; предложение видно автору и админу")

    # ---------- админ принимает время одной кнопкой ----------
    qid_s = db_one("SELECT id FROM questions WHERE suggest_starts IS NOT NULL")["id"]
    qpage = c.get("/admin/questions").text
    assert "suggest-box" in qpage and "Принять время" in qpage
    r = apost(c, f"/admin/questions/{qid_s}/accept_time", {"next": "/admin/questions"})
    assert r.status_code == 303
    drow = db_one("SELECT starts_at, ends_at FROM dates WHERE id=?", (did2,))
    assert drow["starts_at"] == "2030-08-01T19:00" and drow["ends_at"] == "2030-08-01T21:00"
    page = ga.get(f"/c/{tok}").text
    card2 = re.search(r'<article[^>]*id="date-%d".*?</article>' % did2, page, re.S).group(0)
    assert "1 августа 2030" in card2 and "предложить дату" not in card2
    assert "✅ Принято" in card2                  # автор видит авто-ответ
    assert "Принять время" not in c.get("/admin/questions?f=all").text

    # ---------- …или вежливо отказывается ----------
    r = apost(c, "/admin/dates/new", {"name": "Качели", "categories": str(cid)})
    assert r.status_code == 303
    kid = db_one("SELECT id FROM dates WHERE name='Качели'")["id"]
    r = ga.post(f"/c/{tok}/suggest_time",
               data={"date_id": kid, "starts_at": "2030-09-05T15:00"})
    assert r.status_code == 200, r.text
    qid_d = db_one("SELECT id FROM questions WHERE date_id=?", (kid,))["id"]
    r = apost(c, f"/admin/questions/{qid_d}/decline_time", {"next": "/admin/questions"})
    assert r.status_code == 303
    assert db_one("SELECT starts_at FROM dates WHERE id=?", (kid,))["starts_at"] is None
    page = ga.get(f"/c/{tok}").text
    cardk = re.search(r'<article[^>]*id="date-%d".*?</article>' % kid, page, re.S).group(0)
    assert "не получится" in cardk and "предложить дату" in cardk   # чип остался

    # next из формы не должен уводить наружу (open redirect)
    for bad in ("https://evil.com", "//evil.com/x"):
        r = apost(c, f"/admin/questions/{qid_d}/decline_time", {"next": bad})
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.startswith("/admin") and "evil" not in loc, loc
    step("«Принять» назначает время, «Отказаться» — авто-ответ; next не уводит наружу")

    # ---------- событие с вместимостью 1 не принимает второго участника ----------
    g2 = guest_client(700102, tok, "Борис")
    r = g2.post(f"/c/{tok}/book", data={"date_id": did})   # лимит 1 уже занят Аней
    assert r.status_code == 409 and r.json()["detail"]["code"] == "capacity_reached", r.text
    g2page = g2.get(f"/c/{tok}").text
    card1 = re.search(r'<article[^>]*id="date-%d".*?</article>' % did, g2page, re.S).group(0)
    assert "vote-progress" in card1 and "1/1" in card1
    assert "Аня" in card1                                  # имя участника видно всем
    assert "Набрано 1/1" in card1                           # кнопка выбора заблокирована

    r = g2.post(f"/c/{tok}/book", data={"date_id": did2})  # свободное — можно
    assert r.json()["booked"] is True
    page = ga.get(f"/c/{tok}").text
    card2 = re.search(r'<article[^>]*id="date-%d".*?</article>' % did2, page, re.S).group(0)
    assert "vote-progress" in card2 and "1/1" in card2
    assert "Борис" in card2                                # имя участника видно всем

    # один гость выбирает НЕСКОЛЬКО событий; у каждого отдельно действует
    # настроенная вместимость (для этих легаси-событий — 1)
    r = apost(c, "/admin/dates/new", {"name": "Запасной", "categories": str(cid)})
    assert r.status_code == 303
    did_r = db_one("SELECT id FROM dates WHERE name='Запасной'")["id"]
    r = ga.post(f"/c/{tok}/book", data={"date_id": did_r})  # у Ани теперь did + did_r
    assert r.json()["booked"] is True
    rows = db_all("SELECT date_id FROM bookings WHERE category_id=? AND guest_token IN "
                  "(SELECT token FROM guests WHERE name='Аня') ORDER BY date_id", (cid,))
    assert {x["date_id"] for x in rows} == {did, did_r}    # обе брони живут вместе
    page = ga.get(f"/c/{tok}").text
    for d_ in (did, did_r):
        cd = re.search(r'<article[^>]*id="date-%d".*?</article>' % d_, page, re.S).group(0)
        assert "booked-me" in cd
    r = ga.post(f"/c/{tok}/book", data={"date_id": did_r})  # повторный тап — снять
    assert r.json()["booked"] is False
    rows = db_all("SELECT date_id FROM bookings WHERE category_id=? AND guest_token IN "
                  "(SELECT token FROM guests WHERE name='Аня')", (cid,))
    assert len(rows) == 1 and rows[0]["date_id"] == did    # did остался
    assert db_one("SELECT COUNT(*) AS n FROM bookings WHERE category_id=?", (cid,))["n"] == 2

    # ---------- админ может снять чужой выбор ----------
    edit_page = c.get(f"/admin/dates/{did2}/edit").text
    assert "✕ снять" in edit_page and "Борис" in edit_page
    bid = db_one("SELECT id FROM bookings WHERE date_id=?", (did2,))["id"]
    r = apost(c, f"/admin/bookings/{bid}/delete", {"next": f"/admin/dates/{did2}/edit"})
    assert r.status_code == 303 and "/edit" in r.headers["location"]
    assert db_one("SELECT COUNT(*) AS n FROM bookings WHERE category_id=?", (cid,))["n"] == 1
    r = g2.post(f"/c/{tok}/book", data={"date_id": did2})  # Борис выбирает заново
    assert r.json()["booked"] is True
    step("multiple: несколько вариантов на гостя; заполненный — 409; повторный тап снимает")

    # ---------- предложение гостя (без модерации) ----------
    main._rates.clear()
    # дефолт теперь «модерация вкл» — для этого блока явно выключаем
    if db_one("SELECT moderate_proposals FROM categories WHERE id=?", (cid,))[0]:
        apost(c, f"/admin/categories/{cid}/moderation", {})
    assert db_one("SELECT moderate_proposals FROM categories WHERE id=?", (cid,))[0] == 0
    r = ga.post(f"/c/{tok}/propose",
               data={"name": "Кино дома", "links": "kinopoisk.ru"},
               files=[("images", ("k.png", png((90, 120, 180)), "image/png"))])
    j = r.json()
    assert j["ok"] and j["moderated"] is False
    pid = j["id"]
    page = ga.get(f"/c/{tok}").text
    assert "Кино дома" in page and "идея гостя" in page

    r = ga.post(f"/c/{tok}/propose", data={"name": "Спам"},
               files=[("images", (f"s{i}.png", png(), "image/png")) for i in range(6)])
    assert r.status_code == 400 and "Максимум" in r.json()["detail"]
    r = ga.post(f"/c/{tok}/propose", data={"name": "Спам"},
               files=[("images", ("s.png", b"junk", "image/png"))])
    assert r.status_code == 400 and "не похож" in r.json()["detail"]
    r = ga.post(f"/c/{tok}/propose", data={"name": "  "})
    assert r.status_code == 400 and "обязательно" in r.json()["detail"]
    assert db_one("SELECT COUNT(*) AS n FROM dates WHERE name='Спам'")["n"] == 0
    step("гость предложил событие; битые пачки фото и пустые имена отклоняются целиком")

    # ---------- гость правит своё: фото, keep_order ----------
    main._rates.clear()
    img_old = db_one("SELECT id, filename FROM date_images WHERE date_id=?", (pid,))
    assert ga.get(f"/c/{tok}/image/{img_old['filename']}?w=480").status_code == 200
    old_thumb = main.images.RESPONSIVE_DIR / f"{Path(img_old['filename']).stem}.w480.webp"
    assert old_thumb.exists()
    r = ga.post(f"/c/{tok}/propose/{pid}/edit", data={
        "name": "Кино под пледом", "place": "Дом", "links": "ya.ru",
        "comment": "", "starts_at": "", "ends_at": "",
        "remove_image": str(img_old["id"]),
    }, files=[("images", ("k2.png", png((20, 160, 90)), "image/png"))])
    assert r.status_code == 200, r.text
    assert not (main.images.UPLOAD_DIR / img_old["filename"]).exists()
    assert not old_thumb.exists()
    imgs = db_all("SELECT id, filename FROM date_images WHERE date_id=?", (pid,))
    assert len(imgs) == 1 and (main.images.UPLOAD_DIR / imgs[0]["filename"]).exists()
    assert db_one("SELECT url FROM date_links WHERE date_id=?", (pid,))["url"] == "https://ya.ru"

    # второе фото, затем разворот порядка через keep_order (drag-and-drop)
    r = ga.post(f"/c/{tok}/propose/{pid}/edit", data={
        "name": "Кино под пледом", "place": "Дом", "links": "ya.ru",
        "comment": "", "starts_at": "", "ends_at": "",
    }, files=[("images", ("k3.png", png((220, 120, 40)), "image/png"))])
    assert r.status_code == 200
    ids = [x["id"] for x in db_all(
        "SELECT id FROM date_images WHERE date_id=? ORDER BY position, id", (pid,))]
    assert len(ids) == 2
    r = ga.post(f"/c/{tok}/propose/{pid}/edit", data={
        "name": "Кино под пледом", "place": "Дом", "links": "ya.ru",
        "comment": "", "starts_at": "", "ends_at": "",
        "keep_order": f"{ids[1]},{ids[0]}",
    })
    assert r.status_code == 200
    ids2 = [x["id"] for x in db_all(
        "SELECT id FROM date_images WHERE date_id=? ORDER BY position, id", (pid,))]
    assert ids2 == [ids[1], ids[0]]

    page = ga.get(f"/c/{tok}").text
    assert "Кино под пледом" in page
    meta_raw = re.search(r'data-meta="([^"]+)"', page).group(1)
    meta = json.loads(html.unescape(meta_raw))
    assert meta["id"] == pid and len(meta["photos"]) == 2
    step("правка предложения: замена фото без сирот, порядок по keep_order, meta для формы")

    # ---------- чужое/выбранное менять нельзя; удаление чистит файлы ----------
    r = g2.post(f"/c/{tok}/propose/{pid}/edit",
                data={"name": "Чужое", "place": "", "links": "", "comment": "",
                      "starts_at": "", "ends_at": ""})
    assert r.status_code == 403 and "не твоё" in r.json()["detail"]

    r = c.post(f"/admin/dates/{pid}/choose", data={"csrf": "x"})
    assert r.status_code in (303, 404, 405)        # роут удалён: 404 → friendly-redirect
    if r.status_code == 303:
        assert "404" in r.headers["location"] or "%E2%9A%A0" in r.headers["location"]

    fn_pid = imgs[0]["filename"]
    r = ga.post(f"/c/{tok}/propose/{pid}/delete")
    assert r.status_code == 200
    assert not db_one("SELECT 1 FROM dates WHERE id=?", (pid,))
    assert not (main.images.UPLOAD_DIR / fn_pid).exists()
    assert "Кино под пледом" not in ga.get(f"/c/{tok}").text
    step("чужому правка запрещена; удаление чистит файлы; роут /choose выпилен")

    # ---------- событие активно и без категории, но не входит в голосование ----------
    r = apost(c, "/admin/dates/new", {"name": "Сюрприз"})
    assert r.status_code == 303
    did4 = db_one("SELECT id FROM dates WHERE name='Сюрприз'")["id"]
    assert db_one("SELECT is_draft FROM dates WHERE id=?", (did4,))["is_draft"] == 0
    assert "Сюрприз" not in ga.get(f"/c/{tok}").text
    dpage = c.get("/admin/dates?view=active").text
    assert "Сюрприз" in dpage and ">Неактивные<" not in dpage
    # привязка к категории добавляет событие в голосование, не меняя статус
    r = apost(c, f"/admin/categories/{cid}/attach", {"date_id": str(did4)})
    assert r.status_code == 303
    assert db_one("SELECT is_draft FROM dates WHERE id=?", (did4,))["is_draft"] == 0
    assert "Сюрприз" in ga.get(f"/c/{tok}").text
    # отвязка убирает из голосования, но событие остаётся активным в коллекции
    r = apost(c, f"/admin/categories/{cid}/detach", {"date_id": str(did4)})
    assert r.status_code == 303
    assert db_one("SELECT is_draft FROM dates WHERE id=?", (did4,))["is_draft"] == 0
    assert "Сюрприз" not in ga.get(f"/c/{tok}").text
    step("событие без категории активно в коллекции; привязка управляет только голосованием")

    # ---------- модерация предложений ----------
    r = apost(c, f"/admin/categories/{cid}/moderation", {})
    assert r.status_code == 303
    # блок «Предложения» из редактора убран (правка UI) — проверяем состояние в БД
    assert db_one("SELECT moderate_proposals FROM categories WHERE id=?", (cid,))[0] == 1

    main._rates.clear()
    r = ga.post(f"/c/{tok}/propose", data={"name": "Тайное место"},
               files=[("images", ("t.png", png((200, 160, 60)), "image/png"))])
    j = r.json()
    assert j["moderated"] is True
    pid2 = j["id"]
    fn2 = db_one("SELECT filename FROM date_images WHERE date_id=?", (pid2,))["filename"]

    owner_page = ga.get(f"/c/{tok}").text       # автор предложения видит своё «на модерации»
    assert "Тайное место" in owner_page and "ждёт проверки" in owner_page
    assert ga.get(f"/c/{tok}/image/{fn2}").status_code == 200
    other_page = g2.get(f"/c/{tok}").text
    assert "Тайное место" not in other_page
    assert g2.get(f"/c/{tok}/image/{fn2}").status_code == 404
    assert "на модерации" in c.get("/admin/dates?view=active").text

    r = apost(c, f"/admin/dates/{pid2}/publish", {"next": "/admin/dates?view=active"})
    assert "Тайное место" in g2.get(f"/c/{tok}").text
    apost(c, f"/admin/categories/{cid}/moderation", {})  # выключить обратно
    step("модерация: предложение и фото видны только автору до публикации")

    # ---------- архив виден гостям, брони считаются по активным ----------
    # блок статистики убран с главной; счётчики событий переехали в пилюли
    # вкладок на странице «События» (Активные/Архив).
    dash = c.get("/admin/").text
    assert "dcount-row" not in dash and "броней сейчас" not in dash
    dpage = c.get("/admin/dates?view=active").text
    assert "Активные" in dpage and "Архив" in dpage
    assert db_one("SELECT COUNT(*) FROM bookings b JOIN dates d ON d.id=b.date_id "
                  "WHERE d.archived_at IS NULL")[0] == 2   # Аня + Борис на «Ужине»
    r = apost(c, f"/admin/dates/{did}/archive", {"next": "/admin/dates"})
    assert r.status_code == 303
    page = ga.get(f"/c/{tok}").text
    assert "Ужин на крыше" in page
    card = re.search(r'<article[^>]*id="date-%d".*?</article>' % did, page, re.S).group(0)
    # Архивация во время открытого голосования снимает бюллетень: неактивный
    # вариант не сможет победить за спиной у участников.
    assert 'class="card past"' in card                # карточка в общем списке
    assert "проведено с" not in card
    assert 'class="btn book"' not in card             # действий в архиве нет
    assert ga.get(f"/c/{tok}/image/{fn_did}").status_code == 200   # фото остаётся
    assert ga.get(f"/c/{tok}/ics/{did}").status_code == 404
    assert ga.post(f"/c/{tok}/book", data={"date_id": did}).status_code == 404
    # архивная бронь не в счёт: осталась только бронь Бориса на «Без даты»
    assert db_one("SELECT COUNT(*) FROM bookings b JOIN dates d ON d.id=b.date_id "
                  "WHERE d.archived_at IS NULL")[0] == 1
    r = apost(c, f"/admin/dates/{did}/archive", {"next": "/admin/dates?view=archived"})
    page = ga.get(f"/c/{tok}").text
    card = re.search(r'<article[^>]*id="date-%d".*?</article>' % did, page, re.S).group(0)
    assert 'class="btn book"' in card and 'class="btn book on"' not in card
    assert db_one("SELECT COUNT(*) FROM bookings b JOIN dates d ON d.id=b.date_id "
                  "WHERE d.archived_at IS NULL")[0] == 1
    # архив брони НЕ блокирует выбор другого активного события тем же гостем:
    # создаём свежее активное событие и проверяем, что Аня может его выбрать
    r = apost(c, "/admin/dates/new", {"name": "Новый вечер", "categories": str(cid)})
    assert r.status_code == 303
    did_new = db_one("SELECT id FROM dates WHERE name='Новый вечер'")["id"]
    rb = ga.post(f"/c/{tok}/book", data={"date_id": did_new})
    assert rb.status_code == 200 and rb.json().get("booked") is True
    apost(c, f"/admin/dates/{did_new}/delete", {})   # прибираем за тестом
    step("архив остаётся на странице, фото видны, голосование и .ics закрыты")

    # ---------- авто-архив выполняется фоновым проходом ----------
    # Открытое голосование не позволяет через UI добавить вариант, который
    # начинается до дедлайна. Для изолированной проверки миграционного
    # автоархива создаём допустимый вариант и имитируем старую строку в БД.
    r = apost(c, "/admin/dates/new", {
        "name": "Вчерашний вечер", "categories": str(cid)})
    assert r.status_code == 303
    _q = dbm.connect()
    # Старые/внешне импортированные базы могли содержать такую строку ещё до
    # появления v24-инварианта. На мгновение отключаем только новый guard,
    # затем сразу восстанавливаем актуальную схему.
    _q.execute("DROP TRIGGER IF EXISTS trg_dates_open_deadline_update")
    _q.execute(
        "UPDATE dates SET starts_at='2020-02-14T19:00', "
        "ends_at='2020-02-14T22:00' WHERE name='Вчерашний вечер'"
    )
    _q.commit()
    _q.executescript(dbm.SCHEMA)
    _q.close()
    # GET остаётся read-only; тот же helper запускается при старте и далее
    # периодически фоновым циклом приложения.
    assert main.autoarchive_once() >= 1
    page = ga.get(f"/c/{tok}").text
    assert "Вчерашний вечер" in page
    card = re.search(r'<article[^>]*>(?:(?!</article>).)*Вчерашний вечер.*?</article>',
                     page, re.S).group(0)
    assert "past" in card and 'class="btn book"' not in card
    assert db_one("SELECT archived_at FROM dates WHERE name='Вчерашний вечер'")["archived_at"]
    step("фоновый проход переносит просроченное событие в архив; гостевой GET только читает")

    # ---------- выключение и перегенерация ссылки ----------
    apost(c, f"/admin/categories/{cid}/toggle", {})
    r = ga.get(f"/c/{tok}")
    assert r.status_code == 404 and "не действует" in r.text
    assert ga.get(f"/c/{tok}/image/{fn_did}").status_code == 404
    assert ga.post(f"/c/{tok}/book", data={"date_id": did}).status_code == 410
    apost(c, f"/admin/categories/{cid}/toggle", {})

    # Любое ожидающее уведомление с прежним секретным URL должно быть
    # переписано атомарно, даже если его event_key не начинается с category:.
    _q = dbm.connect()
    _owner = _q.execute("SELECT owner_id FROM categories WHERE id=?", (cid,)).fetchone()[0]
    _stamp = main.now_iso()
    _q.execute(
        "INSERT INTO notification_outbox(user_id,kind,event_key,text,send_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (_owner, "date_changed", f"synthetic-change:{cid}",
         f"Открой https://t.local/c/{tok}", "2099-01-01T00:00:00", _stamp, _stamp),
    )
    _q.commit(); _q.close()
    apost(c, f"/admin/categories/{cid}/regenerate", {})
    assert ga.get(f"/c/{tok}").status_code == 404
    detail = c.get(f"/admin/categories/{cid}").text
    new_tok = re.search(r"https://t\.local/c/([A-Za-z0-9_-]+)", detail).group(1)
    assert new_tok != tok
    queued_text = db_one(
        "SELECT text FROM notification_outbox WHERE event_key=?",
        (f"synthetic-change:{cid}",),
    )["text"]
    assert f"/c/{new_tok}" in queued_text and f"/c/{tok}" not in queued_text
    tok = new_tok
    page = ga.get(f"/c/{tok}").text
    # Ссылка меняется, пользовательская сессия сохраняется; снятый при архиве
    # голос намеренно не воскресает.
    assert 'class="btn book on"' not in page and 'id="greetName">Аня<' in page
    step("выключенная ссылка отдаёт 404/410 (и для фото); после перегенерации брони и имя целы")

    # ---------- привязка к категории ----------
    r = apost(c, f"/admin/categories/{cid}/attach", {"date_id": "99999"})
    assert r.status_code == 303
    fr = c.get(r.headers["location"])
    assert "⚠" in fr.text and "не найдено" in fr.text

    apost(c, "/admin/dates/new", {"name": "Гуляка"})
    did3 = db_one("SELECT id FROM dates WHERE name='Гуляка'")["id"]
    apost(c, f"/admin/categories/{cid}/attach", {"date_id": str(did3)})
    assert "Гуляка" in c.get(f"/admin/categories/{cid}").text
    apost(c, f"/admin/categories/{cid}/detach", {"date_id": str(did3)})
    assert not db_one("SELECT 1 FROM date_categories WHERE date_id=? AND category_id=?",
                      (did3, cid))
    step("привязка несуществующего события — мягкая ошибка, attach/detach работают")

    # ---------- порядок фото: drag-and-drop endpoint ----------
    r = apost(c, f"/admin/dates/{did}/edit", {
        "name": "Ужин на крыше", "place": "Крыша, СПб",
        "starts_at": "2030-07-01T18:00", "ends_at": "2030-07-01T21:00",
        "links": "", "comment": "", "categories": str(cid),
    }, files=[("images", ("b.png", png((60, 60, 200)), "image/png"))])
    assert r.status_code == 303
    ids = [x["id"] for x in db_all(
        "SELECT id FROM date_images WHERE date_id=? ORDER BY position, id", (did,))]
    assert len(ids) == 2
    r = apost(c, f"/admin/dates/{did}/images/reorder",
              {"order": f"{ids[1]},{ids[0]}"})
    assert r.status_code == 200 and r.json()["ok"] is True
    ids2 = [x["id"] for x in db_all(
        "SELECT id FROM date_images WHERE date_id=? ORDER BY position, id", (did,))]
    assert ids2 == [ids[1], ids[0]]
    r = apost(c, f"/admin/dates/{did}/images/reorder", {"order": str(ids[0])})
    assert r.status_code == 303          # не перестановка → мягкая ошибка с flash
    assert "Некорректный порядок" in c.get(r.headers["location"]).text
    step("порядок фото сохраняется одним запросом после перетаскивания (обложка — первое)")

    # ---------- экспорт ----------
    # заранее создаём событие с видео, чтобы проверить экспорт видео
    MP4_EXP = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16 + b"\x00" * 64
    r = apost(c, "/admin/dates/new", {"name": "Экспорт-видео"},
              files=[("videos", ("ev.mp4", MP4_EXP, "video/mp4"))])
    assert r.status_code == 303
    exp_vid = db_one(
        "SELECT d.id AS did, dv.filename AS fn FROM date_videos dv "
        "JOIN dates d ON d.id=dv.date_id WHERE d.name='Экспорт-видео'")
    exp_vid_fn = exp_vid["fn"]

    r = c.get("/admin/export/csv")
    assert r.status_code == 200 and r.text.startswith("\ufeff")
    assert "Ужин на крыше" in r.text and "Кто выбрал" in r.text and "Борис" in r.text
    r = c.get("/admin/export/json")
    data = json.loads(r.text)
    assert any(d["name"] == "Ужин на крыше" for d in data["dates"])
    assert any(g["name"] == "Аня" for g in data["guests"])
    # export.json содержит видео
    evd = [d for d in data["dates"] if d["name"] == "Экспорт-видео"][0]
    assert evd["videos"] == [exp_vid_fn], evd["videos"]
    r = c.get("/admin/export/archive")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "app.db" in names and "export.json" in names
    assert any(n.startswith("uploads/") for n in names)
    # видео-файл попал в архив
    assert f"uploads/{exp_vid_fn}" in names, names
    assert any(n.endswith((".mp4", ".webm")) for n in names)
    # подчищаем, чтобы не сдвинуть счётчики пагинации ниже
    apost(c, f"/admin/dates/{exp_vid['did']}/delete", {})
    step("экспорт: CSV с именами, JSON+zip содержат данные, фото и видео")

    # ---------- вопросы: прочитано/удалить ----------
    apost(c, f"/admin/questions/{qid}/toggle", {"next": "/admin/questions"})
    apost(c, f"/admin/questions/{qid}/delete", {"next": "/admin/questions"})
    assert not db_one("SELECT 1 FROM questions WHERE id=?", (qid,))
    step("вопрос можно отметить прочитанным и удалить")

    # ---------- анти-спам лимиты ----------
    main._rates.clear()
    c3 = guest_client(700103, tok, "Спамер")
    for i in range(5):
        r = c3.post(f"/c/{tok}/propose", data={"name": f"спам{i}"})
        assert r.status_code == 200, r.text
    r = c3.post(f"/c/{tok}/propose", data={"name": "спам6"})
    assert r.status_code == 429

    main._rates.clear()
    c4 = guest_client(700104, tok, "Почемучка")
    for i in range(10):
        r = c4.post(f"/c/{tok}/question", data={"date_id": did, "text": f"в{i}?"})
        assert r.status_code == 200, r.text
    r = c4.post(f"/c/{tok}/question", data={"date_id": did, "text": "в11?"})
    assert r.status_code == 429

    main._rates.clear()
    c5 = guest_client(700105, tok, "Кликер")
    for i in range(30):
        r = c5.post(f"/c/{tok}/book", data={"date_id": did_r})
        assert r.status_code == 200, r.text
    r = c5.post(f"/c/{tok}/book", data={"date_id": did_r})
    assert r.status_code == 429
    main._rates.clear()
    step("лимиты: 5 предложений / 10 вопросов за 10 мин, 30 действий с бронью в минуту → 429")

    main._rates["мертвое:g:x"] = [time.time() - 99999]
    main.prune_rate_buckets()
    assert "мертвое:g:x" not in main._rates
    step("чистка пустых вёдер лимитов работает")

    # ---------- notify: текст доходит до httpx, 5xx не роняет ----------
    sent = []
    class _Resp:
        status_code = 500
        text = "Internal Server Error"
    def _fake_post(url, json=None, timeout=None):
        sent.append((url.rsplit("/", 1)[-1],
                     json.get("text") or json["rich_message"]["html"]))
        return _Resp()
    real_post = main.notify.httpx.post
    main.notify.httpx.post = _fake_post
    main.notify.TOKEN, main.notify.CHAT = "t", "c"
    main.notify.notify("проверка 500")        # статус уйдёт в лог, исключения нет
    _Resp.status_code = 200
    main.notify.notify("проверка 200")
    assert [method for method, _ in sent] == [
        "sendRichMessage", "sendMessage", "sendRichMessage"
    ]
    assert "проверка 500" in sent[0][1] and sent[1][1] == "проверка 500"
    assert "проверка 200" in sent[2][1]
    main.notify.TOKEN = main.notify.CHAT = ""  # выключаем обратно
    main.notify.httpx.post = real_post
    step("notify переживает 5xx Telegram и логирует статус (через подмену httpx)")

    # ---------- alert: дедупликация одинаковых алёртов о сбоях ----------
    sent2 = []
    def _fake_post2(url, json=None, timeout=None):
        sent2.append(json.get("text") or json["rich_message"]["html"])
        return _Resp()
    main.notify.httpx.post = _fake_post2
    main.notify.TOKEN, main.notify.CHAT = "t", "c"
    main.notify._alert_seen.clear()
    main.notify.alert("сбой X")
    main.notify.alert("сбой X")           # дубль в окне — не уходит
    main.notify.alert("сбой Y")
    assert len(sent2) == 2 and "сбой X" in sent2[0] and "сбой Y" in sent2[1], sent2
    main.notify.TOKEN = main.notify.CHAT = ""
    main.notify.httpx.post = real_post
    main.notify._alert_seen.clear()
    # обработчик 500-х зарегистрирован (последний рубеж от утечки трейсбеков)
    assert Exception in main.app.exception_handlers
    step("alert троттлит одинаковые сбои; обработчик 500-х подключён")

    # ---------- авто-архив (фоновая функция напрямую) ----------
    conn = dbm.connect()
    _owner = conn.execute("SELECT id FROM users WHERE telegram_id=555001").fetchone()[0]
    conn.execute(
        "INSERT INTO dates(owner_id, name, starts_at, origin, created_at) VALUES(?,?,?,?,?)",
        (_owner, "Прошлогоднее", "2020-01-01T10:00", "admin", main.now_iso()))
    conn.commit()
    conn.close()
    assert main.autoarchive_once() >= 1
    assert db_one("SELECT archived_at FROM dates WHERE name='Прошлогоднее'")["archived_at"]
    step("авто-архив переносит просроченные события")

    # ---------- удаление категории чистит брони ----------
    assert db_one("SELECT COUNT(*) AS n FROM bookings WHERE category_id=?", (cid,))["n"] >= 1
    r = apost(c, f"/admin/categories/{cid}/delete", {})
    assert r.status_code == 303
    assert db_one("SELECT COUNT(*) AS n FROM bookings WHERE category_id=?", (cid,))["n"] == 0
    assert "Ужин на крыше" in c.get("/admin/dates").text
    step("удаление категории чистит её брони, события остаются")

    # ================= фичи v7 =================
    main._rates.clear()
    r = apost(c, "/admin/categories/create", category_data("Витрина"))
    assert r.status_code == 303
    vc = db_one("SELECT id, link_token FROM categories WHERE name='Витрина'")
    vcid, vtok = vc["id"], vc["link_token"]
    configure_voting(vcid, "multiple")
    apost(c, f"/admin/categories/{vcid}/moderation", {})  # витринные фичи без модерации

    # описание категории (видно всем) + разметка в нём
    r = apost(c, f"/admin/categories/{vcid}/rename",
              {"name": "Витрина", "description": "__подчёркнутое__ наш список"})
    assert r.status_code == 303
    gpage = c.get(f"/c/{vtok}").text
    assert "cat-desc" in gpage and "<u>подчёркнутое</u>" in gpage

    # rich-разметка в комментарии события
    r = apost(c, "/admin/dates/new",
              {"name": "Разметка", "categories": str(vcid),
               "comment": "**жирно** и *тонко* и [сайт](https://example.com)"})
    assert r.status_code == 303
    card = re.search(r'<article[^>]*>.*?Разметка.*?</article>',
                     c.get(f"/c/{vtok}").text, re.S).group(0)
    assert "<b>жирно</b>" in card and "<i>тонко</i>" in card
    assert '<a href="https://example.com"' in card

    # модификатор оплаты 50/50 (необязательный)
    r = apost(c, "/admin/dates/new",
              {"name": "Делим счёт", "categories": str(vcid), "pay": "1"})
    assert r.status_code == 303
    did_pay = db_one("SELECT id, pay_split FROM dates WHERE name='Делим счёт'")
    assert did_pay["pay_split"] == 1
    assert "50/50" in c.get(f"/c/{vtok}").text
    assert "checked" in c.get(f"/admin/dates/{did_pay['id']}/edit").text  # галка стоит

    # счётчик событий на гостевой
    gpage = c.get(f"/c/{vtok}").text
    assert "count-line" in gpage and "событи" in gpage

    # место-ссылка: название тянется из <title>, клик ведёт по ссылке
    real_resolve = main.places.resolve_name
    main.places.resolve_name = lambda u: "Кафе «Ромашка»"
    try:
        r = apost(c, "/admin/dates/new",
                  {"name": "Событие с местом", "categories": str(vcid),
                   "place": "https://yandex.ru/maps/-/CPtbJHmP"})
        assert r.status_code == 303
        dm = db_one("SELECT place, place_url FROM dates WHERE name='Событие с местом'")
        assert dm["place"] == "Кафе «Ромашка»"
        assert dm["place_url"] == "https://yandex.ru/maps/-/CPtbJHmP"
        card = re.search(r'<article[^>]*>.*?Событие с местом.*?</article>',
                         c.get(f"/c/{vtok}").text, re.S).group(0)
        assert "Кафе «Ромашка»" in card
        assert 'href="https://yandex.ru/maps/-/CPtbJHmP"' in card
    finally:
        main.places.resolve_name = real_resolve

    # SSRF-защита: автозапрос только на доверённые карты, внутренние адреса режутся
    assert main.places._host_allowed("yandex.ru")
    assert main.places._host_allowed("maps.google.com")
    assert not main.places._host_allowed("evil.com")
    assert not main.places._host_allowed("localhost")
    # не-картовая ссылка: resolve_name не ходит по ней, имя — фолбэк, ссылка цела
    assert main.places.resolve_name("http://169.254.169.254/latest/meta-data/") is None
    assert main.places.resolve_name("https://[некорректный-url") is None
    name, link = main.places.process_place("http://internal.local/admin")
    assert name == "Место на карте" and link == "http://internal.local/admin"
    # Разрешённый домен не может протащить SSRF через HTTP-редирект:
    # каждый следующий Location проверяется ДО сетевого запроса.
    real_get = main.places.httpx.get
    real_public_ip = main.places._resolves_to_public_ip
    requested = []

    class MapRedirect:
        status_code = 302
        headers = {"location": "http://127.0.0.1/private"}
        text = ""

    try:
        main.places._resolves_to_public_ip = lambda host: True

        def fake_map_get(url, **kwargs):
            requested.append((url, kwargs))
            return MapRedirect()

        main.places.httpx.get = fake_map_get
        assert main.places.resolve_name("https://yandex.ru/maps/test") is None
        assert len(requested) == 1
        assert requested[0][0] == "https://yandex.ru/maps/test"
        assert requested[0][1]["follow_redirects"] is False
    finally:
        main.places.httpx.get = real_get
        main.places._resolves_to_public_ip = real_public_ip

    # видео: загрузка админом, отдача с поддержкой Range
    MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16 + b"\x00" * 64
    r = apost(c, "/admin/dates/new", {"name": "С видео", "categories": str(vcid)},
              files=[("videos", ("v.mp4", MP4, "video/mp4"))])
    assert r.status_code == 303
    did_vid = db_one("SELECT id FROM dates WHERE name='С видео'")["id"]
    vrow = db_one("SELECT filename FROM date_videos WHERE date_id=?", (did_vid,))
    assert vrow is not None
    gpage = c.get(f"/c/{vtok}").text
    assert "<video" in gpage and f"/c/{vtok}/video/" in gpage

    vfn = vrow["filename"]
    rv = c.get(f"/c/{vtok}/video/{vfn}")
    assert rv.status_code == 200 and rv.headers.get("accept-ranges") == "bytes"
    rv = c.get(f"/c/{vtok}/video/{vfn}", headers={"Range": "bytes=0-3"})
    assert rv.status_code == 206 and len(rv.content) == 4
    assert rv.headers["content-range"].startswith("bytes 0-3/")

    # ---------- клонирование события админом ----------
    # исходник: фото + видео + ссылка + категория; клонируем и проверяем дубль
    r = apost(c, "/admin/dates/new",
              {"name": "Оригинал", "categories": str(vcid), "place": "Парк",
               "links": "ya.ru", "comment": "будет здорово", "pay": "1"},
              files=[("images", ("o.png", png((40, 80, 120)), "image/png")),
                     ("videos", ("ov.mp4", MP4, "video/mp4"))])
    assert r.status_code == 303
    orig = db_one("SELECT * FROM dates WHERE name='Оригинал'")
    orig_files = {x["filename"] for x in db_all(
        "SELECT filename FROM date_images WHERE date_id=?", (orig["id"],))}
    orig_files |= {x["filename"] for x in db_all(
        "SELECT filename FROM date_videos WHERE date_id=?", (orig["id"],))}

    r = apost(c, f"/admin/dates/{orig['id']}/clone", {"next": "/admin/dates"})
    assert r.status_code == 303
    clone = db_one("SELECT * FROM dates WHERE name='Оригинал (копия)'")
    assert clone is not None and clone["id"] != orig["id"]
    assert clone["is_draft"] == 0 and clone["is_public"] == 0
    assert clone["place"] == "Парк" and clone["pay_split"] == 1
    assert clone["comment"] == "будет здорово"
    # ссылка перенесена; категории НЕ переносятся, клон активен, но непубличен
    assert db_one("SELECT url FROM date_links WHERE date_id=?", (clone["id"],))["url"] \
        == "https://ya.ru"
    assert db_one("SELECT 1 FROM date_categories WHERE date_id=? AND category_id=?",
                  (clone["id"], vcid)) is None
    # файлы — отдельные копии (новые имена, оба существуют на диске)
    clone_files = {x["filename"] for x in db_all(
        "SELECT filename FROM date_images WHERE date_id=?", (clone["id"],))}
    clone_files |= {x["filename"] for x in db_all(
        "SELECT filename FROM date_videos WHERE date_id=?", (clone["id"],))}
    assert len(clone_files) == 2 and clone_files.isdisjoint(orig_files)
    assert all((main.images.UPLOAD_DIR / fn).exists() for fn in clone_files)
    # брони/вопросы НЕ копируются
    assert not db_one("SELECT 1 FROM bookings WHERE date_id=?", (clone["id"],))
    # удаление клона не задевает файлы оригинала
    apost(c, f"/admin/dates/{clone['id']}/delete", {})
    assert all((main.images.UPLOAD_DIR / fn).exists() for fn in orig_files)
    step("клон события: активный непубличный дубль, новые файлы, без броней")

    # ---------- поделиться событием → добавить себе (/d/<share_token>) ----------
    # владелец A создаёт активное событие с фото+видео+ссылкой; у него есть
    # стабильная share-ссылка. Другой пользователь B добавляет копию себе.
    r = apost(c, "/admin/dates/new",
              {"name": "Поделюсь", "categories": str(vcid), "place": "Сад",
               "links": "share.example"},
              files=[("images", ("s.png", png((70, 30, 90)), "image/png")),
                     ("videos", ("sv.mp4", MP4, "video/mp4"))])
    assert r.status_code == 303
    shared = db_one("SELECT * FROM dates WHERE name='Поделюсь'")
    assert shared["share_token"], "у события должен быть share_token"
    stok = shared["share_token"]
    a_files = {x["filename"] for x in db_all(
        "SELECT filename FROM date_images WHERE date_id=?", (shared["id"],))}
    a_files |= {x["filename"] for x in db_all(
        "SELECT filename FROM date_videos WHERE date_id=?", (shared["id"],))}

    # аноним видит превью и приглашение войти (кнопка открывает модалку входа,
    # а не уводит на /login), но не форму «Сохранить к себе»
    anon = TestClient(main.app, follow_redirects=False)
    pg = anon.get(f"/d/{stok}")
    assert pg.status_code == 200
    assert "data-login-open" in pg.text and 'id="loginDlg"' in pg.text
    assert "Сохранить к себе" not in pg.text
    shared_card = re.search(
        r'<article[^>]*id="date-%d".*?</article>' % shared["id"], pg.text, re.S,
    ).group(0)
    assert re.search(r'class="vote-progress-head"[^>]*\bhidden\b', shared_card)
    assert re.search(r'class="vote-progress-track"[^>]*\bhidden\b', shared_card)
    assert pg.headers.get("x-robots-tag") == "noindex"
    # фото события отдаётся по share-ссылке
    a_photo = db_one("SELECT filename FROM date_images WHERE date_id=?", (shared["id"],))
    assert anon.get(f"/d/{stok}/image/{a_photo['filename']}").status_code == 200
    # битый токен → 404 (страница «ссылка не действует»)
    assert anon.get("/d/нет-такого").status_code == 404
    assert anon.post("/d/нет-такого/add").status_code == 404

    # пользователь B логинится, видит карточку с «Выбрать» и форму «Сохранить к себе»
    cb = guest_client(700621, vtok, "Получатель")
    bp = cb.get(f"/d/{stok}")
    assert bp.status_code == 200
    assert f'action="/d/{stok}/add"' in bp.text and "Сохранить к себе" in bp.text
    assert "Выбрать" in bp.text and "Выбрать ♥" not in bp.text
    assert 'data-skin="friends"' in bp.text           # полноценный дружеский UI
    b_uid = db_one("SELECT id FROM users WHERE telegram_id=?", (700621,))["id"]

    # B может выбрать событие прямо со страницы шаринга (контекст — категория)
    rb = cb.post(f"/d/{stok}/book")
    assert rb.status_code == 200 and rb.json()["booked"] is True
    assert db_one("SELECT 1 FROM bookings WHERE date_id=? AND user_id=?",
                  (shared["id"], b_uid))
    # повторный тап снимает выбор
    rb = cb.post(f"/d/{stok}/book")
    assert rb.status_code == 200 and rb.json()["booked"] is False
    # автор своё же событие выбрать не может
    assert c.post(f"/d/{stok}/book").status_code == 400

    # добавляем себе → 303 в редактор нового события B
    r = cb.post(f"/d/{stok}/add")
    assert r.status_code == 303 and "/admin/dates/" in r.headers["location"]
    mine = db_one("SELECT * FROM dates WHERE owner_id=? AND name='Поделюсь'", (b_uid,))
    assert mine is not None and mine["id"] != shared["id"]
    assert mine["archived_at"] is None and mine["is_draft"] == 0    # активное, не черновик
    assert mine["place"] == "Сад"
    assert mine["share_token"] and mine["share_token"] != stok       # свой свежий токен
    # ссылка перенесена, категории/брони — нет
    assert db_one("SELECT url FROM date_links WHERE date_id=?", (mine["id"],))["url"] \
        == "https://share.example"
    assert not db_one("SELECT 1 FROM date_categories WHERE date_id=?", (mine["id"],))
    assert not db_one("SELECT 1 FROM bookings WHERE date_id=?", (mine["id"],))
    # файлы — отдельные копии (новые имена, оба существуют на диске)
    b_files = {x["filename"] for x in db_all(
        "SELECT filename FROM date_images WHERE date_id=?", (mine["id"],))}
    b_files |= {x["filename"] for x in db_all(
        "SELECT filename FROM date_videos WHERE date_id=?", (mine["id"],))}
    assert len(b_files) == 2 and b_files.isdisjoint(a_files)
    assert all((main.images.UPLOAD_DIR / fn).exists() for fn in b_files)
    # своё же событие добавить нельзя (отбой)
    assert c.post(f"/d/{stok}/add").status_code == 400
    cb.close()
    anon.close()
    refresh_csrf(c)                                # вернём CSRF владельцу A для след. блоков
    step("поделиться событием: /d/<токен> превью, добавить себе → копия активна, файлы скопированы, категории/брони не переносятся")

    # ---------- категории: ссылка-токен и шапка с «Выйти» ----------
    cats_page = c.get("/admin/categories").text
    # кнопка «Открыть» на карточке категории убрана; ссылка копируется по data-copy,
    # а сама карточка ведёт в детали категории (aria-label «Открыть …» — не кнопка)
    assert f'/c/{vtok}' in cats_page and 'open-link' not in cats_page
    assert 'class="cat-link"' in cats_page
    # «Выйти» теперь в шапке любой админ-страницы (форма POST /admin/logout)
    assert 'action="/admin/logout"' in cats_page and "Выйти" in cats_page
    # из профиля большая кнопка-логаут убрана
    assert "logout-btn" not in c.get("/admin/profile").text
    step("UI: кнопка «Открыть» на карточке категории; «Выйти» вынесена в шапку")

    # битый «видеофайл» (на самом деле png-байты) — мягкая ошибка
    r = apost(c, "/admin/dates/new", {"name": "Битое видео", "categories": str(vcid)},
              files=[("videos", ("x.mp4", png(), "video/mp4"))])
    assert r.status_code == 303 and "%E2%9A%A0" in r.headers["location"]

    # сирот не остаётся: фото валидное, видео битое → НИ фото, НИ видео на диске
    before = set(p.name for p in main.images.UPLOAD_DIR.iterdir())
    r = apost(c, "/admin/dates/new", {"name": "Сирота-тест", "categories": str(vcid)},
              files=[("images", ("ok.png", png((10, 20, 30)), "image/png")),
                     ("videos", ("bad.mp4", png(), "video/mp4"))])
    assert r.status_code == 303 and "%E2%9A%A0" in r.headers["location"]
    after = set(p.name for p in main.images.UPLOAD_DIR.iterdir())
    assert after == before, f"остались файлы-сироты: {after - before}"
    assert not db_one("SELECT 1 FROM dates WHERE name='Сирота-тест'")

    # больше двух видео за раз — отбой
    r = apost(c, "/admin/dates/new", {"name": "Много видео", "categories": str(vcid)},
              files=[("videos", (f"v{i}.mp4", MP4, "video/mp4")) for i in range(3)])
    assert r.status_code == 303 and "%E2%9A%A0" in r.headers["location"]

    # удаление видео админом чистит и файл
    r = apost(c, f"/admin/dates/{did_vid}/videos/{vrow and db_one('SELECT id FROM date_videos WHERE date_id=?', (did_vid,))['id']}/delete",
              {})
    assert r.status_code == 303
    assert not db_one("SELECT 1 FROM date_videos WHERE date_id=?", (did_vid,))
    assert not (main.images.UPLOAD_DIR / vfn).exists()

    # гость прикрепляет видео к своему предложению
    main._rates.clear()
    gv = guest_client(700106, vtok, "Гостья")
    r = gv.post(f"/c/{vtok}/propose",
                data={"name": "Гостевое видео"},
                files=[("video", ("g.mp4", MP4, "video/mp4"))])
    assert r.status_code == 200 and r.json()["ok"]
    gpid = r.json()["id"]
    assert db_one("SELECT 1 FROM date_videos WHERE date_id=?", (gpid,))

    # порядок событий в категории: перетащили — гость видит новый порядок
    main._rates.clear()
    ids = [r[0] for r in db_all(
        "SELECT date_id FROM date_categories WHERE category_id=?", (vcid,))]
    assert len(ids) >= 3
    # на первое место ставим заведомо видимое (не-черновик, не гостевое предложение)
    # событие — иначе гость его не увидит и проверка порядка будет ложной
    visible = [i for i in ids if not db_one(
        "SELECT is_draft FROM dates WHERE id=?", (i,))["is_draft"]]
    head = visible[-1]
    reordered = [head] + [i for i in ids if i != head]
    r = apost(c, f"/admin/categories/{vcid}/dates_reorder",
              {"order": ",".join(map(str, reordered))})
    assert r.status_code == 200 and r.json()["ok"]
    gpage = c.get(f"/c/{vtok}").text
    first_id = int(re.search(r'<article[^>]*id="date-(\d+)"', gpage).group(1))
    assert first_id == head
    # неполный набор id — отбой
    r = apost(c, f"/admin/categories/{vcid}/dates_reorder",
              {"order": str(reordered[0])})
    assert r.status_code in (400, 303)          # 400 напрямую или friendly-flash
    step("v7: описание+разметка, 50/50, счётчик, место-ссылка, видео с Range, реордер")

    # подчищаем витрину, чтобы не мешать счётчикам пагинации
    apost(c, f"/admin/categories/{vcid}/delete", {})
    for nm in ("Разметка", "Делим счёт", "Событие с местом", "С видео",
               "Битое видео", "Много видео", "Гостевое видео"):
        row = db_one("SELECT id FROM dates WHERE name=?", (nm,))
        if row:
            apost(c, f"/admin/dates/{row['id']}/delete", {})
    main._rates.clear()

    # ---------- пагинация списка событий ----------
    main._rates.clear()
    # этот блок про пагинацию, а не квоту: временно снимаем лимит событий
    _qc = dbm.connect()
    _qc.execute("UPDATE users SET date_limit=999 WHERE telegram_id=555001")
    _qc.commit()
    _qc.close()
    for i in range(1, 32):
        apost(c, "/admin/dates/new", {"name": f"Лист {i:02d}"})
    # события без категории остаются активными; 30 новых карточек на странице.
    p1 = c.get("/admin/dates?view=active").text
    assert "Лист 31" in p1 and "Лист 01" not in p1
    assert "стр. 1 из 2" in p1 and "page=2" in p1
    p2 = c.get("/admin/dates?view=active&page=2").text
    assert "Лист 01" in p2
    step("пагинация: 30 на страницу, старые уезжают на следующую")

    # ---------- редизайн кабинета (date4you): форма, список, дашборд ----------
    main._rates.clear()
    apost(c, "/admin/categories/create", category_data("Поделись-кат"))
    # форма создания: редактируемое превью (click-to-edit), тулбар разметки,
    # виджет времени, модификаторы
    nf = c.get("/admin/dates/new").text
    assert 'class="ed-cols"' in nf and 'id="edCard"' in nf
    assert 'id="descToolbar"' in nf and 'data-wrap="**|**"' in nf
    assert 'id="edTitle"' in nf and 'id="edDesc"' in nf and 'id="edGallery"' in nf
    assert 'data-tr-day' in nf and 'data-bind="pay"' in nf
    # форма по-прежнему шлёт те же поля + CSRF (роут не сломан)
    assert 'name="name"' in nf and 'name="csrf"' in nf and 'name="categories"' in nf
    assert 'name="starts_at"' in nf and 'name="comment"' in nf and 'name="images"' in nf

    # список карточками по умолчанию: сетка, бейджи, меню ⋯, переключатель вида
    lp = c.get("/admin/dates").text
    assert 'class="grid"' in lp and 'class="dcard' in lp
    assert 'class="more"' in lp and 'id="viewtog"' in lp
    assert "без даты" not in lp                       # #13: пустую дату не подписываем
    assert 'class="dcard-link"' in lp                 # вся карточка кликабельна (#7)
    # карточки несут CSRF в формах действий (меню ⋯)
    assert lp.count('name="csrf"') >= 3
    # переключение вида через cookie → SSR рисует стеклянный список (.dlist)
    c.cookies.set("layout", "list")
    lt = c.get("/admin/dates").text
    assert 'class="dlist"' in lt and 'class="grid"' not in lt
    c.cookies.set("layout", "cards")
    assert 'class="grid"' in c.get("/admin/dates").text

    # дашборд: блок «Поделиться» с QR-кодом (инлайновый SVG) и ссылкой
    sh = c.get("/admin/").text
    assert "Поделиться" in sh and "<svg" in sh        # QR нарисован на сервере
    assert "/c/" in sh and "data-copy" in sh          # ссылка копируется по клику
    assert "Показать QR-код" in sh                     # QR можно раскрыть/скачать
    assert 'class="qr-svg"' in sh and 'class="qr-signature"' in sh
    assert "date4you" in c.get("/admin/dates").text   # ребренд в шапке
    # терминология: «гость/гостья» в админке заменены
    assert "Вопросы гостей" not in c.get("/admin/questions").text
    step("редизайн: сплит-форма с превью, список карточками + переключатель, QR на дашборде")

    # ---------- выбор зоны фокуса фото (v7) ----------
    pk = db_one("SELECT id, link_token FROM categories WHERE name='Поделись-кат'")
    pk_cid, pk_tok = pk["id"], pk["link_token"]
    r = apost(c, "/admin/dates/new", {"name": "С фокусом", "categories": str(pk_cid)},
              files=[("images", ("f.png", png((120, 90, 160)), "image/png"))])
    assert r.status_code == 303
    fdid = db_one("SELECT id FROM dates WHERE name='С фокусом'")["id"]
    fimg = db_one("SELECT id, focus FROM date_images WHERE date_id=?", (fdid,))
    assert fimg["focus"] is None                       # по умолчанию центр (NULL)
    # форма правки: сохранённое фото — слайд превью с data-pid и data-focus
    # (зону кадра двигают перетаскиванием прямо в превью, focus шлётся на сервер)
    ef = c.get(f"/admin/dates/{fdid}/edit").text
    assert 'data-focus=' in ef and 'data-pid=' in ef and 'id="edGallery"' in ef
    # сохраняем точку фокуса
    r = apost(c, f"/admin/dates/{fdid}/images/{fimg['id']}/focus", {"focus": "20% 80%"})
    assert r.status_code == 200 and r.json()["focus"] == "20% 80%"
    assert db_one("SELECT focus FROM date_images WHERE id=?", (fimg["id"],))["focus"] == "20% 80%"
    # кривой формат — отбой
    r = apost(c, f"/admin/dates/{fdid}/images/{fimg['id']}/focus", {"focus": "lol"})
    assert r.status_code in (400, 303)
    # гостевая применяет object-position к фото
    gp = c.get(f"/c/{pk_tok}").text
    assert "object-position:20% 80%" in gp
    step("v7: зона фокуса фото сохраняется и применяется в карточке у гостьи")

    # ---------- зона кадра выбирается СРАЗУ при загрузке (image_focuses) ----------
    # Раньше зону можно было задать только после сохранения и повторного входа.
    # Теперь форма шлёт параллельный массив зон — применяем при вставке фото.
    r = apost(c, "/admin/dates/new",
              {"name": "Зона сразу", "categories": str(pk_cid),
               "image_focuses": "10% 20%,90% 30%"},
              files=[("images", ("a.png", png((10, 20, 30)), "image/png")),
                     ("images", ("b.png", png((40, 50, 60)), "image/png"))])
    assert r.status_code == 303
    zdid = db_one("SELECT id FROM dates WHERE name='Зона сразу'")["id"]
    zimgs = db_all("SELECT focus FROM date_images WHERE date_id=? ORDER BY position, id",
                   (zdid,))
    assert [x["focus"] for x in zimgs] == ["10% 20%", "90% 30%"], zimgs
    # кривая зона в массиве → этой фотке центр (NULL), без 400
    r = apost(c, "/admin/dates/new",
              {"name": "Зона кривая", "categories": str(pk_cid),
               "image_focuses": "lol,50% 50%"},
              files=[("images", ("a.png", png((11, 22, 33)), "image/png")),
                     ("images", ("b.png", png((44, 55, 66)), "image/png"))])
    assert r.status_code == 303
    kdid = db_one("SELECT id FROM dates WHERE name='Зона кривая'")["id"]
    kimgs = db_all("SELECT focus FROM date_images WHERE date_id=? ORDER BY position, id",
                   (kdid,))
    assert [x["focus"] for x in kimgs] == [None, "50% 50%"], kimgs
    # форма правки несёт скрытое поле и подсказку про выбор зоны на новой плитке
    nf = c.get("/admin/dates/new").text
    assert 'id="imageFocuses"' in nf and 'name="image_focuses"' in nf
    step("зона кадра задаётся сразу при загрузке фото (image_focuses), кривая → центр")

    # ---------- выход только по POST ----------
    assert c.get("/admin/logout").status_code == 405
    r = apost(c, "/admin/logout", {})
    assert r.status_code == 303 and "/login" in r.headers["location"]
    assert c.get("/admin/").status_code == 303
    step("logout — POST с CSRF; GET отклоняется (405)")

    # после logout снова логинимся, чтобы дальнейшие блоки шли под сессией
    tg_login(c, 555001, username="boss")
    refresh_csrf(c)

# ---------- миграции v1 → v4 (вне приложения) ----------
mig = Path("/tmp/mig.db")
mig.unlink(missing_ok=True)
old_db_path = dbm.DB_PATH
dbm.DB_PATH = mig
conn = sqlite3.connect(mig)
conn.executescript("""
CREATE TABLE categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    link_token TEXT UNIQUE,
    link_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE dates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    place TEXT, starts_at TEXT, ends_at TEXT, comment TEXT,
    origin TEXT NOT NULL DEFAULT 'admin',
    guest_token TEXT,
    is_chosen INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE votes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_id INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    guest_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (date_id, category_id, guest_token)
);
CREATE TABLE questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date_id INTEGER NOT NULL,
    category_id INTEGER,
    guest_token TEXT,
    text TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
INSERT INTO categories(name, link_token, created_at) VALUES('Старая', 'tok123', '2025-01-01T00:00');
INSERT INTO dates(name, created_at) VALUES('Событие А', '2025-01-01T00:00');
INSERT INTO dates(name, created_at) VALUES('Событие Б', '2025-01-02T00:00');
-- гость gA голосовал дважды (за А, потом за Б) — в бронь должен попасть свежий голос
INSERT INTO votes(date_id, category_id, guest_token, created_at) VALUES(1, 1, 'gA', '2025-01-01T10:00');
INSERT INTO votes(date_id, category_id, guest_token, created_at) VALUES(2, 1, 'gA', '2025-01-03T10:00');
INSERT INTO votes(date_id, category_id, guest_token, created_at) VALUES(1, 1, 'gB', '2025-01-02T10:00');
-- gC голосует за то же событие в ту же секунду, что gB: при дедупе v5
-- должен победить больший id (детерминированный ROW_NUMBER)
INSERT INTO votes(date_id, category_id, guest_token, created_at) VALUES(1, 1, 'gC', '2025-01-02T10:00');
INSERT INTO questions(date_id, text, created_at) VALUES(1, 'старый вопрос', '2025-01-01T00:00');
""")
conn.commit()
conn.close()

dbm.init_db()
conn = sqlite3.connect(mig)
conn.row_factory = sqlite3.Row
assert conn.execute("PRAGMA user_version").fetchone()[0] == dbm.LATEST_VERSION
qcols = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
assert {"answer", "answered_at", "suggest_starts", "suggest_ends"} <= qcols
assert "user_id" in qcols                 # v13: автор вопроса (уведомление при ответе)
dcols = {r[1] for r in conn.execute("PRAGMA table_info(dates)")}
assert {"is_draft", "pay_split", "place_url"} <= dcols
assert "capacity" in dcols and conn.execute(
    "SELECT capacity FROM dates WHERE id=1").fetchone()[0] == 1  # v22
assert "is_chosen" not in dcols          # v8: мёртвая колонка дропнута
assert "proposed_by" in dcols             # v13: автор предложения
ccols = {r[1] for r in conn.execute("PRAGMA table_info(categories)")}
assert "description" in ccols
assert "owner_id" in ccols                # v9: владелец категории
assert {"og_title", "og_desc", "og_image", "og_focus"} <= ccols   # v14/v15/v21: превью ссылки
assert {"choice_mode", "voting_deadline", "voting_status", "closed_at",
        "resolved_at", "winner_date_id"} <= ccols                  # v22: голосование
legacy_voting = conn.execute(
    "SELECT choice_mode, voting_deadline, voting_status FROM categories "
    "WHERE name='Старая'").fetchone()
assert legacy_voting["choice_mode"] == "multiple"
assert legacy_voting["voting_deadline"]
assert legacy_voting["voting_status"] == "unconfigured"
assert "owner_id" in dcols                # v9: владелец события
# v13: мягкая очередь модерации + per-user поля + таблица настроек
assert "is_reviewed" in ccols and "is_reviewed" in {r[1] for r in conn.execute("PRAGMA table_info(users)")}
assert "user_id" in {r[1] for r in conn.execute("PRAGMA table_info(bookings)")}
assert "participation_withdrawn_at" in {
    r[1] for r in conn.execute("PRAGMA table_info(bookings)")}
assert "cursor_effects" in {r[1] for r in conn.execute("PRAGMA table_info(users)")}
assert {"purpose", "user_id", "error"} <= {
    r[1] for r in conn.execute("PRAGMA table_info(login_codes)")}
assert conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
# дефолт is_reviewed=1: старые пользователи/категории не «ждут проверки»
assert conn.execute("SELECT is_reviewed FROM categories WHERE name='Старая'").fetchone()[0] == 1
# v9: служебный легаси-владелец и бэкофилл существующих данных на него
for t in ("users", "login_codes"):
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone(), f"после миграции нет таблицы {t}"
legacy = conn.execute("SELECT id, is_operator FROM users WHERE telegram_id=0").fetchone()
assert legacy and legacy["is_operator"] == 1, "нет служебного легаси-владельца"
# старая категория «Старая» и оба события должны принадлежать легаси-владельцу
assert conn.execute(
    "SELECT COUNT(*) FROM categories WHERE owner_id IS NULL").fetchone()[0] == 0
assert conn.execute(
    "SELECT COUNT(*) FROM dates WHERE owner_id IS NULL").fetchone()[0] == 0
assert conn.execute(
    "SELECT owner_id FROM categories WHERE name='Старая'").fetchone()[0] == legacy["id"]
# v10: owner_id ужесточён до NOT NULL (таблицы пересобраны, FK-целостность цела)
for t in ("categories", "dates"):
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
    assert "owner_id" in ddl and "NOT NULL" in ddl.split("owner_id", 1)[1][:40], ddl
assert not conn.execute("PRAGMA foreign_key_check").fetchall(), "rebuild порвал FK"
dccols = {r[1] for r in conn.execute("PRAGMA table_info(date_categories)")}
assert "position" in dccols
dicols = {r[1] for r in conn.execute("PRAGMA table_info(date_images)")}
assert "focus" in dicols          # v7: точка фокуса фото для карточки
assert not conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='votes'").fetchone()
for t in ("guests", "bookings", "date_links", "date_images", "date_categories",
          "date_videos"):
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone(), f"после миграции нет таблицы {t}"
for ix in ("idx_book_cat", "idx_dc_cat", "idx_q_read", "idx_book_vote",
           "idx_book_guest", "idx_dv_date"):
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (ix,)
    ).fetchone(), f"после миграции нет индекса {ix}"
assert not conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_book_date'"
).fetchone(), "v22 должен снять глобальный UNIQUE(date_id)"
# v6 снял UNIQUE(категория, гость): таблица bookings пересобрана без него
bk_sql = conn.execute(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='bookings'"
).fetchone()[0]
assert "UNIQUE" not in bk_sql.upper(), bk_sql
books = {r["guest_token"]: r["date_id"]
         for r in conn.execute("SELECT guest_token, date_id FROM bookings")}
# дедуп v5: на событие 1 претендовали gB и gC (одинаковое время) — остаётся gC
assert books == {"gA": 2, "gC": 1}, books
assert conn.execute("SELECT text FROM questions").fetchone()[0] == "старый вопрос"
conn.close()
dbm.DB_PATH = old_db_path
step("миграции: v1 → v%d — брони, дедуп, мультивыбор, видео, все объекты" % dbm.LATEST_VERSION)

# ---------- бэкап ----------
p = bk.make_backup()
assert p.exists()
conn = sqlite3.connect(p)
assert conn.execute("SELECT COUNT(*) FROM dates").fetchone()[0] >= 1
conn.close()
assert list((DATA / "backups").glob("app-*.db"))
r = subprocess.run([sys.executable, "backup.py"], cwd=ROOT,
                   env={**os.environ}, capture_output=True,
                   text=True, encoding="utf-8")
assert r.returncode == 0 and "Бэкап готов" in r.stdout
step("консистентные снимки базы: модуль, авто-снимок при старте и CLI работают")

css = (ROOT / "static" / "public.css").read_text()
for mm in re.finditer(r'url\("(/static/[^"]+)"\)', css):
    p = ROOT / mm.group(1).lstrip("/")
    assert p.exists(), f"в public.css указан несуществующий файл: {mm.group(1)}"
step("все url() из public.css существуют на диске (регресс 404 шрифтов)")

# ---------- PWA-манифест ----------
with TestClient(main.app, follow_redirects=False) as cpwa:
    r = cpwa.get("/static/manifest.json")
    assert r.status_code == 200, r.status_code
    man = json.loads(r.content)
    assert man["name"] == "date4you — место для ваших встреч"
    assert man["start_url"] == "/" and man["display"] == "standalone"
    for ic in man["icons"]:
        p = ROOT / ic["src"].lstrip("/")
        assert p.exists(), f"в манифесте указана несуществующая иконка: {ic['src']}"
    # манифест и theme-color подключены на странице входа (домен ведёт на /login)
    home = cpwa.get("/login").text
    assert 'rel="manifest"' in home and 'name="theme-color"' in home
step("PWA: манифест отдаётся, иконки на диске, подключён на странице входа")

# ---------- изоляция: owner-гейт хелперов get_owned_* ----------
# Главный инвариант продукта: пользователь видит только свои данные. Здесь
# проверяем сам гейт; HTTP-уровень (все ручки кабинета) — после перевода
# ручек на скоупинг и реального входа.
import ownership  # noqa: E402

iso = dbm.connect()
iso.execute("INSERT INTO users(telegram_id, display_name, created_at) "
            "VALUES(1001, 'Алиса', ?)", (main.now_iso(),))
uA = iso.execute("SELECT id FROM users WHERE telegram_id=1001").fetchone()[0]
iso.execute("INSERT INTO users(telegram_id, display_name, created_at) "
            "VALUES(1002, 'Боб', ?)", (main.now_iso(),))
uB = iso.execute("SELECT id FROM users WHERE telegram_id=1002").fetchone()[0]
iso.execute("INSERT INTO categories(owner_id, name, link_token, created_at) "
            "VALUES(?, 'Категория Алисы', 'iso-tokA', ?)", (uA, main.now_iso()))
catA = iso.execute("SELECT id FROM categories WHERE link_token='iso-tokA'").fetchone()[0]
iso.execute("INSERT INTO dates(owner_id, name, created_at) VALUES(?, 'Событие Алисы', ?)",
            (uA, main.now_iso()))
dateA = iso.execute("SELECT id FROM dates WHERE name='Событие Алисы'").fetchone()[0]
iso.commit()

# владелец достаёт своё
assert ownership.get_owned_category(iso, catA, uA)["id"] == catA
assert ownership.get_owned_date(iso, dateA, uA)["id"] == dateA
# чужой получает 404 (а не 403 — не палим существование)
from fastapi import HTTPException as _HX  # noqa: E402
for fn, oid in ((ownership.get_owned_category, catA), (ownership.get_owned_date, dateA)):
    try:
        fn(iso, oid, uB)
        assert False, "чужой объект не должен быть доступен"
    except _HX as e:
        assert e.status_code == 404, e.status_code
# несуществующий id — тоже 404
try:
    ownership.get_owned_category(iso, 999999, uA)
    assert False
except _HX as e:
    assert e.status_code == 404
# owned_date_ids возвращает только свои
assert ownership.owned_date_ids(iso, uA) == {dateA}
assert ownership.owned_date_ids(iso, uB) == set()
iso.close()
step("изоляция: owner-гейт пускает к своему, чужое и несуществующее → 404")

# ---------- изоляция на HTTP-уровне: две реальные сессии ----------
# Самый важный тест мультитенантности: Алиса и Боб входят через бота, каждый
# заводит свои данные, и НИ ОДНА ручка кабинета не пускает одного к данным
# другого (всегда 404 — не палим существование объекта).

def csrf_of(client) -> str:
    page = client.get("/admin/categories")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def post_as(client, url, data=None, files=None):
    d = dict(data or {})
    d["csrf"] = csrf_of(client)
    return client.post(url, data=d, files=files)


with TestClient(main.app, follow_redirects=False) as ca, \
        TestClient(main.app, follow_redirects=False) as cb:
    assert tg_login(ca, 770001, username="alice").json()["status"] == "ok"
    assert tg_login(cb, 770002, username="bob").json()["status"] == "ok"

    # Алиса заводит категорию и событие (с фото), привязывает
    assert post_as(ca, "/admin/categories/create", category_data("Алисина")).status_code == 303
    catA = db_one("SELECT c.id FROM categories c JOIN users u ON u.id=c.owner_id "
                  "WHERE u.telegram_id=770001 AND c.name='Алисина'")[0]
    r = post_as(ca, "/admin/dates/new",
                {"name": "Событие Алисы", "categories": catA},
                files={"images": ("a.png", png(), "image/png")})
    assert r.status_code == 303
    dateA = db_one("SELECT d.id FROM dates d JOIN users u ON u.id=d.owner_id "
                   "WHERE u.telegram_id=770001 AND d.name='Событие Алисы'")[0]
    imgA = db_one("SELECT filename FROM date_images WHERE date_id=?", (dateA,))[0]

    # Боб не видит Алисиных данных в своих списках
    assert "Алисина" not in cb.get("/admin/categories").text
    assert "Событие Алисы" not in cb.get("/admin/dates").text

    # GET чужих объектов → 404
    assert cb.get(f"/admin/categories/{catA}").status_code == 404
    assert cb.get(f"/admin/dates/{dateA}/edit").status_code == 404
    # чужой файл по прямой ссылке → 404 (а сам владелец его видит)
    assert cb.get(f"/admin/uploads/{imgA}").status_code == 404
    assert ca.get(f"/admin/uploads/{imgA}").status_code == 200

    # POST-мутации над чужим отклоняются (404 → дружелюбный 303-flash,
    # мутация НЕ происходит), данные целы
    for url, data in [
        (f"/admin/categories/{catA}/rename", {"name": "взлом"}),
        (f"/admin/categories/{catA}/delete", {}),
        (f"/admin/categories/{catA}/regenerate", {}),
        (f"/admin/categories/{catA}/attach", {"date_id": dateA}),
        (f"/admin/dates/{dateA}/edit", {"name": "взлом"}),
        (f"/admin/dates/{dateA}/delete", {}),
        (f"/admin/dates/{dateA}/archive", {}),
        (f"/admin/dates/{dateA}/clone", {}),
    ]:
        rr = post_as(cb, url, data)
        assert rr.status_code in (303, 404), (url, rr.status_code)
        if rr.status_code == 303:                      # flash об ошибке, не успех
            assert "/login" not in rr.headers["location"]
    assert db_one("SELECT name FROM categories WHERE id=?", (catA,))[0] == "Алисина"
    assert db_one("SELECT name FROM dates WHERE id=?", (dateA,))[0] == "Событие Алисы"
    assert not db_one("SELECT 1 FROM dates WHERE name='Событие Алисы (копия)'")

    # Боб не может привязать чужое событие в СВОЮ категорию
    assert post_as(cb, "/admin/categories/create", category_data("Бобова")).status_code == 303
    catB = db_one("SELECT c.id FROM categories c JOIN users u ON u.id=c.owner_id "
                  "WHERE u.telegram_id=770002")[0]
    assert post_as(cb, f"/admin/categories/{catB}/attach",
                   {"date_id": dateA}).status_code in (303, 404)
    assert not db_one("SELECT 1 FROM date_categories WHERE date_id=? AND category_id=?",
                      (dateA, catB))

    # экспорт — только операторам: Боб (обычный пользователь) получает 404
    rexp = cb.get("/admin/export/json")
    assert rexp.status_code == 404
    assert "Событие Алисы" not in rexp.text
step("изоляция HTTP: Алиса и Боб не видят и не трогают данные друг друга по всем ручкам")

# ---------- операторская админка (поверхность 3) ----------
# Оператор (555001) видит всех; не-оператор не знает, что /operator существует.
with TestClient(main.app, follow_redirects=False) as cop, \
        TestClient(main.app, follow_redirects=False) as cnop:
    assert tg_login(cop, 555001, username="boss").json()["status"] == "ok"
    assert tg_login(cnop, 880001, username="rando").json()["status"] == "ok"

    # гейт: не-оператор → 404 (не 403 — не палим существование), аноним → /login
    assert cnop.get("/operator/").status_code == 404
    assert cnop.get("/operator/users").status_code == 404
    anon = TestClient(main.app, follow_redirects=False)
    assert anon.get("/operator/").status_code == 303  # → /login

    # дашборд оператора со счётчиками
    dash = cop.get("/operator/")
    assert dash.status_code == 200 and "Обзор платформы" in dash.text

    # список пользователей + поиск по username
    lst = cop.get("/operator/users")
    assert lst.status_code == 200 and "rando" in lst.text
    assert "rando" in cop.get("/operator/users?q=rando").text
    assert "rando" not in cop.get("/operator/users?q=zzz_нет").text

    uid_nop = db_one("SELECT id FROM users WHERE telegram_id=880001")[0]
    op_csrf = re.search(r'name="csrf" value="([^"]+)"',
                        cop.get(f"/operator/users/{uid_nop}").text).group(1)
    def opost(url, data=None):
        d = dict(data or {}); d["csrf"] = op_csrf
        return cop.post(url, data=d)

    # бан / разбан
    assert opost(f"/operator/users/{uid_nop}/ban").status_code == 303
    assert db_one("SELECT is_active FROM users WHERE id=?", (uid_nop,))[0] == 0
    # забаненный не входит
    cbanned = TestClient(main.app, follow_redirects=False)
    assert tg_login(cbanned, 880001).json()["status"] == "banned"
    assert opost(f"/operator/users/{uid_nop}/ban").status_code == 303
    assert db_one("SELECT is_active FROM users WHERE id=?", (uid_nop,))[0] == 1

    # квота
    assert opost(f"/operator/users/{uid_nop}/quota", {"date_limit": "99"}).status_code == 303
    assert db_one("SELECT date_limit FROM users WHERE id=?", (uid_nop,))[0] == 99

    # роль оператора туда-обратно
    assert opost(f"/operator/users/{uid_nop}/operator").status_code == 303
    assert db_one("SELECT is_operator FROM users WHERE id=?", (uid_nop,))[0] == 1
    assert opost(f"/operator/users/{uid_nop}/operator").status_code == 303
    assert db_one("SELECT is_operator FROM users WHERE id=?", (uid_nop,))[0] == 0

    # самозащита: оператор не банит/не разжалует/не удаляет себя
    uid_op = db_one("SELECT id FROM users WHERE telegram_id=555001")[0]
    for act in ("ban", "operator", "delete"):
        rr = opost(f"/operator/users/{uid_op}/{act}")
        assert rr.status_code == 303 and "/login" not in rr.headers["location"]
    assert db_one("SELECT is_active FROM users WHERE id=?", (uid_op,))[0] == 1
    assert db_one("SELECT is_operator FROM users WHERE id=?", (uid_op,))[0] == 1

    # Действия удаляемого участника в чужой категории должны пережить удаление
    # как обезличенные записи: результат не пересчитывается, имя и u<ID> исчезают.
    ident = dbm.connect()
    icat = ident.execute(
        "INSERT INTO categories(owner_id,name,link_token,created_at) VALUES(?,?,?,?)",
        (uid_op, "Категория для обезличивания", "identity-delete-cat", main.now_iso()),
    ).lastrowid
    idate = ident.execute(
        "INSERT INTO dates(owner_id,name,origin,guest_token,proposed_by,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (uid_op, "Предложение удаляемого", "guest", f"u{uid_nop}", uid_nop,
         main.now_iso()),
    ).lastrowid
    ident.execute(
        "INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,0)",
        (idate, icat),
    )
    ident.execute(
        "INSERT INTO guests(token,name,created_at) VALUES(?,?,?)",
        (f"u{uid_nop}", "Имя, которое надо удалить", main.now_iso()),
    )
    ibook = ident.execute(
        "INSERT INTO bookings(date_id,category_id,guest_token,user_id,created_at) "
        "VALUES(?,?,?,?,?)",
        (idate, icat, f"u{uid_nop}", uid_nop, main.now_iso()),
    ).lastrowid
    iquestion = ident.execute(
        "INSERT INTO questions(date_id,category_id,guest_token,user_id,text,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (idate, icat, f"u{uid_nop}", uid_nop, "Обезличить вопрос", main.now_iso()),
    ).lastrowid
    ireport = ident.execute(
        "INSERT INTO reports(target_type,target_id,reporter,reason,created_at) "
        "VALUES('date',?,?,?,?)",
        (idate, f"u{uid_nop}", "Обезличить жалобу", main.now_iso()),
    ).lastrowid
    ident.commit()
    ident.close()

    # удаление аккаунта и принадлежащих ему данных (каскад)
    assert opost(f"/operator/users/{uid_nop}/delete").status_code == 303
    assert not db_one("SELECT 1 FROM users WHERE id=?", (uid_nop,))
    ballot = db_one(
        "SELECT guest_token,user_id FROM bookings WHERE id=?", (ibook,),
    )
    assert ballot and ballot["user_id"] is None
    anon_token = ballot["guest_token"]
    assert anon_token.startswith("deleted-") and anon_token != f"u{uid_nop}"
    assert db_one("SELECT guest_token FROM questions WHERE id=?", (iquestion,))[0] == anon_token
    assert db_one("SELECT guest_token FROM dates WHERE id=?", (idate,))[0] == anon_token
    assert db_one("SELECT reporter FROM reports WHERE id=?", (ireport,))[0] == anon_token
    assert not db_one("SELECT 1 FROM guests WHERE token=?", (f"u{uid_nop}",))
    assert db_one("SELECT name FROM guests WHERE token=?", (anon_token,))[0] == \
        "Удалённый участник"
step("операторская админка: гейт 404 для не-оператора, баны/квоты/роли/удаление, самозащита")


# ---------- #4: импорт JSON (дозапись) + гейт оператора ----------
main._rates.clear()
with TestClient(main.app, follow_redirects=False) as cimp, \
        TestClient(main.app, follow_redirects=False) as cuser:
    assert tg_login(cimp, 555001, username="boss").json()["status"] == "ok"
    icsrf = re.search(r'name="csrf" value="([^"]+)"',
                      cimp.get("/admin/profile").text).group(1)
    payload = {
        "categories": [{"id": 7001, "name": "Импорт-Категория", "link_enabled": 1,
                        "choice_mode": "multiple", "voting_deadline":
                        category_data("x")["voting_deadline"]}],
        "dates": [{"name": "Импортированное событие", "place": "Кафе",
                   "comment": "из бэкапа", "categories": [7001],
                   "links": ["https://example.com"], "images": [], "videos": []}],
        "guests": [], "questions": [],
    }
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = cimp.post("/admin/import/json", data={"csrf": icsrf},
                  files=[("file", ("export.json", blob, "application/json"))])
    assert r.status_code == 303, r.status_code
    imp_owner = db_one("SELECT id FROM users WHERE telegram_id=555001")[0]
    new_cat = db_one("SELECT id FROM categories WHERE name='Импорт-Категория' AND owner_id=?",
                     (imp_owner,))
    assert new_cat, "импортированная категория не создана у оператора"
    imp_date = db_one("SELECT id FROM dates WHERE name='Импортированное событие' AND owner_id=?",
                      (imp_owner,))
    assert imp_date, "импортированное событие не создано"
    # событие привязано к импортированной категории (ремап старого id → новый)
    assert db_one("SELECT 1 FROM date_categories WHERE date_id=? AND category_id=?",
                  (imp_date["id"], new_cat["id"]))
    # ссылка перенесена
    assert db_one("SELECT 1 FROM date_links WHERE date_id=? AND url='https://example.com'",
                  (imp_date["id"],))
    # импорт — только операторам. У обычного пользователя GET-экспорт прямо 404;
    # POST-импорт админский обработчик ошибок превращает в 303-flash «⚠», но
    # данные при этом НЕ импортируются.
    assert tg_login(cuser, 991100, username="plainuser").json()["status"] == "ok"
    uid_plain = db_one("SELECT id FROM users WHERE telegram_id=991100")[0]
    assert cuser.get("/admin/export/json").status_code == 404
    ucsrf = re.search(r'name="csrf" value="([^"]+)"',
                      cuser.get("/admin/profile").text).group(1)
    r = cuser.post("/admin/import/json", data={"csrf": ucsrf},
                   files=[("file", ("export.json", blob, "application/json"))])
    assert r.status_code in (303, 404)
    assert not db_one("SELECT 1 FROM dates WHERE owner_id=? AND name='Импортированное событие'",
                      (uid_plain,)), "не-оператор не должен импортировать данные"
step("#4: импорт export.json дозаписью к оператору (ремап категорий, ссылки); гейт 404 не-оператору")


# ---------- #8: авто-роль оператора из env (без перелогина) ----------
main._rates.clear()
with TestClient(main.app, follow_redirects=False) as crole:
    # входим обычным пользователем, затем «добавляем» его id в OPERATOR_TG_IDS
    assert tg_login(crole, 991200, username="newop").json()["status"] == "ok"
    uid_role = db_one("SELECT id, is_operator FROM users WHERE telegram_id=991200")
    assert uid_role["is_operator"] == 0
    import users as _users
    import config as _cfg
    _saved_ops = set(_users.OPERATOR_TG_IDS)
    _users.OPERATOR_TG_IDS.add(991200)
    _cfg.OPERATOR_TG_IDS.add(991200)
    try:
        # следующий же запрос в кабинет выдаёт роль на лету (current_user)
        assert 'href="/operator/"' in crole.get("/admin/").text   # появилась вкладка «Админ»
        assert db_one("SELECT is_operator FROM users WHERE id=?",
                      (uid_role["id"],))[0] == 1
        # операторская поверхность наследует оформление кабинета
        operator_page = crole.get("/operator/")
        assert operator_page.status_code == 200
        assert 'data-skin="friends"' in operator_page.text
        _skin_db = dbm.connect()
        _skin_db.execute("UPDATE users SET admin_skin='romantic' WHERE id=?", (uid_role["id"],))
        _skin_db.commit(); _skin_db.close()
        assert 'data-skin="romantic"' in crole.get("/operator/").text
        # экспорт и импорт убраны с пользовательской главной и живут в настройках оператора
        assert "Данные платформы" not in crole.get("/admin/").text
        operator_settings = crole.get("/operator/settings").text
        assert "Данные платформы" in operator_settings
        assert "/admin/export/archive" in operator_settings
        assert "/admin/import/json?return_to=operator" in operator_settings
    finally:
        _users.OPERATOR_TG_IDS.clear(); _users.OPERATOR_TG_IDS.update(_saved_ops)
        _cfg.OPERATOR_TG_IDS.clear(); _cfg.OPERATOR_TG_IDS.update(_saved_ops)
step("#8: telegram_id из OPERATOR_TG_IDS получает роль оператора на лету, без перелогина")
main._rates.clear()


# ---------- жалобы: гость → очередь оператора → takedown ----------
with TestClient(main.app, follow_redirects=False) as cown, \
        TestClient(main.app, follow_redirects=False) as cop2, \
        TestClient(main.app, follow_redirects=False) as g:
    assert tg_login(cown, 990001, username="owner").json()["status"] == "ok"
    assert tg_login(cop2, 555001, username="boss").json()["status"] == "ok"

    own_csrf = re.search(r'name="csrf" value="([^"]+)"',
                         cown.get("/admin/categories").text).group(1)
    def ownpost(url, data=None, files=None):
        d = dict(data or {}); d["csrf"] = own_csrf
        return cown.post(url, data=d, files=files)

    # владелец заводит категорию (модерация по умолчанию вкл) и публикует событие
    assert ownpost("/admin/categories/create", category_data("Жалобная")).status_code == 303
    rc = db_one("SELECT id, link_token FROM categories WHERE name='Жалобная'")
    rcid, rtok = rc["id"], rc["link_token"]
    set_moderation(rcid, False)  # выкл, чтобы событие было видно (модерация — операторская)
    assert ownpost("/admin/dates/new",
                   {"name": "Подозрительное", "categories": str(rcid)}).status_code == 303
    rdid = db_one("SELECT id FROM dates WHERE name='Подозрительное'")[0]

    # гость заходит и жалуется на событие (жалоба требует входа)
    main._rates.clear()
    assert tg_login(g, 990011, username="reporter").json()["status"] == "ok"
    assert "пожаловаться" in g.get(f"/c/{rtok}").text
    r = g.post(f"/c/{rtok}/report",
               data={"target_type": "date", "target_id": rdid, "reason": "спам"})
    assert r.status_code == 200 and r.json()["ok"]
    rep = db_one("SELECT id, status FROM reports WHERE target_type='date' AND target_id=?",
                 (rdid,))
    assert rep and rep["status"] == "open"
    rep_id = rep["id"]

    # дубль той же жалобы не плодит записей
    r = g.post(f"/c/{rtok}/report", data={"target_type": "date", "target_id": rdid})
    assert r.status_code == 200
    assert db_one("SELECT COUNT(*) FROM reports WHERE target_id=? AND status='open'",
                  (rdid,))[0] == 1

    # жалоба на чужой/скрытый id → 404, запись не создаётся
    assert g.post(f"/c/{rtok}/report",
                  data={"target_type": "date", "target_id": 999999}).status_code == 404

    # оператор видит жалобу в очереди и счётчик на дашборде
    op2_csrf = re.search(r'name="csrf" value="([^"]+)"',
                         cop2.get("/operator/reports").text).group(1)
    assert "Подозрительное" in cop2.get("/operator/reports").text
    assert "открытых жалоб" in cop2.get("/operator/").text

    # takedown: контент удалён, жалоба закрыта, файлы (если были) тоже
    r = cop2.post(f"/operator/reports/{rep_id}/takedown", data={"csrf": op2_csrf})
    assert r.status_code == 303
    assert not db_one("SELECT 1 FROM dates WHERE id=?", (rdid,))
    assert db_one("SELECT status FROM reports WHERE id=?", (rep_id,))[0] == "resolved"

    # жалоба на категорию + dismiss (контент остаётся)
    main._rates.clear()
    r = g.post(f"/c/{rtok}/report",
               data={"target_type": "category", "target_id": rcid, "reason": "не то"})
    assert r.status_code == 200
    crep = db_one("SELECT id FROM reports WHERE target_type='category' AND target_id=?",
                  (rcid,))[0]
    r = cop2.post(f"/operator/reports/{crep}/resolve",
                  data={"csrf": op2_csrf, "action": "dismiss"})
    assert r.status_code == 303
    assert db_one("SELECT status FROM reports WHERE id=?", (crep,))[0] == "dismissed"
    assert db_one("SELECT 1 FROM categories WHERE id=?", (rcid,))  # категория цела
step("жалобы: гость жалуется (дубль-защита, 404 на скрытый id), оператор видит очередь, takedown/dismiss")


# ---------- оператор: обзор категорий и событий ----------
with TestClient(main.app, follow_redirects=False) as cown, \
        TestClient(main.app, follow_redirects=False) as cop3:
    assert tg_login(cown, 990002, username="creator").json()["status"] == "ok"
    assert tg_login(cop3, 555001, username="boss").json()["status"] == "ok"

    own_csrf = re.search(r'name="csrf" value="([^"]+)"',
                         cown.get("/admin/categories").text).group(1)
    def ownp(url, data=None):
        d = dict(data or {}); d["csrf"] = own_csrf
        return cown.post(url, data=d)

    assert ownp("/admin/categories/create", category_data("Обзорная")).status_code == 303
    ovc = db_one("SELECT id, link_token FROM categories WHERE name='Обзорная'")
    ovcid = ovc["id"]
    ownp("/admin/dates/new", {"name": "Видимое", "categories": str(ovcid)})
    ovdid = db_one("SELECT id FROM dates WHERE name='Видимое'")[0]

    op3_csrf = re.search(r'name="csrf" value="([^"]+)"',
                         cop3.get("/operator/categories").text).group(1)
    def op3p(url, data=None):
        d = dict(data or {}); d["csrf"] = op3_csrf
        return cop3.post(url, data=d)

    # категории: видны в списке с владельцем; поиск по владельцу
    page = cop3.get("/operator/categories").text
    assert "Обзорная" in page and "creator" in page or "Обзорная" in page
    assert "Обзорная" in cop3.get("/operator/categories?q=creator").text

    # выключить/включить ссылку категории
    assert op3p(f"/operator/categories/{ovcid}/toggle").status_code == 303
    assert db_one("SELECT link_enabled FROM categories WHERE id=?", (ovcid,))[0] == 0
    op3p(f"/operator/categories/{ovcid}/toggle")
    assert db_one("SELECT link_enabled FROM categories WHERE id=?", (ovcid,))[0] == 1

    # события: видны в списке + фильтры
    assert "Видимое" in cop3.get("/operator/dates").text
    assert "Видимое" in cop3.get("/operator/dates?q=creator").text
    # архив туда-обратно
    assert op3p(f"/operator/dates/{ovdid}/archive").status_code == 303
    assert db_one("SELECT archived_at FROM dates WHERE id=?", (ovdid,))[0] is not None
    assert "Видимое" in cop3.get("/operator/dates?flt=archived").text
    op3p(f"/operator/dates/{ovdid}/archive")
    assert db_one("SELECT archived_at FROM dates WHERE id=?", (ovdid,))[0] is None

    # удаление события оператором
    assert op3p(f"/operator/dates/{ovdid}/delete").status_code == 303
    assert not db_one("SELECT 1 FROM dates WHERE id=?", (ovdid,))

    # удаление категории НЕ трогает прочие события владельца
    ownp("/admin/dates/new", {"name": "Уцелевшее"})
    surv = db_one("SELECT id FROM dates WHERE name='Уцелевшее'")[0]
    assert op3p(f"/operator/categories/{ovcid}/delete").status_code == 303
    assert not db_one("SELECT 1 FROM categories WHERE id=?", (ovcid,))
    assert db_one("SELECT 1 FROM dates WHERE id=?", (surv,))  # событие цело

    # 404 на несуществующие объекты
    assert op3p("/operator/categories/999999/toggle").status_code in (303, 404)
    assert op3p("/operator/dates/999999/delete").status_code in (303, 404)
step("оператор: обзор категорий/событий, toggle ссылки, архив, удаление; каскад категории щадит события")


# ---------- оператор: обзор броней ----------
with TestClient(main.app, follow_redirects=False) as cown, \
        TestClient(main.app, follow_redirects=False) as cop4, \
        TestClient(main.app, follow_redirects=False) as gb:
    assert tg_login(cown, 990003, username="host").json()["status"] == "ok"
    assert tg_login(cop4, 555001, username="boss").json()["status"] == "ok"

    own_csrf = re.search(r'name="csrf" value="([^"]+)"',
                         cown.get("/admin/categories").text).group(1)
    def ownb(url, data=None):
        d = dict(data or {}); d["csrf"] = own_csrf
        return cown.post(url, data=d)
    assert ownb("/admin/categories/create", category_data("Бронируемая")).status_code == 303
    bc = db_one("SELECT id, link_token FROM categories WHERE name='Бронируемая'")
    bcid, btok = bc["id"], bc["link_token"]
    set_moderation(bcid, False)                                # выкл модерацию (операторская)
    ownb("/admin/dates/new", {"name": "Прогулка", "categories": str(bcid)})
    bdid = db_one("SELECT id FROM dates WHERE name='Прогулка'")[0]
    configure_voting(bcid, "multiple")

    # гость представляется и бронирует
    main._rates.clear()
    assert tg_login(gb, 990033, username="lena").json()["status"] == "ok"
    set_name(gb, btok, "Лена")
    assert gb.post(f"/c/{btok}/book", data={"date_id": bdid}).json()["booked"] is True
    bid = db_one("SELECT id FROM bookings WHERE date_id=?", (bdid,))[0]

    # оператор видит бронь в обзоре + поиск по имени гостя
    page = cop4.get("/operator/bookings").text
    assert "Лена" in page and "Прогулка" in page
    assert "Прогулка" in cop4.get("/operator/bookings?q=Лена").text
    assert "Прогулка" not in cop4.get("/operator/bookings?q=нетакого").text

    # оператор снимает бронь для разбора спора
    op4_csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = cop4.post(f"/operator/bookings/{bid}/delete", data={"csrf": op4_csrf})
    assert r.status_code == 303
    assert not db_one("SELECT 1 FROM bookings WHERE id=?", (bid,))
    assert cop4.post("/operator/bookings/999999/delete",
                     data={"csrf": op4_csrf}).status_code in (303, 404)
step("оператор: обзор броней (поиск по гостю), снятие брони для разбора спора")


# ---------- квота событий (1.4) и per-user анти-всплеск (2.4) ----------
with TestClient(main.app, follow_redirects=False) as cq:
    assert tg_login(cq, 990010, username="quotaman").json()["status"] == "ok"
    uid_q = db_one("SELECT id FROM users WHERE telegram_id=990010")[0]
    cq_csrf = re.search(r'name="csrf" value="([^"]+)"',
                        cq.get("/admin/categories").text).group(1)
    def cqp(url, data=None):
        d = dict(data or {}); d["csrf"] = cq_csrf
        return cq.post(url, data=d)

    # ставим лимит 3, создаём 3 — ок, 4-е отбивается с текстом про лимит
    _q = dbm.connect()
    _q.execute("UPDATE users SET date_limit=3 WHERE id=?", (uid_q,)); _q.commit(); _q.close()
    main._rates.clear()
    for i in range(3):
        assert cqp("/admin/dates/new", {"name": f"Квота {i}"}).status_code == 303
    r = cqp("/admin/dates/new", {"name": "Лишнее"})
    # friendly-flash превращает 400 в 303 с сообщением про лимит в ?msg=
    assert r.status_code == 303
    loc = unquote(r.headers.get("location", ""))
    assert "лимит" in loc.lower()
    assert db_one("SELECT COUNT(*) FROM dates WHERE owner_id=? AND name='Лишнее'",
                  (uid_q,))[0] == 0

    # архивные не считаются в квоту: архивируем одно — снова можно создать
    did_q = db_one("SELECT id FROM dates WHERE owner_id=? AND name='Квота 0'", (uid_q,))[0]
    cqp(f"/admin/dates/{did_q}/archive", {})
    assert cqp("/admin/dates/new", {"name": "После архива"}).status_code == 303

    # per-user анти-всплеск: лимит 40/час — поднимаем квоту, бьём цикл выше лимита
    _q = dbm.connect()
    _q.execute("UPDATE users SET date_limit=999 WHERE id=?", (uid_q,)); _q.commit(); _q.close()
    main._rates.clear()
    locs = [unquote(cqp("/admin/dates/new", {"name": f"Всплеск {i}"})
                    .headers.get("location", "")) for i in range(42)]
    # 429 для POST /admin превращается в 303-flash с ⚠ — ищем его в ?msg=
    assert any("передохни" in l.lower() for l in locs), \
        "per-user лимит datecreate должен сработать на всплеске"
    made = db_one("SELECT COUNT(*) FROM dates WHERE owner_id=? AND name LIKE 'Всплеск %'",
                  (uid_q,))[0]
    assert made == 40, f"ожидалось 40 созданных до лимита, получили {made}"
step("квота: лимит событий отбивает лишнее, архивные не в счёт; per-user анти-всплеск даёт 429")


# ---------- два типа входа: deeplink (bot_linked) vs виджет (без бота) ----------
import hashlib as _hl
import hmac as _hm
import notify as _nf

def _widget_params(tg_id, token, username="widgetuser", first_name="Виджет"):
    """Собирает подписанные поля Telegram Login Widget (как редиректит Telegram)."""
    data = {"id": str(tg_id), "first_name": first_name, "username": username,
            "auth_date": str(int(time.time()))}
    pairs = "\n".join(sorted(f"{k}={v}" for k, v in data.items()))
    secret = _hl.sha256(token.encode()).digest()
    data["hash"] = _hm.new(secret, pairs.encode(), _hl.sha256).hexdigest()
    return data

def _widget_state(client):
    page = client.get("/login")
    match = re.search(r"widget_state=([A-Za-z0-9_-]+)", page.text)
    assert match, "страница входа должна выпустить session-bound Widget state"
    return match.group(1)

with TestClient(main.app, follow_redirects=False) as cw:
    _saved_token = _nf.TOKEN
    _saved_send = _nf.send_to
    _saved_http_post = _nf.httpx.post
    _nf.TOKEN = "123456:test-bot-token"        # включаем проверку подписи
    _nf.send_to = lambda *a, **k: True         # успешная доставка без реальной сети
    class _WebhookOK:
        status_code = 200
        text = '{"ok":true}'
    _nf.httpx.post = lambda *a, **k: _WebhookOK()  # lifespan вложенных клиентов
    try:
        # Два параллельно открытых окна получают независимые одноразовые state.
        good_state = _widget_state(cw)
        stale_state = _widget_state(cw)
        assert good_state != stale_state

        # подделанная подпись → 403, аккаунт не создаётся
        bad = _widget_params(990200, "wrong-token")
        assert cw.get("/auth/widget", params=bad).status_code == 403
        assert not db_one("SELECT 1 FROM users WHERE telegram_id=990200")

        # валидная подпись логинит без отдельной галочки; локальный return_to не
        # входит в HMAC Telegram, но проходит серверный белый список
        good = _widget_params(990200, _nf.TOKEN)
        good["widget_state"] = good_state
        good["return_to"] = "/c/widget-return"
        r = cw.get("/auth/widget", params=good)
        assert r.status_code == 303 and r.headers["location"] == "/c/widget-return"
        assert cw.get("/auth/widget", params=good).status_code == 403, \
            "Widget callback должен быть одноразовым"
        row = db_one("SELECT id, bot_linked FROM users WHERE telegram_id=990200")
        assert row and row["bot_linked"] == 0

        # внешний return_to тоже не ломает HMAC, но отбрасывается → кабинет
        with TestClient(main.app, follow_redirects=False) as cw_external:
            external = _widget_params(990202, _nf.TOKEN)
            external["widget_state"] = _widget_state(cw_external)
            external["return_to"] = "https://evil.example/steal"
            ext = cw_external.get("/auth/widget", params=external)
            assert ext.status_code == 303 and ext.headers["location"] == "/admin/"

        # в кабинете виден баннер «подключить бота»
        assert "Подключить уведомления" in cw.get("/admin/").text

        # тот же человек запускает бота: код purpose-bound к его user_id, чужая
        # сессия его не забирает, poll не переключает аккаунт
        main._rates.clear()
        st = cw.post("/auth/start?return_to=/c/guest-return").json()["code"]
        link_code = db_one("SELECT purpose, user_id FROM login_codes WHERE code=?", (st,))
        assert link_code["purpose"] == "link" and link_code["user_id"] == row["id"]
        tg_open_login(cw, st, 990200, username="widgetuser")
        with TestClient(main.app, follow_redirects=False) as stranger:
            denied = stranger.get(f"/auth/poll?code={st}")
            assert denied.status_code == 403 and denied.json()["status"] == "forbidden"
        assert cw.get(f"/auth/poll?code={st}").json()["status"] == "pending"
        tg_confirm_login(cw, st, 990200, username="widgetuser")
        linked = cw.get(f"/auth/poll?code={st}").json()
        assert linked["status"] == "ok" and linked["linked"] is True
        assert linked["redirect"] == "/c/guest-return"
        assert db_one("SELECT bot_linked FROM users WHERE telegram_id=990200")[0] == 1
        # баннер исчез
        assert "Подключить уведомления" not in cw.get("/admin/").text

        # Telegram другого аккаунта не присоединяется и аккаунты не сливаются
        with TestClient(main.app, follow_redirects=False) as cconf:
            assert tg_login(cconf, 990203, username="other-widget").json()["status"] == "ok"
            other_id = db_one("SELECT id FROM users WHERE telegram_id=990203")["id"]
            conflict_code = cconf.post("/auth/start").json()["code"]
            tg_open_login(cconf, conflict_code, 990200, username="widgetuser")
            assert cconf.get(f"/auth/poll?code={conflict_code}").json()["status"] == "pending"
            tg_confirm_login(cconf, conflict_code, 990200, username="widgetuser")
            conflict = cconf.get(f"/auth/poll?code={conflict_code}").json()
            assert conflict["status"] == "conflict"
            assert db_one("SELECT telegram_id FROM users WHERE id=?", (other_id,))[0] == 990203

        # просроченная подпись (старый auth_date) → 403
        stale = _widget_params(990204, _nf.TOKEN)
        stale["auth_date"] = "1000000000"
        stale_pairs = "\n".join(sorted(f"{k}={v}" for k, v in stale.items() if k != "hash"))
        stale["hash"] = _hm.new(_hl.sha256(_nf.TOKEN.encode()).digest(),
                                stale_pairs.encode(), _hl.sha256).hexdigest()
        stale["widget_state"] = stale_state
        assert cw.get("/auth/widget", params=stale).status_code == 403
    finally:
        _nf.TOKEN = _saved_token
        _nf.send_to = _saved_send
        _nf.httpx.post = _saved_http_post
step("вход: пассивное согласие, безопасный return_to, purpose-bound привязка Telegram без слияния")


# ---------- 1.5: per-owner уведомления по bot_linked ----------
with TestClient(main.app, follow_redirects=False) as cown, \
        TestClient(main.app, follow_redirects=False) as gn:
    # владелец с подключённым ботом (deeplink-вход ставит bot_linked=1)
    assert tg_login(cown, 990300, username="notifyowner").json()["status"] == "ok"
    uid_n = db_one("SELECT id FROM users WHERE telegram_id=990300")[0]
    own_csrf = re.search(r'name="csrf" value="([^"]+)"',
                         cown.get("/admin/categories").text).group(1)
    def ownn(url, data=None):
        d = dict(data or {}); d["csrf"] = own_csrf
        return cown.post(url, data=d)
    ownn("/admin/categories/create", category_data("Уведомления"))
    nc = db_one("SELECT id, link_token FROM categories WHERE name='Уведомления'")
    ntok = nc["link_token"]
    ownn("/admin/dates/new", {"name": "Кафе", "categories": str(nc["id"])})
    ndid = db_one("SELECT id FROM dates WHERE name='Кафе'")[0]
    configure_voting(nc["id"], "multiple")

    prefs_page = cown.get("/admin/questions").text
    assert "Уведомления в Telegram" in prefs_page
    assert '<details class="notif-settings-card"' in prefs_page
    assert '<summary class="notif-settings-head">' in prefs_page
    assert all(f'name="{key}"' in prefs_page for key in
               ("votes", "questions", "proposals", "updates", "reminders", "reviews"))
    _badge_db = dbm.connect()
    _badge_db.executemany(
        "INSERT INTO questions(date_id,category_id,text,created_at) VALUES(?,?,?,?)",
        ((ndid, nc["id"], f"badge-test-{i}", main.now_iso()) for i in range(100)),
    )
    _badge_db.commit(); _badge_db.close()
    badge_page = cown.get("/admin/questions").text
    assert re.search(r'class="bell-count"[^>]*>99\+</span>', badge_page), \
        "счётчик в шапке должен оставаться компактным при трёхзначном числе"
    _badge_db = dbm.connect()
    _badge_db.execute("DELETE FROM questions WHERE text LIKE 'badge-test-%'")
    _badge_db.commit(); _badge_db.close()
    ownn("/admin/questions/settings", {
        "questions": "1", "proposals": "1", "updates": "1", "reminders": "1",
        "reviews": "1",
    })
    assert db_one(
        "SELECT votes FROM notification_preferences WHERE user_id=?", (uid_n,)
    )[0] == 0

    # перехватываем send_to: текст + inline-кнопку rich message
    sent = []
    rich_markups = []
    _saved = _nf.send_to
    def capture_notification(chat, text, **kwargs):
        sent.append((chat, text))
        rich_markups.append(kwargs.get("reply_markup"))
    _nf.send_to = capture_notification
    try:
        main._rates.clear()
        assert tg_login(gn, 990333, username="guestn").json()["status"] == "ok"
        set_name(gn, ntok, "Гостья")
        sent.clear(); rich_markups.clear()  # не считаем служебные сообщения входа гостя
        assert gn.post(f"/c/{ntok}/book", data={"date_id": ndid}).json()["booked"] is True
        assert not sent, f"отключённые голоса не должны приходить, sent={sent}"

        ownn("/admin/questions/settings", {
            "votes": "1", "questions": "1", "proposals": "1",
            "updates": "1", "reminders": "1", "reviews": "1",
        })
        assert gn.post(f"/c/{ntok}/book", data={"date_id": ndid}).json()["booked"] is False
        # фон BackgroundTasks в TestClient выполняется синхронно к этому моменту
        assert any(c == 990300 and "Голос снят" in t for c, t in sent), \
            f"владельцу с bot_linked=1 должно уйти уведомление, sent={sent}"
        assert any(m and m.get("inline_keyboard") for m in rich_markups), rich_markups

        # отключаем бота владельцу → уведомление НЕ шлётся
        _q = dbm.connect()
        _q.execute("UPDATE users SET bot_linked=0 WHERE id=?", (uid_n,)); _q.commit(); _q.close()
        sent.clear()
        gn.post(f"/c/{ntok}/book", data={"date_id": ndid})    # отмена выбора
        gn.post(f"/c/{ntok}/book", data={"date_id": ndid})    # снова выбор
        assert not sent, f"при bot_linked=0 уведомлений быть не должно, sent={sent}"
    finally:
        _nf.send_to = _saved
step("1.5: выбор события шлёт уведомление владельцу при bot_linked=1 и молчит при bot_linked=0")


# ---------- 1.8: юридические документы, пассивное согласие, cookie ----------
with TestClient(main.app, follow_redirects=False) as cl:
    terms = cl.get("/terms")
    assert terms.status_code == 200
    assert "Пользовательское соглашение" in terms.text
    assert "/privacy" in terms.text   # перелинковка на политику
    priv = cl.get("/privacy")
    assert priv.status_code == 200
    assert "Политика конфиденциальности" in priv.text
    assert "152-ФЗ" in priv.text and "удалить свой аккаунт" in priv.text
    # на странице входа — пассивный текст со ссылками, без обязательного действия
    lp = cl.get("/login").text
    assert "Продолжая вход" in lp and "tg-consent" not in lp
    assert 'data-skin-switchable' in lp
    assert 'data-skin-set="friends"' in lp and 'data-skin-set="romantic"' in lp
    assert "Стандартная тема" in lp and "Романтическая тема" in lp
    assert "login-mark-standard" in lp and "logo-standard.png" in lp
    assert "login-mark-romantic" in lp and "logo-romantic.png" in lp
    assert 'transform="translate(2.41 2.64) scale(.78)"' in lp
    assert '<circle cx="12" cy="12" r="11" fill="#FC3F1D"/>' in lp
    assert '<html lang="ru" data-skin="friends" data-skin-switchable>' in lp
    assert "favicon-standard.png" in lp and "favicon-romantic.png" in lp
    assert 'href="/terms"' in lp and 'href="/privacy"' in lp
    # способы входа активны сразу; Telegram — официальный Login Widget,
    # который auth.js подставляет лениво (в dialog — только после открытия).
    assert "data-tg-widget" in lp and 'data-bot=' in lp
    assert "/auth/consent" not in lp
    # Короткая VPN-подсказка не является гейтом; отдельная рекламная
    # плашка и старый поясняющий текст удалены.
    assert "Чтобы войти через Telegram, включите" in lp
    assert ">VPN</a>" in lp and 'rel="noopener sponsored"' in lp
    assert "Telegram не открывается?" not in lp
    assert "Подключить VPN" not in lp
    assert "Вход через Telegram, Google, Discord или Яндекс." not in lp
    # страница входа несёт footer-ссылки на юр-документы
    assert 'href="/terms"' in lp and 'href="/about"' in lp
    # домен ведёт сразу на вход/регистрацию (декоративный лендинг убран)
    root = cl.get("/")
    assert root.status_code == 307 and root.headers["location"] == "/login"
    # cookie-баннер убран — его нет ни на входе, ни на гостевой
    assert "cookie-bar" not in lp
    # Защита работает и без внешнего reverse proxy: заголовки ставит приложение.
    assert terms.headers["x-content-type-options"] == "nosniff"
    assert terms.headers["x-frame-options"] == "DENY"
    assert terms.headers["referrer-policy"] == "no-referrer"
    assert terms.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert terms.headers["x-xss-protection"] == "0"
    assert terms.headers["cache-control"] == "no-store"
    # Внутренние пояснения остаются в исходниках, но не попадают в публичный DOM.
    assert "<!--" not in lp
    assert cl.get("/login", headers={"host": "evil.example"}).status_code == 400
step("1.8: /terms и /privacy доступны; согласие пассивное; VPN-подсказка; / → /login")


# ---------- 1.10: страница «О проекте», поддержка, проекты автора ----------
with TestClient(main.app, follow_redirects=False) as ca:
    ab = ca.get("/about")
    assert ab.status_code == 200
    assert "О проекте" in ab.text
    assert "Тестовое описание проекта." in ab.text          # ABOUT_TEXT
    # контакт поддержки: @username → ссылка t.me
    assert "@date4you_support" in ab.text
    assert "https://t.me/date4you_support" in ab.text
    # проекты автора (оба) как обычные внешние ссылки
    assert "Мой VPN" in ab.text and "https://vpn.example.com" in ab.text
    assert "Блог" in ab.text and "https://blog.example.com" in ab.text
    # /about в футере страницы входа (домен ведёт на /login)
    assert 'href="/about"' in ca.get("/login").text
    # кривая запись проекта без http-ссылки не должна попадать (парсер фильтрует)
    from config import support_link, _parse_projects
    assert _parse_projects("Плохой;ОК|https://ok.com") == [{"name": "ОК", "url": "https://ok.com"}]
    assert support_link()["url"] == "https://t.me/date4you_support"
step("1.10: /about (текст, поддержка @→t.me, проекты автора), футер-ссылки, фильтр кривых проектов")


# ---------- 1.9: публичный профиль /u/<id> (доступен без регистрации) ----------
with TestClient(main.app, follow_redirects=False) as cu1, \
        TestClient(main.app, follow_redirects=False) as cu2, \
        TestClient(main.app, follow_redirects=False) as anon:
    # профиль заполняет владелец; читать его сможет и аноним
    assert tg_login(cu1, 991401, username="alice").json()["status"] == "ok"
    uid1 = db_one("SELECT id FROM users WHERE telegram_id=991401")[0]
    # заполняем профиль пользователя 1 (имя/ДР/пол)
    pc = re.search(r'name="csrf" value="([^"]+)"', cu1.get("/admin/profile").text).group(1)
    cu1.post("/admin/profile", data={"csrf": pc, "display_name": "Алиса",
                                     "birth_date": "1995-06-15", "gender": "f"})
    # второй залогиненный видит чужой профиль целиком (имя, пол, полная ДР)
    assert tg_login(cu2, 991402, username="bob").json()["status"] == "ok"
    pg = cu2.get(f"/u/{uid1}")
    assert pg.status_code == 200, pg.status_code
    assert "Алиса" in pg.text and "Женский" in pg.text and "1995-06-15" in pg.text
    # незалогиненный видит те же публичные данные, но не действия владельца
    r = anon.get(f"/u/{uid1}")
    assert r.status_code == 200, r.status_code
    assert "Алиса" in r.text and "Женский" in r.text and "1995-06-15" in r.text
    assert ">Войти</a>" in r.text and "Редактировать профиль" not in r.text
    # несуществующий/неактивный профиль → 404 для залогиненного
    assert cu2.get("/u/999999").status_code == 404
    # свой профиль помечается (кнопка «Редактировать»)
    assert "Редактировать профиль" in cu1.get(f"/u/{uid1}").text
step("1.9: /u/<id> публичен без регистрации; владелец сохраняет редактирование; 404 на отсутствующий")


# ---------- перф: Cache-Control на статике ----------
with TestClient(main.app, follow_redirects=False) as cs:
    css = cs.get("/static/public.css")
    assert css.status_code == 200
    assert "max-age=3600" in css.headers.get("cache-control", ""), css.headers.get("cache-control")
    png = cs.get("/static/icon-192.png")
    assert png.status_code == 200
    assert "max-age=2592000" in png.headers.get("cache-control", ""), png.headers.get("cache-control")
step("перф: статика отдаётся с Cache-Control (css — час, иконки/шрифты — 30 дней)")


# ---------- #1: подпись автора категории + кнопка «Войти» для анонима ----------
with TestClient(main.app, follow_redirects=False) as cown, \
        TestClient(main.app, follow_redirects=False) as anon:
    assert tg_login(cown, 992001, username="byline").json()["status"] == "ok"
    pc = re.search(r'name="csrf" value="([^"]+)"',
                   cown.get("/admin/profile").text).group(1)
    cown.post("/admin/profile", data={"csrf": pc, "display_name": "Маргарита",
                                      "birth_date": "1992-03-03"})
    cown.post("/admin/categories/create", data={**category_data("Авторская"), "csrf": pc})
    bc = db_one("SELECT id, link_token, owner_id FROM categories WHERE name='Авторская'")
    btok2 = bc["link_token"]
    page = anon.get(f"/c/{btok2}").text
    # подпись автора кликабельна и ведёт на его публичный профиль (для всех)
    assert f'/u/{bc["owner_id"]}?skin=friends' in page and "Маргарита" in page
    # анониму показана кнопка «Войти»; вход — модалкой с возвратом на эту ссылку
    # Адрес возврата теперь передаётся непосредственно Telegram/OAuth-входу;
    # пассивный текст правил не требует checkbox или отдельного consent-route.
    assert ">Войти<" in page and 'data-auth-url="/auth/widget?widget_state=' in page
    assert "return_to=" in page
    assert "data-next=" not in page and "tg-consent" not in page
step("#1: подпись автора кликабельна (/u/<id>), анониму — кнопка «Войти» с возвратом")


# ---------- #4: возврат на гостевую ссылку после входа (next) ----------
with TestClient(main.app, follow_redirects=False) as cret:
    nxt = f"/c/{btok2}"
    cret.get(f"/login?next={nxt}")                 # сохраняет next в сессии
    poll = tg_login(cret, 992002, username="returner")
    assert poll.json()["status"] == "ok"
    assert poll.json()["redirect"] == nxt, poll.json()
    # чужой next (open redirect) отбрасывается → кабинет
    cret2 = TestClient(main.app, follow_redirects=False)
    cret2.get("/login?next=https://evil.com")
    poll2 = tg_login(cret2, 992003, username="ret2")
    assert poll2.json()["redirect"] == "/admin/", poll2.json()
step("#4: после входа возврат на /c/<токен> через next; внешний next отбрасывается")


# ---------- #3: уведомления автору (ответ на вопрос, публикация предложения) ----------
import notify as _nf3
with TestClient(main.app, follow_redirects=False) as cown, \
        TestClient(main.app, follow_redirects=False) as gq:
    assert tg_login(cown, 992101, username="qowner").json()["status"] == "ok"
    own_csrf = re.search(r'name="csrf" value="([^"]+)"',
                         cown.get("/admin/categories").text).group(1)
    def ownq(url, data=None):
        d = dict(data or {}); d["csrf"] = own_csrf
        return cown.post(url, data=d)
    ownq("/admin/categories/create", category_data("Вопросная"))
    qc = db_one("SELECT id, link_token FROM categories WHERE name='Вопросная'")
    qtok = qc["link_token"]
    set_moderation(qc['id'], False)   # выкл, чтобы предложения публиковались сразу
    ownq("/admin/dates/new", {"name": "Кофейня", "categories": str(qc["id"])})
    qdid = db_one("SELECT id FROM dates WHERE name='Кофейня'")[0]

    # гость (с подключённым ботом) задаёт вопрос
    main._rates.clear()
    assert tg_login(gq, 992102, username="asker").json()["status"] == "ok"
    set_name(gq, qtok, "Спрашивающий")
    assert gq.post(f"/c/{qtok}/question",
                   data={"date_id": qdid, "text": "Есть веранда?"}).status_code == 200
    qid_n = db_one("SELECT id FROM questions WHERE date_id=? AND text='Есть веранда?'",
                   (qdid,))[0]
    uid_asker = db_one("SELECT id FROM users WHERE telegram_id=992102")[0]
    assert db_one("SELECT user_id FROM questions WHERE id=?", (qid_n,))[0] == uid_asker

    # перехват send_to: ответ админа должен уведомить автора вопроса
    sent = []
    _saved = _nf3.send_to
    _nf3.send_to = lambda chat, text, **kwargs: sent.append((chat, text))
    try:
        r = ownq(f"/admin/questions/{qid_n}/answer",
                 {"text": "Да, есть!", "next": "/admin/questions"})
        assert r.status_code == 303
        assert any(c == 992102 and "Ответ на твой вопрос" in t for c, t in sent), sent

        # публикация гостевого предложения уведомляет его автора
        set_moderation(qc['id'], True)   # вкл модерацию (операторская настройка)
        main._rates.clear()
        rp = gq.post(f"/c/{qtok}/propose", data={"name": "Прогулка у реки"})
        assert rp.json()["moderated"] is True
        ppid = rp.json()["id"]
        sent.clear()
        ownq(f"/admin/dates/{ppid}/publish", {"next": "/admin/dates?view=active"})
        assert any(c == 992102 and "опубликовано" in t for c, t in sent), sent
    finally:
        _nf3.send_to = _saved
step("#3: ответ на вопрос и публикация предложения уведомляют автора (по user_id/proposed_by)")


# ---------- #9: админ редактирует чужие категории и события ----------
with TestClient(main.app, follow_redirects=False) as cuser, \
        TestClient(main.app, follow_redirects=False) as cadm:
    assert tg_login(cuser, 992201, username="victim").json()["status"] == "ok"
    assert tg_login(cadm, 555001, username="boss").json()["status"] == "ok"
    uc = re.search(r'name="csrf" value="([^"]+)"',
                   cuser.get("/admin/categories").text).group(1)
    def up(url, data=None):
        d = dict(data or {}); d["csrf"] = uc
        return cuser.post(url, data=d)
    up("/admin/categories/create", category_data("Чужая категория"))
    oc = db_one("SELECT id FROM categories WHERE name='Чужая категория'")[0]
    up("/admin/dates/new", {"name": "Чужое событие", "categories": str(oc)})
    odid = db_one("SELECT id FROM dates WHERE name='Чужое событие'")[0]

    ac = re.search(r'name="csrf" value="([^"]+)"',
                   cadm.get("/admin/profile").text).group(1)
    def ap(url, data=None):
        d = dict(data or {}); d["csrf"] = ac
        return cadm.post(url, data=d)
    # админ открывает и правит чужую категорию/событие (обычный кабинет, гейт is_operator)
    assert cadm.get(f"/admin/categories/{oc}").status_code == 200
    assert ap(f"/admin/categories/{oc}/rename", {"name": "Переименована админом"}).status_code == 303
    assert db_one("SELECT name FROM categories WHERE id=?", (oc,))[0] == "Переименована админом"
    assert cadm.get(f"/admin/dates/{odid}/edit").status_code == 200
    assert ap(f"/admin/dates/{odid}/edit",
              {"name": "Событие правил админ", "categories": str(oc)}).status_code == 303
    assert db_one("SELECT name FROM dates WHERE id=?", (odid,))[0] == "Событие правил админ"
    # владелец события не сменился (админ правит в контексте владельца)
    owner_of = db_one("SELECT u.telegram_id FROM dates d JOIN users u ON u.id=d.owner_id "
                      "WHERE d.id=?", (odid,))[0]
    assert owner_of == 992201
    # операторские списки несут ссылку «Редактировать» в кабинет
    assert f"/admin/categories/{oc}" in cadm.get("/operator/categories").text
    assert f"/admin/dates/{odid}/edit" in cadm.get("/operator/dates").text

    # обычный пользователь по-прежнему НЕ может трогать чужое (404 → flash)
    other = TestClient(main.app, follow_redirects=False)
    assert tg_login(other, 992202, username="rando2").json()["status"] == "ok"
    assert other.get(f"/admin/categories/{oc}").status_code == 404
    assert other.get(f"/admin/dates/{odid}/edit").status_code == 404
step("#9: админ правит чужие категории/события (владелец не меняется); не-оператор → 404")


# ---------- #11: переключатели модерации (мягкая очередь) ----------
with TestClient(main.app, follow_redirects=False) as cadm:
    assert tg_login(cadm, 555001, username="boss").json()["status"] == "ok"
    ac = re.search(r'name="csrf" value="([^"]+)"',
                   cadm.get("/operator/settings").text).group(1)
    # по умолчанию модерация выключена
    import settings as _setm
    _sc = dbm.connect()
    assert not _setm.is_on(_sc, _setm.MODERATE_USERS)
    assert not _setm.is_on(_sc, _setm.MODERATE_CATEGORIES)
    _sc.close()

    # включаем обе через форму настроек
    r = cadm.post("/operator/settings",
                  data={"csrf": ac, "moderate_users": "1", "moderate_categories": "1"})
    assert r.status_code == 303
    _sc = dbm.connect()
    assert _setm.is_on(_sc, _setm.MODERATE_USERS)
    assert _setm.is_on(_sc, _setm.MODERATE_CATEGORIES)
    _sc.close()

    # новый пользователь при включённой модерации → is_reviewed=0 (доступ при этом есть)
    cnew = TestClient(main.app, follow_redirects=False)
    assert tg_login(cnew, 992301, username="freshuser").json()["status"] == "ok"
    uid_new = db_one("SELECT id, is_reviewed FROM users WHERE telegram_id=992301")
    assert uid_new["is_reviewed"] == 0
    assert cnew.get("/admin/").status_code == 200          # мягкая очередь — доступ открыт

    # новая категория при включённой модерации → is_reviewed=0
    nc = re.search(r'name="csrf" value="([^"]+)"',
                   cnew.get("/admin/categories").text).group(1)
    cnew.post("/admin/categories/create", data={**category_data("Свежая категория"), "csrf": nc})
    fresh_cat = db_one("SELECT id, is_reviewed, link_enabled FROM categories WHERE name='Свежая категория'")
    assert fresh_cat["is_reviewed"] == 0 and fresh_cat["link_enabled"] == 1   # ссылка работает сразу

    # очередь у админа показывает обоих и даёт «Одобрить»
    review = cadm.get("/operator/review").text
    assert "freshuser" in review and "Свежая категория" in review
    assert cadm.get("/operator/").text.count("На проверке") >= 1
    rc = re.search(r'name="csrf" value="([^"]+)"', review).group(1)
    assert cadm.post(f"/operator/review/user/{uid_new['id']}/approve",
                     data={"csrf": rc}).status_code == 303
    assert db_one("SELECT is_reviewed FROM users WHERE id=?", (uid_new["id"],))[0] == 1
    assert cadm.post(f"/operator/review/category/{fresh_cat['id']}/approve",
                     data={"csrf": rc}).status_code == 303
    assert db_one("SELECT is_reviewed FROM categories WHERE id=?", (fresh_cat["id"],))[0] == 1

    # выключаем обратно → новые снова одобрены сразу
    cadm.post("/operator/settings", data={"csrf": ac})     # без чекбоксов = выкл
    coff = TestClient(main.app, follow_redirects=False)
    assert tg_login(coff, 992302, username="afteroff").json()["status"] == "ok"
    assert db_one("SELECT is_reviewed FROM users WHERE telegram_id=992302")[0] == 1
step("#11: тумблеры модерации помечают новых is_reviewed=0 (доступ открыт), очередь и одобрение работают")


# ---------- #8: «Оператор» переименован в «Админ» в интерфейсе ----------
with TestClient(main.app, follow_redirects=False) as cadm:
    assert tg_login(cadm, 555001, username="boss").json()["status"] == "ok"
    assert 'href="/operator/"' in cadm.get("/admin/").text  # ссылка-вкладка «Админ» в шапке кабинета
    op = cadm.get("/operator/").text
    assert "⚙ Админ" in op and "админов" in op            # бренд и счётчик
    assert "Оператор" not in cadm.get("/operator/users").text
step("#8: роль «Оператор» переименована в «Админ» во всех видимых местах")


# ---------- #2/#6/#10: мелкие правки UI (карточка без фото, профиль, видео) ----------
with TestClient(main.app, follow_redirects=False) as cown:
    assert tg_login(cown, 992401, username="uiowner").json()["status"] == "ok"
    pc = re.search(r'name="csrf" value="([^"]+)"',
                   cown.get("/admin/profile").text).group(1)
    # #6: в профиле нет старого хинта, пол по умолчанию «Не указан»
    pf = cown.get("/admin/profile").text
    assert "Как тебя увидят получатели приглашений" not in pf
    assert "Не указан" in pf
    cown.post("/admin/profile", data={"csrf": pc, "display_name": "UIвладелец",
                                      "birth_date": "1991-01-01"})
    cown.post("/admin/categories/create", data={**category_data("UI-кат"), "csrf": pc})
    uic = db_one("SELECT id, link_token FROM categories WHERE name='UI-кат'")
    set_moderation(uic['id'], False)  # выкл (модерация — операторская настройка)
    # событие без фото
    cown.post("/admin/dates/new", data={"csrf": pc, "name": "Без картинок",
                                        "categories": str(uic["id"])})
    # #2: в админ-списке у карточки без фото нет блока-плейсхолдера .ph
    lp = cown.get("/admin/dates").text
    nocard = re.search(r'<div class="dcard[^"]*nocover[^"]*">.*?Без картинок', lp, re.S)
    assert nocard and 'class="ph' not in nocard.group(0), "карточка без фото не должна иметь .ph-плейсхолдер"
    # #2: на гостевой у карточки без фото нет градиентной «крышки» (.booked-overlay.flat / .accent)
    gp = cown.get(f"/c/{uic['link_token']}").text
    card = re.search(r'<article[^>]*class="card[^"]*nophoto[^"]*".*?</article>', gp, re.S).group(0)
    assert "booked-overlay" not in card and "accent" not in card
    # Актуальный гостевой редактор использует ту же галерею, что админский:
    # текущее и новое видео становятся слайдами, без отдельного старого блока.
    assert 'id="propSlides"' in gp and 'id="propVideo"' in gp
    assert 'class="pcard editable"' in gp
step("#2/#6/#10: карточка без фото без заглушки; профиль без хинта; гостевой редактор карточный")


# ---------- НОВОЕ: лента комьюнити, публичность, профиль, меню категорий, OAuth ----------
def _csrf(client, url="/admin/categories"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


# Готовим двух владельцев: Ната (автор публичного события) и Гоша (зритель).
main._rates.clear()
with TestClient(main.app, follow_redirects=False) as cnata, \
        TestClient(main.app, follow_redirects=False) as cgosha:
    assert tg_login(cnata, 771001, username="nata").json()["status"] == "ok"
    assert tg_login(cgosha, 771002, username="gosha").json()["status"] == "ok"
    nc = _csrf(cnata)
    cnata.post("/admin/categories/create", data={**category_data("Ната-кат"), "csrf": nc})
    ncat = db_one("SELECT id, link_token FROM categories WHERE name='Ната-кат'")
    set_moderation(ncat["id"], False)

    # C2: тумблер публичности есть в редакторе; по умолчанию публичное (checked)
    newform = cnata.get("/admin/dates/new").text
    assert 'name="is_public"' in newform and "общей ленте событий" in newform

    # создаём ПУБЛИЧНОЕ событие (чекбокс отправлен)
    cnata.post("/admin/dates/new", data={
        "csrf": nc, "name": "Пикник на закате", "categories": str(ncat["id"]),
        "comment": "Плед и вино", "place": "Парк", "is_public": "1"},
        files=[("images", ("public.png", make_png((180, 105, 125)), "image/png"))])
    pub = db_one("SELECT id, is_public, share_token, owner_id FROM dates WHERE name='Пикник на закате'")
    assert pub["is_public"] == 1, "по умолчанию событие публичное"

    # создаём ПРИВАТНОЕ событие (чекбокс НЕ отправлен) — в ленту не попадёт
    cnata.post("/admin/dates/new", data={
        "csrf": nc, "name": "Секретный ужин", "categories": str(ncat["id"])})
    priv = db_one("SELECT id, is_public FROM dates WHERE name='Секретный ужин'")
    assert priv["is_public"] == 0, "без чекбокса событие приватное"

    # C3: у Гоши на главной — лента событий вместо «Последних действий»
    dash = cgosha.get("/admin/").text
    assert "Лента событий" in dash and "Последние действия" not in dash
    assert "Встречи сообщества" not in dash
    assert "Публичные события других людей" not in dash
    assert 'id="communityFeed"' in dash
    assert 'id="communityReportDlg"' in dash and 'id="communityReportForm"' in dash

    # фрагмент ленты: видно чужое публичное, НЕ видно приватное и своё
    feed = cgosha.get("/admin/community").text
    assert "Пикник на закате" in feed, "публичное чужое событие в ленте"
    assert "Секретный ужин" not in feed, "приватное в ленту не попадает"
    assert "Ната-кат" not in feed, "категория в ленте не показывается"
    assert "Добавить в коллекцию" in feed, "главное действие карточки копирует событие"
    assert f'data-add="/d/{pub["share_token"]}/add"' in feed
    assert 'class="cfeed-owner"' not in feed and "Автор:" not in feed
    assert f'href="/u/{pub["owner_id"]}"' not in feed
    assert "data-community-report" in feed and "пожаловаться" in feed
    assert f'data-report-url="/d/{pub["share_token"]}/report"' in feed
    assert "?w=480 480w" in feed and 'fetchpriority="low"' in feed
    # автор не видит своё событие в собственной ленте
    assert "Пикник на закате" not in cnata.get("/admin/community").text

    # C4: мини-виджет отдаётся, есть кнопка «Добавить себе»
    wid = cgosha.get(f"/admin/community/date/{pub['id']}").text
    assert "Пикник на закате" in wid and "Добавить себе" in wid
    assert 'class="cfeed-owner"' in wid and f'href="/u/{pub["owner_id"]}"' in wid
    assert f"/d/{pub['share_token']}/add" in wid
    assert "?w=1600 1600w" in wid and "data-full=" in wid
    # приватное чужое событие виджетом не открыть
    assert cgosha.get(f"/admin/community/date/{priv['id']}").status_code == 404

    # Жалоба из карточки уходит в существующий публичный endpoint с CSRF
    # авторизованной сессии и попадает в операторскую очередь.
    main._rates.clear()
    report = cgosha.post(
        f"/d/{pub['share_token']}/report",
        data={"target_type": "date", "target_id": pub["id"], "reason": "Проверить"},
        headers={"X-Requested-With": "fetch"},
    )
    assert report.status_code == 200 and report.json()["ok"] is True
    gosha_reporter = "u" + str(db_one(
        "SELECT id FROM users WHERE telegram_id=771002",
    )[0])
    assert db_one(
        "SELECT reason FROM reports WHERE target_type='date' AND target_id=? "
        "AND reporter=?",
        (pub["id"], gosha_reporter),
    )["reason"] == "Проверить"

    # C4: «Добавить себе» через fetch → JSON, копия появляется у Гоши
    add = cgosha.post(f"/d/{pub['share_token']}/add", headers={"X-Requested-With": "fetch"})
    assert add.status_code == 200 and add.json()["ok"] is True
    assert db_one("SELECT COUNT(*) FROM dates WHERE owner_id=(SELECT id FROM users "
                  "WHERE telegram_id=771002) AND name='Пикник на закате'")[0] == 1

    # «Хочу сходить» независимо от копии в коллекции: хранится связь с
    # оригиналом, появляется в профиле и получает один review-prompt.
    gc = _csrf(cgosha)
    shared = cgosha.get(f"/d/{pub['share_token']}").text
    assert "Хочу сходить" in shared
    assert f'/u/{pub["owner_id"]}?skin=friends' in shared, \
        "имя владельца share-события ведёт в профиль с темой категории"
    want = cgosha.post(f"/d/{pub['share_token']}/want", data={"csrf": gc})
    assert want.status_code == 303
    gosha_id = db_one("SELECT id FROM users WHERE telegram_id=771002")[0]
    assert db_one(
        "SELECT 1 FROM date_wants WHERE user_id=? AND date_id=?",
        (gosha_id, pub["id"]),
    )
    assert "Пикник на закате" in cgosha.get(f"/u/{gosha_id}?tab=want").text
    prompt = db_one(
        "SELECT kind,action_label FROM notification_outbox "
        "WHERE event_key=?",
        (social.prompt_key(pub["id"], gosha_id),),
    )
    assert prompt["kind"] == "review_prompt" and prompt["action_label"] == "Оставить обзор"

    # Имитируем прошедшее событие. Review due следует реальному окончанию, а
    # не более раннему дедлайну голосования; пересчёт сохраняет тот же key.
    past_start = (datetime.now() - timedelta(hours=4)).replace(
        second=0, microsecond=0).isoformat(timespec="minutes")
    past_end = (datetime.now() - timedelta(hours=1)).replace(
        second=0, microsecond=0).isoformat(timespec="minutes")
    past_deadline = (datetime.now() - timedelta(days=1)).replace(
        second=0, microsecond=0).isoformat(timespec="minutes")
    sq = dbm.connect()
    sq.execute(
        "UPDATE categories SET voting_status='unconfigured', closed_at=NULL, "
        "voting_deadline=? WHERE id=?", (past_deadline, ncat["id"]),
    )
    sq.execute(
        "UPDATE dates SET starts_at=?, ends_at=? WHERE id=?",
        (past_start, past_end, pub["id"]),
    )
    social.queue_review_prompts_for_date(sq, pub["id"])
    sq.commit(); sq.close()

    review_page = cgosha.get(f"/d/{pub['share_token']}#review").text
    assert "Удалось сходить?" in review_page and 'name="rating"' in review_page
    made_review = cgosha.post(f"/d/{pub['share_token']}/review", data={
        "csrf": gc, "rating": "5", "text": "Очень тёплая встреча",
    })
    assert made_review.status_code == 303
    assert made_review.headers["location"].startswith(
        f"/u/{gosha_id}?tab=reviews&msg="
    )
    assert made_review.headers["location"].endswith("#profileCollection")
    review = db_one(
        "SELECT id,rating,text,is_public FROM date_reviews WHERE user_id=? AND date_id=?",
        (gosha_id, pub["id"]),
    )
    assert tuple(review[k] for k in ("rating", "text", "is_public")) == \
        (5, "Очень тёплая встреча", 1)
    assert db_one(
        "SELECT 1 FROM notification_outbox WHERE kind='review_received' "
        "AND user_id=?", (pub["owner_id"],),
    )
    own_reviews = cgosha.get(f"/u/{gosha_id}?tab=reviews").text
    assert "Очень тёплая встреча" in own_reviews and "Удалить" in own_reviews
    assert f'/u/{gosha_id}/reviews/{review["id"]}/widget' in own_reviews
    own_review_widget = cgosha.get(
        f'/u/{gosha_id}/reviews/{review["id"]}/widget',
    ).text
    assert "Сохранить обзор" in own_review_widget
    assert "Добавить себе" not in own_review_widget and "Спросить" not in own_review_widget
    assert f'/d/{pub["share_token"]}/review/{review["id"]}' in own_review_widget
    assert "Пикник на закате" not in cgosha.get(f"/u/{gosha_id}?tab=want").text, \
        "после обзора встреча не дублируется в «Хочу сходить»"

    hidden = cgosha.post(
        f"/u/{gosha_id}/reviews/{review['id']}/hide", data={"csrf": gc},
    )
    assert hidden.status_code == 303
    assert "Очень тёплая встреча" not in cnata.get(f"/u/{gosha_id}?tab=reviews").text
    assert not db_one(
        "SELECT 1 FROM date_reviews WHERE id=?", (review["id"],),
    )
    queued_review = db_one(
        "SELECT reason FROM review_queue WHERE user_id=? AND date_id=?",
        (gosha_id, pub["id"]),
    )
    assert queued_review["reason"] == "review_deleted"
    assert "Пикник на закате" in cgosha.get("/admin/questions?f=reviews").text
    edited = cgosha.post(
        f"/u/{gosha_id}/reviews/{review['id']}/edit",
        data={"csrf": gc, "rating": "4", "text": "Обновлённый обзор"},
    )
    assert edited.status_code == 404
    recreated = cgosha.post(f"/d/{pub['share_token']}/review", data={
        "csrf": gc, "rating": "4", "text": "Обновлённый обзор",
    })
    assert recreated.status_code == 303
    assert not db_one(
        "SELECT 1 FROM review_queue WHERE user_id=? AND date_id=?",
        (gosha_id, pub["id"]),
    )
    assert "Обновлённый обзор" in cnata.get(f"/u/{gosha_id}?tab=reviews").text

    # C5: публичный профиль Наты перечисляет её публичные события (без приватных)
    prof = cgosha.get(f"/u/{pub['owner_id']}").text
    assert "Пикник на закате" in prof and "Секретный ужин" not in prof
    assert "Коллекция событий" in prof and "Публичные события" not in prof
    assert "?w=480 480w" in prof and 'fetchpriority="high"' in prof

    # Профиль не тянет бесконечную историю одним HTML: 12 карточек на страницу.
    q = dbm.connect()
    extra_ids = []
    for idx in range(12):
        extra_ids.append(main.public_routes.insert_date(
            q, name=f"Публичное {idx}", place=None, starts=None, ends=None,
            comment=None, origin="admin", guest_token=None, owner_id=pub["owner_id"],
            draft=0, pay_split=0, is_public=1))
    q.commit()
    prof_first = cgosha.get(f"/u/{pub['owner_id']}").text
    prof_second = cgosha.get(f"/u/{pub['owner_id']}?page=2").text
    assert prof_first.count('class="pub-card"') == 12
    assert prof_second.count('class="pub-card"') == 1
    assert "1 / 2" in prof_first and "2 / 2" in prof_second
    assert "Ещё →" not in prof_first and 'aria-label="Следующая страница"' in prof_first
    assert "#profileCollection" in prof_first, "пагинация возвращает к коллекции"
    q.executemany("DELETE FROM dates WHERE id=?", [(x,) for x in extra_ids])
    q.commit()
    q.close()
step("новое C2–C5: тумблер публичности, лента комьюнити, виджет+добавить, публичный профиль")


# ---------- НОВОЕ: меню ⋯ на категориях, чистка редактора, OAuth-заготовки ----------
main._rates.clear()
with TestClient(main.app, follow_redirects=False) as cui:
    assert tg_login(cui, 771003, username="uimenu").json()["status"] == "ok"
    uc = _csrf(cui)
    cui.post("/admin/categories/create", data={**category_data("Меню-кат"), "csrf": uc})
    mcat = db_one("SELECT id, link_token FROM categories WHERE name='Меню-кат'")

    # B2: в списке категорий — меню ⋯ (три пункта), больше нет строки-linkbox
    cats = cui.get("/admin/categories").text
    assert 'class="more"' in cats and "Скопировать ссылку" in cats
    assert "Перегенерировать ссылку" in cats and "Удалить категорию" in cats
    assert "Отключить ссылку" in cats
    assert all(mark not in cats for mark in ("👀 Открыть", "🔗 Скопировать ссылку",
                                              "🔒 Отключить ссылку", "↻ Перегенерировать ссылку",
                                              "🗑 Удалить категорию"))
    assert "copy-code" not in cats, "строка-ссылка на списке категорий убрана"

    # B3: основные действия «Сохранить» и «Отмена» остаются на виду,
    # а сброс, ссылка и удаление собраны в компактном меню ⋯.
    ed = cui.get(f"/admin/categories/{mcat['id']}").text
    assert "Сбросить превью" in ed and 'id="resetPreviewForm"' in ed
    assert "cat-editor-actions" in ed and "cat-editor-menu" in ed
    assert "cat-editor-main-actions" in ed and ">Отмена</a>" in ed
    assert "Скопировать ссылку" in ed
    assert "Открыть ссылку" in ed
    assert "Удалить категорию" in ed
    assert "Никто не выбрал" not in ed

    # B3: «Сбросить превью» чистит и картинку, и текст превью
    cui.post(f"/admin/categories/{mcat['id']}/rename",
             data={"csrf": uc, "name": "Меню-кат", "og_title": "Заголовок",
                   "og_desc": "Описание"})
    assert db_one("SELECT og_title FROM categories WHERE id=?", (mcat["id"],))[0] == "Заголовок"
    assert cui.post(f"/admin/categories/{mcat['id']}/preview/reset",
                    data={"csrf": uc}).status_code == 303
    row = db_one("SELECT og_title, og_desc, og_image FROM categories WHERE id=?", (mcat["id"],))
    assert row["og_title"] is None and row["og_desc"] is None and row["og_image"] is None

    # D-новое: своя картинка превью + WYSIWYG-кроп по точке фокуса (og_focus).
    # HTTPException на POST /admin друж. обработчик превращает в 303-редирект с
    # флешем (см. friendly_http_exc). Без своей картинки фокус нечего кропать →
    # не сохраняется (редирект, og_focus остаётся NULL).
    cui.post(f"/admin/categories/{mcat['id']}/og_focus",
             data={"csrf": uc, "focus": "50% 50%",
                   "expected_image": "", "expected_focus": "50% 50%"})
    assert db_one("SELECT og_focus FROM categories WHERE id=?", (mcat["id"],))[0] is None
    cui.post(f"/admin/categories/{mcat['id']}/rename",
             data={"csrf": uc, "name": "Меню-кат"},
             files={"og_image": ("og.png", make_png(), "image/png")})
    preview_row = db_one(
        "SELECT og_image,og_focus FROM categories WHERE id=?", (mcat["id"],))
    assert preview_row["og_image"] and preview_row["og_focus"] is None
    # кривой фокус не сохраняется; корректный — сохраняется и нормализуется (JSON 200)
    cui.post(f"/admin/categories/{mcat['id']}/og_focus",
             data={"csrf": uc, "focus": "999% x",
                   "expected_image": preview_row["og_image"],
                   "expected_focus": "50% 50%"})
    assert db_one("SELECT og_focus FROM categories WHERE id=?", (mcat["id"],))[0] is None
    assert cui.post(f"/admin/categories/{mcat['id']}/og_focus",
                    data={"csrf": uc, "focus": "20% 80%",
                          "expected_image": preview_row["og_image"],
                          "expected_focus": "50% 50%"}).status_code == 200
    assert db_one("SELECT og_focus FROM categories WHERE id=?", (mcat["id"],))[0] == "20% 80%"
    # og-preview отдаёт кроп 1200×630 (WebP), не падает
    assert cui.get(f"/admin/categories/{mcat['id']}/og-preview").status_code == 200

    # D: кнопки OAuth-провайдеров на странице входа — активны сразу, с иконками.
    # Нужен анонимный клиент.
    with TestClient(main.app, follow_redirects=False) as canon:
        login = canon.get("/login").text
        assert "/auth/discord" in login and "/auth/google" in login and "/auth/yandex" in login
        assert "data-login-methods" in login and "oauth-ico" in login
        assert "tg-consent" not in login
    # не настроенный провайдер → 503; неизвестный → 404; настроенный → редирект на провайдера
    assert cui.get("/auth/google").status_code == 503
    assert cui.get("/auth/google/callback").status_code == 503
    assert cui.get("/auth/unknown").status_code == 404
step("новое B2/B3/D: меню ⋯ категорий, чистка редактора+сброс превью, OAuth-кнопки-иконки")


# ---------- НОВОЕ: полный OAuth-поток (настроенный провайдер, мок httpx) ----------
import auth_routes as _ar

# «Настраиваем» google: вписываем client_id/secret прямо в рантайм-конфиг роутов.
_ar.OAUTH_PROVIDERS["google"] = ("cid-test", "secret-test")


class _OAuthResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
    def json(self):
        return self._payload


class _FakeOAuthClient:
    """Подменяет httpx.Client в auth_routes: token→access_token, userinfo→профиль."""
    provider_uid = "google-user-777"
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, url, data=None, headers=None):
        return _OAuthResp(200, {"access_token": "at-123"})
    def get(self, url, headers=None):
        return _OAuthResp(200, {"sub": _FakeOAuthClient.provider_uid,
                                "name": "Гоша OAuth", "email": "gosha@oauth.test"})


main._rates.clear()
_real_client = _ar.httpx.Client
_ar.httpx.Client = _FakeOAuthClient
try:
    with TestClient(main.app, follow_redirects=False) as coa:
        # старт: настроенный провайдер редиректит на страницу авторизации Google
        start = coa.get("/auth/google")
        assert start.status_code == 303, start.status_code
        loc = start.headers["location"]
        assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth")
        assert "client_id=cid-test" in loc and "redirect_uri=" in loc
        state = re.search(r"state=([^&]+)", loc).group(1)

        # callback с верным state → создаётся аккаунт без telegram_id и логинит
        cb = coa.get(f"/auth/google/callback?code=abc&state={state}")
        assert cb.status_code == 303, cb.status_code
        assert cb.headers["location"] in ("/admin/", "/admin")
        u = db_one("SELECT id, telegram_id, display_name FROM users "
                   "WHERE display_name='Гоша OAuth'")
        assert u is not None and u["telegram_id"] is None, "OAuth-аккаунт без telegram_id"
        link = db_one("SELECT provider, provider_uid, user_id FROM oauth_accounts "
                      "WHERE provider='google'")
        assert link and link["provider_uid"] == _FakeOAuthClient.provider_uid
        assert link["user_id"] == u["id"]
        # Кабинет доступен под OAuth-аккаунтом (нет telegram — не падает),
        # а в профиле остаётся предложение подключить уведомления.
        assert coa.get("/admin/").status_code == 200
        assert "Уведомления в Telegram" in coa.get("/admin/profile").text

        # Подключение Telegram из OAuth-сессии дополняет ЭТОТ аккаунт, не создаёт
        # второй TG-аккаунт и не переключает user_id браузера.
        tg_code = coa.post("/auth/start").json()["code"]
        tg_link = db_one("SELECT purpose, user_id FROM login_codes WHERE code=?", (tg_code,))
        assert tg_link["purpose"] == "link" and tg_link["user_id"] == u["id"]
        tg_open_login(coa, tg_code, 773001, username="oauth-telegram")
        assert coa.get(f"/auth/poll?code={tg_code}").json()["status"] == "pending"
        tg_confirm_login(coa, tg_code, 773001, username="oauth-telegram")
        tg_done = coa.get(f"/auth/poll?code={tg_code}").json()
        assert tg_done["status"] == "ok" and tg_done["linked"] is True
        linked_oauth = db_one("SELECT id, telegram_id, bot_linked FROM users WHERE id=?",
                              (u["id"],))
        assert linked_oauth["telegram_id"] == 773001 and linked_oauth["bot_linked"] == 1
        assert db_one("SELECT COUNT(*) FROM users WHERE telegram_id=773001")[0] == 1
        assert coa.get("/admin/").status_code == 200
        assert "Уведомления в Telegram" not in coa.get("/admin/profile").text

        # повторный вход тем же провайдером НЕ плодит дубль-аккаунт
        coa2 = TestClient(main.app, follow_redirects=False)
        s2 = coa2.get("/auth/google")
        st2 = re.search(r"state=([^&]+)", s2.headers["location"]).group(1)
        coa2.get(f"/auth/google/callback?code=xyz&state={st2}")
        assert db_one("SELECT COUNT(*) FROM users WHERE display_name='Гоша OAuth'")[0] == 1

    # подделка state → 403 (CSRF-защита колбэка)
    main._rates.clear()
    with TestClient(main.app, follow_redirects=False) as cbad:
        cbad.get("/auth/google")   # кладёт настоящий state в сессию
        assert cbad.get("/auth/google/callback?code=abc&state=WRONG").status_code == 403

    # привязка соцсети к TG-аккаунту из профиля (?link=1) + отвязка
    main._rates.clear()
    _FakeOAuthClient.provider_uid = "google-link-999"
    with TestClient(main.app, follow_redirects=False) as clink:
        assert tg_login(clink, 773100, username="linker").json()["status"] == "ok"
        # профиль показывает блок соцсетей с кнопкой «Привязать»
        prof = clink.get("/admin/profile").text
        assert "Соцсети и сервисы" in prof and "/auth/google?link=1" in prof
        # старт привязки помечает режим link и ведёт на провайдера
        st = clink.get("/auth/google?link=1")
        assert st.status_code == 303
        state = re.search(r"state=([^&]+)", st.headers["location"]).group(1)
        cb = clink.get(f"/auth/google/callback?code=c&state={state}")
        assert cb.status_code == 303 and "/admin/profile" in cb.headers["location"]
        me = db_one("SELECT id FROM users WHERE telegram_id=773100")
        link = db_one("SELECT user_id FROM oauth_accounts WHERE provider_uid='google-link-999'")
        assert link and link["user_id"] == me["id"], "соцсеть привязана к TG-аккаунту, дубль не создан"
        # в профиле теперь «Привязан» + кнопка «Отвязать»
        prof2 = clink.get("/admin/profile").text
        assert "Привязан" in prof2
        csrf = re.search(r'name="csrf" value="([^"]+)"', prof2).group(1)
        # отвязка разрешена (есть Telegram как запасной способ входа)
        assert clink.post("/admin/profile/oauth/google/unlink",
                          data={"csrf": csrf}).status_code == 303
        assert db_one("SELECT COUNT(*) FROM oauth_accounts WHERE user_id=?", (me["id"],))[0] == 0
finally:
    _ar.httpx.Client = _real_client
    _ar.OAUTH_PROVIDERS["google"] = ("", "")
step("новое OAuth: настроенный провайдер — старт-редирект, callback заводит аккаунт без TG, дубль не плодится, поддельный state → 403")


# ---------- НОВОЕ: мелкие UI-правки (счётчики на вкладке, red/green, ⋯, авто-heal) ----------
main._rates.clear()
with TestClient(main.app, follow_redirects=False) as cui2:
    assert tg_login(cui2, 773200, username="uifix").json()["status"] == "ok"
    uc2 = re.search(r'name="csrf" value="([^"]+)"', cui2.get("/admin/categories").text).group(1)
    cui2.post("/admin/categories/create", data={**category_data("Ц"), "csrf": uc2})
    cc = db_one("SELECT id FROM categories WHERE name='Ц'")
    # #2: на главной больше НЕТ блока счётчиков/статистики
    dash = cui2.get("/admin/").text
    assert "dcount-row" not in dash and "броней сейчас" not in dash
    assert ">Создать событие</a>" in dash and "+ Создать событие" not in dash
    # #12: мобильная короткая подпись кнопки «Ссылка» на главной
    assert "lbl-short" in dash
    # Профиль: тема управляется только кнопкой в шапке, а настройка следа
    # находится внутри основной анкеты (на телефоне её скрывает CSS).
    profile = cui2.get("/admin/profile").text
    assert "theme-pick" not in profile
    assert 'name="admin_skin"' in profile
    assert "Стандартный" in profile and "Романтический" in profile
    assert "Меняет твой кабинет" not in profile
    assert "Индиго, бирюза и янтарные детали" not in profile
    assert "Авторская розово-персиковая тема" not in profile
    assert "cursor-effects-toggle" in profile and 'name="cursor_effects"' in profile
    assert "Работает только на компьютере. По умолчанию выключено" not in profile
    assert "<b>Помощь</b>" in profile and "https://t.me/artiwayn" in profile
    assert "Связаться с поддержкой" in profile and "admin-icon-telegram" in profile
    assert "tour-course-actions" in profile
    assert "Короткие подсказки по основным разделам" not in profile
    help_actions = re.search(r'<div class="tour-course-actions">(.*?)</div>', profile, re.S).group(1)
    assert "События</a>" in help_actions and "Встречи</a>" not in help_actions
    assert help_actions.index("Категории") < help_actions.index("События")
    assert 'class="social-links"' in profile and "social-service" in profile
    # Счётчики событий живут в двух статусах: Активные/Архив.
    cui2.post("/admin/dates/new", data={"csrf": uc2, "name": "Акт", "categories": str(cc["id"])})
    dpage = cui2.get("/admin/dates?view=active").text
    assert re.search(r'view=active[^>]*>Активные\s*<span class="pill">1</span>', dpage)
    assert ">Неактивные<" not in dpage
    # #4: под тумблером публичности больше нет пояснительного текста
    nf = cui2.get("/admin/dates/new").text
    assert 'class="btn ghost editor-back date-editor-back"' in nf and "← Назад" in nf
    assert "видят все пользователи в ленте на главной" not in nf
    assert 'class="capacity-stepper"' in nf and 'data-step="-1"' in nf
    assert "Создатель не считается" not in nf and "Лимит применяется" not in nf
    # Редкие действия категории спрятаны в меню ⋯ рядом с «Сохранить».
    ed = cui2.get(f"/admin/categories/{cc['id']}").text
    assert 'class="btn editor-back"' in ed and "← Назад" in ed
    assert "← Все категории" not in ed
    assert "cat-editor-menu" in ed and "Установить стандартное превью" in ed
    assert "Отключить ссылку" not in ed and "Скопировать категорию" not in ed
    assert ">Описание</label>" in ed and "Описание (необязательно)" not in ed
    assert re.search(r'id="ogWarn"[^>]*hidden', ed)
    assert 'data-tour="category-description"' in ed
    assert "cat-editor-actions" in ed and "Открыть ссылку" in ed
    assert 'data-tour="category-share-copy"' in ed
    assert 'type="datetime-local"' in ed and "data-picker-only" in ed

    # Гостевая ссылка: актуальный редактор-виджет, кнопка без плюса,
    # и все варианты оплаты действительно сохраняются сервером.
    cat_token = db_one("SELECT link_token FROM categories WHERE id=?", (cc["id"],))["link_token"]
    public_page = cui2.get(f"/c/{cat_token}").text
    assert "date-widget-dialog" in public_page and 'class="pcard editable"' in public_page
    assert public_page.count("Предложить своё событие") >= 3
    assert "Создать своё событие" not in public_page
    assert 'id="propEdTitle"' in public_page and 'aria-required="true"' in public_page
    assert '<span class="plus"' not in public_page
    assert "Создатель не считается" not in public_page
    assert 'class="capacity-stepper"' in public_page
    assert "Голосование пока не открыто" not in public_page
    assert 'data-countdown-label>До конца голосования</span>' in public_page
    assert "data-vote-countdown" in public_page
    assert "Голосование идёт до" not in public_page
    assert all(f'name="pay" value="{v}"' in public_page for v in range(4))
    proposed = cui2.post(
        f"/c/{cat_token}/propose",
        data={"name": "Гостевой модификатор", "pay": "2", "capacity": "3"},
    )
    assert proposed.status_code == 200, proposed.text
    modifier_date = db_one(
        "SELECT pay_split, capacity FROM dates WHERE id=?",
        (proposed.json()["id"],),
    )
    assert modifier_date["pay_split"] == 2 and modifier_date["capacity"] == 3

    cui2.post(f"/admin/categories/{cc['id']}/toggle", data={"csrf": uc2})   # выключаем ссылку
    ed2 = cui2.get(f"/admin/categories/{cc['id']}").text
    assert "cat-editor-menu" in ed2 and "Ссылка отключена" in ed2
    assert "Включить ссылку" not in ed2
    # В списке — одна полноширинная кнопка создания, без старого поля и
    # без служебных плашек о необходимости настроить голосование.
    cats = cui2.get("/admin/categories").text
    assert ">Создать категорию</a>" in cats
    assert "Название новой категории" not in cats
    assert "настрой голосование" not in cats.lower()
    assert 'class="card cat-card has-thumb"' in cats
    assert 'class="cat-thumb"' in cats
    assert f'/admin/categories/{cc["id"]}/og-preview?skin=' in cats
    assert "og-friends.jpg" not in cats and "og-default.jpg" not in cats
    category_new = cui2.get("/admin/categories/new").text
    assert 'action="/admin/categories/create"' in category_new
    assert "Новая категория" in category_new and "Например, Летние планы" in category_new
    assert 'class="btn editor-back category-new-back"' in category_new
    # #10: ⋯-меню идёт ПЕРЕД стрелкой (menu-wrap раньше cat-arrow)
    assert cats.index("menu-wrap") < cats.index("cat-arrow")

    # GET списка событий не чинит данные и не захватывает writer-lock.
    # Одноразовый repair легаси-строк проверяется отдельно миграционным тестом.
    stuck = db_one("SELECT id FROM dates WHERE name='Акт'")
    conn = dbm.connect()
    conn.execute("UPDATE dates SET is_draft=1 WHERE id=?", (stuck["id"],))  # искусственно «залипло»
    conn.commit(); conn.close()
    cui2.get("/admin/dates?view=active")
    assert db_one("SELECT is_draft FROM dates WHERE id=?", (stuck["id"],))[0] == 1
    conn = dbm.connect()
    conn.execute("UPDATE dates SET is_draft=0 WHERE id=?", (stuck["id"],))
    conn.commit(); conn.close()
    # Гостевое предложение с категорией тоже остаётся черновиком.
    conn = dbm.connect()
    conn.execute("INSERT INTO dates(owner_id,name,origin,is_draft,created_at) "
                 "VALUES((SELECT id FROM users WHERE telegram_id=773200),'Гостевое','guest',1,'2026-01-01T00:00:00')")
    gp = conn.execute("SELECT id FROM dates WHERE name='Гостевое'").fetchone()["id"]
    conn.execute("INSERT INTO date_categories(date_id,category_id,position) VALUES(?,?,0)", (gp, cc["id"]))
    conn.commit(); conn.close()
    cui2.get("/admin/dates?view=active")
    assert db_one("SELECT is_draft FROM dates WHERE id=?", (gp,))[0] == 1, "гостевое предложение не авто-публикуется"
step("новое UI: счётчики на вкладке «События», red/green toggle, ⋯ перед стрелкой; GET списка не пишет в БД")


# ---------- НОВОЕ: обучающий тур (spotlight) при первом заходе ----------
main._rates.clear()
with TestClient(main.app, follow_redirects=False) as ctour:
    assert tg_login(ctour, 773300, username="tourist").json()["status"] == "ok"
    dash = ctour.get("/admin/").text
    assert "tour.js" in dash, "скрипт тура подключён на главной"
    # сам файл тура отдаётся статикой с версией
    m = re.search(r'src="(/static/tour\.js[^"]*)"', dash)
    assert m, "ссылка на tour.js есть"
    tour_response = ctour.get(m.group(1))
    assert tour_response.status_code == 200
    tour_source = tour_response.text
    # Логотип VPN контентно-версионирован: старый кэшированный 404/битый ответ
    # больше не переживает деплой.
    vpn_asset = re.search(r'src="(/static/vpn-logo\.webp\?v=[^"]+)"', dash)
    assert vpn_asset, "логотип VPN подключён через asset() с хэшем"
    vpn_response = ctour.get(vpn_asset.group(1))
    assert vpn_response.status_code == 200
    assert vpn_response.headers["content-type"].startswith("image/webp")
    theme_match = re.search(r'src="(/static/theme\.js[^"]*)"', dash)
    assert theme_match, "общий скрипт темы подключён"
    theme_source = ctour.get(theme_match.group(1)).text
    assert "document.startViewTransition" in theme_source
    assert "circle(0px at " in theme_source
    assert "::view-transition-new(root)" in theme_source
    assert "prefers-reduced-motion: reduce" in theme_source
    assert "getBoundingClientRect" in theme_source
    assert "animateSkin" in theme_source and "animateAppearance" in theme_source
    assert "queuedAppearance" in theme_source
    assert "minDuration: 1050" in theme_source and "maxDuration: 1550" in theme_source
    assert 'SKIN_COOKIE = "d4y_skin"' in theme_source
    assert 'root.dataset.theme = theme' in theme_source
    assert 'root.dataset.skin = skin' in theme_source
    # Отдельных обучений списков больше нет; редактор события содержит только
    # карточку, модификаторы и публикацию.
    assert '"dates-list": [' not in tour_source
    assert '"categories-list": [' not in tour_source
    assert "Фото и видео" not in tour_source
    assert "Когда и где" not in tour_source
    assert "Сохрани результат" not in tour_source
    assert "Делись ссылкой с друзьями." in tour_source
    assert "Нужен VPN?" in tour_source
    assert "Тогда жми сюда и забирай бесплатный пробный период." in tour_source
    assert 'extra: "#communityFeed .cfeed-card:first-child"' in tour_source
    assert tour_source.index('sel: \'[data-tour="dashboard-feed"]\'') < \
        tour_source.index('sel: \'[data-tour="dashboard-share"]\'')
    assert 'sel: \'[data-tour="category-actions"]\'' in tour_source
    assert "document.documentElement.classList.add(\"tour-lock\")" in tour_source
    # Автопоказ любого курса — один раз навсегда, без повторов после смены версии.
    # Ручной запуск «Основ» переживает Turbo-переход через sessionStorage.
    assert "var VERSIONS" not in tour_source
    assert "seen[id] = true" in tour_source
    assert 'var REQUEST_KEY = "d4y_tour_request"' in tour_source
    assert "sessionStorage.setItem(REQUEST_KEY, id)" in tour_source
    assert "sessionStorage.removeItem(REQUEST_KEY)" in tour_source
    assert "start(id, true)" in tour_source
    # Мобильный тур следит за живой геометрией изменяемых редакторов, но не
    # измеряет layout бесконечно: observers запускают короткое окно стабилизации.
    # Шаг сообщества прокручивает заголовок вместе с первой карточкой.
    assert "function desiredScrollTop(step, target, r)" in tour_source
    assert 'extraTarget(step)' in tour_source
    assert '"ResizeObserver" in window' in tour_source
    assert '"MutationObserver" in window' in tour_source
    assert "geometryFramesLeft = Math.max" in tour_source
    assert "if (geometryFramesLeft > 0) geometryFrame = requestAnimationFrame(watchGeometry)" in tour_source
    assert 'sel: \'[data-tour="category-share-copy"]\'' not in tour_source
    # Ручной повтор курса не связывает «Основы» с редакторами цепочкой.
    assert "function continuation" not in tour_source
step("новое: туры остались только в редакторах; лишние шаги события удалены")


print(f"\nВсе проверки пройдены: {OK} блоков ✔")
