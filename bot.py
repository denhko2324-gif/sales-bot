#!/usr/bin/env python3
"""Бот статистики продаж канала по метке ❌ в описании поста.

Что делает:
  * каждые CHECK_MINUTES минут смотрит свежие посты канала (публичная страница t.me/s/<канал>)
    и запоминает, у каких постов появился ❌ (товар продан);
  * каждый день в REPORT_HOUR:00 по Китаю присылает итог за день и список новых проданных;
  * команды: /today (продано сегодня), /month (за последние 30 дней), /chatid.

Только стандартная библиотека Python 3.9+. Настройки берутся из переменных окружения (см. .env.example).
"""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser

TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL = os.environ.get("CHANNEL", "rusclothing")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x}
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "21"))
CHECK_MINUTES = int(os.environ.get("CHECK_MINUTES", "30"))
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "6"))  # страниц свежих постов на каждую проверку
DB_PATH = os.environ.get("DB_PATH", "sales.db")
TZ = timezone(timedelta(hours=8))  # Китай, UTC+8
SOLD = "❌"  # ❌

API = f"https://api.telegram.org/bot{TOKEN}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; sales-stats-bot)"}


# ---------- разбор страницы канала ----------
class Post:
    def __init__(self, post_id):
        self.id = post_id
        self.text = ""
        self.posted = None  # datetime (UTC)
        self.views = ""


class PageParser(HTMLParser):
    """Достаёт из t.me/s/<канал> id поста, текст с переносами строк, дату публикации и просмотры."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self._cur = None
        self._depth = 0          # глубина div внутри сообщения
        self._in_text = 0        # глубина div текста
        self._in_views = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "") or ""
        if tag == "div" and "tgme_widget_message" in cls.split() and a.get("data-post"):
            pid = a["data-post"].split("/")[-1]
            if pid.isdigit():
                self._cur = Post(int(pid))
                self.posts.append(self._cur)
                self._depth = 1
                return
        if self._cur is None:
            return
        if tag == "div":
            self._depth += 1
            if self._in_text:
                self._in_text += 1
            elif "tgme_widget_message_text" in cls.split():
                self._in_text = 1
        elif tag == "br" and self._in_text:
            self._cur.text += "\n"
        elif tag == "time" and a.get("datetime") and self._cur.posted is None:
            try:
                self._cur.posted = datetime.fromisoformat(a["datetime"]).astimezone(timezone.utc)
            except ValueError:
                pass
        elif tag == "img" and self._in_text and a.get("alt"):  # эмодзи могут быть картинками
            self._cur.text += a["alt"]
        elif tag == "span" and "tgme_widget_message_views" in cls.split():
            self._in_views = True

    def handle_endtag(self, tag):
        if self._cur is None:
            return
        if tag == "div":
            if self._in_text:
                self._in_text -= 1
            self._depth -= 1
            if self._depth <= 0:
                self._cur = None
        elif tag == "span":
            self._in_views = False

    def handle_data(self, data):
        if self._cur is None:
            return
        if self._in_text:
            self._cur.text += data
        elif self._in_views:
            self._cur.views += data.strip()


def parse_page(html):
    p = PageParser()
    p.feed(html)
    # альбомы приходят отдельными сообщениями: оставляем только те, у которых есть текст
    return [x for x in p.posts if x.text.strip()]


PRICE_RE = re.compile(r"(\d[\d ., ]*\d|\d)\s*(?:₽|р\.?(?![а-яa-z])|руб)", re.I)


def parse_price(s):
    m = PRICE_RE.search(s)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def sold_items(post):
    """Список проданных позиций поста: [(название, цена)].
    Если пост из одной позиции и ❌ стоит где угодно в тексте — берём весь пост.
    Если позиций несколько (строки с ценами) — берём только строки с ❌."""
    lines = [l.strip() for l in post.text.splitlines() if l.strip()]
    priced = [l for l in lines if parse_price(l) is not None]
    if SOLD not in post.text:
        return []
    if len(priced) > 1:
        out = []
        for l in priced:
            if SOLD in l:
                t = PRICE_RE.sub("", l.replace(SOLD, ""))
                title = re.sub(r"\s+", " ", t).strip(" -–—:")
                out.append((title, parse_price(l)))
        if out:
            return out
    first = PRICE_RE.sub("", lines[0].replace(SOLD, "")) if lines else ""
    title = re.sub(r"\s+", " ", first).strip(" -–—:") or f"пост {post.id}"
    return [(title, next((parse_price(l) for l in priced), None))]


# ---------- загрузка ----------
def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def scan_recent(pages):
    """Свежие посты канала (без поиска), страница за страницей назад."""
    posts, before = [], None
    for _ in range(pages):
        url = f"https://t.me/s/{CHANNEL}" + (f"?before={before}" if before else "")
        got = parse_page(fetch(url))
        if not got:
            break
        posts += got
        before = min(p.id for p in got)
        time.sleep(1)
    return posts


def scan_sold_since(days):
    """Посты с ❌, опубликованные за последние days дней (через поиск по каналу)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out, before = [], None
    for _ in range(60):
        url = f"https://t.me/s/{CHANNEL}?q={urllib.parse.quote(SOLD)}" + (f"&before={before}" if before else "")
        got = parse_page(fetch(url))
        if not got:
            break
        for p in got:
            if p.posted and p.posted < since:
                return out
            out.append(p)
        before = min(p.id for p in got)
        time.sleep(1)
    return out


# ---------- база ----------
db_lock = threading.Lock()


def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS sold(
        post_id INTEGER, idx INTEGER, title TEXT, price INTEGER,
        posted TEXT, detected TEXT, reported INTEGER DEFAULT 0, PRIMARY KEY(post_id, idx))""")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return c


conn = db()


def check_new():
    """Ищет новые ❌ среди свежих постов. Возвращает число новых позиций."""
    posts = scan_recent(SCAN_PAGES)
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        first_run = conn.execute("SELECT v FROM meta WHERE k='init'").fetchone() is None
        added = 0
        for p in posts:
            for i, (title, price) in enumerate(sold_items(p)):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO sold(post_id, idx, title, price, posted, detected, reported) VALUES (?,?,?,?,?,?,?)",
                    (p.id, i, title, price, p.posted.isoformat() if p.posted else None, None if first_run else now, 1 if first_run else 0))
                added += cur.rowcount
        if first_run:  # первый запуск: уже проданное не считаем «новым»
            conn.execute("INSERT INTO meta VALUES ('init', ?)", (now,))
        conn.commit()
    return 0 if first_run else added


# ---------- сообщения ----------
def money(n):
    return f"{n:,}".replace(",", " ") + " ₽"


def fmt_items(rows):
    lines, total, unknown = [], 0, 0
    for post_id, title, price in rows:
        link = f"https://t.me/{CHANNEL}/{post_id}"
        if price:
            total += price
            lines.append(f"• {title} — {money(price)}\n  {link}")
        else:
            unknown += 1
            lines.append(f"• {title} — цена не найдена\n  {link}")
    return lines, total, unknown


def build_daily():
    with db_lock:
        rows = conn.execute(
            "SELECT post_id, title, price FROM sold WHERE reported=0 ORDER BY detected").fetchall()
        conn.execute("UPDATE sold SET reported=1 WHERE reported=0")
        conn.commit()
    day = datetime.now(TZ).strftime("%d.%m.%Y")
    if not rows:
        return f"Статистика @{CHANNEL} за {day}\nНовых проданных позиций (❌) не появилось."
    lines, total, unknown = fmt_items(rows)
    head = f"Статистика @{CHANNEL} за {day}\nПродано позиций: {len(rows)}, сумма по ценам из постов: {money(total)}"
    if unknown:
        head += f" (у {unknown} цену не удалось определить)"
    return head + "\n\nНовые проданные:\n" + "\n".join(lines)


def build_today():
    start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    with db_lock:
        rows = conn.execute(
            "SELECT post_id, title, price FROM sold WHERE detected>=? ORDER BY detected", (start,)).fetchall()
    if not rows:
        return "Сегодня новых проданных позиций бот пока не заметил."
    lines, total, unknown = fmt_items(rows)
    return (f"Сегодня замечено проданных: {len(rows)}, сумма: {money(total)}\n\n" + "\n".join(lines))


def build_month():
    posts = scan_sold_since(30)
    rows = []
    for p in posts:
        for title, price in sold_items(p):
            rows.append((p.id, title, price))
    if not rows:
        return "За последние 30 дней проданных позиций с ❌ не найдено."
    total = sum(r[2] for r in rows if r[2])
    unknown = sum(1 for r in rows if not r[2])
    brands = {}
    for _, title, price in rows:
        key = " ".join(title.split()[:2]) if title else "?"
        b = brands.setdefault(key, [0, 0])
        b[0] += 1
        b[1] += price or 0
    top = sorted(brands.items(), key=lambda kv: -kv[1][0])[:10]
    head = (f"Проданные за 30 дней (по дате публикации поста): {len(rows)} поз., "
            f"сумма по ценам из постов: {money(total)}")
    if unknown:
        head += f"; без цены: {unknown}"
    body = "\n".join(f"• {k}: {v[0]} шт., {money(v[1])}" for k, v in top)
    return head + "\n\nЧаще всего (первые слова названия):\n" + body


# ---------- Telegram ----------
def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def send(chat_id, text):
    for i in range(0, len(text), 3800):  # лимит Telegram ~4096 символов
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 3800], disable_web_page_preview="true")


def commands_loop():
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=30)
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                text = (msg.get("text") or "").strip().lower()
                chat = (msg.get("chat") or {}).get("id")
                if not chat or not text.startswith("/"):
                    continue
                cmd = text.split()[0].split("@")[0]
                if cmd in ("/start", "/chatid"):
                    send(chat, f"Ваш chat id: {chat}\nДобавьте его в ALLOWED_CHAT_IDS, чтобы получать статистику.")
                    continue
                if ALLOWED and chat not in ALLOWED:
                    continue
                if cmd == "/today":
                    send(chat, build_today())
                elif cmd == "/month":
                    send(chat, "Собираю данные за 30 дней, это займёт около минуты…")
                    send(chat, build_month())
                elif cmd == "/check":
                    n = check_new()
                    send(chat, f"Проверка выполнена, новых проданных позиций: {n}")
        except Exception as e:  # сеть может мигать, просто пробуем снова
            print("commands_loop:", e, flush=True)
            time.sleep(10)


def scheduler_loop():
    last_report_day = None
    last_check = 0.0
    while True:
        try:
            now = datetime.now(TZ)
            if time.time() - last_check >= CHECK_MINUTES * 60:
                last_check = time.time()
                print(f"[{now:%d.%m %H:%M}] проверка, новых: {check_new()}", flush=True)
            if now.hour == REPORT_HOUR and last_report_day != now.date():
                check_new()
                text = build_daily()
                for chat in ALLOWED:
                    send(chat, text)
                last_report_day = now.date()
        except Exception as e:
            print("scheduler_loop:", e, flush=True)
        time.sleep(30)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    threading.Thread(target=scheduler_loop, daemon=True).start()
    print("Бот запущен", flush=True)
    commands_loop()
