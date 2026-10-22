"""
Finance Relay — локальный сторож для macOS.

Читает уведомления/сообщения, которые уже приходят на этот Mac с айфона,
фильтрует только финансовое (через parser.py), отбрасывает коды/OTP и
дописывает события в finance_events.jsonl — тот же файл, что читает Hermes.

Источники:
  * Messages.app  ~/Library/Messages/chat.db
        сюда попадают СМС с айфона (Пересылка сообщений), в т.ч. Сбер/900.
  * Центр уведомлений  ~/Library/Group Containers/group.com.apple.usernoted/db2/db
        сюда попадают всплывающие уведомления приложений.

ВАЖНО: процессу, который это запускает (Terminal / python / launchd),
нужен Full Disk Access — иначе macOS вернёт 'authorization denied'.
См. README, раздел про iPhone + Mac.

Запуск:
    python3 mac_watch.py --once      # один проход (для cron/launchd)
    python3 mac_watch.py --loop 30   # опрос каждые 30 секунд
    python3 mac_watch.py --once --all-senders   # не только банки (для отладки)
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from parser import parse  # переиспользуем регексовый парсер

HOME = Path.home()
CHAT_DB = HOME / "Library/Messages/chat.db"
NOTE_DB = HOME / "Library/Group Containers/group.com.apple.usernoted/db2/db"

DATA_DIR = Path(os.environ.get("FINANCE_RELAY_DIR", Path(__file__).parent / "data"))
JSONL_PATH = DATA_DIR / "finance_events.jsonl"
STATE_PATH = DATA_DIR / "watch_state.json"
DEDUP_PATH = DATA_DIR / "watch_seen.json"

# Отправители, которых считаем банками/госами. Регистр не важен, ищем подстроку.
# Пустой список + --all-senders = обрабатывать всех (парсер всё равно отфильтрует).
BANK_SENDERS = re.compile(
    r"(900|sber|сбер|tinkoff|t-?bank|т-?банк|тинькофф|alfa|альфа|vtb|втб|gosuslugi"
    r"|госуслуг|nalog|фнс|fssp|ozon|озон|900|9respect)",
    re.IGNORECASE,
)

# Второй рубеж против кодов/OTP (первый — внутри parser.parse).
SECRET = re.compile(
    r"(никому не сообщайте|код подтверждени|одноразов|\botp\b|\bcvv\b|\bcvc\b|код для входа)",
    re.IGNORECASE,
)

MAC_EPOCH = 978307200  # 2001-01-01 UTC в unix-секундах


# --- Состояние (курсоры, дедуп) ---------------------------------------------

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False))
    tmp.replace(path)


# --- Извлечение текста из Messages ------------------------------------------

def _extract_attributed_body(blob: bytes) -> Optional[str]:
    """
    На новых macOS текст сообщения лежит не в message.text, а в attributedBody
    (сериализованный NSAttributedString). Достаём читаемую строку эвристикой.
    """
    if not blob:
        return None
    try:
        s = blob.decode("utf-8", errors="ignore")
    except Exception:
        return None
    # После маркера NSString обычно идёт длина и сам текст.
    m = re.search(r"NSString\x94\x84\x01\+(.*?)\x86", s, re.DOTALL)
    if m:
        return m.group(1).strip("\x00").strip() or None
    # Фолбэк: самый длинный кусок печатных символов.
    chunks = re.findall(r"[ -~ -￿]{6,}", s)
    chunks = [c for c in chunks if not c.startswith(("NS", "__k", "street", "$class"))]
    return max(chunks, key=len).strip() if chunks else None


def read_new_messages(since_rowid: int, all_senders: bool) -> tuple[list[tuple[int, str, str, float]], int]:
    """Возвращает (список (rowid, sender, text, unix_ts), max_rowid)."""
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out: list[tuple[int, str, str, float]] = []
    max_rowid = since_rowid
    try:
        rows = con.execute(
            """
            SELECT m.ROWID AS rowid, m.text AS text, m.attributedBody AS body,
                   m.date AS date, m.is_from_me AS from_me, h.id AS sender
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID > ?
            ORDER BY m.ROWID ASC
            """,
            (since_rowid,),
        ).fetchall()
    finally:
        con.close()

    for r in rows:
        max_rowid = max(max_rowid, r["rowid"])
        if r["from_me"]:
            continue
        sender = r["sender"] or ""
        if not all_senders and not BANK_SENDERS.search(sender):
            continue
        text = r["text"] or _extract_attributed_body(r["body"])
        if not text:
            continue
        # date: наносекунды с 2001 (новые macOS) или секунды (старые)
        raw = r["date"] or 0
        ts = MAC_EPOCH + (raw / 1e9 if raw > 1e11 else raw)
        out.append((r["rowid"], sender, text, ts))
    return out, max_rowid


# --- Извлечение из Центра уведомлений ---------------------------------------

def read_notifications(seen_ids: set[str], all_senders: bool) -> list[tuple[str, str, str, float]]:
    """Возвращает список (uid, app, text, unix_ts) новых уведомлений."""
    con = sqlite3.connect(f"file:{NOTE_DB}?mode=ro", uri=True)
    out: list[tuple[str, str, str, float]] = []
    try:
        # Схема: record(rec_id, app_id, uuid, data, delivered_date); app(app_id, identifier)
        rows = con.execute(
            """
            SELECT r.rec_id AS rec_id, r.data AS data, r.delivered_date AS d,
                   a.identifier AS app
            FROM record r LEFT JOIN app a ON r.app_id = a.app_id
            """
        ).fetchall()
    except Exception:
        con.close()
        return out
    con.close()

    for rec_id, data, d, app in rows:
        uid = f"note:{rec_id}"
        if uid in seen_ids:
            continue
        app = app or ""
        if not all_senders and not BANK_SENDERS.search(app):
            continue
        title, body = _parse_note_plist(data)
        text = ". ".join(filter(None, [title, body]))
        if not text:
            continue
        ts = MAC_EPOCH + (d or 0)
        out.append((uid, app, text, ts))
    return out


def _parse_note_plist(data) -> tuple[Optional[str], Optional[str]]:
    if not data:
        return None, None
    try:
        pl = plistlib.loads(bytes(data))
    except Exception:
        return None, None
    req = pl.get("req", pl) if isinstance(pl, dict) else {}
    if not isinstance(req, dict):
        return None, None
    return req.get("titl"), req.get("body") or req.get("subt")


# --- Запись событий ----------------------------------------------------------

def emit(text: str, sender: str, source_type: str, ts: float, dedup: set[str]) -> Optional[dict]:
    if SECRET.search(text):
        return None
    key = f"{source_type}|{sender}|{text[:80]}|{int(ts // 60)}"
    if key in dedup:
        return None
    ev = parse(text, source_app=sender, source_type=source_type, device_id="mac-mini")
    if ev is None:
        return None
    ev["received_at"] = datetime.now(timezone.utc).isoformat()
    ev["timestamp"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    dedup.add(key)
    with JSONL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


# --- Основной проход ---------------------------------------------------------

def run_once(all_senders: bool) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state = _load_json(STATE_PATH, {"chat_rowid": 0})
    dedup = set(_load_json(DEDUP_PATH, []))
    count = 0

    if CHAT_DB.exists():
        try:
            msgs, max_rowid = read_new_messages(state.get("chat_rowid", 0), all_senders)
            for _, sender, text, ts in msgs:
                ev = emit(text, sender, "sms", ts, dedup)
                if ev:
                    count += 1
                    print(f"NEW sms  {ev['bank']}/{ev['event_type']} {ev['amount']} {ev.get('merchant') or ''}", flush=True)
            state["chat_rowid"] = max_rowid
        except sqlite3.OperationalError as e:
            print(f"[chat.db] {e} — нужен Full Disk Access (см. README)", flush=True)

    if NOTE_DB.exists():
        try:
            notes = read_notifications(dedup, all_senders)
            for uid, app, text, ts in notes:
                ev = emit(text, app, "push", ts, dedup)
                if ev:
                    count += 1
                    print(f"NEW push {ev['bank']}/{ev['event_type']} {ev['amount']} {ev.get('merchant') or ''}", flush=True)
                dedup.add(uid)
        except sqlite3.OperationalError as e:
            print(f"[usernoted] {e} — нужен Full Disk Access", flush=True)

    # Держим дедуп-множество в разумных пределах.
    _save_json(DEDUP_PATH, list(dedup)[-2000:])
    _save_json(STATE_PATH, state)
    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="один проход и выход")
    ap.add_argument("--loop", type=int, metavar="SEC", help="опрашивать каждые SEC секунд")
    ap.add_argument("--all-senders", action="store_true", help="не ограничивать банками (отладка)")
    args = ap.parse_args()

    if args.loop:
        print(f"watching every {args.loop}s → {JSONL_PATH}", flush=True)
        while True:
            run_once(args.all_senders)
            time.sleep(args.loop)
    else:
        n = run_once(args.all_senders)
        print(f"done, {n} new event(s) → {JSONL_PATH}", flush=True)


if __name__ == "__main__":
    main()
