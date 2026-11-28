import os
import time
from datetime import datetime, timezone
from urllib import error, parse, request

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SYMBOL = os.environ.get("TRADE_SYMBOL", "BTCUSDT").strip().upper()
INTERVAL = os.environ.get("TRADE_INTERVAL", "5m").strip()
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
MAX_CYCLES = int(os.environ.get("MAX_CYCLES", "0"))
ONCE = os.environ.get("RUN_ONCE", "0").strip() in ("1", "true", "yes")

ENDPOINTS = [
    f"https://data-api.binance.vision/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=30",
    f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=30",
    f"https://api1.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=30",
    f"https://api2.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=30",
    f"https://api3.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=30",
]


def send_telegram_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
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


def fetch_klines() -> list:
    last_err = None
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        "Accept": "application/json",
    }
    for url in ENDPOINTS:
        try:
            req = request.Request(url, headers=headers)
            with request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    import json

                    data = json.loads(resp.read().decode("utf-8"))
                    if isinstance(data, list) and len(data) >= 20:
                        return data
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"All market endpoints failed: {last_err}")


def compute_ema(values: list[float], period: int) -> list[float]:
    k = 2.0 / (period + 1)
    ema = [values[0]]
    for price in values[1:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def evaluate_signal(klines: list) -> dict | None:
    closed = klines[:-1]
    if len(closed) < 22:
        return None
    closes = [float(c[4]) for c in closed]
    highs = [float(c[2]) for c in closed]
    lows = [float(c[3]) for c in closed]

    ema_fast = compute_ema(closes, 7)
    ema_slow = compute_ema(closes, 21)

    prev_fast, curr_fast = ema_fast[-2], ema_fast[-1]
    prev_slow, curr_slow = ema_slow[-2], ema_slow[-1]
    price = closes[-1]
    atr_like = sum(highs[i] - lows[i] for i in range(-14, 0)) / 14.0
    if atr_like <= 0:
        atr_like = max(price * 0.003, 1.0)

    if prev_fast <= prev_slow and curr_fast > curr_slow and price > curr_slow:
        sl = price - atr_like * 1.5
        tp = price + atr_like * 2.5
        return {
            "side": "BUY / LONG",
            "entry": price,
            "sl": sl,
            "tp": tp,
            "close_time": int(closed[-1][6]),
        }

    if prev_fast >= prev_slow and curr_fast < curr_slow and price < curr_slow:
        sl = price + atr_like * 1.5
        tp = price - atr_like * 2.5
        return {
            "side": "SELL / SHORT",
            "entry": price,
            "sl": sl,
            "tp": tp,
            "close_time": int(closed[-1][6]),
        }

    return None


def main() -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] Starting signal monitor for {SYMBOL} ({INTERVAL})")
    last_sent_key = ""
    cycles = 0

    while True:
        cycles += 1
        try:
            klines = fetch_klines()
            sig = evaluate_signal(klines)
            if sig:
                key = f"{sig['side']}:{sig['close_time']}"
                if key != last_sent_key:
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    msg = (
                        f"TRADE SIGNAL: {sig['side']}\n"
                        f"Pair: {SYMBOL} ({INTERVAL})\n"
                        f"Entry: {sig['entry']:.2f}\n"
                        f"Stop Loss: {sig['sl']:.2f}\n"
                        f"Take Profit: {sig['tp']:.2f}\n"
                        f"Time: {ts}"
                    )
                    send_telegram_message(msg)
                    last_sent_key = key
                    print(f"[{ts}] Sent signal: {key}")
            else:
                print("No trade signal right now.")
        except error.HTTPError as exc:
            print(f"HTTP error {exc.code}: {exc.reason}")
        except Exception as exc:
            print(f"Error: {exc}")

        if ONCE or (MAX_CYCLES > 0 and cycles >= MAX_CYCLES):
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
