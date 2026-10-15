"""Durable owner-bound reports from the Demo position exit guard.

Reports acknowledge a local guard or its verified outcome. This module never
contacts a trading terminal and never sends an order.
"""

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from telegram.error import Forbidden, RetryAfter

from bot import market_store, mtf_presentation, trade_store, trade_timing


logger = logging.getLogger(__name__)
EVENT_FIELDS = frozenset({"event_id", "status", "deadline", "observed_at", "reason", "code", "deal", "order"})
STATUSES = frozenset({"armed", "closed", "unknown", "failed", "missing"})
REASONS = frozenset({
    "holding_limit", "session_cutoff", "close_not_confirmed", "ownership_unverified",
    "exit_guard_blocked", "exit_preflight_rejected", "exit_quote_changed", "position_closed",
})
SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS mt5_demo_exit_events (
        event_id UUID PRIMARY KEY,
        offer_id UUID NOT NULL REFERENCES mt5_trade_offers(id),
        bot_id BIGINT NOT NULL,
        device_id UUID NOT NULL,
        claim_id UUID NOT NULL,
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        event JSONB NOT NULL CHECK (jsonb_typeof(event) = 'object'),
        created_at TIMESTAMPTZ NOT NULL,
        notified BOOLEAN NOT NULL DEFAULT FALSE,
        notified_at TIMESTAMPTZ,
        FOREIGN KEY (bot_id, device_id) REFERENCES mt5_devices(bot_id, device_id)
    )""",
    """CREATE INDEX IF NOT EXISTS mt5_demo_exit_events_notifications_idx
        ON mt5_demo_exit_events(bot_id, created_at, event_id) WHERE NOT notified""",
)


def _uuid(value):
    if type(value) is str and str(UUID(value)) == value:
        return UUID(value)
    if isinstance(value, UUID):
        return value
    raise ValueError("Invalid identifier")


def _utc(value):
    if type(value) is str and len(value) <= 40:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Invalid timestamp")
    return value.astimezone(timezone.utc)


def _object(value):
    return json.loads(value) if isinstance(value, str) else value


def validate_event(event, payload, now):
    """Validate a report against the fixed plan, including after entry expiry."""
    if type(event) is not dict or set(event) != EVENT_FIELDS:
        raise ValueError("Invalid exit event")
    _uuid(event["event_id"])
    if event["status"] not in STATUSES or event["reason"] not in REASONS:
        raise ValueError("Invalid exit event state")
    if any(type(event[key]) is not int or not 0 <= event[key] < 2**63 for key in ("code", "deal", "order")):
        raise ValueError("Invalid execution identifier")
    timing = payload.get("trade_timing") if type(payload) is dict else None
    if not trade_timing.validate_trade_timing(timing, bar_time=payload.get("bar_time"), require_entry_open=False):
        raise ValueError("Invalid frozen trade plan")
    observed, deadline, clock = _utc(event["observed_at"]), _utc(event["deadline"]), _utc(now)
    evaluated = _utc(timing["evaluated_at"])
    # The bridge uses broker deal time; the existing execution receipt uses
    # its acknowledgment clock. Bound the real fill instead of equating them.
    # Some brokers expose only whole-second deal.time; permit its flooring
    # error without extending the strict outer/session deadlines.
    if not evaluated + timedelta(seconds=3599) <= deadline <= min(
        _utc(timing["latest_exit_at"]), _utc(timing["session_exit_at"]),
    ) or not evaluated <= observed <= clock + timedelta(seconds=30):
        raise ValueError("Invalid exit event clock")
    if event["status"] == "armed" and (observed > deadline or event["reason"] not in {"holding_limit", "session_cutoff"}):
        raise ValueError("Invalid guard activation")
    if event["status"] == "closed" and event["reason"] not in {"holding_limit", "session_cutoff", "position_closed"}:
        raise ValueError("Invalid close confirmation")
    if event["status"] == "closed" and event["reason"] in {"holding_limit", "session_cutoff"} and observed < deadline:
        raise ValueError("A timed close cannot precede its deadline")
    return event


def _bound_offer(offer, device, bot_id, device_id, offer_id, claim_id):
    try:
        payload = _object(offer["payload"])
        return (
            offer["bot_id"] == bot_id and _uuid(offer["device_id"]) == device_id
            and _uuid(offer["id"]) == offer_id and _uuid(offer["claim_id"]) == claim_id
            and offer["status"] in {"filled", "unknown"} and offer.get("executing_at") is not None
            and payload["workflow"] == "demo_execution" and payload["account_mode"] == "demo"
            and type(payload["volume"]) in (int, float) and payload["volume"] == .01
            and device.get("active", True) is True and _uuid(device["device_id"]) == device_id
            and device["account_mode"] == "demo" and type(device["volume"]) in (int, float)
            and device["volume"] == .01
            and payload["symbol"] == device["symbol"] == os.getenv("MARKET_GOLD_SYMBOL", "XAUUSD").strip()
            and device["owner_chat_id"] == device["owner_user_id"] == offer["chat_id"] == offer["user_id"]
            and type(offer["chat_id"]) is int and offer["chat_id"] > 0
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


async def initialize_schema(pool):
    async with pool.acquire() as connection:
        async with connection.transaction():
            for statement in SCHEMA_STATEMENTS:
                await connection.execute(statement)


async def record_event(pool, bot_id, device_id, offer_id, claim_id, event, now):
    """Recheck owner/workflow in the insert; identical retries preserve delivery."""
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
    row = await pool.fetchrow(
        """INSERT INTO mt5_demo_exit_events AS saved
            (event_id, offer_id, bot_id, device_id, claim_id, chat_id, user_id, event, created_at)
        SELECT $5, offer.id, offer.bot_id, offer.device_id, offer.claim_id,
            offer.chat_id, offer.user_id, $6::jsonb, $7
        FROM mt5_trade_offers AS offer
        JOIN mt5_devices AS device ON device.bot_id = offer.bot_id AND device.device_id = offer.device_id
        WHERE offer.bot_id = $1 AND offer.device_id = $2 AND offer.id = $3 AND offer.claim_id = $4
            AND offer.status IN ('filled', 'unknown') AND offer.executing_at IS NOT NULL
            AND offer.payload->>'workflow' = 'demo_execution'
            AND offer.payload->>'account_mode' = 'demo' AND offer.payload->>'volume' = '0.01'
            AND device.account_mode = 'demo' AND device.volume = 0.01
            AND device.symbol = offer.payload->>'symbol'
            AND device.owner_chat_id = offer.chat_id AND device.owner_user_id = offer.user_id
            AND offer.chat_id = offer.user_id
        ON CONFLICT (event_id) DO UPDATE SET event = saved.event
        WHERE saved.offer_id = EXCLUDED.offer_id AND saved.bot_id = EXCLUDED.bot_id
            AND saved.device_id = EXCLUDED.device_id AND saved.claim_id = EXCLUDED.claim_id
            AND saved.chat_id = EXCLUDED.chat_id AND saved.user_id = EXCLUDED.user_id
            AND saved.event = EXCLUDED.event
        RETURNING event_id""",
        bot_id, _uuid(device_id), _uuid(offer_id), _uuid(claim_id), _uuid(event["event_id"]), encoded, _utc(now),
    )
    return row is not None


async def list_notifications(pool, bot_id, limit=20):
    rows = await pool.fetch(
        """SELECT report.*, offer.payload FROM mt5_demo_exit_events AS report
        JOIN mt5_trade_offers AS offer ON offer.id = report.offer_id AND offer.bot_id = report.bot_id
        JOIN mt5_devices AS device ON device.bot_id = report.bot_id AND device.device_id = report.device_id
        WHERE report.bot_id = $1 AND NOT report.notified
            AND offer.payload->>'workflow' = 'demo_execution'
            AND device.owner_chat_id = report.chat_id AND device.owner_user_id = report.user_id
            AND report.chat_id = report.user_id
            AND device.account_mode = 'demo' AND device.volume = 0.01
        ORDER BY report.created_at, report.event_id LIMIT $2""", bot_id, limit,
    )
    return [dict(row, event=_object(row["event"]), payload=_object(row["payload"])) for row in rows]


async def mark_notified(pool, bot_id, event_id, now):
    return bool(await pool.fetchval(
        """UPDATE mt5_demo_exit_events SET notified = TRUE, notified_at = $3
        WHERE bot_id = $1 AND event_id = $2 AND NOT notified RETURNING TRUE""",
        bot_id, _uuid(event_id), _utc(now),
    ))


def event_text(report, now):
    event, payload = report["event"], report["payload"]
    status = event["status"]
    if status == "armed":
        heading = ("🕒 أكد جسر Demo تفعيل حماية مدة الصفقة."
                   if _utc(now) <= _utc(event["deadline"]) else "🕒 وصل تأكيد سابق لتفعيل حماية مدة الصفقة؛ الموعد المذكور صار بالماضي، راجع حالة MT5 الحالية.")
    elif status == "closed":
        heading = "✅ أكد جسر Demo أن الصفقة صارت مسكّرة؛ سبب الإغلاق ووقته الفعلي موجودين بسجل MT5."
    else:
        heading = "⚠️ حماية الخروج لم تؤكد إغلاق الصفقة؛ راجع MT5 الآن وتحقق من أي صفقة مفتوحة. لا توجد إعادة إغلاق تلقائية بعد نتيجة غير مؤكدة."
    return "\n".join([
        heading, f"{payload['symbol']} | Demo | {payload['direction']}",
        f"موعد الخروج المحدّد: {mtf_presentation.lebanon_time(event['deadline'])}.",
        f"وقت تأكيد الجهاز: {mtf_presentation.lebanon_time(event['observed_at'])}.",
    ])


async def send_notifications(service, bot, now_utc):
    """Acknowledge successful delivery only; subscription cannot hide exits."""
    for report in await list_notifications(service.pool, service.bot_id):
        try:
            device = await trade_store.get_device(service.pool, service.bot_id, report["device_id"])
            if (device is None or device.get("owner_chat_id") != report["chat_id"]
                    or device.get("owner_user_id") != report["user_id"]
                    or report["chat_id"] != report["user_id"]
                    or device.get("active", True) is not True or device.get("account_mode") != "demo"
                    or type(device.get("volume")) not in (int, float) or device["volume"] != .01
                    or device.get("symbol") != report["payload"].get("symbol")):
                continue
            await bot.send_message(report["chat_id"], event_text(report, now_utc()), parse_mode=None)
            await mark_notified(service.pool, service.bot_id, report["event_id"], now_utc())
        except Forbidden:
            await market_store.disable_subscription(service.pool, service.bot_id, report["chat_id"])
        except RetryAfter as error:
            await service.telegram_backoff(error)
            return False
        except Exception as error:
            logger.warning("MT5 exit notification failed (%s).", type(error).__name__)
    return True


def install_routes(app, read_request, now_utc):
    async def exit_result(request: Request):
        service, body, error = await read_request(request, "demo_execution")
        if error is not None:
            return error
        try:
            if set(body) != {"device_id", "offer_id", "claim_id", "event"}:
                raise ValueError("Invalid fields")
            device_id, offer_id, claim_id = (_uuid(body[key]) for key in ("device_id", "offer_id", "claim_id"))
            device = await trade_store.get_device(service.pool, service.bot_id, device_id)
            offer = await trade_store.get_offer(service.pool, service.bot_id, offer_id)
            if not _bound_offer(offer, device, service.bot_id, device_id, offer_id, claim_id):
                return JSONResponse({"detail": "Exit report is not bound to this Demo claim"}, status_code=409)
            now = now_utc()
            event = validate_event(body["event"], _object(offer["payload"]), now)
            recorded = await record_event(service.pool, service.bot_id, device_id, offer_id, claim_id, event, now)
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            return JSONResponse({"detail": "Invalid Demo exit event"}, status_code=422)
        if not recorded:
            return JSONResponse({"detail": "Exit event conflicts with its saved claim or contents"}, status_code=409)
        return {"ok": True}

    app.add_api_route("/api/mt5/demo/exit-result", exit_result, methods=["POST"])
