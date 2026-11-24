import os, json, argparse, bisect
import ccxt

API_KEY = os.getenv("BYBIT_API_KEY", "")
SECRET_KEY = os.getenv("BYBIT_SECRET_KEY", "")
USE_DEMO = os.getenv("BYBIT_DEMO", "false").lower() == "true"

WATCHLIST = [
    "SOL/USDT:USDT",
    "SUI/USDT:USDT",
    "DOGE/USDT:USDT",
    "XRP/USDT:USDT",
    "1000PEPE/USDT:USDT",
    "WIF/USDT:USDT",
]
MAX_POSITIONS = 2
MIN_FREE_USDT = 3.6
MAX_LEVERAGE = 10

DEFAULT_MARGIN = 3.5
DEFAULT_LEVERAGE = 5
DEFAULT_TP = 15.0        # persen dari harga
DEFAULT_SL = 5.0         # persen dari harga
DEFAULT_BE_TRIGGER = 7.5 # geser SL ke break-even setelah profit segini (persen harga)

VOL_MIN_RATIO = 1.2
FUNDING_MAX = 0.0005     # 0.05%/8 jam
TAKER_FEE = 0.00055      # per sisi
SLIPPAGE = 0.0003        # per sisi
FUNDING_PER_HOUR = 0.0001 / 8   # asumsi funding 0.01%/8 jam

exchange = ccxt.bybit({
    "apiKey": API_KEY,
    "secret": SECRET_KEY,
    "enableRateLimit": True,
    "options": {"defaultType": "linear", "recvWindow": 10000},
})
if USE_DEMO:
    exchange.enable_demo_trading(True)


# ---------- Indikator ----------
def ema_series(prices, period):
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    out = [ema]
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
        out.append(ema)
    return out


def calc_ema(prices, period):
    s = ema_series(prices, period)
    return s[-1] if s else None


def calc_rsi(prices, period=14):
    """RSI Wilder."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ---------- Sinyal ----------
def trend_4h(c4h):
    """Return 'UP' / 'DOWN' / None berdasarkan harga vs EMA50 di 4H (candle closed)."""
    if len(c4h) < 60:
        return None
    closes = [c[4] for c in c4h]
    ema50 = calc_ema(closes, 50)
    if ema50 is None:
        return None
    return "UP" if closes[-1] > ema50 else "DOWN"


def get_signal(c1h, c4h):
    """c1h dan c4h: HANYA candle yang sudah close. Return dict atau None."""
    if len(c1h) < 60:
        return None
    t4 = trend_4h(c4h)
    if t4 is None:
        return None

    closes = [c[4] for c in c1h]
    volumes = [c[5] for c in c1h]
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    rsi = calc_rsi(closes)
    atr = calc_atr(c1h)
    avg_vol = sum(volumes[-21:-1]) / 20
    if None in (ema9, ema21, rsi, atr) or avg_vol <= 0:
        return None

    vol_ratio = volumes[-1] / avg_vol
    price = closes[-1]

    side = None
    if t4 == "UP" and ema9 > ema21 and price > ema9 and 52 <= rsi <= 68 and vol_ratio >= VOL_MIN_RATIO:
        side = "LONG"
    elif t4 == "DOWN" and ema9 < ema21 and price < ema9 and 32 <= rsi <= 48 and vol_ratio >= VOL_MIN_RATIO:
        side = "SHORT"
    if side is None:
        return None

    return {
        "side": side, "price": price, "trend_4h": t4,
        "ema9": ema9, "ema21": ema21, "rsi": rsi,
        "vol_ratio": vol_ratio, "atr_pct": atr / price * 100,
    }


def fetch_closed(sym, tf, limit):
    """Ambil candle dan buang yang masih berjalan."""
    return exchange.fetch_ohlcv(sym, timeframe=tf, limit=limit + 1)[:-1]


def to_ccxt_symbol(symbol_raw):
    base = symbol_raw.upper().replace("USDT", "")
    if base == "PEPE":
        base = "1000PEPE"
    return f"{base}/USDT:USDT"


# ---------- Scan ----------
def scan_market():
    balance = exchange.fetch_balance()
    free_usdt = float(balance.get("USDT", {}).get("free", 0) or 0)

    raw_positions = exchange.fetch_positions(WATCHLIST)
    active = [p for p in raw_positions if float(p.get("contracts") or 0) > 0]
    active_symbols = [p["symbol"] for p in active]

    if len(active) >= MAX_POSITIONS or free_usdt < MIN_FREE_USDT:
        print(json.dumps({
            "status": "FULL_OR_LOW_BALANCE",
            "mode": "DEMO" if USE_DEMO else "LIVE",
            "free_usdt": round(free_usdt, 2),
            "active_count": len(active),
            "active_symbols": active_symbols,
        }))
        return

    candidates, errors = [], []
    for sym in WATCHLIST:
        if sym in active_symbols:
            continue
        try:
            sig = get_signal(fetch_closed(sym, "1h", 200), fetch_closed(sym, "4h", 100))
            if not sig:
                continue
            funding = exchange.fetch_funding_rate(sym).get("fundingRate") or 0
            if sig["side"] == "LONG" and funding > FUNDING_MAX:
                continue
            if sig["side"] == "SHORT" and funding < -FUNDING_MAX:
                continue
            candidates.append({
                "symbol": sym.replace("/USDT:USDT", "USDT"),
                "side": sig["side"],
                "price": sig["price"],
                "trend_4h": sig["trend_4h"],
                "rsi_1h": round(sig["rsi"], 2),
                "volume_spike_x": round(sig["vol_ratio"], 2),
                "atr_pct_1h": round(sig["atr_pct"], 3),
                "funding_rate": funding,
            })
        except Exception as e:
            errors.append({"symbol": sym, "error": str(e)[:120]})

    candidates.sort(key=lambda c: c["volume_spike_x"], reverse=True)
    out = {"mode": "DEMO" if USE_DEMO else "LIVE", "free_usdt": round(free_usdt, 2)}
    if candidates:
        out["status"] = "CANDIDATE_FOUND"
        out["candidates"] = candidates[:2]
    else:
        out["status"] = "NO_SETUP"
    if errors:
        out["errors"] = errors
    print(json.dumps(out))


# ---------- Eksekusi ----------
def execute_trade(symbol_raw, side, margin, leverage, tp_pct, sl_pct):
    ccxt_sym = to_ccxt_symbol(symbol_raw)
    if ccxt_sym not in WATCHLIST:
        print(json.dumps({"status": "ERROR", "reason": "symbol di luar watchlist"}))
        return
    if side.upper() not in ("LONG", "SHORT"):
        print(json.dumps({"status": "ERROR", "reason": "side harus LONG atau SHORT"}))
        return
    if leverage > MAX_LEVERAGE or sl_pct * leverage > 50:
        print(json.dumps({
            "status": "ERROR",
            "reason": f"leverage terlalu tinggi atau SL terlalu lebar (maks lev {MAX_LEVERAGE}, SL x lev <= 50)",
        }))
        return

    exchange.load_markets()

    raw_positions = exchange.fetch_positions(WATCHLIST)
    active = [p for p in raw_positions if float(p.get("contracts") or 0) > 0]
    if ccxt_sym in [p["symbol"] for p in active] or len(active) >= MAX_POSITIONS:
        print(json.dumps({"status": "SKIPPED", "reason": "posisi sudah ada atau penuh"}))
        return

    try:
        exchange.set_margin_mode("isolated", ccxt_sym, params={"leverage": int(leverage)})
    except Exception:
        pass
    try:
        exchange.set_leverage(int(leverage), ccxt_sym)
    except Exception:
        pass

    price = float(exchange.fetch_ticker(ccxt_sym)["last"])
    notional = float(margin) * int(leverage)
    amount = float(exchange.amount_to_precision(ccxt_sym, notional / price))

    market = exchange.market(ccxt_sym)
    min_amt = (market.get("limits", {}).get("amount", {}) or {}).get("min") or 0
    min_cost = (market.get("limits", {}).get("cost", {}) or {}).get("min") or 0
    if amount < min_amt or amount * price < min_cost:
        print(json.dumps({
            "status": "ERROR", "reason": "ukuran order di bawah minimum",
            "amount": amount, "min_amount": min_amt, "min_cost": min_cost,
        }))
        return

    is_long = side.upper() == "LONG"
    sgn = 1 if is_long else -1
    tp_price = exchange.price_to_precision(ccxt_sym, price * (1 + sgn * float(tp_pct) / 100))
    sl_price = exchange.price_to_precision(ccxt_sym, price * (1 - sgn * float(sl_pct) / 100))

    order = exchange.create_order(
        symbol=ccxt_sym,
        type="market",
        side="buy" if is_long else "sell",
        amount=amount,
        params={
            "takeProfit": str(tp_price),
            "stopLoss": str(sl_price),
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "tpslMode": "Full",
        },
    )

    print(json.dumps({
        "status": "EXECUTED",
        "mode": "DEMO" if USE_DEMO else "LIVE",
        "symbol": symbol_raw,
        "side": side.upper(),
        "amount": amount,
        "est_entry": price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "order_id": order.get("id"),
    }))


# ---------- Manage: geser SL ke break-even ----------
def manage_positions(be_trigger):
    """
    Jalankan berkala (misal tiap 15 menit lewat cron/pm2).
    Kalau profit posisi >= be_trigger% dari harga masuk, SL digeser ke harga masuk (+buffer fee).
    """
    raw_positions = exchange.fetch_positions(WATCHLIST)
    active = [p for p in raw_positions if float(p.get("contracts") or 0) > 0]
    results = []
    for p in active:
        try:
            entry = float(p.get("entryPrice") or 0)
            mark = float(p.get("markPrice") or 0)
            if entry <= 0 or mark <= 0:
                continue
            is_long = (p.get("side") or "").lower() == "long"
            move_pct = (mark - entry) / entry * 100 * (1 if is_long else -1)
            cur_sl = float((p.get("info") or {}).get("stopLoss") or 0)

            already_be = (cur_sl >= entry) if is_long else (0 < cur_sl <= entry)
            if move_pct < be_trigger or already_be:
                results.append({"symbol": p["symbol"], "move_pct": round(move_pct, 2), "action": "none"})
                continue

            buffer = 0.001  # 0.1% untuk menutup fee
            new_sl = entry * (1 + buffer) if is_long else entry * (1 - buffer)
            new_sl = exchange.price_to_precision(p["symbol"], new_sl)
            exchange.private_post_v5_position_trading_stop({
                "category": "linear",
                "symbol": p["info"]["symbol"],
                "stopLoss": str(new_sl),
                "slTriggerBy": "LastPrice",
                "tpslMode": "Full",
                "positionIdx": 0,
            })
            results.append({"symbol": p["symbol"], "move_pct": round(move_pct, 2),
                            "action": "SL_MOVED_TO_BE", "new_sl": new_sl})
        except Exception as e:
            results.append({"symbol": p.get("symbol"), "error": str(e)[:150]})
    print(json.dumps({"status": "MANAGED", "mode": "DEMO" if USE_DEMO else "LIVE", "positions": results}))


# ---------- Backtest ----------
def backtest(symbol_raw, tp_pct, sl_pct, leverage, be_trigger, max_hold_hours=240):
    """
    Uji sinyal yang sama di data historis 1H (~41 hari) dengan filter 4H.
    Masuk di open candle berikutnya. Jika TP dan SL kena di candle yang sama -> SL duluan.
    be_trigger > 0: SL digeser ke break-even setelah profit mencapai be_trigger%.
    Fee, slippage, dan funding dihitung.
    """
    sym = to_ccxt_symbol(symbol_raw)
    d1 = exchange.fetch_ohlcv(sym, timeframe="1h", limit=1000)[:-1]
    d4 = exchange.fetch_ohlcv(sym, timeframe="4h", limit=1000)[:-1]
    if len(d1) < 150 or len(d4) < 60:
        print(json.dumps({"symbol": symbol_raw, "error": "data tidak cukup"}))
        return

    h1_ms, h4_ms = 3600 * 1000, 4 * 3600 * 1000
    close_ts_4h = [c[0] + h4_ms for c in d4]   # waktu 4H candle selesai
    fee_pct = (TAKER_FEE + SLIPPAGE) * 2 * 100

    trades, i = [], 100
    while i < len(d1) - 2:
        now_end = d1[i][0] + h1_ms
        k = bisect.bisect_right(close_ts_4h, now_end)
        sig = get_signal(d1[: i + 1], d4[:k])
        if not sig:
            i += 1
            continue

        entry = d1[i + 1][1]
        is_long = sig["side"] == "LONG"
        sgn = 1 if is_long else -1
        tp = entry * (1 + sgn * tp_pct / 100)
        sl = entry * (1 - sgn * sl_pct / 100)
        be_done = False
        result, exit_idx = None, min(i + 1 + max_hold_hours, len(d1) - 1)

        for j in range(i + 1, exit_idx + 1):
            hi, lo = d1[j][2], d1[j][3]
            hit_sl = lo <= sl if is_long else hi >= sl
            hit_tp = hi >= tp if is_long else lo <= tp
            if hit_sl:
                result = (sl - entry) / entry * 100 * sgn
                exit_idx = j
                break
            if hit_tp:
                result, exit_idx = tp_pct, j
                break
            if be_trigger > 0 and not be_done:
                fav = (hi - entry) / entry * 100 if is_long else (entry - lo) / entry * 100
                if fav >= be_trigger:
                    sl = entry * (1 + sgn * 0.001)
                    be_done = True
        if result is None:
            move = (d1[exit_idx][4] - entry) / entry * 100 * sgn
            result = move

        hours_held = exit_idx - i
        funding_cost = FUNDING_PER_HOUR * hours_held * 100
        trades.append({"pnl": result - fee_pct - funding_cost, "hours": hours_held})
        i = exit_idx + 1

    if not trades:
        print(json.dumps({"symbol": symbol_raw, "trades": 0}))
        return

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    print(json.dumps({
        "symbol": symbol_raw,
        "candles_1h": len(d1),
        "settings": {"tp": tp_pct, "sl": sl_pct, "lev": leverage, "be_trigger": be_trigger},
        "trades": len(trades),
        "winrate_pct": round(len(wins) / len(trades) * 100, 1),
        "avg_hold_hours": round(sum(t["hours"] for t in trades) / len(trades), 1),
        "avg_pnl_pct_price": round(sum(pnls) / len(pnls), 3),
        "total_pnl_pct_margin": round(sum(pnls) * leverage, 1),
        "max_drawdown_pct_margin": round(max_dd * leverage, 1),
    }))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["scan", "execute", "manage", "backtest"])
    parser.add_argument("--symbol")
    parser.add_argument("--side")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    parser.add_argument("--tp", type=float, default=DEFAULT_TP)
    parser.add_argument("--sl", type=float, default=DEFAULT_SL)
    parser.add_argument("--be", type=float, default=DEFAULT_BE_TRIGGER,
                        help="geser SL ke break-even setelah profit sekian persen (0 = mati; di backtest juga berlaku)")
    args = parser.parse_args()

    if args.action == "scan":
        scan_market()
    elif args.action == "execute":
        execute_trade(args.symbol, args.side, args.margin, args.leverage, args.tp, args.sl)
    elif args.action == "manage":
        manage_positions(args.be)
    elif args.action == "backtest":
        backtest(args.symbol, args.tp, args.sl, args.leverage, args.be)
