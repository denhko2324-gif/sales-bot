#!/usr/bin/env python3
"""Версия бота для GitHub Actions: запускается по расписанию, делает всё за один проход и завершается.

За один запуск:
  1. читает новые команды боту (/today, /month, /check, /start, /chatid) и отвечает на них;
  2. смотрит выдачу поиска по ❌ в канале и запоминает новые проданные позиции;
  3. после REPORT_HOUR:00 по Китаю (один раз в сутки) присылает итог дня и список новых проданных;
  4. сохраняет состояние в state.json (его коммитит workflow).
"""
import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import bot  # парсер и тексты сообщений из bot.py (нужен DB_PATH=:memory:, см. workflow)

STATE_FILE = os.environ.get("STATE_FILE", "state.json")


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
    """Посты с ❌ через поиск по каналу (самые свежие по номеру поста), по pages страниц."""
    out, before = [], None
    for _ in range(pages):
        url = f"https://t.me/s/{bot.CHANNEL}?q={urllib.parse.quote(bot.SOLD)}" + (f"&before={before}" if before else "")
        got = bot.parse_page(bot.fetch(url))
        if not got:
            break
        out += got
        before = min(p.id for p in got)
        time.sleep(1)
    return out


def check_new(st, posts=None):
    """Ищет новые ❌ среди постов из поиска. Первый запуск только запоминает базу."""
    if posts is None:
        posts = scan_sold_pages(bot.SCAN_PAGES)
    now = datetime.now(timezone.utc).isoformat()
    first = not st.get("init_sold")
    added = 0
    for p in posts:
        for title, price in bot.sold_items(p):
            key = f"{p.id}:{title}"
            if key in st["sold"]:
                continue
            st["sold"][key] = {"post_id": p.id, "title": title, "price": price,
                               "posted": p.posted.isoformat() if p.posted else None,
                               "detected": None if first else now, "reported": first}
            added += 1
    st["init_sold"] = True
    print(f"проверка: постов с ❌ прочитано {len(posts)}, новых позиций {0 if first else added}"
          f"{' (первый запуск, база сохранена)' if first else ''}", flush=True)
    return 0 if first else added


def build_daily(st, now=None):
    now = now or datetime.now(bot.TZ)
    rows = [(v["post_id"], v["title"], v["price"]) for v in sorted(
        (v for v in st["sold"].values() if not v["reported"]), key=lambda v: v["detected"] or "")]
    for v in st["sold"].values():
        v["reported"] = True
    day = now.strftime("%d.%m.%Y")
    if not rows:
        return f"Статистика @{bot.CHANNEL} за {day}\nНовых проданных позиций (❌) не появилось."
    lines, total, unknown = bot.fmt_items(rows)
    head = (f"Статистика @{bot.CHANNEL} за {day}\nПродано позиций: {len(rows)}, "
            f"сумма по ценам из постов: {bot.money(total)}")
    if unknown:
        head += f" (у {unknown} цену не удалось определить)"
    return head + "\n\nНовые проданные:\n" + "\n".join(lines)


def build_today(st, now=None):
    now = now or datetime.now(bot.TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    rows = [(v["post_id"], v["title"], v["price"]) for v in sorted(
        (v for v in st["sold"].values() if v["detected"] and datetime.fromisoformat(v["detected"]) >= start),
        key=lambda v: v["detected"])]
    if not rows:
        return "Сегодня новых проданных позиций бот пока не заметил."
    lines, total, _ = bot.fmt_items(rows)
    return f"Сегодня замечено проданных: {len(rows)}, сумма: {bot.money(total)}\n\n" + "\n".join(lines)


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
                           f"чтобы получать статистику.")
            continue
        if bot.ALLOWED and chat not in bot.ALLOWED:
            continue
        if cmd == "/today":
            bot.send(chat, build_today(st))
        elif cmd == "/month":
            bot.send(chat, bot.build_month())
        elif cmd == "/check":
            n = check_new(st)
            bot.send(chat, f"Проверка выполнена, новых проданных позиций: {n}")


def main():
    if not bot.TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    st = load_state()
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
