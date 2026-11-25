#!/usr/bin/env python3
"""
Сповіщення про оновлення графіка погодинних відключень Львівобленерго в Telegram.

Бере дані з того ж API, яким користується сайт poweron.loe.lviv.ua
(api.loe.lviv.ua — неофіційний, недокументований, може змінитися).
Якщо графік для вашої групи з'явився або змінився, надсилає повідомлення
(і картинку графіка) у Telegram.

Тільки стандартна бібліотека Python 3.9+, нічого встановлювати не треба.

Налаштування через змінні середовища:
  TELEGRAM_BOT_TOKEN  — токен бота від @BotFather (обов'язково)
  TELEGRAM_CHAT_ID    — ваш chat id (обов'язково)
  LOE_GROUP           — ваша група/підчерга, напр. "1.1". Порожньо = всі групи.
  SEND_IMAGE          — "1" (типово) надсилати картинку графіка, "0" — лише текст
  STATE_FILE          — файл стану (типово state.json поруч зі скриптом)
  WARN_MINUTES        — за скільки хвилин до відключення попереджати (типово 30, "0" — вимкнути).
                        Працює лише коли задано LOE_GROUP.

Запуск:
  python loe_notify.py          — одна перевірка (для cron / GitHub Actions)
  python loe_notify.py --loop   — перевіряти безкінечно кожні 10 хв (для свого ПК/сервера)
  python loe_notify.py --test   — надіслати тестове повідомлення з поточним графіком
"""

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.loe.lviv.ua"
MENUS_URL = API_BASE + "/api/menus?page=1&type=photo-grafic"
# Запасні адреси (Сьогодні / Завтра), якщо основна не відповідає
FALLBACK_ITEM_URLS = [API_BASE + "/api/menu_items/238", API_BASE + "/api/menu_items/256"]

UA = "Mozilla/5.0 (personal LOE schedule notifier)"
CHECK_INTERVAL_SEC = 600

GROUP_RE = re.compile(r"Груп[аи]\s*(\d+\.\d+)\s*\.?\s*(.*)", re.IGNORECASE)
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
INTERVAL_RE = re.compile(r"з\s*(\d{1,2}):(\d{2})\s*до\s*(\d{1,2}):(\d{2})", re.IGNORECASE)


def _kyiv_tz():
    try:
        from zoneinfo import ZoneInfo
        for name in ("Europe/Kyiv", "Europe/Kiev"):
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001
                pass
    except ImportError:
        pass
    return dt.timezone(dt.timedelta(hours=2))


KYIV = _kyiv_tz()


# ---------------------------------------------------------------- HTTP ----

def http_get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/ld+json, application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(5_000_000).decode("utf-8"))


def http_get_json_retry(url, tries=3):
    last = None
    for i in range(tries):
        try:
            return http_get_json(url)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise last


# ---------------------------------------------------------- LOE data ----

def fetch_items():
    """Повертає список пунктів меню (зазвичай «Сьогодні» і «Завтра»)."""
    try:
        data = http_get_json_retry(MENUS_URL)
        members = data.get("hydra:member") or data.get("member") or (data if isinstance(data, list) else [])
        items = []
        for m in members:
            items.extend(m.get("menuItems") or [])
        if items:
            return items
    except Exception as e:  # noqa: BLE001
        print(f"[warn] основний endpoint не спрацював: {e}", file=sys.stderr)

    items = []
    for url in FALLBACK_ITEM_URLS:
        try:
            items.append(http_get_json_retry(url))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {url}: {e}", file=sys.stderr)
    return items


def html_to_lines(raw):
    if not raw:
        return []
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", raw)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ")
    return [re.sub(r"\s+", " ", ln).strip() for ln in t.split("\n") if ln.strip()]


def parse_item(item):
    """Витягує дату графіка, «станом на», рядки груп і картинку з одного пункту."""
    lines = html_to_lines(item.get("rawHtml") or item.get("rawMobileHtml") or "")
    sched_date, as_of, groups = None, None, {}
    for ln in lines:
        g = GROUP_RE.search(ln)
        if g:
            groups[g.group(1)] = g.group(2).strip()
            continue
        low = ln.lower()
        if "станом на" in low:
            as_of = ln.split("станом на", 1)[-1].strip() if "станом на" in ln else ln
        elif sched_date is None and DATE_RE.search(ln):
            sched_date = DATE_RE.search(ln).group(1)
    img = item.get("imageUrl") or ""
    if img and not img.startswith("http"):
        img = API_BASE + ("" if img.startswith("/") else "/") + img
    if img and urllib.parse.urlparse(img).hostname != "api.loe.lviv.ua":
        img = ""  # не довіряємо картинкам з інших доменів
    return {
        "name": item.get("name") or "",
        "date": sched_date,
        "as_of": as_of,
        "groups": groups,
        "image": img,
    }


def fingerprint(parsed, group):
    """Хеш лише змістовної частини: дата + рядок(и) групи.
    «Станом на» сюди не входить, щоб не спамити при перепублікації без змін."""
    if group:
        body = parsed["groups"].get(group, "")
    else:
        body = json.dumps(parsed["groups"], ensure_ascii=False, sort_keys=True)
    if not body and not parsed["groups"]:
        # Тексту немає — орієнтуємося на картинку
        body = "img:" + parsed["image"]
    return hashlib.sha256(f"{parsed['date']}|{body}".encode()).hexdigest()[:16]


# ---------------------------------------------------------- Telegram ----

def tg_call(token, method, params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {method}: {e.code} {e.read().decode(errors='replace')}") from None


def tg_notify(token, chat_id, text, image_url=None):
    if image_url and len(text) <= 1000:
        try:
            tg_call(token, "sendPhoto", {"chat_id": chat_id, "photo": image_url, "caption": text, "parse_mode": "HTML"})
            return
        except RuntimeError as e:
            print(f"[warn] картинку не вдалося надіслати: {e}", file=sys.stderr)
    tg_call(token, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"})


def build_message(parsed, group, is_new):
    head = "🆕 <b>Новий графік</b>" if is_new else "🔄 <b>Графік змінено</b>"
    title = f"{head} на {parsed['date'] or parsed['name']}"
    out = [f"⚡ {title}"]
    if parsed["as_of"]:
        out.append(f"<i>станом на {html.escape(parsed['as_of'])}</i>")
    out.append("")
    if group:
        line = parsed["groups"].get(group)
        if line:
            out.append(f"<b>Група {group}:</b> {html.escape(line)}")
        else:
            out.append(f"Для групи {group} у тексті графіка нічого не знайдено — дивіться картинку.")
    elif parsed["groups"]:
        for g, txt in sorted(parsed["groups"].items(), key=lambda kv: [int(x) for x in kv[0].split(".")]):
            out.append(f"<b>{g}:</b> {html.escape(txt)}")
    else:
        out.append("Текстового графіка немає — дивіться картинку.")
    out.append("")
    out.append('<a href="https://poweron.loe.lviv.ua/">poweron.loe.lviv.ua</a>')
    return "\n".join(out)


# ---------------------------------------------------------- warnings ----

def outage_intervals(parsed_items, group):
    """Усі відключення групи як [(початок, кінець)] у київському часі.
    Суміжні відрізки (напр. 20:00–24:00 і завтра 00:00–04:00) зливаються в один."""
    raw = []
    for p in parsed_items:
        line = p["groups"].get(group) or ""
        if not p["date"] or not line:
            continue
        try:
            day = dt.datetime.strptime(p["date"], "%d.%m.%Y").replace(tzinfo=KYIV)
        except ValueError:
            continue
        for h1, m1, h2, m2 in INTERVAL_RE.findall(line):
            start = day + dt.timedelta(hours=int(h1), minutes=int(m1))
            end = day + dt.timedelta(hours=int(h2), minutes=int(m2))  # 24:00 → наступна доба
            if end > start:
                raw.append((start, end))
    raw.sort()
    merged = []
    for s, e in raw:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def fmt_hm(t, day_start):
    """24:00 замість 00:00 наступного дня, якщо так читається природніше."""
    if t.hour == 0 and t.minute == 0 and t.date() > day_start.date():
        return "24:00"
    if t.date() > day_start.date():
        return t.strftime("%H:%M") + " (завтра)"
    return t.strftime("%H:%M")


def check_warnings(cfg, parsed_items, state, now=None):
    """Надсилає попередження за WARN_MINUTES до початку відключення. Повертає True, якщо щось надіслано."""
    lead = cfg["warn_minutes"]
    if not cfg["group"] or lead <= 0:
        return False
    now = now or dt.datetime.now(KYIV)
    warned = state.setdefault("_warned", {})
    sent = False
    for start, end in outage_intervals(parsed_items, cfg["group"]):
        mins_left = (start - now).total_seconds() / 60
        key = start.strftime("%Y-%m-%d %H:%M")
        if not (0 < mins_left <= lead) or key in warned:
            continue
        mins = max(1, round(mins_left))
        text = (f"⚠️ <b>Через {mins} хв вимкнуть світло</b>\n\n"
                f"Група {cfg['group']}: з {start.strftime('%H:%M')} до {fmt_hm(end, start)}\n"
                "Заряди телефон, павербанк і ноутбук 🔋")
        tg_notify(cfg["token"], cfg["chat_id"], text)
        print(f"[ok] попередження про {key} надіслано")
        warned[key] = now.strftime("%Y-%m-%d %H:%M")
        sent = True
    return sent


# ------------------------------------------------------------- state ----

def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(path, state):
    warned = state.pop("_warned", {})
    # графіки: залишаємо лише останні 10 записів
    keys = list(state.keys())[-10:]
    state = {k: state[k] for k in keys}
    # попередження: лише за останні 3 доби
    cutoff = (dt.datetime.now(KYIV) - dt.timedelta(days=3)).strftime("%Y-%m-%d")
    warned = {k: v for k, v in warned.items() if k[:10] >= cutoff}
    if warned:
        state["_warned"] = warned
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


# -------------------------------------------------------------- main ----

def check_once(cfg, force=False):
    items = fetch_items()
    if not items:
        print("[info] даних від LOE немає (або API недоступне)")
        return
    state = load_state(cfg["state_file"])
    changed = False
    parsed_items = []
    for item in items:
        p = parse_item(item)
        if not p["groups"] and not p["image"]:
            continue  # порожній пункт — графіка немає
        parsed_items.append(p)
        key = p["date"] or p["name"] or "unknown"
        fp = fingerprint(p, cfg["group"])
        old = state.get(key)
        if old == fp and not force:
            print(f"[info] {key}: без змін")
            continue
        msg = build_message(p, cfg["group"], is_new=old is None)
        tg_notify(cfg["token"], cfg["chat_id"], msg, p["image"] if cfg["send_image"] else None)
        print(f"[ok] {key}: надіслано")
        state[key] = fp
        changed = True
    try:
        if check_warnings(cfg, parsed_items, state):
            changed = True
    except Exception as e:  # noqa: BLE001
        print(f"[error] попередження: {e}", file=sys.stderr)
    if changed:
        save_state(cfg["state_file"], state)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        sys.exit("Задайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID (див. README).")
    cfg = {
        "token": token,
        "chat_id": chat_id,
        "group": os.environ.get("LOE_GROUP", "").strip(),
        "send_image": os.environ.get("SEND_IMAGE", "1").strip() != "0",
        "state_file": os.environ.get("STATE_FILE") or str(Path(__file__).with_name("state.json")),
        "warn_minutes": int(os.environ.get("WARN_MINUTES", "30").strip() or "30"),
    }
    if "--test" in sys.argv:
        check_once(cfg, force=True)
    elif "--loop" in sys.argv:
        while True:
            try:
                check_once(cfg)
            except Exception as e:  # noqa: BLE001
                print(f"[error] {e}", file=sys.stderr)
            time.sleep(CHECK_INTERVAL_SEC)
    else:
        check_once(cfg)


if __name__ == "__main__":
    main()
