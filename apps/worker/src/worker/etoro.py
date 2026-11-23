"""eToro public API client for the agent worker (async httpx).

Auth: an agent portfolio Bearer token (ETORO_TOKEN x "Authorization: Bearer"),
or the x-user-key/x-api-key pair. The WAF bans the default Python user agent,
so a custom User-Agent is mandatory. Credentials never appear in errors or logs.

Route map (base https://public-api.etoro.com/api/v1), account segment "demo"/"real".
Demo routes verified in production use 2026-11-21; the real trading-info and
execution paths are the API's documented analogues and MUST be re-verified in
dry-run against the live route catalog before the first real order.
"""

import uuid
import zoneinfo
from typing import Any
from urllib.parse import urlencode

import httpx

from env.etoro import env as etoro_env

PARIS = zoneinfo.ZoneInfo("Europe/Paris")
PIP = 0.0001
M5 = 300
M15 = 900
TIMEOUT_SECONDS = 30
RETRYABLE = {500, 502, 503, 504, 0}  # 0 = network/timeout -> single idempotent retry
USER_AGENT = "etoro-agent-workflow/1.0"


def routes(account: str) -> dict[str, str]:
    """Trading routes for one account segment. ``acc`` is empty for real."""
    acc = "/demo" if account == "demo" else ""
    return {
        "pnl": f"/trading/info{acc}/pnl",
        "portfolio": f"/trading/info{acc}/portfolio",
        "history": f"/trading/info/trade{acc}/history",
        "market_close": f"/trading/execution{acc}/market-close-orders/positions/{{position_id}}",
        "market_open": f"/trading/execution{acc}/market-open-orders/by-amount",
    }


def market_routes() -> dict[str, str]:
    """Public market-data routes: candles and live rates (no account segment)."""
    return {
        "candles": "/market-data/instruments/{instrument_id}/history/candles/desc/{interval}/{count}",
        "rates": "/market-data/instruments/rates",
    }


class EtoroClient:
    """Thin async wrapper returning (status, data). Never raises for HTTP errors."""

    def __init__(self) -> None:
        self._routes = routes(etoro_env.etoro_account)
        self._base = etoro_env.etoro_base_url.rstrip("/")

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "x-request-id": request_id or str(uuid.uuid4()),
        }
        if etoro_env.etoro_token:
            headers["Authorization"] = f"Bearer {etoro_env.etoro_token}"
        elif etoro_env.etoro_user_key and etoro_env.etoro_api_key:
            headers["x-user-key"] = etoro_env.etoro_user_key
            headers["x-api-key"] = etoro_env.etoro_api_key
        else:
            raise RuntimeError(
                "No eToro credential: set ETORO_TOKEN (agent portfolio Bearer token) "
                "or both ETORO_USER_KEY and ETORO_API_KEY, as workspace secrets"
            )
        return headers

    async def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> tuple[int, Any]:
        url = f"{self._base}{path}"
        if params:
            url += "?" + urlencode(params)
        headers = self._headers(request_id)
        data = None
        if body is not None:
            data = body  # httpx json-encodes and sets Content-Type
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            try:
                res = await client.request(method, url, headers=headers, json=data if body is not None else None)
            except httpx.HTTPError:
                return 0, {"error": "network"}
            text = res.text
            parsed: Any = None
            if text:
                try:
                    parsed = res.json()
                except ValueError:
                    parsed = {"raw": text[:220]}
            return res.status_code, parsed

    # -- market data ------------------------------------------------
    async def get_candles(self, instrument_id: int, interval: str, count: int, interval_secs: int) -> list[dict[str, Any]] | None:
        """Closed candles only, oldest first, or None on failure."""
        from datetime import datetime, timedelta, timezone

        path_tpl = market_routes()["candles"]
        status, data = await self.call(
            "GET", path_tpl.format(instrument_id=instrument_id, interval=interval, count=count)
        )
        if status != 200 or not data:
            return None
        try:
            bars = data["candles"][0]["candles"]
        except (KeyError, IndexError, TypeError):
            return None
        import datetime as _dt

        now = _dt.datetime.now(timezone.utc)
        out = []
        for c in sorted(bars, key=lambda x: x["fromDate"]):
            start = datetime.fromisoformat(c["fromDate"].replace("Z", "+00:00"))
            if start + timedelta(seconds=interval_secs) <= now:
                out.append({k: c[k] for k in ("fromDate", "open", "high", "low", "close")})
        return out

    async def get_rate(self, instrument_id: int) -> tuple[float, float] | None:
        """(ask, bid) or None."""
        status, data = await self.call("GET", market_routes()["rates"], params={"instrumentIds": instrument_id})
        if status != 200 or not data:
            return None
        for r in data.get("rates", []) or []:
            if r.get("instrumentID") == instrument_id:
                try:
                    return float(r["ask"]), float(r["bid"])
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    # -- account -----------------------------------------------------
    async def get_balance(self) -> float | None:
        status, data = await self.call("GET", self._routes["pnl"])
        if status != 200 or not data:
            return None
        cp = data.get("clientPortfolio") or {}
        credit = cp.get("credit") or cp.get("amount")
        try:
            return float(credit) if credit is not None else None
        except (TypeError, ValueError):
            return None

    async def get_positions(self, instrument_id: int) -> list[dict[str, Any]] | None:
        status, data = await self.call("GET", self._routes["portfolio"])
        if status != 200 or not data:
            return None
        out = []
        for p in data.get("positions") or []:
            if (p.get("instrumentID") or p.get("instrumentId")) != instrument_id:
                continue
            out.append({
                "id": p.get("positionID") or p.get("positionId"),
                "is_buy": bool(p.get("isBuy")),
                "open_rate": float(p.get("openRate") or 0.0),
                "units": float(p.get("units") or 0.0),
            })
        return out

    async def get_today_trades(self, instrument_id: int) -> list[dict[str, Any]] | None:
        """EUR/USD trades closed today (Paris), chronological, or None on failure."""
        from datetime import datetime, timezone

        now_paris = datetime.now(PARIS)
        start_utc = now_paris.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        min_date = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        trades: list[dict[str, Any]] = []
        page = 1
        while page <= 5:
            status, data = await self.call(
                "GET", self._routes["history"],
                params={"minDate": min_date, "page": page, "pageSize": 200},
            )
            if status != 200 or not isinstance(data, list):
                return None
            trades.extend(t for t in data if t.get("instrumentId") == instrument_id)
            if len(data) < 200:
                break
            page += 1
        out = []
        for t in trades:
            try:
                closed = datetime.fromisoformat(t["closeTimestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if closed.astimezone(PARIS).date() != now_paris.date():
                continue
            out.append({
                "position_id": t.get("positionId"),
                "net": float(t.get("netProfit") or 0.0) - float(t.get("fees") or 0.0),
                "closed": closed.isoformat(),
            })
        out.sort(key=lambda t: t["closed"])
        return out
