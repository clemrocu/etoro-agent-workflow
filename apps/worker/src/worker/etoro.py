"""eToro public API client for the agent worker (async httpx).

AUTH (verified live 2026-11-23 against the eToro agent-portfolios docs and API):
an agent-portfolio userToken travels in the ``x-user-key`` header TOGETHER with
the canonical eToro partner ``x-api-key`` (public value documented by eToro in
etoro-builders/etoro-agent-skills, api-conventions.md). It is NOT an OAuth
Bearer token: sending it as ``Authorization: Bearer`` returns 401 on every
route (verified). The main-account x-user-key + x-api-key pair works the same
way and remains supported as an alternative.
The WAF bans the default Python user agent, so a custom User-Agent is
mandatory. Credentials never appear in errors or logs.

Route map (base https://public-api.etoro.com/api/v1), verified live with an
agent token on the REAL environment 2026-11-23: real pnl needs the explicit
/real/ segment, portfolio and trade history do not.
"""

import uuid
import zoneinfo
from typing import Any
from urllib.parse import urlencode

import httpx

from env.etoro import env as etoro_env

PARIS = zoneinfo.ZoneInfo("Europe/Paris")
PIP = 0.0001          # legacy, kept for log readability on FX pairs
M15 = 900
H1 = 3600
TIMEOUT_SECONDS = 30
RETRYABLE = {500, 502, 503, 504, 0}  # 0 = network/timeout -> single idempotent retry
USER_AGENT = "etoro-agent-workflow/2.0"

# Canonical eToro Public API partner key (public value, documented by eToro):
# api-conventions.md: "The x-api-key header is always the canonical eToro
# Public API partner key... regardless of account type."
PARTNER_API_KEY = "sdgdskldFPLGfjHn1421dgnlxdGTbngdflg6290bRjslfihsjhSDsdgGHH25hjf"


def routes(account: str) -> dict[str, str]:
    """Trading routes for one account segment. ``acc`` keeps the demo paths."""
    acc = "/demo" if account == "demo" else ""
    real = "/real" if account != "demo" else acc
    return {
        "pnl": f"/trading/info{real}/pnl",
        "portfolio": f"/trading/info{acc}/portfolio",
        "history": "/trading/info/trade/history",
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
            # Agent portfolio userToken: an API user key, never a Bearer token.
            headers["x-api-key"] = PARTNER_API_KEY
            headers["x-user-key"] = etoro_env.etoro_token
        elif etoro_env.etoro_user_key and etoro_env.etoro_api_key:
            headers["x-api-key"] = etoro_env.etoro_api_key
            headers["x-user-key"] = etoro_env.etoro_user_key
        else:
            raise RuntimeError(
                "No eToro credential: set ETORO_TOKEN (agent portfolio user token) "
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
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            try:
                res = await client.request(method, url, headers=headers, json=body)
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
    async def get_candles(
        self, instrument_id: int, interval: str, count: int, interval_secs: int
    ) -> list[dict[str, Any]] | None:
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

    async def get_rates(self, instrument_ids: list[int]) -> dict[int, tuple[float, float]]:
        """Batched live rates: {instrument_id: (ask, bid)} (one API call)."""
        status, data = await self.call(
            "GET", market_routes()["rates"], params={"instrumentIds": ",".join(map(str, instrument_ids))}
        )
        out: dict[int, tuple[float, float]] = {}
        if status != 200 or not data:
            return out
        for r in data.get("rates", []) or []:
            try:
                out[int(r["instrumentID"])] = (float(r["ask"]), float(r["bid"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

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

    async def get_positions(self) -> list[dict[str, Any]] | None:
        """All open positions of the account, whatever the instrument."""
        status, data = await self.call("GET", self._routes["portfolio"])
        if status != 200 or not data:
            return None
        out = []
        for p in data.get("clientPortfolio", {}).get("positions") or []:
            try:
                out.append({
                    "id": p.get("positionID") or p.get("positionId"),
                    "instrument_id": int(p.get("instrumentID") or 0),
                    "is_buy": bool(p.get("isBuy")),
                    "open_rate": float(p.get("openRate") or 0.0),
                    "units": float(p.get("units") or 0.0),
                    "opened_at": p.get("openDateTime"),
                    "stop_loss_rate": p.get("stopLossRate"),
                    "take_profit_rate": p.get("takeProfitRate"),
                })
            except (TypeError, ValueError):
                continue
        return out

    async def get_today_trades(self) -> list[dict[str, Any]] | None:
        """Trades of ALL instruments closed today (Paris), chronological."""
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
            trades.extend(data)
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
                "instrument_id": t.get("instrumentId"),
                "net": float(t.get("netProfit") or 0.0) - float(t.get("fees") or 0.0),
                "closed": closed.isoformat(),
            })
        out.sort(key=lambda t: t["closed"])
        return out
