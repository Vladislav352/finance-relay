#!/usr/bin/env bash
# Быстрая проверка сервера. Использование:
#   FINANCE_RELAY_TOKEN=твой-токен ./send_test.sh
# По умолчанию бьёт в localhost:8788.
set -euo pipefail

HOST="${FINANCE_RELAY_HOST:-http://127.0.0.1:8788}"
TOKEN="${FINANCE_RELAY_TOKEN:?Задай FINANCE_RELAY_TOKEN}"

echo "== /health =="
curl -s "$HOST/health"; echo

echo "== /ingest-raw (разбор текста на сервере) =="
curl -s -X POST "$HOST/ingest-raw" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Т-Банк. Покупка 1 250 ₽ Пятёрочка. Доступно 42 350 ₽. Карта *1234","source_app":"test"}'
echo

echo "== /finance-event (готовый JSON от приложения) =="
curl -s -X POST "$HOST/finance-event" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"event_id":"00000000-0000-0000-0000-000000000001","timestamp":"2026-09-28T10:00:00Z","source_type":"push","bank":"sber","event_type":"expense","amount":540,"currency":"RUB","merchant":"Аптека","balance_after":83000,"confidence":0.6}'
echo

echo "== /events =="
curl -s "$HOST/events?limit=5" -H "Authorization: Bearer $TOKEN"; echo
