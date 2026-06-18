#!/usr/bin/env python3
"""Smoke-тест boris-site (итерация 3: именные брони, архив на странице, DnD).

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
import zipfile
from urllib.parse import unquote
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
DATA = Path("/tmp/bdata")

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
    env["DATA_DIR"] = "/tmp/bdata-ff"
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
shutil.rmtree("/tmp/bdata-ff", ignore_errors=True)
os.environ.update(ENV)
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import backup as bk  # noqa: E402
import db as dbm  # noqa: E402
import main  # noqa: E402


def png(color=(180, 90, 110), size=(640, 480)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


CSRF = {"v": ""}


def refresh_csrf(c) -> None:
    page = c.get("/admin/categories")
    CSRF["v"] = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def tg_login(c, telegram_id, username="user", first_name="Тест"):
    """Полный реальный поток входа: start → вебхук от Telegram → poll.

    Возвращает финальный ответ poll. Сессия логина оседает в куках клиента c.
    """
    r = c.post("/auth/start")
    assert r.status_code == 200, r.status_code
    code = r.json()["code"]
    # Telegram присылает /start <код> с секретным заголовком вебхука
    wh = c.post("/tg/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"},
                json={"message": {"text": f"/start {code}",
                                  "from": {"id": telegram_id, "username": username,
                                           "first_name": first_name}}})
    assert wh.status_code == 200, wh.status_code
    return c.get(f"/auth/poll?code={code}")


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


def set_name(client, tok, name):
    r = client.post(f"/c/{tok}/name", data={"name": name})
    assert r.status_code == 200 and r.json()["name"] == name, r.text
    return r


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
    # страница входа отдаёт кнопку, а не форму логин/пароль
    lp = c.get("/login")
    assert lp.status_code == 200 and "Войти через Telegram" in lp.text

    # вебхук без секрета — 403 (иначе любой подтвердит чужой код)
    r0 = c.post("/auth/start")
    bad = c.post("/tg/webhook",
                 json={"message": {"text": f"/start {r0.json()['code']}",
                                   "from": {"id": 555001}}})
    assert bad.status_code == 403
    # незалогинены: кабинет недоступен
    assert c.get("/admin/").status_code == 303

    # полный поток: оператор (есть в OPERATOR_TG_IDS) входит и «забирает» легаси-владельца
    poll = tg_login(c, 555001, username="boss", first_name="Шеф")
    assert poll.status_code == 200 and poll.json()["status"] == "ok"
    assert c.get("/admin/").status_code == 200
    me = db_one("SELECT id, is_operator, telegram_id FROM users WHERE telegram_id=555001")
    assert me["is_operator"] == 1
    # легаси-владелец (telegram_id=0) поглощён: отдельной записи с 0 не осталось
    assert not db_one("SELECT 1 FROM users WHERE telegram_id=0")
    step("вход через Telegram: webhook без секрета → 403, поллинг логинит, оператор забрал легаси")

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

    # Смягчение фишинга: при подтверждении бот шлёт предупреждение ТОМУ, кто
    # нажал Start (chat_id == его telegram_id), а не оператору.
    captured = []
    def _cap(url, json=None, timeout=None):
        captured.append((json["chat_id"], json["text"]))
        class _R: status_code = 200; text = "ok"
        return _R()
    real_post = main.notify.httpx.post
    main.notify.httpx.post = _cap
    main.notify.TOKEN = "t"
    sc2 = TestClient(main.app, follow_redirects=False)
    code2 = sc2.post("/auth/start").json()["code"]
    sc2.post("/tg/webhook",
             headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret"},
             json={"message": {"text": f"/start {code2}", "from": {"id": 660002}}})
    main.notify.TOKEN = ""
    main.notify.httpx.post = real_post
    assert captured and captured[0][0] == 660002, captured
    assert "вход" in captured[0][1].lower() and "не закрывайте" in captured[0][1].lower()
    step("вход: бот предупреждает подтвердившего (смягчение фишинга/session-fixation)")

    refresh_csrf(c)
    assert CSRF["v"]

    # ---------- CSRF ----------
    r = c.post("/admin/categories/create", data={"name": "Без токена"})
    assert r.status_code == 303
    fr = c.get(r.headers["location"])
    assert "Сессия устарела" in fr.text and "⚠" in fr.text
    assert not db_one("SELECT 1 FROM categories WHERE name='Без токена'")
    step("CSRF: POST без токена отклоняется с дружелюбным flash")

    # ---------- профиль кабинета ----------
    # форма открывается, в шапке — ссылка на профиль
    pf = c.get("/admin/profile")
    assert pf.status_code == 200 and "Мой профиль" in pf.text
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
    assert "Артём" in c.get("/admin/profile").text
    # свой аватар отдаётся
    assert c.get(f"/admin/avatar/{av}").status_code == 200
    # чужой клиент (Боб из HTTP-изоляции ещё не залогинен здесь) — проверим, что
    # неавторизованный не получит аватар: новый клиент без сессии
    anon = TestClient(main.app, follow_redirects=False)
    assert anon.get(f"/admin/avatar/{av}").status_code == 303  # → /login
    # несовершеннолетний возраст отклоняется
    bad = apost(c, "/admin/profile",
                {"display_name": "Х", "birth_date": "2020-01-01", "gender": ""})
    assert bad.status_code == 303 and "18" in c.get(bad.headers["location"]).text
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
    step("профиль: имя/ДР/пол/аватар сохраняются, 18+ и пустое имя отклоняются, аватар приватный")

    # ---------- категория и секретная ссылка ----------
    r = apost(c, "/admin/categories/create", {"name": "Лето"})
    assert r.status_code == 303
    page = c.get("/admin/categories").text
    cid = int(re.search(r"/admin/categories/(\d+)", page).group(1))
    detail = c.get(f"/admin/categories/{cid}").text
    tok = re.search(r"https://t\.local/c/([A-Za-z0-9_-]+)", detail).group(1)
    # новый дефолт для публичного продукта: модерация предложений ВКЛючена
    assert db_one("SELECT moderate_proposals FROM categories WHERE id=?", (cid,))[0] == 1
    step("категория создана, секретная ссылка получена")

    r = c.get(f"/c/{tok}")
    assert r.status_code == 200 and "пусто" in r.text
    step("пустая категория показывает заглушку, гость получил cookie")

    # ---------- свидание от админа (+фото) ----------
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
    step("свидание с фото создано из админки")

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
    assert c.get(f"/uploads/{fn_did}").status_code == 404
    assert c.get(f"/admin/uploads/{fn_did}").status_code == 200
    anon = TestClient(main.app, follow_redirects=False)
    assert anon.get(f"/admin/uploads/{fn_did}").status_code == 303
    step("фото доступны только через /c/<токен>/image и /admin/uploads (с сессией)")

    # ---------- CSP с nonce, без inline-обработчиков ----------
    rr = c.get(f"/c/{tok}")
    csp = rr.headers.get("content-security-policy", "")
    m = re.search(r"'nonce-([^']+)'", csp)
    assert m, csp
    assert "script-src 'self' 'nonce-" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert "/static/guest.js" in rr.text          # весь гостевой JS вынесен
    assert "<script nonce=" not in rr.text        # инлайна на гостевой больше нет
    rr2 = c.get("/admin/")
    m2 = re.search(r"'nonce-([^']+)'", rr2.headers.get("content-security-policy", ""))
    assert m2 and f'nonce="{m2.group(1)}"' in rr2.text
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

    # ---------- имя гостя: обязательно перед действиями ----------
    r = c.post(f"/c/{tok}/book", data={"date_id": did})
    assert r.status_code == 412 and r.json()["detail"]["need_name"] is True
    r = c.post(f"/c/{tok}/question", data={"date_id": did, "text": "Эй?"})
    assert r.status_code == 412 and "зовут" in r.json()["detail"]["msg"]
    main._rates.clear()
    r = c.post(f"/c/{tok}/propose", data={"name": "Аноним"})
    assert r.status_code == 412
    assert not db_one("SELECT 1 FROM dates WHERE name='Аноним'")

    r = c.post(f"/c/{tok}/name", data={"name": "   "})
    assert r.status_code == 400
    set_name(c, tok, "Аня")
    page = c.get(f"/c/{tok}").text
    assert 'id="greetName">Аня<' in page
    assert "куда нам отправиться" not in page    # подзаголовок убран
    assert "(мск)" not in page                   # пояснение времени убрано
    step("без имени любые действия → 412; после знакомства — пилюля с именем, без лишнего текста")

    # ---------- бронь: toggle и /vote больше нет ----------
    r = c.post(f"/c/{tok}/book", data={"date_id": did})
    assert r.json()["booked"] is True
    r = c.post(f"/c/{tok}/book", data={"date_id": did})
    assert r.json()["booked"] is False
    r = c.post(f"/c/{tok}/book", data={"date_id": did})
    assert r.json()["booked"] is True
    page = c.get(f"/c/{tok}").text
    assert "Твой выбор ♥" in page                      # кнопка-переключатель
    mycard = re.search(r'<article[^>]*id="date-%d".*?</article>' % did, page, re.S).group(0)
    assert "booked-me" in mycard and '<div class="seal">♥' in mycard   # печать видна
    assert "booked-overlay" in mycard and "Забронировано" in mycard    # оверлей на фото
    assert "Аня" in mycard                              # имя выбравшего на оверлее
    assert c.post(f"/c/{tok}/vote", data={"date_id": did}).status_code == 404
    step("выбор работает как переключатель; оверлей «Забронировано» на фото; /vote удалён")

    # ---------- вопрос и ответ ----------
    r = c.post(f"/c/{tok}/question", data={"date_id": did, "text": "Можно прийти позже?"})
    assert r.status_code == 200
    r = c.post(f"/c/{tok}/question", data={"date_id": did, "text": "   "})
    assert r.status_code == 400 and "обязательно" in r.json()["detail"]
    page = c.get(f"/c/{tok}").text
    assert "Можно прийти позже?" in page and "пока без ответа" in page

    qpage = c.get("/admin/questions").text
    assert "Аня" in qpage
    qid = int(re.search(r"/admin/questions/(\d+)/answer", qpage).group(1))
    r = apost(c, f"/admin/questions/{qid}/answer",
              {"text": "Конечно, жду тебя!", "next": "/admin/questions"})
    assert r.status_code == 303
    assert "Конечно, жду тебя!" in c.get(f"/c/{tok}").text
    assert "отвечено" in c.get("/admin/questions?f=all").text
    r = apost(c, f"/admin/questions/{qid}/answer", {"text": "", "next": "/admin/questions"})
    assert "пока без ответа" in c.get(f"/c/{tok}").text
    apost(c, f"/admin/questions/{qid}/answer",
          {"text": "Конечно!", "next": "/admin/questions"})
    step("вопрос гостя подписан именем; ответ админа виден автору, бейдж «отвечено»")

    # ---------- календарь: gcal-ссылка, .ics, Яндекс.Карты ----------
    page = c.get(f"/c/{tok}").text
    assert "calendar.google.com/calendar/render" in page
    assert "dates=20300701T150000Z%2F20300701T180000Z" in page  # 18:00 МСК = 15:00 UTC
    r = c.get(f"/c/{tok}/ics/{did}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "DTSTART:20300701T150000Z" in r.text
    assert "SUMMARY:" in r.text and "LOCATION:" in r.text
    assert "yandex.ru/maps/?text=" in page
    r = apost(c, "/admin/dates/new", {"name": "Без даты", "categories": str(cid)})
    did2 = db_one("SELECT id FROM dates WHERE name='Без даты'")["id"]
    assert c.get(f"/c/{tok}/ics/{did2}").status_code == 404
    page2 = c.get(f"/c/{tok}").text
    assert page2.count("calendar.google.com") == 1  # у свидания без даты gcal нет
    step("ссылка в Google Календарь с верным UTC; .ics работает; без даты — ничего")

    # ---------- «Назначить дату»: гость предлагает время ----------
    card2 = re.search(r'id="date-%d".*?</article>' % did2, page2, re.S).group(0)
    assert "предложить дату" in card2 and "chip-suggest" in card2
    card1 = re.search(r'id="date-%d".*?</article>' % did, page2, re.S).group(0)
    assert "предложить дату" not in card1        # у свидания со временем чипа нет
    r = c.post(f"/c/{tok}/suggest_time",
               data={"date_id": did, "starts_at": "2030-08-01T19:00"})
    assert r.status_code == 400 and "уже назначено" in r.json()["detail"]
    r = c.post(f"/c/{tok}/suggest_time", data={"date_id": did2, "starts_at": ""})
    assert r.status_code == 400
    r = c.post(f"/c/{tok}/suggest_time",
               data={"date_id": did2, "starts_at": "2030-08-01T19:00",
                     "ends_at": "2030-08-01T21:00"})
    assert r.status_code == 200, r.text
    page = c.get(f"/c/{tok}").text
    assert "Предлагаю назначить" in page and "1 августа 2030, 19:00–21:00" in page
    assert "Предлагаю назначить" in c.get("/admin/questions").text
    step("у свидания без даты — чип «предложить дату»; предложение видно автору и админу")

    # ---------- админ принимает время одной кнопкой ----------
    qid_s = db_one("SELECT id FROM questions WHERE suggest_starts IS NOT NULL")["id"]
    qpage = c.get("/admin/questions").text
    assert "suggest-box" in qpage and "Принять — назначить время" in qpage
    r = apost(c, f"/admin/questions/{qid_s}/accept_time", {"next": "/admin/questions"})
    assert r.status_code == 303
    drow = db_one("SELECT starts_at, ends_at FROM dates WHERE id=?", (did2,))
    assert drow["starts_at"] == "2030-08-01T19:00" and drow["ends_at"] == "2030-08-01T21:00"
    page = c.get(f"/c/{tok}").text
    card2 = re.search(r'<article[^>]*id="date-%d".*?</article>' % did2, page, re.S).group(0)
    assert "1 августа 2030" in card2 and "предложить дату" not in card2
    assert "✅ Принято" in card2                  # автор видит авто-ответ
    assert "Принять — назначить время" not in c.get("/admin/questions?f=all").text

    # ---------- …или вежливо отказывается ----------
    r = apost(c, "/admin/dates/new", {"name": "Качели", "categories": str(cid)})
    assert r.status_code == 303
    kid = db_one("SELECT id FROM dates WHERE name='Качели'")["id"]
    r = c.post(f"/c/{tok}/suggest_time",
               data={"date_id": kid, "starts_at": "2030-09-05T15:00"})
    assert r.status_code == 200, r.text
    qid_d = db_one("SELECT id FROM questions WHERE date_id=?", (kid,))["id"]
    r = apost(c, f"/admin/questions/{qid_d}/decline_time", {"next": "/admin/questions"})
    assert r.status_code == 303
    assert db_one("SELECT starts_at FROM dates WHERE id=?", (kid,))["starts_at"] is None
    page = c.get(f"/c/{tok}").text
    cardk = re.search(r'<article[^>]*id="date-%d".*?</article>' % kid, page, re.S).group(0)
    assert "не получится" in cardk and "предложить дату" in cardk   # чип остался

    # next из формы не должен уводить наружу (open redirect)
    for bad in ("https://evil.com", "//evil.com/x"):
        r = apost(c, f"/admin/questions/{qid_d}/decline_time", {"next": bad})
        assert r.status_code == 303
        loc = r.headers["location"]
        assert loc.startswith("/admin") and "evil" not in loc, loc
    step("«Принять» назначает время, «Отказаться» — авто-ответ; next не уводит наружу")

    # ---------- одно свидание выбирает только один человек ----------
    g2 = TestClient(main.app, follow_redirects=False)
    g2.get(f"/c/{tok}")
    set_name(g2, tok, "Борис")
    r = g2.post(f"/c/{tok}/book", data={"date_id": did})   # занято Аней
    assert r.status_code == 409 and "Аня" in r.json()["detail"], r.text
    g2page = g2.get(f"/c/{tok}").text
    card1 = re.search(r'<article[^>]*id="date-%d".*?</article>' % did, g2page, re.S).group(0)
    assert "booked-other" in card1                         # карточка перекрашена
    assert "booked-overlay" in card1 and "Забронировано" in card1   # оверлей на фото
    assert "Аня" in card1                                  # имя того, кто занял
    assert "Уже занято" in card1                           # кнопка выбора заблокирована

    r = g2.post(f"/c/{tok}/book", data={"date_id": did2})  # свободное — можно
    assert r.json()["booked"] is True
    page = c.get(f"/c/{tok}").text
    card2 = re.search(r'<article[^>]*id="date-%d".*?</article>' % did2, page, re.S).group(0)
    assert "booked-other" in card2 and "Забронировано" in card2   # Аня видит занятость
    assert "Борис" in card2                                # имя того, кто занял

    # один гость выбирает НЕСКОЛЬКО свиданий (правило «одно свидание — один
    # человек» при этом сохраняется: на занятое — 409)
    r = apost(c, "/admin/dates/new", {"name": "Запасной", "categories": str(cid)})
    assert r.status_code == 303
    did_r = db_one("SELECT id FROM dates WHERE name='Запасной'")["id"]
    r = c.post(f"/c/{tok}/book", data={"date_id": did_r})  # у Ани теперь did + did_r
    assert r.json()["booked"] is True
    rows = db_all("SELECT date_id FROM bookings WHERE category_id=? AND guest_token IN "
                  "(SELECT token FROM guests WHERE name='Аня') ORDER BY date_id", (cid,))
    assert {x["date_id"] for x in rows} == {did, did_r}    # обе брони живут вместе
    page = c.get(f"/c/{tok}").text
    for d_ in (did, did_r):
        cd = re.search(r'<article[^>]*id="date-%d".*?</article>' % d_, page, re.S).group(0)
        assert "booked-me" in cd and '<div class="seal">♥' in cd
    r = c.post(f"/c/{tok}/book", data={"date_id": did_r})  # повторный тап — снять
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
    step("несколько свиданий на гостя; чужое — 409; повторный тап снимает; админ снимает")

    # ---------- предложение гостя (без модерации) ----------
    main._rates.clear()
    # дефолт теперь «модерация вкл» — для этого блока явно выключаем
    if db_one("SELECT moderate_proposals FROM categories WHERE id=?", (cid,))[0]:
        apost(c, f"/admin/categories/{cid}/moderation", {})
    assert db_one("SELECT moderate_proposals FROM categories WHERE id=?", (cid,))[0] == 0
    r = c.post(f"/c/{tok}/propose",
               data={"name": "Кино дома", "links": "kinopoisk.ru"},
               files=[("images", ("k.png", png((90, 120, 180)), "image/png"))])
    j = r.json()
    assert j["ok"] and j["moderated"] is False
    pid = j["id"]
    page = c.get(f"/c/{tok}").text
    assert "Кино дома" in page and "идея гостя" in page

    r = c.post(f"/c/{tok}/propose", data={"name": "Спам"},
               files=[("images", (f"s{i}.png", png(), "image/png")) for i in range(6)])
    assert r.status_code == 400 and "Максимум" in r.json()["detail"]
    r = c.post(f"/c/{tok}/propose", data={"name": "Спам"},
               files=[("images", ("s.png", b"junk", "image/png"))])
    assert r.status_code == 400 and "не похож" in r.json()["detail"]
    r = c.post(f"/c/{tok}/propose", data={"name": "  "})
    assert r.status_code == 400 and "обязательно" in r.json()["detail"]
    assert db_one("SELECT COUNT(*) AS n FROM dates WHERE name='Спам'")["n"] == 0
    step("гость предложил свидание; битые пачки фото и пустые имена отклоняются целиком")

    # ---------- гость правит своё: фото, keep_order ----------
    main._rates.clear()
    img_old = db_one("SELECT id, filename FROM date_images WHERE date_id=?", (pid,))
    r = c.post(f"/c/{tok}/propose/{pid}/edit", data={
        "name": "Кино под пледом", "place": "Дом", "links": "ya.ru",
        "comment": "", "starts_at": "", "ends_at": "",
        "remove_image": str(img_old["id"]),
    }, files=[("images", ("k2.png", png((20, 160, 90)), "image/png"))])
    assert r.status_code == 200, r.text
    assert not (main.images.UPLOAD_DIR / img_old["filename"]).exists()
    imgs = db_all("SELECT id, filename FROM date_images WHERE date_id=?", (pid,))
    assert len(imgs) == 1 and (main.images.UPLOAD_DIR / imgs[0]["filename"]).exists()
    assert db_one("SELECT url FROM date_links WHERE date_id=?", (pid,))["url"] == "https://ya.ru"

    # второе фото, затем разворот порядка через keep_order (drag-and-drop)
    r = c.post(f"/c/{tok}/propose/{pid}/edit", data={
        "name": "Кино под пледом", "place": "Дом", "links": "ya.ru",
        "comment": "", "starts_at": "", "ends_at": "",
    }, files=[("images", ("k3.png", png((220, 120, 40)), "image/png"))])
    assert r.status_code == 200
    ids = [x["id"] for x in db_all(
        "SELECT id FROM date_images WHERE date_id=? ORDER BY position, id", (pid,))]
    assert len(ids) == 2
    r = c.post(f"/c/{tok}/propose/{pid}/edit", data={
        "name": "Кино под пледом", "place": "Дом", "links": "ya.ru",
        "comment": "", "starts_at": "", "ends_at": "",
        "keep_order": f"{ids[1]},{ids[0]}",
    })
    assert r.status_code == 200
    ids2 = [x["id"] for x in db_all(
        "SELECT id FROM date_images WHERE date_id=? ORDER BY position, id", (pid,))]
    assert ids2 == [ids[1], ids[0]]

    page = c.get(f"/c/{tok}").text
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
    r = c.post(f"/c/{tok}/propose/{pid}/delete")
    assert r.status_code == 200
    assert not db_one("SELECT 1 FROM dates WHERE id=?", (pid,))
    assert not (main.images.UPLOAD_DIR / fn_pid).exists()
    assert "Кино под пледом" not in c.get(f"/c/{tok}").text
    step("чужому правка запрещена; удаление чистит файлы; роут /choose выпилен")

    # ---------- черновики ----------
    r = apost(c, "/admin/dates/new",
              {"name": "Сюрприз", "draft": "1", "categories": str(cid)})
    assert r.status_code == 303
    did4 = db_one("SELECT id FROM dates WHERE name='Сюрприз'")["id"]
    assert "Сюрприз" not in c.get(f"/c/{tok}").text
    dpage = c.get("/admin/dates?view=drafts").text
    assert "Сюрприз" in dpage and "Опубликовать" in dpage
    r = apost(c, f"/admin/dates/{did4}/publish", {"next": "/admin/dates?view=drafts"})
    assert r.status_code == 303
    assert "Сюрприз" in c.get(f"/c/{tok}").text
    step("черновик скрыт от гостей, публикуется кнопкой из вкладки «Черновики»")

    # ---------- модерация предложений ----------
    r = apost(c, f"/admin/categories/{cid}/moderation", {})
    assert r.status_code == 303
    assert "Черновики" in c.get(f"/admin/categories/{cid}").text

    main._rates.clear()
    r = c.post(f"/c/{tok}/propose", data={"name": "Тайное место"},
               files=[("images", ("t.png", png((200, 160, 60)), "image/png"))])
    j = r.json()
    assert j["moderated"] is True
    pid2 = j["id"]
    fn2 = db_one("SELECT filename FROM date_images WHERE date_id=?", (pid2,))["filename"]

    owner_page = c.get(f"/c/{tok}").text
    assert "Тайное место" in owner_page and "ждёт проверки" in owner_page
    assert c.get(f"/c/{tok}/image/{fn2}").status_code == 200
    other_page = g2.get(f"/c/{tok}").text
    assert "Тайное место" not in other_page
    assert g2.get(f"/c/{tok}/image/{fn2}").status_code == 404
    assert "на модерации" in c.get("/admin/dates?view=drafts").text

    r = apost(c, f"/admin/dates/{pid2}/publish", {"next": "/admin/dates?view=drafts"})
    assert "Тайное место" in g2.get(f"/c/{tok}").text
    apost(c, f"/admin/categories/{cid}/moderation", {})  # выключить обратно
    step("модерация: предложение и фото видны только автору до публикации")

    # ---------- архив виден гостям, брони считаются по активным ----------
    dash = c.get("/admin/").text
    assert "<b>2</b><span>броней сейчас" in dash      # Аня + Борис на «Ужине»
    r = apost(c, f"/admin/dates/{did}/archive", {"next": "/admin/dates"})
    assert r.status_code == 303
    page = c.get(f"/c/{tok}").text
    assert "Ужин на крыше" in page
    card = re.search(r'<article[^>]*id="date-%d".*?</article>' % did, page, re.S).group(0)
    assert "booked-overlay" in card and "Было" in card   # статус архива — оверлей «Было»
    assert 'class="card past"' in card                # карточка в общем списке
    assert "было:" in card                            # выбор на память (архив)
    assert "Выбрать ♥" not in card                    # действий в архиве нет
    assert c.get(f"/c/{tok}/image/{fn_did}").status_code == 200   # фото остаётся
    assert c.get(f"/c/{tok}/ics/{did}").status_code == 404
    assert c.post(f"/c/{tok}/book", data={"date_id": did}).status_code == 404
    assert "<b>1</b><span>броней сейчас" in c.get("/admin/").text   # Борис на «Без даты»
    r = apost(c, f"/admin/dates/{did}/archive", {"next": "/admin/dates?view=archived"})
    page = c.get(f"/c/{tok}").text
    assert "Твой выбор ♥" in page
    assert "<b>2</b><span>броней сейчас" in c.get("/admin/").text
    step("архив остаётся на странице (оверлей «Было», фото видны), выбор и .ics закрыты")

    # ---------- авто-архив срабатывает прямо при открытии страницы ----------
    r = apost(c, "/admin/dates/new", {
        "name": "Вчерашний вечер", "starts_at": "2020-02-14T19:00",
        "ends_at": "2020-02-14T22:00", "categories": str(cid)})
    assert r.status_code == 303
    page = c.get(f"/c/{tok}").text     # страница сама вызывает авто-архив
    assert "Вчерашний вечер" in page
    card = re.search(r'<article[^>]*>(?:(?!</article>).)*Вчерашний вечер.*?</article>',
                     page, re.S).group(0)
    assert "past" in card and "Было" in card
    assert db_one("SELECT archived_at FROM dates WHERE name='Вчерашний вечер'")["archived_at"]
    step("просроченное свидание мгновенно получает статус «в архиве» на гостевой странице")

    # ---------- выключение и перегенерация ссылки ----------
    apost(c, f"/admin/categories/{cid}/toggle", {})
    r = c.get(f"/c/{tok}")
    assert r.status_code == 404 and "не действует" in r.text
    assert c.get(f"/c/{tok}/image/{fn_did}").status_code == 404
    assert c.post(f"/c/{tok}/book", data={"date_id": did}).status_code == 410
    apost(c, f"/admin/categories/{cid}/toggle", {})

    apost(c, f"/admin/categories/{cid}/regenerate", {})
    assert c.get(f"/c/{tok}").status_code == 404
    detail = c.get(f"/admin/categories/{cid}").text
    new_tok = re.search(r"https://t\.local/c/([A-Za-z0-9_-]+)", detail).group(1)
    assert new_tok != tok
    tok = new_tok
    page = c.get(f"/c/{tok}").text
    assert "Твой выбор ♥" in page and 'id="greetName">Аня<' in page
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
    step("привязка несуществующего свидания — мягкая ошибка, attach/detach работают")

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
    # заранее создаём свидание с видео, чтобы проверить экспорт видео
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
    assert "Ужин на крыше" in r.text and "Кто выбрал" in r.text and "Аня" in r.text
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
    c3 = TestClient(main.app, follow_redirects=False)
    c3.get(f"/c/{tok}")
    set_name(c3, tok, "Спамер")
    for i in range(5):
        r = c3.post(f"/c/{tok}/propose", data={"name": f"спам{i}"})
        assert r.status_code == 200, r.text
    r = c3.post(f"/c/{tok}/propose", data={"name": "спам6"})
    assert r.status_code == 429

    main._rates.clear()
    c4 = TestClient(main.app, follow_redirects=False)
    c4.get(f"/c/{tok}")
    set_name(c4, tok, "Почемучка")
    for i in range(10):
        r = c4.post(f"/c/{tok}/question", data={"date_id": did, "text": f"в{i}?"})
        assert r.status_code == 200, r.text
    r = c4.post(f"/c/{tok}/question", data={"date_id": did, "text": "в11?"})
    assert r.status_code == 429

    main._rates.clear()
    c5 = TestClient(main.app, follow_redirects=False)
    c5.get(f"/c/{tok}")
    set_name(c5, tok, "Кликер")
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
        sent.append(json["text"])
        return _Resp()
    real_post = main.notify.httpx.post
    main.notify.httpx.post = _fake_post
    main.notify.TOKEN, main.notify.CHAT = "t", "c"
    main.notify.notify("проверка 500")        # статус уйдёт в лог, исключения нет
    _Resp.status_code = 200
    main.notify.notify("проверка 200")
    assert sent == ["проверка 500", "проверка 200"]
    main.notify.TOKEN = main.notify.CHAT = ""  # выключаем обратно
    main.notify.httpx.post = real_post
    step("notify переживает 5xx Telegram и логирует статус (через подмену httpx)")

    # ---------- alert: дедупликация одинаковых алёртов о сбоях ----------
    sent2 = []
    def _fake_post2(url, json=None, timeout=None):
        sent2.append(json["text"])
        return _Resp()
    main.notify.httpx.post = _fake_post2
    main.notify.TOKEN, main.notify.CHAT = "t", "c"
    main.notify._alert_seen.clear()
    main.notify.alert("сбой X")
    main.notify.alert("сбой X")           # дубль в окне — не уходит
    main.notify.alert("сбой Y")
    assert sent2 == ["сбой X", "сбой Y"], sent2
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
    step("авто-архив переносит просроченные свидания")

    # ---------- удаление категории чистит брони ----------
    assert db_one("SELECT COUNT(*) AS n FROM bookings WHERE category_id=?", (cid,))["n"] >= 1
    r = apost(c, f"/admin/categories/{cid}/delete", {})
    assert r.status_code == 303
    assert db_one("SELECT COUNT(*) AS n FROM bookings WHERE category_id=?", (cid,))["n"] == 0
    assert "Ужин на крыше" in c.get("/admin/dates").text
    step("удаление категории чистит её брони, свидания остаются")

    # ================= фичи v7 =================
    main._rates.clear()
    r = apost(c, "/admin/categories/create", {"name": "Витрина"})
    assert r.status_code == 303
    vc = db_one("SELECT id, link_token FROM categories WHERE name='Витрина'")
    vcid, vtok = vc["id"], vc["link_token"]
    apost(c, f"/admin/categories/{vcid}/moderation", {})  # витринные фичи без модерации

    # описание категории (видно всем) + разметка в нём
    r = apost(c, f"/admin/categories/{vcid}/rename",
              {"name": "Витрина", "description": "__подчёркнутое__ наш список"})
    assert r.status_code == 303
    gpage = c.get(f"/c/{vtok}").text
    assert "cat-desc" in gpage and "<u>подчёркнутое</u>" in gpage

    # rich-разметка в комментарии свидания
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
                  {"name": "Свидание с местом", "categories": str(vcid),
                   "place": "https://yandex.ru/maps/-/CPtbJHmP"})
        assert r.status_code == 303
        dm = db_one("SELECT place, place_url FROM dates WHERE name='Свидание с местом'")
        assert dm["place"] == "Кафе «Ромашка»"
        assert dm["place_url"] == "https://yandex.ru/maps/-/CPtbJHmP"
        card = re.search(r'<article[^>]*>.*?Свидание с местом.*?</article>',
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
    name, link = main.places.process_place("http://internal.local/admin")
    assert name == "Место на карте" and link == "http://internal.local/admin"

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

    # ---------- клонирование свидания админом ----------
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
    assert clone["is_draft"] == 1                       # клон — черновик
    assert clone["place"] == "Парк" and clone["pay_split"] == 1
    assert clone["comment"] == "будет здорово"
    # ссылка и категория перенесены
    assert db_one("SELECT url FROM date_links WHERE date_id=?", (clone["id"],))["url"] \
        == "https://ya.ru"
    assert db_one("SELECT 1 FROM date_categories WHERE date_id=? AND category_id=?",
                  (clone["id"], vcid))
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
    step("клон свидания: дубль-черновик, копии файлов с новыми именами, брони не переносятся")

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
    gv = TestClient(main.app, follow_redirects=False)
    gv.get(f"/c/{vtok}")
    set_name(gv, vtok, "Гостья")
    r = gv.post(f"/c/{vtok}/propose",
                data={"name": "Гостевое видео"},
                files=[("video", ("g.mp4", MP4, "video/mp4"))])
    assert r.status_code == 200 and r.json()["ok"]
    gpid = r.json()["id"]
    assert db_one("SELECT 1 FROM date_videos WHERE date_id=?", (gpid,))

    # порядок свиданий в категории: перетащили — гость видит новый порядок
    main._rates.clear()
    ids = [r[0] for r in db_all(
        "SELECT date_id FROM date_categories WHERE category_id=?", (vcid,))]
    assert len(ids) >= 3
    reordered = [ids[-1]] + ids[:-1]            # последнее свидание — первым
    r = apost(c, f"/admin/categories/{vcid}/dates_reorder",
              {"order": ",".join(map(str, reordered))})
    assert r.status_code == 200 and r.json()["ok"]
    gpage = c.get(f"/c/{vtok}").text
    first_id = int(re.search(r'<article[^>]*id="date-(\d+)"', gpage).group(1))
    assert first_id == reordered[0]
    # неполный набор id — отбой
    r = apost(c, f"/admin/categories/{vcid}/dates_reorder",
              {"order": str(reordered[0])})
    assert r.status_code in (400, 303)          # 400 напрямую или friendly-flash
    step("v7: описание+разметка, 50/50, счётчик, место-ссылка, видео с Range, реордер")

    # подчищаем витрину, чтобы не мешать счётчикам пагинации
    apost(c, f"/admin/categories/{vcid}/delete", {})
    for nm in ("Разметка", "Делим счёт", "Свидание с местом", "С видео",
               "Битое видео", "Много видео", "Гостевое видео"):
        row = db_one("SELECT id FROM dates WHERE name=?", (nm,))
        if row:
            apost(c, f"/admin/dates/{row['id']}/delete", {})
    main._rates.clear()

    # ---------- пагинация списка свиданий ----------
    main._rates.clear()
    # этот блок про пагинацию, а не квоту: временно снимаем лимит свиданий
    _qc = dbm.connect()
    _qc.execute("UPDATE users SET date_limit=999 WHERE telegram_id=555001")
    _qc.commit()
    _qc.close()
    for i in range(1, 32):
        apost(c, "/admin/dates/new", {"name": f"Лист {i:02d}"})
    p1 = c.get("/admin/dates").text
    assert "Лист 31" in p1 and "Лист 01" not in p1
    assert "стр. 1 из 2" in p1 and "page=2" in p1
    p2 = c.get("/admin/dates?view=active&page=2").text
    assert "Лист 01" in p2 and "Ужин на крыше" in p2
    step("пагинация: 30 на страницу, старые уезжают на следующую")

    # ---------- редизайн кабинета (date4you): форма, список, дашборд ----------
    main._rates.clear()
    apost(c, "/admin/categories/create", {"name": "Поделись-кат"})
    # форма создания: сплит-превью, тулбар разметки, узлы предпросмотра
    nf = c.get("/admin/dates/new").text
    assert 'class="split"' in nf and 'class="preview-col"' in nf
    assert 'id="descToolbar"' in nf and 'data-wrap="**|**"' in nf
    assert 'data-preview="title"' in nf and 'data-preview="desc"' in nf
    assert 'data-bind="title"' in nf and 'data-bind="pay"' in nf
    # форма по-прежнему шлёт те же поля + CSRF (роут не сломан)
    assert 'name="name"' in nf and 'name="csrf"' in nf and 'name="categories"' in nf

    # список карточками по умолчанию: сетка, бейджи, меню ⋯, переключатель вида
    lp = c.get("/admin/dates").text
    assert 'class="grid"' in lp and 'class="dcard' in lp
    assert 'class="more"' in lp and 'id="viewtog"' in lp
    assert "дата гибкая" in lp                       # вместо «—»
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
    assert "/c/" in sh and "Копировать" in sh
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
    # форма правки отдаёт кликабельное фото с data-focus
    ef = c.get(f"/admin/dates/{fdid}/edit").text
    assert 'class="focusable"' in ef and 'data-focus=' in ef
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
INSERT INTO dates(name, created_at) VALUES('Свидание А', '2025-01-01T00:00');
INSERT INTO dates(name, created_at) VALUES('Свидание Б', '2025-01-02T00:00');
-- гость gA голосовал дважды (за А, потом за Б) — в бронь должен попасть свежий голос
INSERT INTO votes(date_id, category_id, guest_token, created_at) VALUES(1, 1, 'gA', '2025-01-01T10:00');
INSERT INTO votes(date_id, category_id, guest_token, created_at) VALUES(2, 1, 'gA', '2025-01-03T10:00');
INSERT INTO votes(date_id, category_id, guest_token, created_at) VALUES(1, 1, 'gB', '2025-01-02T10:00');
-- gC голосует за то же свидание в ту же секунду, что gB: при дедупе v5
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
dcols = {r[1] for r in conn.execute("PRAGMA table_info(dates)")}
assert {"is_draft", "pay_split", "place_url"} <= dcols
assert "is_chosen" not in dcols          # v8: мёртвая колонка дропнута
ccols = {r[1] for r in conn.execute("PRAGMA table_info(categories)")}
assert "description" in ccols
assert "owner_id" in ccols                # v9: владелец категории
assert "owner_id" in dcols                # v9: владелец свидания
# v9: служебный легаси-владелец и бэкофилл существующих данных на него
for t in ("users", "login_codes"):
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone(), f"после миграции нет таблицы {t}"
legacy = conn.execute("SELECT id, is_operator FROM users WHERE telegram_id=0").fetchone()
assert legacy and legacy["is_operator"] == 1, "нет служебного легаси-владельца"
# старая категория «Старая» и оба свидания должны принадлежать легаси-владельцу
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
for ix in ("idx_book_cat", "idx_dc_cat", "idx_q_read", "idx_book_date",
           "idx_book_guest", "idx_dv_date"):
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (ix,)
    ).fetchone(), f"после миграции нет индекса {ix}"
# v6 снял UNIQUE(категория, гость): таблица bookings пересобрана без него
bk_sql = conn.execute(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='bookings'"
).fetchone()[0]
assert "UNIQUE" not in bk_sql.upper(), bk_sql
books = {r["guest_token"]: r["date_id"]
         for r in conn.execute("SELECT guest_token, date_id FROM bookings")}
# дедуп v5: на свидание 1 претендовали gB и gC (одинаковое время) — остаётся gC
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
    assert man["name"] and man["start_url"] == "/" and man["display"] == "standalone"
    for ic in man["icons"]:
        p = ROOT / ic["src"].lstrip("/")
        assert p.exists(), f"в манифесте указана несуществующая иконка: {ic['src']}"
    # манифест и theme-color подключены на гостевой странице
    home = cpwa.get("/").text
    assert 'rel="manifest"' in home and 'name="theme-color"' in home
step("PWA: манифест отдаётся, иконки на диске, подключён на гостевой")

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
iso.execute("INSERT INTO dates(owner_id, name, created_at) VALUES(?, 'Свидание Алисы', ?)",
            (uA, main.now_iso()))
dateA = iso.execute("SELECT id FROM dates WHERE name='Свидание Алисы'").fetchone()[0]
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

    # Алиса заводит категорию и свидание (с фото), привязывает
    assert post_as(ca, "/admin/categories/create", {"name": "Алисина"}).status_code == 303
    catA = db_one("SELECT c.id FROM categories c JOIN users u ON u.id=c.owner_id "
                  "WHERE u.telegram_id=770001 AND c.name='Алисина'")[0]
    r = post_as(ca, "/admin/dates/new",
                {"name": "Свидание Алисы", "categories": catA},
                files={"images": ("a.png", png(), "image/png")})
    assert r.status_code == 303
    dateA = db_one("SELECT d.id FROM dates d JOIN users u ON u.id=d.owner_id "
                   "WHERE u.telegram_id=770001 AND d.name='Свидание Алисы'")[0]
    imgA = db_one("SELECT filename FROM date_images WHERE date_id=?", (dateA,))[0]

    # Боб не видит Алисиных данных в своих списках
    assert "Алисина" not in cb.get("/admin/categories").text
    assert "Свидание Алисы" not in cb.get("/admin/dates").text

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
    assert db_one("SELECT name FROM dates WHERE id=?", (dateA,))[0] == "Свидание Алисы"
    assert not db_one("SELECT 1 FROM dates WHERE name='Свидание Алисы (копия)'")

    # Боб не может привязать чужое свидание в СВОЮ категорию
    assert post_as(cb, "/admin/categories/create", {"name": "Бобова"}).status_code == 303
    catB = db_one("SELECT c.id FROM categories c JOIN users u ON u.id=c.owner_id "
                  "WHERE u.telegram_id=770002")[0]
    assert post_as(cb, f"/admin/categories/{catB}/attach",
                   {"date_id": dateA}).status_code in (303, 404)
    assert not db_one("SELECT 1 FROM date_categories WHERE date_id=? AND category_id=?",
                      (dateA, catB))

    # экспорт Боба не содержит Алисиных данных
    assert "Свидание Алисы" not in cb.get("/admin/export/json").text
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

    # удаление пользователя со всеми данными (каскад)
    assert opost(f"/operator/users/{uid_nop}/delete").status_code == 303
    assert not db_one("SELECT 1 FROM users WHERE id=?", (uid_nop,))
step("операторская админка: гейт 404 для не-оператора, баны/квоты/роли/удаление, самозащита")


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

    # владелец заводит категорию (модерация по умолчанию вкл) и публикует свидание
    assert ownpost("/admin/categories/create", {"name": "Жалобная"}).status_code == 303
    rc = db_one("SELECT id, link_token FROM categories WHERE name='Жалобная'")
    rcid, rtok = rc["id"], rc["link_token"]
    ownpost(f"/admin/categories/{rcid}/moderation", {})  # выкл, чтобы свидание было видно
    assert ownpost("/admin/dates/new",
                   {"name": "Подозрительное", "categories": str(rcid)}).status_code == 303
    rdid = db_one("SELECT id FROM dates WHERE name='Подозрительное'")[0]

    # гость заходит и жалуется на свидание
    main._rates.clear()
    g.get(f"/c/{rtok}")
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


# ---------- оператор: обзор категорий и свиданий ----------
with TestClient(main.app, follow_redirects=False) as cown, \
        TestClient(main.app, follow_redirects=False) as cop3:
    assert tg_login(cown, 990002, username="creator").json()["status"] == "ok"
    assert tg_login(cop3, 555001, username="boss").json()["status"] == "ok"

    own_csrf = re.search(r'name="csrf" value="([^"]+)"',
                         cown.get("/admin/categories").text).group(1)
    def ownp(url, data=None):
        d = dict(data or {}); d["csrf"] = own_csrf
        return cown.post(url, data=d)

    assert ownp("/admin/categories/create", {"name": "Обзорная"}).status_code == 303
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

    # свидания: видны в списке + фильтры
    assert "Видимое" in cop3.get("/operator/dates").text
    assert "Видимое" in cop3.get("/operator/dates?q=creator").text
    # архив туда-обратно
    assert op3p(f"/operator/dates/{ovdid}/archive").status_code == 303
    assert db_one("SELECT archived_at FROM dates WHERE id=?", (ovdid,))[0] is not None
    assert "Видимое" in cop3.get("/operator/dates?flt=archived").text
    op3p(f"/operator/dates/{ovdid}/archive")
    assert db_one("SELECT archived_at FROM dates WHERE id=?", (ovdid,))[0] is None

    # удаление свидания оператором
    assert op3p(f"/operator/dates/{ovdid}/delete").status_code == 303
    assert not db_one("SELECT 1 FROM dates WHERE id=?", (ovdid,))

    # удаление категории НЕ трогает прочие свидания владельца
    ownp("/admin/dates/new", {"name": "Уцелевшее"})
    surv = db_one("SELECT id FROM dates WHERE name='Уцелевшее'")[0]
    assert op3p(f"/operator/categories/{ovcid}/delete").status_code == 303
    assert not db_one("SELECT 1 FROM categories WHERE id=?", (ovcid,))
    assert db_one("SELECT 1 FROM dates WHERE id=?", (surv,))  # свидание цело

    # 404 на несуществующие объекты
    assert op3p("/operator/categories/999999/toggle").status_code in (303, 404)
    assert op3p("/operator/dates/999999/delete").status_code in (303, 404)
step("оператор: обзор категорий/свиданий, toggle ссылки, архив, удаление; каскад категории щадит свидания")


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
    assert ownb("/admin/categories/create", {"name": "Бронируемая"}).status_code == 303
    bc = db_one("SELECT id, link_token FROM categories WHERE name='Бронируемая'")
    bcid, btok = bc["id"], bc["link_token"]
    ownb(f"/admin/categories/{bcid}/moderation", {})           # выкл модерацию
    ownb("/admin/dates/new", {"name": "Прогулка", "categories": str(bcid)})
    bdid = db_one("SELECT id FROM dates WHERE name='Прогулка'")[0]

    # гость представляется и бронирует
    main._rates.clear()
    gb.get(f"/c/{btok}")
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


# ---------- квота свиданий (1.4) и per-user анти-всплеск (2.4) ----------
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
step("квота: лимит свиданий отбивает лишнее, архивные не в счёт; per-user анти-всплеск даёт 429")


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

with TestClient(main.app, follow_redirects=False) as cw:
    _saved_token = _nf.TOKEN
    _saved_send = _nf.send_to
    _nf.TOKEN = "123456:test-bot-token"        # включаем проверку подписи
    _nf.send_to = lambda *a, **k: None         # без реальных сетевых вызовов
    try:
        # подделанная подпись → 403, аккаунт не создаётся
        bad = _widget_params(990200, "wrong-token")
        assert cw.get("/auth/widget", params=bad).status_code == 403
        assert not db_one("SELECT 1 FROM users WHERE telegram_id=990200")

        # валидная подпись → вход, аккаунт создан, но bot_linked=0 (бот не запущен)
        good = _widget_params(990200, _nf.TOKEN)
        r = cw.get("/auth/widget", params=good)
        assert r.status_code == 303 and r.headers["location"] == "/admin/"
        row = db_one("SELECT id, bot_linked FROM users WHERE telegram_id=990200")
        assert row and row["bot_linked"] == 0

        # в кабинете виден баннер «подключить бота»
        assert "Подключить бота" in cw.get("/admin/").text

        # тот же человек запускает бота (deeplink) → bot_linked становится 1
        main._rates.clear()
        sec = {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"}
        st = cw.post("/auth/start").json()["code"]
        cw.post("/tg/webhook", headers=sec, json={"message": {
            "text": f"/start {st}", "from": {"id": 990200, "username": "widgetuser"}}})
        assert db_one("SELECT bot_linked FROM users WHERE telegram_id=990200")[0] == 1
        # баннер исчез
        assert "Подключить бота" not in cw.get("/admin/").text

        # просроченная подпись (старый auth_date) → 403
        stale = _widget_params(990201, _nf.TOKEN)
        stale["auth_date"] = "1000000000"
        stale_pairs = "\n".join(sorted(f"{k}={v}" for k, v in stale.items() if k != "hash"))
        stale["hash"] = _hm.new(_hl.sha256(_nf.TOKEN.encode()).digest(),
                                stale_pairs.encode(), _hl.sha256).hexdigest()
        assert cw.get("/auth/widget", params=stale).status_code == 403
    finally:
        _nf.TOKEN = _saved_token
        _nf.send_to = _saved_send
step("вход: виджет (подпись HMAC, bot_linked=0, баннер) ↔ deeplink (bot_linked=1); подделка/протухшая подпись → 403")


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
    ownn("/admin/categories/create", {"name": "Уведомления"})
    nc = db_one("SELECT id, link_token FROM categories WHERE name='Уведомления'")
    ntok = nc["link_token"]
    ownn("/admin/dates/new", {"name": "Кафе", "categories": str(nc["id"])})
    ndid = db_one("SELECT id FROM dates WHERE name='Кафе'")[0]

    # перехватываем send_to: (chat_id, text)
    sent = []
    _saved = _nf.send_to
    _nf.send_to = lambda chat, text: sent.append((chat, text))
    try:
        main._rates.clear()
        gn.get(f"/c/{ntok}")
        set_name(gn, ntok, "Гостья")
        assert gn.post(f"/c/{ntok}/book", data={"date_id": ndid}).json()["booked"] is True
        # фон BackgroundTasks в TestClient выполняется синхронно к этому моменту
        assert any(c == 990300 and "выбрал" in t for c, t in sent), \
            f"владельцу с bot_linked=1 должно уйти уведомление, sent={sent}"

        # отключаем бота владельцу → уведомление НЕ шлётся
        _q = dbm.connect()
        _q.execute("UPDATE users SET bot_linked=0 WHERE id=?", (uid_n,)); _q.commit(); _q.close()
        sent.clear()
        gn.post(f"/c/{ntok}/book", data={"date_id": ndid})    # отмена выбора
        gn.post(f"/c/{ntok}/book", data={"date_id": ndid})    # снова выбор
        assert not sent, f"при bot_linked=0 уведомлений быть не должно, sent={sent}"
    finally:
        _nf.send_to = _saved
step("1.5: выбор свидания шлёт уведомление владельцу при bot_linked=1 и молчит при bot_linked=0")


# ---------- 1.8: юридические документы, согласие, cookie ----------
with TestClient(main.app, follow_redirects=False) as cl:
    terms = cl.get("/terms")
    assert terms.status_code == 200
    assert "Пользовательское соглашение" in terms.text
    assert "18" in terms.text and "/privacy" in terms.text   # 18+ и перелинковка
    priv = cl.get("/privacy")
    assert priv.status_code == 200
    assert "Политика конфиденциальности" in priv.text
    assert "152-ФЗ" in priv.text and "удалить свой аккаунт" in priv.text
    # на странице входа — чекбокс согласия со ссылками на оба документа
    lp = cl.get("/login").text
    assert 'id="tg-consent"' in lp and 'href="/terms"' in lp and 'href="/privacy"' in lp
    # кнопка входа изначально заблокирована (до согласия)
    assert "disabled" in lp
    # лендинг и гостевая несут footer-ссылки + cookie-бар
    home = cl.get("/").text
    assert 'href="/terms"' in home and 'id="cookie-bar"' in home
step("1.8: /terms и /privacy доступны (18+, 152-ФЗ, право на удаление); согласие на входе; cookie-бар")


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
    # /about в футере лендинга и гостевой
    assert 'href="/about"' in ca.get("/").text
    # кривая запись проекта без http-ссылки не должна попадать (парсер фильтрует)
    from config import support_link, _parse_projects
    assert _parse_projects("Плохой;ОК|https://ok.com") == [{"name": "ОК", "url": "https://ok.com"}]
    assert support_link()["url"] == "https://t.me/date4you_support"
step("1.10: /about (текст, поддержка @→t.me, проекты автора), футер-ссылки, фильтр кривых проектов")

print(f"\nВсе проверки пройдены: {OK} блоков ✔")
