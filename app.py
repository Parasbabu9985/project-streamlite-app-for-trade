import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time
from streamlit_autorefresh import st_autorefresh
from SmartApi import SmartConnect
import pyotp
import requests

# ---- NumPy >=2.0 compatibility for pandas_ta (imports NaN) ----
if not hasattr(np, "NaN"):
    np.NaN = np.nan
import pandas_ta as ta

# ===================== USER CONFIG =====================
API_KEY = ""
CLIENT_ID = "
TOTP_SECRET = ""
CLIENT_PASSWORD = ""

BOT_TOKEN = ""
CHAT_ID = ""

# ===================== UI / REFRESH =====================
st.set_page_config(page_title="NIFTY/BANKNIFTY Signal App", layout="wide")
st_autorefresh(interval=10_000, key="datarefresh")  # every 10s
st.title("📈 Live NIFTY/BANKNIFTY Signal App")

# Sidebar controls
st.sidebar.header("⚙️ Settings")

symbol = st.sidebar.selectbox("Select Index", ["NIFTY", "BANKNIFTY"], index=0)

interval_label = st.sidebar.selectbox(
    "Interval",
    ["1 Minute", "5 Minute", "15 Minute", "30 Minute", "1 Hour", "1 Day"],
    index=1,
)

# Map UI interval to SmartAPI interval
INTERVAL_MAP = {
    "1 Minute": "ONE_MINUTE",
    "5 Minute": "FIVE_MINUTE",
    "15 Minute": "FIFTEEN_MINUTE",
    "30 Minute": "THIRTY_MINUTE",
    "1 Hour": "ONE_HOUR",
    "1 Day": "ONE_DAY",
}
interval = INTERVAL_MAP[interval_label]

# Date range pickers
st.sidebar.subheader("📅 Date Range")
default_start = (datetime.now() - timedelta(days=1)).date()
default_end = datetime.now().date()
start_date = st.sidebar.date_input("From", default_start)
end_date = st.sidebar.date_input("To", default_end)

# Market hours helper (IST)
market_open = time(9, 15)
market_close = time(15, 30)

# Build from/to strings in SmartAPI format "YYYY-mm-dd HH:MM"
from_dt = datetime.combine(start_date, market_open)
# If end date is today, cap to current time (but not past market close)
now = datetime.now()
if end_date == now.date():
    to_time = min(now.time(), market_close)
    to_dt = datetime.combine(end_date, to_time)
else:
    to_dt = datetime.combine(end_date, market_close)

fromdate = from_dt.strftime("%Y-%m-%d %H:%M")
todate = to_dt.strftime("%Y-%m-%d %H:%M")

# ===================== SmartAPI Login =====================
try:
    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    data = obj.generateSession(CLIENT_ID, CLIENT_PASSWORD, totp)
    jwt_token = data["data"]["jwtToken"]
    feed_token = obj.getfeedToken()
    st.sidebar.success("✅ Logged in")
except Exception as e:
    st.sidebar.error(f"Login failed: {e}")
    st.stop()

# ===================== Symbol Tokens (Angel SmartAPI) =====================
SYMBOL_TOKENS = {
    "NIFTY": "99926000",      # NIFTY 50
    "BANKNIFTY": "99926009",  # NIFTY BANK
}
symbol_token = SYMBOL_TOKENS[symbol]

# ===================== Fetch Candles =====================
def fetch_candles(client: SmartConnect, token: str, interval: str, fromdate: str, todate: str) -> pd.DataFrame:
    payload = {
        "exchange": "NSE",
        "symboltoken": token,
        "interval": interval,
        "fromdate": fromdate,
        "todate": todate,
    }
    try:
        resp = client.getCandleData(payload)
        # Validate response
        if not resp or "data" not in resp or resp["data"] in (None, [], "null"):
            return pd.DataFrame()
        raw = resp["data"]
        if not isinstance(raw, list) or len(raw) == 0 or not isinstance(raw[0], list):
            return pd.DataFrame()
        df = pd.DataFrame(raw, columns=["datetime", "open", "high", "low", "close", "volume"])
        # Type conversions
        df["datetime"] = pd.to_datetime(df["datetime"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.dropna(subset=["open", "high", "low", "close"], inplace=True)
        df.sort_values("datetime", inplace=True)
        df.set_index("datetime", inplace=True)
        return df
    except Exception:
        return pd.DataFrame()

df = fetch_candles(obj, symbol_token, interval, fromdate, todate)

# Guard: no data
if df.empty:
    st.warning(
        "No candle data returned for the selected date range/interval.\n\n"
        "Try a different date range, ensure it's a trading day and within market hours, or change the interval."
    )
    st.stop()

# ===================== Indicators (pandas_ta) =====================
# These will naturally create NaNs at the start until enough periods are available.
df["EMA20"] = ta.ema(df["close"], length=20)
df["EMA50"] = ta.ema(df["close"], length=50)
df["RSI"] = ta.rsi(df["close"], length=14)

macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
if macd is not None and not macd.empty:
    df = pd.concat([df, macd], axis=1)
    df.rename(
        columns={
            "MACD_12_26_9": "MACD",
            "MACDh_12_26_9": "MACD_hist",
            "MACDs_12_26_9": "MACD_signal",
        },
        inplace=True,
    )

# Volume spike (guard against short series)
if len(df) >= 20:
    df["AvgVol20"] = df["volume"].rolling(window=20).mean()
    df["VolumeSpike"] = df["volume"] > (df["AvgVol20"] * 1.5)
else:
    df["AvgVol20"] = np.nan
    df["VolumeSpike"] = False

# ===================== Signal Logic (with safety for NaNs) =====================
# Use the latest row that has all required indicator values
needed_cols = ["EMA20", "EMA50", "RSI", "close", "VolumeSpike"]
valid = df.dropna(subset=[c for c in needed_cols if c in df.columns])
signal = "HOLD"
if not valid.empty:
    last_row = valid.iloc[-1]
    entry_price = st.session_state.get("entry_price", 0.0)

    if (last_row["EMA20"] > last_row["EMA50"]) and (last_row["RSI"] < 70) and (bool(last_row["VolumeSpike"])):
        signal = "BUY"
        st.session_state["entry_price"] = float(last_row["close"])
    elif (last_row["EMA20"] < last_row["EMA50"]) and (last_row["RSI"] > 30) and (bool(last_row["VolumeSpike"])):
        signal = "SELL"
        st.session_state["entry_price"] = float(last_row["close"])

    # Trailing stop example (1%)
    trailing_percent = 1.0
    if entry_price > 0:
        stop_loss_price = entry_price * (1 - trailing_percent / 100)
        take_profit_price = entry_price * (1 + trailing_percent / 100)
        if last_row["close"] <= stop_loss_price:
            signal = "SELL (Stop Loss Hit)"
        elif last_row["close"] >= take_profit_price:
            signal = "SELL (Take Profit Hit)"

    # P/L preview
    quantity = 25
    if "entry_price" in st.session_state and st.session_state["entry_price"] > 0:
        pl = (last_row["close"] - st.session_state["entry_price"]) * quantity
        st.info(f"💰 Profit/Loss: ₹{pl:.2f}")
else:
    st.warning("Not enough candles to compute indicators yet. Showing charts only.")
    last_row = df.iloc[-1]  # safe now because df is not empty

# ===================== Display =====================
left, right = st.columns([2, 1])
with left:
    st.subheader(f"📊 Latest Signal: {signal}")
    st.line_chart(df[["close", "EMA20", "EMA50"]].dropna(how="all"))
    st.line_chart(df[["RSI"]].dropna(how="all"))
    st.bar_chart(df[["volume"]])

with right:
    st.markdown(
        f"""
**Symbol:** `{symbol}`  
**Interval:** `{interval_label}`  
**From:** `{fromdate}`  
**To:** `{todate}`  
"""
    )
    st.dataframe(df.tail(20))

# ===================== Telegram Alert =====================
def send_telegram(message: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        params = {"chat_id": CHAT_ID, "text": message}
        requests.get(url, params=params, timeout=5)
    except Exception:
        pass

if st.button("Send Signal to Telegram"):
    send_telegram(f"{symbol} Signal: {signal} at {datetime.now().strftime('%H:%M:%S')}")


