import json
import os
import time
from datetime import datetime, timezone
from urllib import parse, request

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SYMBOL = os.environ.get("TRADE_SYMBOL", "BTCUSDT").strip().upper()
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
ONCE = os.environ.get("RUN_ONCE", "0").strip() in ("1", "true", "yes")
STATE_FILE = os.environ.get("STATE_FILE", "signal_state.json")

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}

ENDPOINTS = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
    "https://api1.binance.com/api/v3/klines",
    "https://api2.binance.com/api/v3/klines",
    "https://api3.binance.com/api/v3/klines",
]


def current_month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def load_state(now_dt: datetime) -> dict:
    m_key = current_month_key(now_dt)
    default_state = {
        "month": m_key,
        "total_signals": 0,
        "wins": 0,
        "losses": 0,
        "open_trades": [],
        "sent_keys": [],
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("month") != m_key:
            return default_state
        data.setdefault("total_signals", 0)
        data.setdefault("wins", 0)
        data.setdefault("losses", 0)
        data.setdefault("open_trades", [])
        data.setdefault("sent_keys", [])
        return data
    except Exception:
        return default_state


def save_state(state: dict) -> None:
    tmp_path = f"{STATE_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, STATE_FILE)


def send_telegram_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram HTTP {resp.status}")


def fetch_klines(interval: str, limit: int = 80) -> list:
    last_err = None
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        "Accept": "application/json",
    }
    for base in ENDPOINTS:
        url = f"{base}?symbol={SYMBOL}&interval={interval}&limit={limit}"
        try:
            req = request.Request(url, headers=headers)
            with request.urlopen(req, timeout=12) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if isinstance(data, list) and len(data) >= 35:
                        return data
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"Market fetch failed ({interval}): {last_err}")


def ema(values: list[float], period: int) -> list[float]:
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    val = sum(trs[-period:]) / max(1, min(period, len(trs)))
    return val if val > 0 else max(closes[-1] * 0.002, 0.5)


def analyze_series(klines: list, use_forming_candle: bool = False) -> dict:
    # Closed candles exclude the still-forming last kline unless pre-signal uses live forming candle
    base_klines = klines if use_forming_candle else klines[:-1]
    closes = [float(c[4]) for c in base_klines]
    highs = [float(c[2]) for c in base_klines]
    lows = [float(c[3]) for c in base_klines]
    volumes = [float(c[5]) for c in base_klines]

    ema_fast = ema(closes, 9)
    ema_mid = ema(closes, 21)
    ema_trend = ema(closes, 50) if len(closes) >= 50 else ema_mid
    cur_rsi = rsi(closes, 14)
    cur_atr = atr(highs, lows, closes, 14)
    avg_vol = sum(volumes[-20:]) / max(1, min(20, len(volumes)))
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    price = closes[-1]
    bull_score = 0.0
    bear_score = 0.0

    if ema_fast[-1] > ema_mid[-1]:
        bull_score += 1.5
    else:
        bear_score += 1.5

    if price > ema_trend[-1]:
        bull_score += 1.5
    else:
        bear_score += 1.5

    if ema_fast[-1] > ema_fast[-2] and ema_mid[-1] >= ema_mid[-2]:
        bull_score += 1.0
    elif ema_fast[-1] < ema_fast[-2] and ema_mid[-1] <= ema_mid[-2]:
        bear_score += 1.0

    if 45.0 <= cur_rsi <= 72.0:
        bull_score += 1.0
    if 28.0 <= cur_rsi <= 55.0:
        bear_score += 1.0

    if vol_ratio >= 0.9:
        bull_score += 0.5
        bear_score += 0.5

    # Always resolve to BUY or SELL (never WAIT)
    if bull_score >= bear_score:
        side = "BUY"
        sl = price - (cur_atr * 1.5)
        tp = price + (cur_atr * 2.5)
    else:
        side = "SELL"
        sl = price + (cur_atr * 1.5)
        tp = price - (cur_atr * 2.5)

    return {
        "side": side,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "rsi": cur_rsi,
        "atr": cur_atr,
        "open_time": int(base_klines[-1][0]),
        "close_time": int(base_klines[-1][6]),
    }


def settle_open_trades(state: dict, klines_1m: list) -> None:
    if not state["open_trades"]:
        return
    closed_1m = klines_1m[:-1]
    still_open = []
    for tr in state["open_trades"]:
        resolved = False
        for c in closed_1m:
            c_close_t = int(c[6])
            if c_close_t <= tr["entry_close_time"]:
                continue
            c_high = float(c[2])
            c_low = float(c[3])
            if tr["side"] == "BUY":
                if c_low <= tr["sl"]:
                    state["losses"] += 1
                    resolved = True
                    break
                if c_high >= tr["tp"]:
                    state["wins"] += 1
                    resolved = True
                    break
            else:  # SELL
                if c_high >= tr["sl"]:
                    state["losses"] += 1
                    resolved = True
                    break
                if c_low <= tr["tp"]:
                    state["wins"] += 1
                    resolved = True
                    break
        if not resolved:
            still_open.append(tr)
    state["open_trades"] = still_open


def format_monthly_footer(state: dict) -> str:
    return (
        f"\n📊 Monthly ({state['month']}): "
        f"Total={state['total_signals']} | "
        f"Win={state['wins']} | "
        f"Loss={state['losses']}"
    )


def run_cycle() -> None:
    now_dt = datetime.now(timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)
    state = load_state(now_dt)

    klines_by_tf = {}
    for tf in TIMEFRAMES:
        try:
            klines_by_tf[tf] = fetch_klines(tf, limit=80)
        except Exception as exc:
            print(f"[{tf}] fetch warning: {exc}")

    if "1m" in klines_by_tf:
        settle_open_trades(state, klines_by_tf["1m"])

    # 1) Pre-signal 1 minute before candle close (e.g. minute 21 for minute 22)
    for tf, klines in klines_by_tf.items():
        forming_close_ms = int(klines[-1][6])
        ms_left = forming_close_ms - now_ms
        if 0 < ms_left <= 65_000:
            pre_key = f"PRE:{tf}:{forming_close_ms}"
            if pre_key not in state["sent_keys"]:
                pred = analyze_series(klines, use_forming_candle=True)
                msg = (
                    f"⏳ PRE-SIGNAL ({tf} closing soon)\n"
                    f"Pair: {SYMBOL}\n"
                    f"Next Bias: {pred['side']}\n"
                    f"Est. Entry: {pred['entry']:.2f}\n"
                    f"SL: {pred['sl']:.2f} | TP: {pred['tp']:.2f}"
                    f"{format_monthly_footer(state)}"
                )
                send_telegram_message(msg)
                state["sent_keys"].append(pre_key)

    # 2) Confirmed closed-candle signals across all requested timeframes (including weekly start)
    is_week_start = (now_dt.weekday() == 0 and now_dt.hour < 6)
    for tf, klines in klines_by_tf.items():
        closed_candle = klines[-2]
        closed_close_ms = int(closed_candle[6])
        sig_key = f"SIG:{tf}:{closed_close_ms}"
        if sig_key in state["sent_keys"]:
            continue

        # On 1w, prioritize early week window or fresh weekly close
        if tf == "1w" and not is_week_start and (now_ms - closed_close_ms) > 86_400_000:
            continue

        sig = analyze_series(klines, use_forming_candle=False)
        state["total_signals"] += 1
        state["open_trades"].append(
            {
                "tf": tf,
                "side": sig["side"],
                "entry": sig["entry"],
                "sl": sig["sl"],
                "tp": sig["tp"],
                "entry_close_time": closed_close_ms,
            }
        )
        state["sent_keys"].append(sig_key)

        ts_str = now_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = (
            f"🎯 TRADE SIGNAL: {sig['side']}\n"
            f"Pair: {SYMBOL} ({tf})\n"
            f"Entry: {sig['entry']:.2f}\n"
            f"Stop Loss: {sig['sl']:.2f}\n"
            f"Take Profit: {sig['tp']:.2f}\n"
            f"RSI(14): {sig['rsi']:.1f}\n"
            f"Time: {ts_str}"
            f"{format_monthly_footer(state)}"
        )
        send_telegram_message(msg)

    # Keep sent_keys bounded
    if len(state["sent_keys"]) > 500:
        state["sent_keys"] = state["sent_keys"][-500:]

    save_state(state)
    print(
        f"[{now_dt.strftime('%H:%M:%S UTC')}] Done | "
        f"Total={state['total_signals']} W={state['wins']} L={state['losses']}"
    )


def main() -> None:
    while True:
        try:
            run_cycle()
        except Exception as exc:
            print(f"Cycle error: {exc}")
        if ONCE:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
