#!/usr/bin/env python3
"""Версия бота для GitHub Actions: запускается по расписанию, делает всё за один проход и завершается.

За один запуск:
  1. читает новые команды боту (/today, /month, /check, /channels, /add, /remove, /start, /chatid) и отвечает на них;
  2. смотрит выдачу поиска по ❌ в канале и запоминает новые проданные позиции;
  3. после REPORT_HOUR:00 по Китаю (один раз в сутки) присылает итог дня и список новых проданных;
  4. сохраняет состояние в state.json (его коммитит workflow).
"""
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import bot  # парсер и тексты сообщений из bot.py (нужен DB_PATH=:memory:, см. workflow)

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DEFAULT_CHANNELS = [c.strip().lstrip("@").lower() for c in os.environ.get("CHANNELS", "rusclothing,amahasla").split(",") if c.strip()]
CHANNELS = list(DEFAULT_CHANNELS)  # актуальный список; при запуске берётся из state.json (команды /add, /remove)
NAME_RE = re.compile(r"^[a-z0-9_]{4,32}$")


class _Post:
    def __init__(self, p, text):
        self.id, self.text, self.posted = p.id, text, p.posted


def sold_of(p):
    """Проданные позиции поста: ❌ или хештег #продано (на @amahasla)."""
    if bot.SOLD not in p.text and "#продано" in p.text.lower():
        p = _Post(p, bot.SOLD + " " + p.text)
    return bot.sold_items(p)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"init": False, "offset": 0, "last_report_day": None, "sold": {}}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def scan_sold_pages(pages):
    """Посты с ❌ через поиск по каналу (bot.CHANNEL), по pages страниц."""
    out, before = [], None
    for _ in range(pages):
        url = f"https://t.me/s/{bot.CHANNEL}?q={urllib.parse.quote(bot.SOLD)}" + (f"&before={before}" if before else "")
        got = bot.parse_page(bot.fetch(url))
        print(f"страница поиска: {url} -> постов {len(got)}", flush=True)
        if not got:
            break
        out += got
        before = min(p.id for p in got)
        time.sleep(1)
    return out


def scan_channel(ch):
    bot.CHANNEL = ch
    posts, seen = [], set()
    for p in scan_sold_pages(bot.SCAN_PAGES) + bot.scan_recent(int(os.environ.get("RECENT_PAGES", "40"))):
        if p.id not in seen:
            seen.add(p.id)
            posts.append(p)
    return posts


def check_new(st, posts_by_channel=None):
    """Ищет новые проданные среди постов каналов. Новый канал в первый раз только запоминает базу."""
    now = datetime.now(timezone.utc).isoformat()
    inited = st.setdefault("init_channels", [])
    total_added = 0
    for ch in CHANNELS:
        try:
            posts = posts_by_channel[ch] if posts_by_channel else scan_channel(ch)
        except Exception as e:
            print(f"канал @{ch}: ошибка чтения: {e}", flush=True)
            continue
        first = ch not in inited
        added = 0
        for p in posts:
            for title, price in sold_of(p):
                key = f"{ch}:{p.id}:{title}"
                if key in st["sold"]:
                    continue
                st["sold"][key] = {"channel": ch, "post_id": p.id, "title": title, "price": price,
                                   "posted": p.posted.isoformat() if p.posted else None,
                                   "detected": None if first else now, "reported": first}
                added += 1
        inited.append(ch) if first else None
        print(f"проверка @{ch}: постов прочитано {len(posts)}, новых позиций {0 if first else added}"
              f"{' (первый запуск канала, база сохранена)' if first else ''}", flush=True)
        total_added += 0 if first else added
    st["init_sold"] = True
    return total_added


def fmt_rows(rows):
    """rows: [(channel, post_id, title, price)] -> текст со списком по каналам."""
    total, unknown, parts = 0, 0, []
    for ch in CHANNELS:
        mine = [r for r in rows if r[0] == ch]
        if not mine:
            continue
        lines, sub = [], 0
        for _, pid, title, price in mine:
            link = f"https://t.me/{ch}/{pid}"
            if price:
                sub += price
                lines.append(f"• {title} — {bot.money(price)}\n  {link}")
            else:
                unknown += 1
                lines.append(f"• {title} — цена не найдена\n  {link}")
        total += sub
        parts.append(f"@{ch}: {len(mine)} поз., {bot.money(sub)}\n" + "\n".join(lines))
    return "\n\n".join(parts), total, unknown


def _row(v):
    return (v.get("channel", CHANNELS[0]), v["post_id"], v["title"], v["price"])


def build_daily(st, now=None):
    now = now or datetime.now(bot.TZ)
    rows = [_row(v) for v in sorted(
        (v for v in st["sold"].values() if not v["reported"]), key=lambda v: v["detected"] or "")]
    for v in st["sold"].values():
        v["reported"] = True
    day = now.strftime("%d.%m.%Y")
    names = ", ".join("@" + c for c in CHANNELS)
    if not rows:
        return f"Статистика {names} за {day}\nНовых проданных позиций не появилось."
    body, total, unknown = fmt_rows(rows)
    head = f"Статистика {names} за {day}\nПродано позиций: {len(rows)}, сумма по ценам из постов: {bot.money(total)}"
    if unknown:
        head += f" (у {unknown} цену не удалось определить)"
    return head + "\n\nНовые проданные:\n" + body


def build_today(st, now=None):
    now = now or datetime.now(bot.TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    rows = [_row(v) for v in sorted(
        (v for v in st["sold"].values() if v["detected"] and datetime.fromisoformat(v["detected"]) >= start),
        key=lambda v: v["detected"])]
    if not rows:
        return "Сегодня новых проданных позиций бот пока не заметил."
    body, total, _ = fmt_rows(rows)
    return f"Сегодня замечено проданных: {len(rows)}, сумма: {bot.money(total)}\n\n" + body


def build_month_all():
    out = []
    for ch in CHANNELS:
        bot.CHANNEL = ch
        try:
            out.append(f"=== @{ch} ===\n" + bot.build_month())
        except Exception as e:
            out.append(f"=== @{ch} ===\nне удалось прочитать: {e}")
    return "\n\n".join(out)


def maybe_report(st, now=None):
    """Отчёт раз в сутки: после REPORT_HOUR по Китаю (если запуск GitHub задержался, отправим при следующем)."""
    now = now or datetime.now(bot.TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour >= bot.REPORT_HOUR and st.get("last_report_day") != today:
        if not bot.ALLOWED:
            print("отчёт пропущен: не задан ALLOWED_CHAT_IDS", flush=True)
            return
        text = build_daily(st, now)
        ok = False
        for chat in bot.ALLOWED:
            try:
                bot.send(chat, text)
                ok = True
            except Exception as e:
                print(f"не удалось отправить отчёт в чат {chat}: {e}", flush=True)
        if ok:
            st["last_report_day"] = today
            print("ежедневный отчёт отправлен", flush=True)


def handle_commands(st):
    res = bot.tg("getUpdates", offset=st["offset"], timeout=0)
    for u in res.get("result", []):
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        chat = (msg.get("chat") or {}).get("id")
        if not chat or not text.startswith("/"):
            continue
        cmd = text.split()[0].split("@")[0]
        if cmd in ("/start", "/chatid"):
            bot.send(chat, f"Ваш chat id: {chat}\nДобавьте его в секрет ALLOWED_CHAT_IDS на GitHub, "
                           f"чтобы получать статистику.\nКоманды: /today /month /check /channels /add @канал /remove @канал")
            continue
        if bot.ALLOWED and chat not in bot.ALLOWED:
            continue
        arg = text.split()[1].lstrip("@") if len(text.split()) > 1 else ""
        if cmd == "/channels":
            bot.send(chat, "Отслеживаю каналы: " + ", ".join("@" + c for c in CHANNELS))
        elif cmd == "/add":
            if not NAME_RE.match(arg):
                bot.send(chat, "Напишите так: /add @имя_канала")
            elif arg in CHANNELS:
                bot.send(chat, f"@{arg} уже в списке.")
            else:
                try:
                    ok = "tgme_widget_message" in bot.fetch(f"https://t.me/s/{arg}")
                except Exception:
                    ok = False
                if not ok:
                    bot.send(chat, f"Не удалось прочитать @{arg}: канал должен быть публичным и с открытой веб-лентой.")
                else:
                    CHANNELS.append(arg)
                    st["channels"] = list(CHANNELS)
                    bot.send(chat, f"Добавил @{arg}. Уже проданное запомню без отчёта, дальше буду присылать новые "
                                   f"(проданным считаю пост с ❌ или хештегом #продано).")
        elif cmd == "/remove":
            if arg in CHANNELS and len(CHANNELS) > 1:
                CHANNELS.remove(arg)
                st["channels"] = list(CHANNELS)
                bot.send(chat, f"Убрал @{arg}.")
            else:
                bot.send(chat, "Такого канала нет в списке (или он последний, его убрать нельзя). Список: /channels")
        elif cmd == "/today":
            bot.send(chat, build_today(st))
        elif cmd == "/month":
            bot.send(chat, build_month_all())
        elif cmd == "/check":
            n = check_new(st)
            bot.send(chat, f"Проверка выполнена, новых проданных позиций: {n}")


def main():
    if not bot.TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    st = load_state()
    CHANNELS[:] = st.get("channels") or DEFAULT_CHANNELS
    try:
        try:
            handle_commands(st)
        except Exception as e:
            print("команды:", e, flush=True)
        check_new(st)
        try:
            maybe_report(st)
        except Exception as e:
            print("отчёт:", e, flush=True)
    finally:
        save_state(st)


if __name__ == "__main__":
    main()
