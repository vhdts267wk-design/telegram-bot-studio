"""Private pairing and separately scoped execution or manual ticket requests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hmac
import json
import os
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from bot import manual_ticket_store, market_store, mtf_runtime, proposal_overlay, trade_store

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
    async def read_request(request, workflow=None):
        service = application.bot_data.get("market_service")
        automatic_flag = getattr(service, "trading_enabled", False)
        manual_flag = getattr(service, "manual_tickets_enabled", False)
        if type(automatic_flag) is not bool or type(manual_flag) is not bool:
            return None, None, JSONResponse({"detail": "Requested MT5 workflow is not configured"}, status_code=404)
        automatic = False
        # The configured manual mode must block legacy execution even if a
        # stale service object still advertises automatic execution.
        manual = manual_flag is True or os.getenv("MT5_MANUAL_TICKETS_ENABLED", "false").strip().lower() == "true"
        key = os.getenv("MT5_MANUAL_BRIDGE_KEY", "") if manual else getattr(settings, "market_bridge_key", "")
        # Ambiguous configuration fails closed, including legacy clients.
        enabled = automatic != manual
        if workflow == "automatic":
            enabled = enabled and automatic
        elif workflow == "manual_ticket":
            enabled = enabled and manual
        if not isinstance(key, str) or len(key) < 32 or service is None or not enabled:
            return None, None, JSONResponse({"detail": "Requested MT5 workflow is not configured"}, status_code=404)
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
        service, payload, error = await read_request(request, "automatic")
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
                or not timedelta(seconds=-5) <= now - _utc(feed["quote"]["time"]) <= timedelta(seconds=30)
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
        service, payload, error = await read_request(request, "automatic")
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

    async def fresh_manual_device(service, device_id, *, include_feed=False):
        device = await trade_store.get_device(service.pool, service.bot_id, device_id)
        if device is None:
            return None
        _policy(device)
        snapshot = await market_store.get_cache(service.pool, service.bot_id, "broker_feed")
        if snapshot is None:
            return None
        # Use the time after storage reads, so their latency cannot hide a
        # quote or device that has become stale while this request waits.
        now = now_utc()
        if (
            device.get("owner_chat_id") is None
            or device.get("owner_chat_id") != device.get("owner_user_id")
            or not timedelta(0) <= now - device["last_seen_at"] <= timedelta(seconds=180)
        ):
            return None
        from bot.market_monitor import validate_feed, _utc
        feed = validate_feed(snapshot["payload"], now, device["symbol"])
        if (
            feed.get("device_id") != str(device_id) or "execution" not in feed
            or not timedelta(0) <= now - snapshot["updated_at"] <= timedelta(seconds=180)
            or not timedelta(seconds=-5) <= now - _utc(feed["quote"]["time"]) <= timedelta(seconds=10)
        ):
            return None
        if mtf_runtime.evaluate_feed(feed, now).get("state") != "signal":
            return None
        return (device, now, feed) if include_feed else (device, now)

    async def manual_chart(request: Request):
        service, payload, error = await read_request(request, "manual_ticket")
        if error is not None:
            return error
        try:
            if set(payload) != {"device_id"}:
                raise ValueError("Invalid fields")
            device_id = _uuid(payload["device_id"])
            current = await fresh_manual_device(service, device_id, include_feed=True)
            if current is None:
                return {"proposal": None}
            device, _, _ = current
            owner = (device["owner_chat_id"], device["owner_user_id"])
            if await service.risk_pause(owner[0]) is not None:
                return {"proposal": None}
            offer = await manual_ticket_store.get_chart_offer(service.pool, service.bot_id, device_id, now_utc())
            if offer is None or not await trade_store.subscription_active(service.pool, service.bot_id, owner[0]):
                return {"proposal": None}
            # Re-read the bound device and broker quote after every awaited
            # eligibility check. This endpoint never refreshes a heartbeat,
            # modifies an offer or acquires a preparation claim.
            current = await fresh_manual_device(service, device_id, include_feed=True)
            if current is None:
                return {"proposal": None}
            device, now, feed = current
            if (device["owner_chat_id"], device["owner_user_id"]) != owner:
                return {"proposal": None}
            return {"proposal": proposal_overlay.build_chart_overlay(offer, device, feed, now)}
        except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
            return JSONResponse({"detail": "Invalid device or market data"}, status_code=422)

    def manual_poll_response(preparation=None):
        response = {"preparation": preparation}
        if mtf_runtime.signal_mode() == "experimental_demo":
            response.update(signal_mode="experimental_demo", entry_window_seconds=30, provisional=True)
        return response

    async def manual_poll(request: Request):
        service, payload, error = await read_request(request, "manual_ticket")
        if error is not None:
            return error
        try:
            if set(payload) != {"device_id"}:
                raise ValueError("Invalid fields")
            device_id, now = _uuid(payload["device_id"]), now_utc()
            device = await trade_store.get_device(service.pool, service.bot_id, device_id)
            if device is None:
                return JSONResponse({"detail": "Device is not registered"}, status_code=404)
            _policy(device)
            await trade_store.heartbeat(service.pool, service.bot_id, device_id, now)
            await manual_ticket_store.expire_offers(service.pool, service.bot_id, now)
            current = await fresh_manual_device(service, device_id, include_feed=True)
            if current is None or await service.risk_pause(current[0]["owner_chat_id"]) is not None:
                return manual_poll_response()
            owner = (current[0]["owner_chat_id"], current[0]["owner_user_id"])
            # Re-read after asynchronous risk/storage work. A heartbeat cannot
            # substitute for a fresh feed or refresh the frozen offer expiry.
            current = await fresh_manual_device(service, device_id, include_feed=True)
            if current is None or (current[0]["owner_chat_id"], current[0]["owner_user_id"]) != owner:
                return manual_poll_response()
            _, now, feed = current
            qualified = mtf_runtime.evaluate_feed(feed, now)
            if not mtf_runtime.eligible_result(qualified, now):
                return manual_poll_response()
            proposal_context = {key: qualified[key] for key in ("direction", "bar_time", "confirmation_bar_time", "direction_bar_time", "broker_fingerprint", "policy_id", "cost_context", "execution")}
            claim_options = {"qualification_id": qualified.get("qualification_id"),
                             "strategy_fingerprint": qualified["strategy_fingerprint"],
                             "proposal_context": proposal_context}
            if mtf_runtime.is_experimental_result(qualified):
                claim_options["signal_mode"] = "experimental_demo"
                proposal_context["cost_context"] = {key: qualified["cost_context"][key]
                                                     for key in ("loss_cash_per_price_unit", "profit_cash_per_price_unit")}
            offer = await manual_ticket_store.claim_offer(service.pool, service.bot_id, device_id, now, **claim_options)
        except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
            return JSONResponse({"detail": "Invalid device or market data"}, status_code=422)
        if offer is None:
            return manual_poll_response()
        try:
            current = await fresh_manual_device(service, device_id, include_feed=True)
            if (
                current is None or (current[0]["owner_chat_id"], current[0]["owner_user_id"]) != owner
                or (offer["chat_id"], offer["user_id"]) != owner
                or not mtf_runtime.eligible_payload(offer["payload"], current[2], current[1])
            ):
                return manual_poll_response()
        except (ValueError, TypeError, KeyError, OverflowError, InvalidOperation):
            return manual_poll_response()
        try:
            expires_at = offer["expires_at"]
            if mtf_runtime.is_experimental_result(offer["payload"]):
                from bot.multi_timeframe import _utc
                closed = _utc(offer["payload"]["bar_time"]) + timedelta(minutes=1)
                expires_at = min(_utc(expires_at), closed + timedelta(seconds=30))
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            return manual_poll_response()
        return manual_poll_response({
            "id": str(offer["id"]), "claim_id": str(offer["claim_id"]), "workflow": "manual_ticket",
            "payload": offer["payload"], "expires_at": expires_at.isoformat(),
        })

    async def manual_result(request: Request):
        service, payload, error = await read_request(request, "manual_ticket")
        if error is not None:
            return error
        try:
            if set(payload) != {"device_id", "offer_id", "claim_id", "result"}:
                raise ValueError("Invalid fields")
            outcome = payload["result"]
            if (
                type(outcome) is not dict or set(outcome) != {"status"}
                or type(outcome.get("status")) is not str or outcome["status"] not in {"prepared", "failed"}
            ):
                raise ValueError("Invalid preparation result")
            completed = await manual_ticket_store.complete_offer(
                service.pool, service.bot_id, _uuid(payload["device_id"]),
                _uuid(payload["offer_id"]), _uuid(payload["claim_id"]), outcome, now_utc(),
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            return JSONResponse({"detail": "Invalid preparation result"}, status_code=422)
        if completed is None:
            return JSONResponse({"detail": "Preparation claim is no longer active"}, status_code=409)
        return {"ok": True}

    app.add_api_route("/api/mt5/register", register, methods=["POST"])
    app.add_api_route("/api/mt5/poll", poll, methods=["POST"])
    app.add_api_route("/api/mt5/result", result, methods=["POST"])
    app.add_api_route("/api/mt5/manual/poll", manual_poll, methods=["POST"])
    app.add_api_route("/api/mt5/manual/result", manual_result, methods=["POST"])
    app.add_api_route("/api/mt5/manual/chart", manual_chart, methods=["POST"])
