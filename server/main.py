"""
Finance Relay — локальный приёмник финансовых событий.

Принимает нормализованный JSON от Android-приложения (или ручной пересылки),
проверяет Bearer-токен и дописывает событие в finance_events.jsonl.
Hermes читает этот файл.

Запуск:
    pip install -r requirements.txt
    export FINANCE_RELAY_TOKEN="придумай-длинный-секрет"
    uvicorn main:app --host 0.0.0.0 --port 8788

Никаких банковских логинов/паролей здесь нет и быть не должно.
Сервер только принимает готовые события и складывает их в файл.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

# --- Конфигурация -----------------------------------------------------------

TOKEN = os.environ.get("FINANCE_RELAY_TOKEN", "")
DATA_DIR = Path(os.environ.get("FINANCE_RELAY_DIR", Path(__file__).parent / "data"))
JSONL_PATH = DATA_DIR / "finance_events.jsonl"
DB_PATH = DATA_DIR / "finance_events.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)

_write_lock = threading.Lock()

# Слова, которые не должны попадать в сохранённые события даже случайно.
# Второй рубеж обороны — приложение уже должно было отбросить такие уведомления.
_SECRET_PATTERNS = [
    re.compile(r"\b\d{4,8}\b\s*[-—]?\s*код", re.IGNORECASE),
    re.compile(r"\bкод\b.{0,12}\b\d{3,8}\b", re.IGNORECASE),
    re.compile(r"\b(otp|one[\s-]?time)\b", re.IGNORECASE),
    re.compile(r"никому не сообщайте", re.IGNORECASE),
    re.compile(r"\bпароль\b", re.IGNORECASE),
    re.compile(r"\bcvv\b|\bcvc\b", re.IGNORECASE),
]


# --- Модель события ----------------------------------------------------------

class FinanceEvent(BaseModel):
    event_id: str
    timestamp: str
    source_type: str                      # push | sms | manual
    source_app: Optional[str] = None
    package_name: Optional[str] = None
    bank: Optional[str] = None            # sber | tbank | gosuslugi | unknown
    event_type: Optional[str] = None      # expense | income | transfer | balance | debt | tax | fine | refund | unknown
    amount: Optional[float] = None
    currency: Optional[str] = None
    account_hint: Optional[str] = None    # только вида ****1234
    merchant: Optional[str] = None
    balance_after: Optional[float] = None
    debt_amount: Optional[float] = None
    deadline: Optional[str] = None
    raw_title: Optional[str] = None
    raw_text: Optional[str] = None
    confidence: float = 0.0
    device_id: Optional[str] = None
    received_at: Optional[str] = Field(default=None)


# --- Хранилище ---------------------------------------------------------------

def _init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id     TEXT PRIMARY KEY,
            timestamp    TEXT,
            received_at  TEXT,
            source_type  TEXT,
            bank         TEXT,
            event_type   TEXT,
            amount       REAL,
            currency     TEXT,
            account_hint TEXT,
            merchant     TEXT,
            balance_after REAL,
            debt_amount  REAL,
            deadline     TEXT,
            payload      TEXT
        )
        """
    )
    con.commit()
    con.close()


def _looks_like_secret(event: FinanceEvent) -> bool:
    blob = " ".join(filter(None, [event.raw_title, event.raw_text]))
    return any(p.search(blob) for p in _SECRET_PATTERNS)


def _store(event: FinanceEvent) -> bool:
    """Возвращает False, если событие — дубль (по event_id)."""
    payload = event.model_dump()
    with _write_lock:
        con = sqlite3.connect(DB_PATH)
        try:
            cur = con.execute("SELECT 1 FROM events WHERE event_id = ?", (event.event_id,))
            if cur.fetchone():
                return False
            con.execute(
                """
                INSERT INTO events (event_id, timestamp, received_at, source_type, bank,
                                    event_type, amount, currency, account_hint, merchant,
                                    balance_after, debt_amount, deadline, payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.event_id, event.timestamp, event.received_at, event.source_type,
                    event.bank, event.event_type, event.amount, event.currency,
                    event.account_hint, event.merchant, event.balance_after,
                    event.debt_amount, event.deadline, json.dumps(payload, ensure_ascii=False),
                ),
            )
            con.commit()
        finally:
            con.close()
        with JSONL_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return True


# --- Приложение --------------------------------------------------------------

app = FastAPI(title="Finance Relay", version="1.0.0")
_init_db()


def _check_auth(authorization: Optional[str]) -> None:
    if not TOKEN:
        raise HTTPException(500, "FINANCE_RELAY_TOKEN не задан на сервере")
    expected = f"Bearer {TOKEN}"
    if authorization != expected:
        raise HTTPException(401, "Неверный или отсутствующий токен")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "jsonl": str(JSONL_PATH), "time": datetime.now(timezone.utc).isoformat()}


@app.post("/finance-event")
async def finance_event(request: Request, authorization: Optional[str] = Header(default=None)) -> dict:
    _check_auth(authorization)
    body = await request.json()
    event = FinanceEvent(**body)
    event.received_at = datetime.now(timezone.utc).isoformat()

    if _looks_like_secret(event):
        # Не сохраняем и не логируем содержимое — только факт отказа.
        return {"status": "dropped", "reason": "secret_or_otp_detected"}

    is_new = _store(event)
    line = (
        f"[{event.received_at}] {event.bank or '?'}/{event.event_type or '?'} "
        f"{event.amount if event.amount is not None else ''} {event.currency or ''} "
        f"{event.merchant or ''}".strip()
    )
    print(("NEW  " if is_new else "DUP  ") + line, flush=True)
    return {"status": "stored" if is_new else "duplicate", "event_id": event.event_id}


@app.post("/ingest-raw")
async def ingest_raw(request: Request, authorization: Optional[str] = Header(default=None)) -> dict:
    """
    Ручная пересылка: тело {"text": "...", "source_app": "?"}.
    Сервер сам разбирает текст регексом (parser.py). Работает без Android-приложения
    и с iPhone — можно слать через любой шорткат/скрипт.
    """
    _check_auth(authorization)
    from parser import parse

    body = await request.json()
    text = (body or {}).get("text", "")
    parsed = parse(
        text,
        source_app=(body or {}).get("source_app", ""),
        source_type=(body or {}).get("source_type", "manual"),
        device_id=(body or {}).get("device_id", "manual"),
    )
    if parsed is None:
        return {"status": "dropped", "reason": "not_financial_or_secret"}

    event = FinanceEvent(**parsed)
    event.received_at = datetime.now(timezone.utc).isoformat()
    if _looks_like_secret(event):
        return {"status": "dropped", "reason": "secret_or_otp_detected"}
    is_new = _store(event)
    print(("NEW  " if is_new else "DUP  ") + f"[raw] {event.event_type} {event.amount} {event.merchant or ''}", flush=True)
    return {"status": "stored" if is_new else "duplicate", "event": event.model_dump()}


@app.get("/events")
def list_events(limit: int = 50, authorization: Optional[str] = Header(default=None)) -> dict:
    """Последние события — для отладки и для Hermes."""
    _check_auth(authorization)
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT payload FROM events ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        con.close()
    return {"count": len(rows), "events": [json.loads(r[0]) for r in rows]}
