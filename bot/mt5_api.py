"""Private market-device pairing and single-use, human-approved demo requests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hmac
import json
import os
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from bot import market_store, trade_store

MAX_CONTROL_BYTES = 8192


def now_utc():
    return datetime.now(timezone.utc)


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("Invalid identifier")
    return UUID(value)


def _finite_object(body):
    def reject_constant(value):
        raise ValueError("Invalid JSON number")
    def unique_pairs(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate field")
            result[name] = value
        return result
    result = json.loads(body, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    if type(result) is not dict:
        raise ValueError("Object required")
    return result


def _policy(payload):
    # This release is explicitly authorized for a demo account and 0.01 lot.
    # A live account cannot opt itself in through an incoming API request.
    if payload["account_mode"] != "demo":
        raise ValueError("Demo account required")
    volume = payload["volume"]
    if type(volume) not in (int, float) or Decimal(str(volume)) != Decimal("0.01"):
        raise ValueError("Configured volume required")
    symbol = payload["symbol"]
    if type(symbol) is not str or symbol != os.getenv("MARKET_GOLD_SYMBOL", "XAUUSD").strip():
        raise ValueError("Configured symbol required")


def install_routes(app, application, settings):
    async def read_request(request):
        key = getattr(settings, "market_bridge_key", "")
        service = application.bot_data.get("market_service")
        if len(key) < 32 or service is None or not getattr(service, "trading_enabled", False):
            return None, None, JSONResponse({"detail": "Demo execution is not configured"}, status_code=404)
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode("utf-8"),
            ("Bearer " + key).encode("utf-8"),
        ):
            return None, None, JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if service.source != "mt5":
            return None, None, JSONResponse({"detail": "MT5 source required"}, status_code=409)
        size, parts = 0, []
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_CONTROL_BYTES:
                return None, None, JSONResponse({"detail": "Payload too large"}, status_code=413)
            parts.append(chunk)
        try:
            payload = _finite_object(b"".join(parts))
        except (ValueError, TypeError, UnicodeDecodeError, OverflowError):
            return None, None, JSONResponse({"detail": "Invalid device request"}, status_code=422)
        return service, payload, None

    async def register(request: Request):
        service, payload, error = await read_request(request)
        if error is not None:
            return error
        try:
            if set(payload) != {"device_id", "symbol", "account_mode", "volume", "pair_code_hash"}:
                raise ValueError("Invalid fields")
            _policy(payload)
            device = await trade_store.register_device(
                service.pool, service.bot_id, _uuid(payload["device_id"]),
                payload["symbol"], payload["account_mode"], payload["volume"],
                payload["pair_code_hash"], now_utc(),
            )
        except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
            return JSONResponse({"detail": "Invalid or changed device configuration"}, status_code=422)
        return {"ok": True, "paired": device.get("owner_user_id") is not None}

    async def poll(request: Request):
        service, payload, error = await read_request(request)
        if error is not None:
            return error
        try:
            if set(payload) != {"device_id"}:
                raise ValueError("Invalid fields")
            device_id = _uuid(payload["device_id"])
            device = await trade_store.get_device(service.pool, service.bot_id, device_id)
            if device is None:
                return JSONResponse({"detail": "Device is not registered"}, status_code=404)
            _policy(device)
            now = now_utc()
            await trade_store.heartbeat(service.pool, service.bot_id, device_id, now)
            await trade_store.expire_offers(service.pool, service.bot_id, now)
            snapshot = await market_store.get_cache(service.pool, service.bot_id, "broker_feed")
            if snapshot is None:
                return {"trade": None}
            # A registration heartbeat alone does not prove a recent quote.
            from bot.market_monitor import validate_feed, _utc
            feed = validate_feed(snapshot["payload"], now, device["symbol"])
            if (
                feed.get("device_id") != str(device_id) or "execution" not in feed
                or not timedelta(0) <= now - snapshot["updated_at"] <= timedelta(seconds=180)
                or not timedelta(0) <= now - _utc(feed["quote"]["time"]) <= timedelta(seconds=30)
            ):
                return {"trade": None}
            if device.get("owner_chat_id") is None or await service.risk_pause(device["owner_chat_id"]) is not None:
                return {"trade": None}
            offer = await trade_store.claim_offer(service.pool, service.bot_id, device_id, now)
        except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
            return JSONResponse({"detail": "Invalid device or market data"}, status_code=422)
        if offer is None:
            return {"trade": None}
        return {"trade": {"id": str(offer["id"]), "claim_id": str(offer["claim_id"]),
                          "payload": offer["payload"], "expires_at": offer["expires_at"].isoformat()}}

    async def result(request: Request):
        service, payload, error = await read_request(request)
        if error is not None:
            return error
        try:
            if set(payload) != {"device_id", "offer_id", "claim_id", "result"}:
                raise ValueError("Invalid fields")
            outcome = payload["result"]
            if type(outcome) is not dict or not set(outcome) <= {"status", "code", "order_ticket", "executed_at"}:
                raise ValueError("Invalid result")
            if outcome.get("status") not in {"filled", "failed", "unknown"}:
                raise ValueError("Invalid status")
            if "code" in outcome and (type(outcome["code"]) is not int or not -2**31 <= outcome["code"] < 2**31):
                raise ValueError("Invalid result code")
            if "order_ticket" in outcome and (type(outcome["order_ticket"]) is not int or not 0 < outcome["order_ticket"] < 2**63):
                raise ValueError("Invalid ticket")
            if outcome["status"] == "filled" and (outcome.get("code") != 10009 or "order_ticket" not in outcome):
                raise ValueError("An actual full execution result is required")
            now = now_utc()
            if "executed_at" in outcome:
                if type(outcome["executed_at"]) is not str or len(outcome["executed_at"]) > 40:
                    raise ValueError("Invalid result time")
                stamp = datetime.fromisoformat(outcome["executed_at"].replace("Z", "+00:00"))
                if stamp.tzinfo is None or not now - timedelta(days=1) <= stamp <= now + timedelta(seconds=30):
                    raise ValueError("Invalid result time")
            completed = await trade_store.complete_offer(
                service.pool, service.bot_id, _uuid(payload["device_id"]),
                _uuid(payload["offer_id"]), _uuid(payload["claim_id"]), outcome, now,
            )
        except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
            return JSONResponse({"detail": "Invalid execution result"}, status_code=422)
        if completed is None:
            return JSONResponse({"detail": "Execution claim is no longer active"}, status_code=409)
        return {"ok": True}

    app.add_api_route("/api/mt5/register", register, methods=["POST"])
    app.add_api_route("/api/mt5/poll", poll, methods=["POST"])
    app.add_api_route("/api/mt5/result", result, methods=["POST"])
