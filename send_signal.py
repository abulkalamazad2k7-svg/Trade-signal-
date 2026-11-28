import json
import os
import time
from datetime import datetime, timezone
from urllib import parse, request

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SYMBOL = os.environ.get("TRADE_SYMBOL", "BTCUSDT").strip().upper()
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "15"))
ONCE = os.environ.get("RUN_ONCE", "0").strip() in ("1", "true", "yes")
STATE_FILE = os.environ.get("STATE_FILE", "signal_state.json")

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "1d"]

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


def evaluate_signal(klines: list, use_forming: bool = True) -> dict | None:
    series = klines if use_forming else klines[:-1]
    closes = [float(c[4]) for c in series]
    highs = [float(c[2]) for c in series]
    lows = [float(c[3]) for c in series]
    volumes = [float(c[5]) for c in series]

    if len(closes) < 25:
        return None

    ema_fast = ema(closes, 9)
    ema_mid = ema(closes, 21)
    ema_trend = ema(closes, 50) if len(closes) >= 50 else ema_mid
    cur_rsi = rsi(closes, 14)
    cur_atr = atr(highs, lows, closes, 14)
    avg_vol = sum(volumes[-21:-1]) / max(1, min(20, len(volumes) - 1))
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    price = closes[-1]
    open_ms = int(series[-1][0])
    close_ms = int(series[-1][6])

    buy_ok = (
        ema_fast[-1] > ema_mid[-1]
        and ema_fast[-1] >= ema_fast[-2]
        and price > ema_mid[-1]
        and price >= ema_trend[-1] * 0.999
        and 50.0 <= cur_rsi <= 75.0
        and vol_ratio >= 0.85
    )

    sell_ok = (
        ema_fast[-1] < ema_mid[-1]
        and ema_fast[-1] <= ema_fast[-2]
        and price < ema_mid[-1]
        and price <= ema_trend[-1] * 1.001
        and 25.0 <= cur_rsi <= 50.0
        and vol_ratio >= 0.85
    )

    if buy_ok:
        side = "BUY"
        sl = price - (cur_atr * 1.5)
        tp = price + (cur_atr * 2.5)
    elif sell_ok:
        side = "SELL"
        sl = price + (cur_atr * 1.5)
        tp = price - (cur_atr * 2.5)
    else:
        return None

    return {
        "side": side,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "rsi": cur_rsi,
        "open_time": open_ms,
        "close_time": close_ms,
    }


def is_due_now(tf: str, klines: list, now_ms: int) -> tuple[bool, bool, int]:
    """
    Returns (should_check, use_forming_candle, target_event_ms).
    - Intraday TFs (1m, 5m, 15m, 30m, 1h): triggers during final 65s before candle close.
    - Daily TF (1d): triggers near day start (within first 20m of new daily open candle).
    """
    forming_open_ms = int(klines[-1][0])
    forming_close_ms = int(klines[-1][6])

    if tf == "1d":
        elapsed_since_day_open = now_ms - forming_open_ms
        if 0 <= elapsed_since_day_open <= 1_200_000:
            return True, True, forming_open_ms
        return False, True, forming_open_ms

    ms_left = forming_close_ms - now_ms
    if 0 < ms_left <= 65_000:
        return True, True, forming_close_ms
    return False, True, forming_close_ms


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
            else:
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

    for tf, klines in klines_by_tf.items():
        due, use_forming, event_ms = is_due_now(tf, klines, now_ms)
        if not due:
            continue

        sig_key = f"SIG:{tf}:{event_ms}"
        if sig_key in state["sent_keys"]:
            continue

        sig = evaluate_signal(klines, use_forming=use_forming)
        if not sig:
            continue

        state["total_signals"] += 1
        state["open_trades"].append(
            {
                "tf": tf,
                "side": sig["side"],
                "entry": sig["entry"],
                "sl": sig["sl"],
                "tp": sig["tp"],
                "entry_close_time": event_ms,
            }
        )
        state["sent_keys"].append(sig_key)

        target_dt = datetime.fromtimestamp(event_ms / 1000.0, tz=timezone.utc)
        target_str = target_dt.strftime("%Y-%m-%d %H:%M UTC")
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

        msg = (
            f"🎯 TRADE SIGNAL ({tf} @ {target_str}): {sig['side']}\n"
            f"Pair: {SYMBOL}\n"
            f"Entry: {sig['entry']:.2f}\n"
            f"Stop Loss: {sig['sl']:.2f}\n"
            f"Take Profit: {sig['tp']:.2f}\n"
            f"RSI(14): {sig['rsi']:.1f}\n"
            f"Sent At: {now_str}"
            f"{format_monthly_footer(state)}"
        )
        send_telegram_message(msg)

    if len(state["sent_keys"]) > 600:
        state["sent_keys"] = state["sent_keys"][-600:]

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
