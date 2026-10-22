"""
Регексовый парсер банковских уведомлений (RU).

Используется:
  * сервером — для endpoint /ingest-raw (ручная пересылка текста);
  * как эталон логики, повторённой в Android-приложении (Parser.kt).

Задача — превратить строку вроде
    "Покупка 1 250,50 ₽ Пятёрочка. Баланс: 83 500 ₽. Карта *1234"
в структурированное событие. Коды подтверждения / OTP парсер отбрасывает.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

# Отбрасываем всё, что похоже на код/пароль/OTP.
SECRET_MARKERS = re.compile(
    r"(никому не сообщайте|код подтверждени|одноразов|пароль|\botp\b|\bcvv\b|\bcvc\b"
    r"|код для входа|код:\s*\d)", re.IGNORECASE,
)

# Рекламный шум без денежного события.
PROMO_MARKERS = re.compile(
    r"(кэшбэк до|кешбэк до|скидк|акци|распродаж|дарим|специальное предложение|промокод)",
    re.IGNORECASE,
)

BANKS = {
    "sber": re.compile(r"(сбер|sber)", re.IGNORECASE),
    "tbank": re.compile(r"(т-?банк|t-?bank|тинькофф|tinkoff)", re.IGNORECASE),
    "alfa": re.compile(r"(альфа|alfa)", re.IGNORECASE),
    "vtb": re.compile(r"\bвтб\b|\bvtb\b", re.IGNORECASE),
    "gosuslugi": re.compile(r"(госуслуг|фнс|фссп|налог|штраф|жкх|задолженност)", re.IGNORECASE),
    "ozon": re.compile(r"(озон банк|ozon банк|озон\s*банк)", re.IGNORECASE),
    "sbp": re.compile(r"(сбп|система быстрых платеж)", re.IGNORECASE),
}

INCOME = re.compile(r"(поступлени|зачислени|пополнени|перевод от|вам перевели|возврат)", re.IGNORECASE)
EXPENSE = re.compile(r"(покупк|списани|оплат|снятие|платёж|платеж)", re.IGNORECASE)
TRANSFER = re.compile(r"(перевод\b|перевели\b|перечислени)", re.IGNORECASE)
BALANCE = re.compile(r"(остаток|доступно|баланс)", re.IGNORECASE)
DEBT = re.compile(r"(задолженност|долг|к оплате|оплатить до|начислен)", re.IGNORECASE)
TAX = re.compile(r"(налог|фнс)", re.IGNORECASE)
FINE = re.compile(r"(штраф|фссп|гибдд)", re.IGNORECASE)

# Сумма: "1 250,50", "1250.50", "83 500", возможно со знаком валюты.
AMOUNT = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,](\d{2}))?(?![\d%])\s*(₽|руб|rub|р\.|\$|€|тг|kzt|usd|eur)?",
    re.IGNORECASE,
)
BALANCE_AMOUNT = re.compile(
    r"(?:остаток|доступно|баланс)[:\s]*"
    r"(\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,](\d{2}))?",
    re.IGNORECASE,
)
CARD = re.compile(r"(?:карт[аеы]?|сч[её]т|счет)\D{0,6}\*?(\d{4})", re.IGNORECASE)
DEADLINE = re.compile(r"до\s+(\d{2})[.\-/](\d{2})[.\-/](\d{4})", re.IGNORECASE)

CURRENCY_MAP = {
    "₽": "RUB", "руб": "RUB", "rub": "RUB", "р.": "RUB",
    "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR",
    "тг": "KZT", "kzt": "KZT",
}


def _num(whole: str, frac: Optional[str]) -> float:
    whole = whole.replace(" ", "").replace(" ", "")
    return float(f"{whole}.{frac}") if frac else float(whole)


def parse(text: str, source_app: str = "", package_name: str = "",
          source_type: str = "manual", device_id: str = "server") -> Optional[dict]:
    """Возвращает нормализованное событие или None, если это код/реклама/не про деньги."""
    if not text or SECRET_MARKERS.search(text):
        return None

    has_money_word = any(p.search(text) for p in (INCOME, EXPENSE, TRANSFER, BALANCE, DEBT))
    amounts = list(AMOUNT.finditer(text))
    if not amounts and not has_money_word:
        return None
    if PROMO_MARKERS.search(text) and not amounts:
        return None

    bank = next((name for name, rx in BANKS.items() if rx.search(text)), "unknown")

    if TAX.search(text):
        event_type = "tax"
    elif FINE.search(text):
        event_type = "fine"
    elif DEBT.search(text):
        event_type = "debt"
    elif INCOME.search(text):
        event_type = "income"
    elif EXPENSE.search(text):
        event_type = "expense"
    elif TRANSFER.search(text):
        event_type = "transfer"
    elif BALANCE.search(text):
        event_type = "balance"
    else:
        event_type = "unknown"

    balance_after = None
    bm = BALANCE_AMOUNT.search(text)
    if bm:
        balance_after = _num(bm.group(1), bm.group(2))

    amount = None
    currency = None
    for m in amounts:
        # Пропускаем ту сумму, что является балансом.
        if bm and m.start() == bm.start(1):
            continue
        amount = _num(m.group(1), m.group(2))
        cur = (m.group(3) or "").lower().strip()
        currency = CURRENCY_MAP.get(cur, "RUB")
        break
    if amount is None and balance_after is not None:
        currency = "RUB"

    debt_amount = amount if event_type in ("debt", "tax", "fine") else None

    account_hint = None
    cm = CARD.search(text)
    if cm:
        account_hint = f"****{cm.group(1)}"

    deadline = None
    dm = DEADLINE.search(text)
    if dm:
        deadline = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"

    merchant = None
    mm = re.search(r"(?:в|у|от)\s+([A-ZА-ЯЁ][\wА-Яа-яЁё .-]{2,30})", text)
    if mm:
        merchant = mm.group(1).strip()
        # Обрезаем хвост, если зацепили следующее предложение/поле.
        merchant = re.split(r"\s+(?:карт|сч[её]т|баланс|остаток|доступно)", merchant, flags=re.IGNORECASE)[0]
        merchant = merchant.strip().rstrip(".").strip() or None

    return {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_type": source_type,
        "source_app": source_app or None,
        "package_name": package_name or None,
        "bank": bank,
        "event_type": event_type,
        "amount": amount,
        "currency": currency,
        "account_hint": account_hint,
        "merchant": merchant,
        "balance_after": balance_after,
        "debt_amount": debt_amount,
        "deadline": deadline,
        "raw_title": None,
        "raw_text": text,
        "confidence": 0.6 if event_type != "unknown" else 0.3,
        "device_id": device_id,
    }
