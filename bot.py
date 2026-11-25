from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Optional
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("munna-ai-signals")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OTCHARTS_API_KEY = os.getenv("OTCHARTS_API_KEY", "").strip()
WATCHLIST = [s.strip() for s in os.getenv(
    "WATCHLIST", "EURUSD_otc,GBPUSD_otc,USDJPY_otc,AUDUSD_otc,BTCUSD_otc"
).split(",") if s.strip()]
MIN_PAYOUT = float(os.getenv("MIN_PAYOUT", "80"))
MIN_ADX = float(os.getenv("MIN_ADX", "20"))
MIN_HISTORY_BARS = int(os.getenv("MIN_HISTORY_BARS", "120"))
MAX_TICK_AGE = int(os.getenv("MAX_TICK_AGE_SECONDS", "12"))
MAX_INTRA_CANDLE_GAP = int(os.getenv("MAX_INTRA_CANDLE_GAP_SECONDS", "30"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "180"))
DB_PATH = os.getenv("DATABASE_PATH", "munna_signals.sqlite3")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Dhaka")
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
BASE_URL = "https://otcharts.com"
EXPIRY_SECONDS = 60


@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float

    def update(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price


@dataclass
class SymbolState:
    symbol: str
    display_name: str
    payout: float
    is_open: bool
    history: Deque[Candle]
    current: Optional[Candle] = None
    current_full: bool = False
    last_tick_ts: float = 0.0
    last_signal_ts: float = 0.0
    pending_signal_id: Optional[int] = None


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                display_name TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_ts INTEGER NOT NULL,
                expiry_ts INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                payout REAL NOT NULL,
                score TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                exit_price REAL,
                exit_ts INTEGER,
                created_at INTEGER NOT NULL
            );
            """
        )
        self.conn.execute(
            "UPDATE signals SET status='VOID',exit_price=NULL,exit_ts=? WHERE status='PENDING'",
            (int(time.time()),),
        )
        self.conn.commit()

    def get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def create_signal(
        self, state: SymbolState, direction: str, entry_ts: int,
        entry_price: float, score: str, reason: str,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO signals
            (symbol,display_name,direction,entry_ts,expiry_ts,entry_price,payout,score,reason,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,'PENDING',?)""",
            (state.symbol, state.display_name, direction, entry_ts,
             entry_ts + EXPIRY_SECONDS, entry_price, state.payout, score,
             reason, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def settle_signal(self, signal_id: int, status: str, exit_price: Optional[float], exit_ts: int) -> None:
        self.conn.execute(
            "UPDATE signals SET status=?,exit_price=?,exit_ts=? WHERE id=?",
            (status, exit_price, exit_ts, signal_id),
        )
        self.conn.commit()

    def get_signal(self, signal_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()

    def report(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status,COUNT(*) AS n FROM signals GROUP BY status"
        ).fetchall()
        data = {r["status"]: int(r["n"]) for r in rows}
        return {k: data.get(k, 0) for k in ["PENDING", "WIN", "LOSS", "TIE", "VOID"]}

    def history(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# --------------------------- Technical indicators ---------------------------

def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result = [50.0] * period

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    result.append(value(avg_gain, avg_loss))
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        result.append(value(avg_gain, avg_loss))
    return result


def adx_di(candles: list[Candle], period: int = 14) -> tuple[float, float, float]:
    if len(candles) < period * 2 + 2:
        return 0.0, 0.0, 0.0
    tr_list: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        up_move = cur.high - prev.high
        down_move = prev.low - cur.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        tr_list.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))

    tr_s = sum(tr_list[:period])
    plus_s = sum(plus_dm[:period])
    minus_s = sum(minus_dm[:period])
    dx_values: list[float] = []

    def di_values(tr: float, p: float, m: float) -> tuple[float, float, float]:
        if tr <= 0:
            return 0.0, 0.0, 0.0
        pdi = 100.0 * p / tr
        mdi = 100.0 * m / tr
        denom = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / denom if denom else 0.0
        return pdi, mdi, dx

    pdi, mdi, dx = di_values(tr_s, plus_s, minus_s)
    dx_values.append(dx)
    for i in range(period, len(tr_list)):
        tr_s = tr_s - (tr_s / period) + tr_list[i]
        plus_s = plus_s - (plus_s / period) + plus_dm[i]
        minus_s = minus_s - (minus_s / period) + minus_dm[i]
        pdi, mdi, dx = di_values(tr_s, plus_s, minus_s)
        dx_values.append(dx)

    if len(dx_values) < period:
        return 0.0, pdi, mdi
    adx_val = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx_val = ((adx_val * (period - 1)) + dx) / period
    return adx_val, pdi, mdi


def analyse_strict(candles: list[Candle], min_adx: float) -> Optional[dict[str, Any]]:
    """Return a strict confluence setup, not a probability or guarantee."""
    needed = max(MIN_HISTORY_BARS, 60)
    if len(candles) < needed:
        return None
    recent = candles[-needed:]
    # A missing minute invalidates the sample; never synthesize missing bars.
    if any(recent[i].time - recent[i - 1].time != 60 for i in range(1, len(recent))):
        return None

    closes = [c.close for c in recent]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    rsi_values = rsi(closes, 14)
    macd_line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    macd_signal = ema(macd_line, 9)
    hist = [a - b for a, b in zip(macd_line, macd_signal)]
    adx_val, plus_di, minus_di = adx_di(recent, 14)

    last = recent[-1]
    body = abs(last.close - last.open)
    candle_range = last.high - last.low
    body_ratio = body / candle_range if candle_range > 0 else 0.0
    # Avoid unusually large one-bar spikes versus the preceding 20 true ranges.
    tr_values = []
    for i in range(1, len(recent)):
        c, p = recent[i], recent[i - 1]
        tr_values.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    avg_tr = sum(tr_values[-21:-1]) / max(1, len(tr_values[-21:-1]))
    spike_ok = candle_range <= (2.2 * avg_tr if avg_tr > 0 else float("inf"))

    if not rsi_values or len(hist) < 3:
        return None
    rsi_now = rsi_values[-1]
    bullish = (
        ema20[-1] > ema50[-1]
        and ema20[-1] > ema20[-4]
        and last.close > ema20[-1]
        and 53.0 <= rsi_now <= 66.0
        and hist[-1] > 0 and hist[-1] > hist[-2]
        and adx_val >= min_adx and plus_di > minus_di
        and last.close > last.open and body_ratio >= 0.45
        and spike_ok
    )
    bearish = (
        ema20[-1] < ema50[-1]
        and ema20[-1] < ema20[-4]
        and last.close < ema20[-1]
        and 34.0 <= rsi_now <= 47.0
        and hist[-1] < 0 and hist[-1] < hist[-2]
        and adx_val >= min_adx and minus_di > plus_di
        and last.close < last.open and body_ratio >= 0.45
        and spike_ok
    )
    if not bullish and not bearish:
        return None

    direction = "CALL" if bullish else "PUT"
    reason = (
        f"EMA20/50 aligned; RSI={rsi_now:.1f}; MACD histogram confirms; "
        f"ADX={adx_val:.1f}; candle body={body_ratio:.2f}; no detected bar gap/spike"
    )
    return {
        "direction": direction,
        "score": "7/7 rule checks",
        "reason": reason,
        "rsi": rsi_now,
        "adx": adx_val,
    }


# ------------------------------- Market engine -------------------------------

class SignalEngine:
    def __init__(self, store: Store):
        self.store = store
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=None, write=15, pool=15),
            headers={"Authorization": f"Bearer {OTCHARTS_API_KEY}"} if OTCHARTS_API_KEY else {},
        )
        self.states: dict[str, SymbolState] = {}
        self.application: Optional[Application] = None
        self.paused = False
        self.feed_status = "NOT CONFIGURED"
        self.last_error = ""
        self._last_notice = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    async def _notify_owner(self, message: str) -> None:
        if not self.application:
            return
        owner = self.store.get_setting("owner_chat_id")
        if not owner:
            return
        try:
            await self.application.bot.send_message(chat_id=int(owner), text=message)
        except Exception as exc:  # Telegram may be temporarily unavailable
            log.warning("Could not send owner notification: %s", exc)

    async def _api_get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        response = await self.client.get(f"{BASE_URL}{path}", params=params)
        if response.status_code >= 400:
            snippet = response.text[:400]
            raise RuntimeError(f"OTCharts HTTP {response.status_code}: {snippet}")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("OTCharts returned a non-JSON response") from exc

    async def _catalogue(self) -> dict[str, dict[str, Any]]:
        payload = await self._api_get("/v1/symbols", {"venue": "quotex"})
        return {item.get("symbol"): item for item in payload.get("symbols", [])}

    async def _prepare_watchlist(self) -> None:
        if not OTCHARTS_API_KEY:
            raise RuntimeError("OTCHARTS_API_KEY is missing in .env")
        if not WATCHLIST:
            raise RuntimeError("WATCHLIST is empty in .env")
        if len(WATCHLIST) > 5:
            raise RuntimeError("This starter config supports up to 5 symbols for one Lite stream")

        catalogue = await self._catalogue()
        accepted: list[tuple[str, dict[str, Any]]] = []
        skipped: list[str] = []
        for symbol in WATCHLIST:
            item = catalogue.get(symbol)
            if not item:
                skipped.append(f"{symbol}: not in current Quotex catalogue")
                continue
            if item.get("open") is not True:
                skipped.append(f"{symbol}: closed for trading")
                continue
            payout = item.get("payout")
            if payout is None or float(payout) < MIN_PAYOUT:
                skipped.append(f"{symbol}: payout unavailable or below {MIN_PAYOUT:g}%")
                continue
            accepted.append((symbol, item))

        if skipped:
            await self._notify_owner("⚠️ Watchlist filter notes:\n" + "\n".join(skipped))
        if not accepted:
            raise RuntimeError("No symbols passed open-market and payout filters. Check symbols/plan/payouts.")

        self.states.clear()
        for symbol, item in accepted:
            data = await self._api_get(
                "/v1/candles",
                {"venue": "quotex", "symbol": symbol, "tf": 60, "limit": max(160, MIN_HISTORY_BARS + 20)},
            )
            raw_candles = data.get("candles", [])
            now_bucket = int(time.time() // 60) * 60
            hist: Deque[Candle] = deque(maxlen=500)
            for c in raw_candles:
                try:
                    candle = Candle(
                        time=int(c["time"]), open=float(c["open"]), high=float(c["high"]),
                        low=float(c["low"]), close=float(c["close"]),
                    )
                    if candle.time + 60 <= now_bucket:
                        hist.append(candle)
                except (KeyError, TypeError, ValueError):
                    continue
            if len(hist) < MIN_HISTORY_BARS:
                skipped.append(f"{symbol}: only {len(hist)} complete history bars; need {MIN_HISTORY_BARS}")
                continue
            # Verify last bars are continuous before accepting the history seed.
            tail = list(hist)[-MIN_HISTORY_BARS:]
            if any(tail[i].time - tail[i - 1].time != 60 for i in range(1, len(tail))):
                skipped.append(f"{symbol}: history contains missing 1-minute bars; waiting for clean data")
                continue
            self.states[symbol] = SymbolState(
                symbol=symbol,
                display_name=item.get("name") or symbol,
                payout=float(item["payout"]),
                is_open=True,
                history=hist,
            )

        if not self.states:
            raise RuntimeError("No symbols had enough clean candle history to start Strict mode")

        names = ", ".join(f"{s} ({v.payout:g}%)" for s, v in self.states.items())
        await self._notify_owner(
            "✅ Quotex watchlist prepared.\n"
            f"Symbols: {names}\n"
            "Mode: STRICT · 1-minute paper signals\n"
            "No order execution is enabled."
        )

    async def _refresh_market_meta(self) -> None:
        catalogue = await self._catalogue()
        for symbol, state in self.states.items():
            item = catalogue.get(symbol)
            if not item:
                state.is_open = False
                continue
            payout = item.get("payout")
            state.is_open = item.get("open") is True and payout is not None and float(payout) >= MIN_PAYOUT
            if payout is not None:
                state.payout = float(payout)

    async def run(self, application: Application) -> None:
        self.application = application
        if not OTCHARTS_API_KEY:
            self.feed_status = "WAITING FOR OTCHARTS API KEY"
            await self._notify_owner("ℹ️ Add OTCHARTS_API_KEY to .env, then restart the bot to enable market data.")
            return

        while True:
            try:
                self.feed_status = "CHECKING DATA ACCESS"
                if not self.states:
                    await self._prepare_watchlist()
                else:
                    await self._refresh_market_meta()
                symbols = ",".join(self.states.keys())
                url = f"{BASE_URL}/v1/stream"
                params = {"venue": "quotex", "symbol": symbols}
                self.feed_status = "CONNECTING LIVE READ-ONLY STREAM"
                async with self.client.stream("GET", url, params=params) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        raise RuntimeError(f"OTCharts stream HTTP {response.status_code}: {body}")
                    self.feed_status = "LIVE STREAM CONNECTED · PAPER ONLY"
                    await self._notify_owner("🟢 Live read-only market stream connected. Strict demo/paper analysis is active.")
                    event_name = "message"
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line == "":
                            if data_lines:
                                raw = "\n".join(data_lines)
                                await self._handle_sse(event_name, raw)
                            event_name, data_lines = "message", []
                        elif line.startswith("event:"):
                            event_name = line.partition(":")[2].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line.partition(":")[2].strip())
                raise RuntimeError("Live stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.feed_status = "DISCONNECTED / RECONNECTING"
                self.last_error = str(exc)
                log.exception("Market feed error")
                now = time.time()
                if now - self._last_notice > 60:
                    await self._notify_owner(
                        "🔴 Market feed disconnected. No new signals will be issued until data reconnects.\n"
                        f"Reason: {str(exc)[:350]}"
                    )
                    self._last_notice = now
                # Do not hammer the provider if a key/plan error occurs.
                await asyncio.sleep(60 if any(code in str(exc) for code in ["HTTP 401", "HTTP 402", "HTTP 403"]) else 10)

    async def _handle_sse(self, event_name: str, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if event_name == "connected":
            subscribed = set(data.get("symbols", []))
            for sym, state in self.states.items():
                if sym not in subscribed:
                    state.is_open = False
            log.info("Stream connected for: %s", sorted(subscribed))
            return
        if event_name == "comment":
            return
        if event_name in {"closed", "dropped", "replaced"}:
            log.warning("Stream event %s: %s", event_name, raw[:300])
            await self._notify_owner(f"⚠️ Feed event: {event_name}\n{raw[:500]}\nAffected symbols will not be treated as live.")
            if event_name == "replaced":
                raise RuntimeError("OTCharts stream was replaced by another session")
            affected = data.get("closed", []) if event_name == "closed" else data.get("dropped", [])
            for sym in affected:
                if sym in self.states:
                    self.states[sym].is_open = False
            return
        if event_name == "tick":
            symbol = data.get("symbol")
            if symbol not in self.states:
                return
            try:
                ts = float(data["time"])
                price = float(data["price"])
            except (KeyError, TypeError, ValueError):
                return
            if price <= 0 or ts <= 0:
                return
            await self._on_tick(symbol, ts, price)

    async def _on_tick(self, symbol: str, ts: float, price: float) -> None:
        state = self.states[symbol]
        previous_tick_ts = state.last_tick_ts
        if previous_tick_ts and ts - previous_tick_ts > MAX_INTRA_CANDLE_GAP:
            # The missing ticks may hide an intrabar high/low; reset rather than infer it.
            state.history.clear()
            state.current = None
            state.current_full = False
            log.info("%s: tick gap %.1fs; strict warm-up reset", symbol, ts - previous_tick_ts)
        state.last_tick_ts = ts
        bucket = int(ts // 60) * 60
        if state.current is None:
            state.current = Candle(bucket, price, price, price, price)
            # A candle first observed well after its minute began is partial.
            # Discard the bootstrap sample and wait for 120 clean live bars.
            state.current_full = (ts - bucket) <= 5
            if not state.current_full:
                state.history.clear()
                log.info("%s: stream began mid-candle; strict warm-up reset", symbol)
            return

        if bucket == state.current.time:
            state.current.update(price)
            await self._maybe_settle(state, ts, price)
            return

        if bucket < state.current.time:
            # Out-of-order tick: ignore it; never rewrite a closed paper candle.
            return

        completed = state.current
        delta = bucket - completed.time
        new_candle_full = (ts - bucket) <= 5
        gap_free_transition = delta == 60 and state.current_full and new_candle_full
        if delta == 60 and state.current_full:
            state.history.append(completed)
        elif delta != 60:
            # A feed gap invalidates the rolling sample; never bridge it with fake bars.
            state.history.clear()
        state.current = Candle(bucket, price, price, price, price)
        state.current_full = new_candle_full

        # Settle old paper trade first, using the first tick at/after expiry.
        await self._maybe_settle(state, ts, price)

        if not gap_free_transition or self.paused or not state.is_open:
            return
        if state.pending_signal_id is not None:
            pending_row = self.store.get_signal(state.pending_signal_id)
            if pending_row and pending_row["status"] == "PENDING":
                return
            state.pending_signal_id = None
        if ts - state.last_signal_ts < COOLDOWN_SECONDS:
            return
        if abs(time.time() - ts) > MAX_TICK_AGE:
            return

        setup = analyse_strict(list(state.history), MIN_ADX)
        if not setup:
            return

        # Use the first tick of the new minute as the paper/reference entry.
        signal_id = self.store.create_signal(
            state, setup["direction"], int(ts), price, setup["score"], setup["reason"]
        )
        state.pending_signal_id = signal_id
        state.last_signal_ts = ts
        await self._notify_owner(self._format_signal(state, setup, ts, price, signal_id))

    async def _maybe_settle(self, state: SymbolState, ts: float, price: float) -> None:
        signal_id = state.pending_signal_id
        if not signal_id:
            return
        row = self.store.get_signal(signal_id)
        if not row or row["status"] != "PENDING":
            state.pending_signal_id = None
            return
        due = int(row["expiry_ts"])
        if ts < due:
            return
        delay = ts - due
        if delay > 5:
            status = "VOID"
        else:
            entry = float(row["entry_price"])
            direction = row["direction"]
            if abs(price - entry) < 1e-12:
                status = "TIE"
            elif (direction == "CALL" and price > entry) or (direction == "PUT" and price < entry):
                status = "WIN"
            else:
                status = "LOSS"
        self.store.settle_signal(signal_id, status, price if status != "VOID" else None, int(ts))
        state.pending_signal_id = None
        icon = {"WIN": "✅", "LOSS": "❌", "TIE": "➖", "VOID": "⚪"}[status]
        msg = (
            f"📊 MUNNA AI RESULT · 1 MIN\n\n"
            f"{row['display_name']} · {row['symbol']}\n"
            f"{row['direction']} {icon} {status}\n"
            f"Reference entry: {float(row['entry_price']):.6g}\n"
        )
        if status != "VOID":
            msg += f"Reference exit: {price:.6g}\n"
        else:
            msg += "Result void: feed tick arrived too late after expiry.\n"
        msg += "Paper/feed result only — your manual trade may differ."
        await self._notify_owner(msg)

    @staticmethod
    def _format_signal(state: SymbolState, setup: dict[str, Any], ts: float, price: float, signal_id: int) -> str:
        local_time = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL_TZ)
        arrow = "▲" if setup["direction"] == "CALL" else "▼"
        return (
            "📊 MUNNA AI SIGNAL · STRICT\n\n"
            f"{state.display_name} ({state.symbol})\n"
            f"{arrow} {setup['direction']}\n"
            f"Entry time: {local_time.strftime('%Y-%m-%d %H:%M:%S')} (Dhaka)\n"
            f"Expiry: 1 Minute\n"
            f"Reference entry: {price:.6g}\n"
            f"Payout filter: {state.payout:g}%\n"
            f"Confluence: {setup['score']} (not a win probability)\n\n"
            f"Analysis: {setup['reason']}\n\n"
            "DEMO / PAPER REFERENCE ONLY. Not a guaranteed call; no order is placed."
        )


# -------------------------------- Telegram UI --------------------------------

def get_engine(context: ContextTypes.DEFAULT_TYPE) -> SignalEngine:
    return context.application.bot_data["engine"]


def owner_id(store: Store) -> Optional[int]:
    value = store.get_setting("owner_chat_id")
    return int(value) if value else None


async def require_owner(update: Update, store: Store) -> bool:
    if not update.effective_chat or update.effective_chat.type != "private":
        if update.effective_message:
            await update.effective_message.reply_text("এই bot শুধু private chat-এ ব্যবহার করা যাবে।")
        return False
    current = update.effective_chat.id
    owner = owner_id(store)
    if owner is None:
        return False
    if current != owner:
        if update.effective_message:
            await update.effective_message.reply_text("এই private bot-এর owner access তোমার নেই।")
        return False
    return True


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = get_engine(context)
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.effective_message.reply_text("দয়া করে bot-কে private message করো।")
        return
    owner = owner_id(engine.store)
    if owner is None:
        engine.store.set_setting("owner_chat_id", str(update.effective_chat.id))
        owner = update.effective_chat.id
        log.info("Locked first private chat as bot owner")
    if update.effective_chat.id != owner:
        await update.effective_message.reply_text("এই bot ইতিমধ্যে private owner-এর জন্য lock করা আছে।")
        return
    await update.effective_message.reply_text(
        "স্বাগতম MUNNA AI SIGNALS-এ।\n\n"
        "Mode: STRICT · 1-minute expiry · DEMO/PAPER only\n"
        "Bot নিজে থেকে trade করে না। Signal না এলে WAIT-ই সঠিক সিদ্ধান্ত হতে পারে।\n\n"
        "Commands: /status /report /history /pause /resume /help\n\n"
        "প্রথম owner access এই private chat-এ lock করা হয়েছে। Token কাউকে দিও না।"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = get_engine(context)
    if not await require_owner(update, engine.store):
        return
    await update.effective_message.reply_text(
        "MUNNA AI SIGNALS — Commands\n"
        "/status — feed ও watchlist status\n"
        "/report — paper WIN/LOSS/TIE/VOID count\n"
        "/history — শেষ 10টি signal/result\n"
        "/pause — নতুন signal বন্ধ\n"
        "/resume — strict paper analysis চালু\n"
        "/help — এই নির্দেশনা\n\n"
        "১ মিনিট expiry। No auto-trading. Rule confluence score জয়ের সম্ভাবনা নয়."
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = get_engine(context)
    if not await require_owner(update, engine.store):
        return
    now = time.time()
    rows = []
    for symbol, state in engine.states.items():
        age = now - state.last_tick_ts if state.last_tick_ts else None
        tick_text = f"{age:.0f}s ago" if age is not None else "no tick yet"
        rows.append(f"• {symbol}: open={state.is_open}, payout={state.payout:g}%, tick={tick_text}")
    report = engine.store.report()
    text = (
        "🟢 MUNNA AI STATUS\n\n"
        f"Feed: {engine.feed_status}\n"
        f"Mode: {'PAUSED' if engine.paused else 'STRICT / PAPER'}\n"
        f"Expiry: 1 minute\n"
        f"Watchlist: {len(engine.states)} active symbol(s)\n"
        + ("\n".join(rows) if rows else "No eligible symbols loaded.")
        + f"\n\nPaper records: WIN {report['WIN']} · LOSS {report['LOSS']} · TIE {report['TIE']} · VOID {report['VOID']}"
    )
    if engine.last_error:
        text += f"\nLast feed error: {engine.last_error[:250]}"
    await update.effective_message.reply_text(text)


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = get_engine(context)
    if not await require_owner(update, engine.store):
        return
    r = engine.store.report()
    settled = r["WIN"] + r["LOSS"]
    accuracy = (100.0 * r["WIN"] / settled) if settled else 0.0
    await update.effective_message.reply_text(
        "📈 PAPER PERFORMANCE\n\n"
        f"WIN: {r['WIN']}\nLOSS: {r['LOSS']}\nTIE: {r['TIE']}\nVOID: {r['VOID']}\nPENDING: {r['PENDING']}\n\n"
        f"Win rate (WIN / WIN+LOSS): {accuracy:.1f}%\n"
        "TIE/VOID excluded. This is a historical paper statistic, not a forecast or guarantee."
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = get_engine(context)
    if not await require_owner(update, engine.store):
        return
    rows = engine.store.history(10)
    if not rows:
        await update.effective_message.reply_text("এখনো কোনো signal record নেই।")
        return
    lines = ["📚 LAST SIGNALS · PAPER RECORD"]
    for row in rows:
        dt = datetime.fromtimestamp(row["entry_ts"], timezone.utc).astimezone(LOCAL_TZ)
        icon = {"WIN": "✅", "LOSS": "❌", "TIE": "➖", "VOID": "⚪", "PENDING": "⏳"}.get(row["status"], "•")
        lines.append(
            f"{dt.strftime('%d %b %H:%M:%S')} · {row['symbol']} · {row['direction']} · {icon} {row['status']}"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = get_engine(context)
    if not await require_owner(update, engine.store):
        return
    engine.paused = True
    await update.effective_message.reply_text("⏸️ New signals paused. Pending paper results will still be tracked.")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = get_engine(context)
    if not await require_owner(update, engine.store):
        return
    engine.paused = False
    await update.effective_message.reply_text("▶️ Strict paper analysis resumed. No guarantee of a signal.")


async def post_init(application: Application) -> None:
    store = Store(DB_PATH)
    engine = SignalEngine(store)
    application.bot_data["engine"] = engine
    application.create_task(engine.run(application), name="market-data-feed")
    await application.bot.set_my_commands([
        ("start", "Start private bot"),
        ("status", "Feed and watchlist status"),
        ("report", "Paper performance"),
        ("history", "Recent paper signals"),
        ("pause", "Pause new signals"),
        ("resume", "Resume strict analysis"),
        ("help", "Help and commands"),
    ])


async def post_shutdown(application: Application) -> None:
    engine = application.bot_data.get("engine")
    if engine:
        await engine.close()


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Edit .env first.")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    log.info("Starting MUNNA AI Signals bot (strict / 1m / paper-only)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
