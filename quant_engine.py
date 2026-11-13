# -*- coding: utf-8 -*-
"""四大正統機構級／名家專屬量化模型引擎（De-bloat 版）。

去紙老虎化重構原則：
    每類資產只跑真正適合它的正統日線策略，嚴禁把股票名家指標硬套在非股票資產上。

    A. Stocks (NVDA / AAPL / 2330.TW)  → 遠藤洋 MA25 上升趨勢 × 茂爺爺日線三合一 × 森口亮估值
    B. Crypto (BTC / ETH / SOL)        → 劉承彥 CTA 111 唐奇安突破 × 4H/1D 濾網 × 吊燈停損
    C. Income (VNQ / O / 0823.HK / TLT / IEF) → 無風險利差 (Yield Spread) × 200MA 均值回歸
    D. Macro  (VOO / QQQ / GLD / IAU / SGOV)   → Gary Antonacci 雙動能 × Faber 200MA 絕對動能

輸出：
    ./public/data/latest_signals.json 與 ./data/latest_signals.json（雙路徑同步輸出，供前端四大分頁與 ibkr/binance 橋接器讀取）
    ./data/signal_ledger.csv（歷史訊號帳本，以 date + symbol 去重）
"""

import datetime
import json
import math
import os
import sys

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from config import (
    ASSET_UNIVERSE,
    FINE_CATEGORY_OF_SYMBOL,
    MODEL_LABEL,
    CASH_PARKING_SYMBOLS,
    CASH_PARKING_LABEL,
    FUNDAMENTAL_FALLBACK,
)

OFFLINE_MODE = "--offline" in sys.argv

HISTORY_DIR = os.path.join(".", "data", "history")
OUTPUT_DIR = os.path.join(".", "public", "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "latest_signals.json")
# Hostinger 根目錄直接載入根 index.html，前端會依序抓 public/data/ 與 data/，
# 故最新訊號需同時輸出到兩處，避免前端抓 data/latest_signals.json 時得到 404 HTML。
ROOT_OUTPUT_FILE = os.path.join(".", "data", "latest_signals.json")
LEDGER_FILE = os.path.join(".", "data", "signal_ledger.csv")
CUSTOM_ESTIMATES_PATH = os.path.join(".", "data", "custom_estimates.json")

MAX_HISTORY_ROWS = 1000

# ===== 資料源常數 =====
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
CRYPTO_BINANCE_SYMBOLS = {"BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT", "SOL-USD": "SOLUSDT"}
MACRO_TICKERS = {"^TNX": "us10y_yield", "DX-Y.NYB": "dxy", "^VIX": "vix"}
FNG_API_URL = "https://api.alternative.me/fng/?limit=1"
POLITE_SLEEP_SECONDS = 0.4
YFINANCE_RETRY_MAX = 3

# ===== 技術指標週期 =====
ATR_PERIOD = 14
RSI_PERIOD = 14
MA_LONG_PERIOD = 200
DONCHIAN_PERIOD = 20
ENDO_MA = 25
ICHIMOKU_TENKAN = 9
ICHIMOKU_KIJUN = 26
ICHIMOKU_SENKOU_B = 52
ICHIMOKU_SHIFT = 26
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ===== 模型專屬參數 =====
STOCK_DEFENSE_ATR_MULT = 2.0      # 個股 ATR 防守 = Close - 2.0 × ATR14
CTA_CHANDELIER_ATR_MULT = 2.5     # 幣圈吊燈停損 = Highest_High_20d - 2.5 × ATR14
CTA_VOTE_THRESHOLD = 4            # CTA 六策看多票數門檻
CTA_TAKER_THRESHOLD = 50.0        # 幣安 Taker 主動買盤比門檻 (%)
RULE_OF_40_THRESHOLD = 40.0
DEFAULT_BASE_PER = 15.0
DEFAULT_COMPOUND_YEARS = 3

# ===== 摩擦成本與滑點（單邊，百分比點） =====
EXPECTED_SLIPPAGE_PCT = {
    "Stocks": 0.15, "REITs": 0.15, "ETFs": 0.15,
    "Crypto": 0.20, "Bonds": 0.08, "Gold": 0.08,
}

# ===== 訊號定義（四大正統策略專屬） =====
# A. 個股（遠藤洋 × 茂爺爺 × 森口亮）
SIGNAL_STOCK_WATCH = "⚪ 趨勢未立・觀望"
SIGNAL_STOCK_BUY = "🟢 遠藤洋趨勢買進"
SIGNAL_STOCK_HOLD = "🔵 趨勢多頭・續抱"
SIGNAL_STOCK_STOP = "🔴 跌破防守・停損出場"
# B. 加密貨幣（劉承彥 CTA 111）
SIGNAL_CRYPTO_BEAR = "🔴 熊市空頭防禦 (100% 空手轉現金)"
SIGNAL_CRYPTO_BULL = "🟢 牛市多頭區"
SIGNAL_CRYPTO_BUY = "🟢 CTA 順勢突破買進"
SIGNAL_CRYPTO_WATCH = "⚪ 牛市待突破・觀望"
SIGNAL_CRYPTO_STOP = "🔴 跌破吊燈停損・平倉"
# C. 收息與債券（無風險利差 × 200MA）
SIGNAL_INCOME_BUY = "🟢 收息配置買進"
SIGNAL_INCOME_PAUSE = "⚪ 跌破年線・暫停收息買進 (資金停泊 SGOV)"
SIGNAL_INCOME_HOLD = "🔵 收息續抱・利差未達標"
# D. 指數 ETF / 黃金 / 現金池（Gary Antonacci 雙動能 × Faber 200MA）
SIGNAL_MACRO_BUY = "🟢 雙動能配置買進"
SIGNAL_MACRO_HOLD = "🔵 雙動能持有・非首位"
SIGNAL_MACRO_NEUTRAL = "⚪ 動能不足・觀望"
SIGNAL_MACRO_PARK = "💵 現金停泊 SGOV"
SIGNAL_SGOV_POOL = "💵 現金停泊資金池"

# 明確看多訊號（僅這些才產生新買單與倉位）
BUY_SIGNALS = {
    SIGNAL_STOCK_BUY,
    SIGNAL_CRYPTO_BUY,
    SIGNAL_INCOME_BUY,
    SIGNAL_MACRO_BUY,
}

LEDGER_COLUMNS = [
    "date", "symbol", "model", "close_price", "signal", "suggested_weight_pct",
    "rule_of_40", "target_price_2", "floor_price", "yield_spread",
    "momentum_rank", "cta_vote_score",
]

# ===== 數值工具 =====
def to_float(v):
    """安全轉 float，失敗或非有限數回傳 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _last(s, k=-1):
    """取 Series 最後第 k 筆（k=-1 為最新），資料不足回傳 None。"""
    if s is None or len(s) == 0:
        return None
    try:
        return to_float(s.iloc[k])
    except Exception:
        return None


def round2(v):
    if v is None:
        return None
    return round(v, 2)


# ===== 技術指標 =====
def compute_rsi(close, period=RSI_PERIOD):
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_atr(high, low, close, period=ATR_PERIOD):
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def compute_macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    macd_hist = macd - macd_signal
    return macd, macd_signal, macd_hist


def compute_adx(high, low, close, period=14):
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    atr = compute_atr(high, low, close, period)
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    return adx, plus_di, minus_di


def compute_ichimoku(high, low):
    tenkan = (high.rolling(ICHIMOKU_TENKAN).max() + low.rolling(ICHIMOKU_TENKAN).min()) / 2.0
    kijun = (high.rolling(ICHIMOKU_KIJUN).max() + low.rolling(ICHIMOKU_KIJUN).min()) / 2.0
    senkou_a = ((tenkan + kijun) / 2.0).shift(ICHIMOKU_SHIFT)
    senkou_b = (
        (high.rolling(ICHIMOKU_SENKOU_B).max() + low.rolling(ICHIMOKU_SENKOU_B).min()) / 2.0
    ).shift(ICHIMOKU_SHIFT)
    return tenkan, kijun, senkou_a, senkou_b


def compute_indicators(df):
    """將 OHLCV 轉為策略所需技術指標 DataFrame。"""
    out = df.copy()
    out["Close"] = out["Close"].astype(float)
    out["High"] = out["High"].astype(float)
    out["Low"] = out["Low"].astype(float)
    out["Open"] = out["Open"].astype(float)
    if "Volume" in out.columns:
        out["Volume"] = out["Volume"].astype(float)
    close = out["Close"]
    out["ma20"] = close.rolling(20).mean()
    out["ma25"] = close.rolling(ENDO_MA).mean()
    out["ma60"] = close.rolling(60).mean()
    out["ma200"] = close.rolling(MA_LONG_PERIOD).mean()
    out["ma25_20d_ago"] = out["ma25"].shift(20)
    out["rsi14"] = compute_rsi(close, RSI_PERIOD)
    out["atr14"] = compute_atr(out["High"], out["Low"], close, ATR_PERIOD)
    out["high20"] = out["High"].rolling(DONCHIAN_PERIOD).max()
    out["low20"] = out["Low"].rolling(DONCHIAN_PERIOD).min()
    out["macd"], out["macd_signal"], out["macd_hist"] = compute_macd(close)
    out["tenkan"], out["kijun"], out["senkou_a"], out["senkou_b"] = compute_ichimoku(out["High"], out["Low"])
    out["adx"], out["plus_di"], out["minus_di"] = compute_adx(out["High"], out["Low"], close)
    out["ret_63"] = close.pct_change(63)
    out["ret_126"] = close.pct_change(126)
    out["vol20"] = close.pct_change().rolling(20).std()
    return out


# ===== 資料抓取與快取 =====
def fetch_yfinance_history(symbol):
    """抓取 yfinance 2 年日 K（OHLCV），回傳標準化 DataFrame。"""
    data = yf.download(symbol, period="2y", interval="1d", auto_adjust=False, progress=False)
    if data is None or data.empty:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data.reset_index()
    data.columns = [str(c) for c in data.columns]
    rename = {}
    for c in data.columns:
        low = c.lower()
        if low == "date":
            rename[c] = "Date"
        elif low == "open":
            rename[c] = "Open"
        elif low == "high":
            rename[c] = "High"
        elif low == "low":
            rename[c] = "Low"
        elif low == "close":
            rename[c] = "Close"
        elif low == "volume":
            rename[c] = "Volume"
    data = data.rename(columns=rename)
    keep = [c for c in ("Date", "Open", "High", "Low", "Close", "Volume") if c in data.columns]
    data = data[keep]
    data["Date"] = pd.to_datetime(data["Date"])
    return data.set_index("Date")


def fetch_binance_klines(symbol):
    """抓取幣安日 K（含 Taker 主動買盤欄位），回傳標準化 DataFrame。"""
    pair = CRYPTO_BINANCE_SYMBOLS.get(symbol)
    if not pair:
        return None
    resp = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": pair, "interval": "1d", "limit": 1000},
        timeout=15,
    )
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        return None
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_vol", "trades", "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(rows, columns=cols)
    df = df[["open_time", "open", "high", "low", "close", "volume", "taker_base", "taker_quote"]]
    df["Date"] = pd.to_datetime(df["open_time"].astype(int), unit="ms")
    for c in ("open", "high", "low", "close", "volume", "taker_base", "taker_quote"):
        df[c] = df[c].astype(float)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    df = df[["Date", "Open", "High", "Low", "Close", "Volume", "taker_base", "taker_quote"]]
    return df.set_index("Date")


def fetch_binance_klines_4h(symbol):
    """抓取幣安 4 小時 K 線收盤價序列（供 4H MA20 濾網使用）。"""
    pair = CRYPTO_BINANCE_SYMBOLS.get(symbol)
    if not pair:
        return None
    try:
        resp = requests.get(
            BINANCE_KLINES_URL,
            params={"symbol": pair, "interval": "4h", "limit": 60},
            timeout=15,
        )
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return None
        return [float(r[4]) for r in rows]
    except Exception:
        return None


def load_local_history_csv(symbol):
    path = os.path.join(HISTORY_DIR, f"{symbol}.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        df["Date"] = pd.to_datetime(df["Date"])
        return df.set_index("Date")
    except Exception:
        return None


def save_history(symbol, df):
    if df is None or df.empty:
        return
    keep = ["Open", "High", "Low", "Close", "Volume"]
    extra = [c for c in ("taker_base", "taker_quote") if c in df.columns]
    out = df[keep + extra].copy()
    out = out.dropna(subset=["Close"])
    out = out[~out.index.duplicated(keep="first")].sort_index()
    if len(out) > MAX_HISTORY_ROWS:
        out = out.tail(MAX_HISTORY_ROWS)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    out.to_csv(os.path.join(HISTORY_DIR, f"{symbol}.csv"), encoding="utf-8")


def load_market_data(symbol, is_crypto):
    """依標的選擇資料源，回傳 (df, source)。離線模式只讀本地 CSV。"""
    source = "Local CSV Cache"
    df = None
    if not OFFLINE_MODE:
        try:
            df = fetch_binance_klines(symbol) if is_crypto else fetch_yfinance_history(symbol)
            if df is not None and not df.empty:
                save_history(symbol, df)
                source = "Binance Official API" if is_crypto else "Yahoo Finance (Live)"
        except Exception:
            df = None
    if df is None or df.empty:
        df = load_local_history_csv(symbol)
        source = "Local CSV Cache"
    return df, source


def _load_custom_estimates():
    if not os.path.exists(CUSTOM_ESTIMATES_PATH):
        return {}
    try:
        with open(CUSTOM_ESTIMATES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_fundamentals(symbol):
    """基本面：離線用 FUNDAMENTAL_FALLBACK；線上再以 yfinance info 覆寫。"""
    info = dict(FUNDAMENTAL_FALLBACK.get(symbol, {}))
    if not OFFLINE_MODE:
        try:
            live = yf.Ticker(symbol).info or {}
            for k in (
                "trailingEps", "forwardEps", "revenueGrowth", "earningsGrowth",
                "operatingMargins", "forwardPE", "priceToBook",
                "trailingAnnualDividendYield", "trailingPE", "marketCap",
                "heldPercentInsiders",
            ):
                if live.get(k) is not None:
                    info[k] = live[k]
        except Exception:
            pass
    return info


def _earnings_date(symbol):
    est = _load_custom_estimates()
    overrides = est.get("overrides", {})
    item = overrides.get(symbol, {})
    return item.get("earnings_date")


# ===== 巨觀概覽（10Y 美債 / DXY / VIX / 幣圈恐慌貪婪） =====
def vix_zone(vix):
    if vix is None:
        return None
    if vix < 15:
        return "低波"
    if vix <= 25:
        return "正常"
    return "高波恐慌"


def fetch_macro_overview():
    overview = {
        "us10y_yield": None,
        "dxy": None,
        "vix": None,
        "vix_zone": None,
        "crypto_fear_greed": None,
        "crypto_fear_greed_classification": None,
        "us10y_200d_mean": None,
    }
    if OFFLINE_MODE:
        return overview
    for ticker, key in MACRO_TICKERS.items():
        try:
            df = yf.download(ticker, period="1y", interval="1d", progress=False)
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                close = df["Close"].astype(float)
                overview[key] = round2(float(close.iloc[-1]))
                if key == "us10y_yield" and len(close) >= 200:
                    overview["us10y_200d_mean"] = round2(float(close.tail(200).mean()))
        except Exception:
            continue
    try:
        resp = requests.get(FNG_API_URL, timeout=10)
        payload = resp.json()
        data = payload.get("data") or []
        if data:
            item = data[0]
            overview["crypto_fear_greed"] = to_float(item.get("value"))
            overview["crypto_fear_greed_classification"] = item.get("value_classification")
    except Exception:
        pass
    overview["vix_zone"] = vix_zone(overview["vix"])
    return overview


# ===== 森口亮估值（40% 法則 / 目標價② / 預測低點） =====
def compute_moriguchi_valuation(symbol, info):
    est = _load_custom_estimates().get("overrides", {}).get(symbol, {})
    base_per = to_float(est.get("base_per")) or DEFAULT_BASE_PER
    per_low = to_float(est.get("per_low_avg")) or (base_per * 0.6)

    eps = to_float(info.get("trailingEps"))
    eps_next = to_float(info.get("forwardEps")) or eps
    growth = to_float(info.get("earningsGrowth")) or to_float(info.get("revenueGrowth")) or 0.0
    rev_growth = to_float(info.get("revenueGrowth")) or growth
    op_margin = to_float(info.get("operatingMargins")) or 0.0

    rule_of_40 = round2((rev_growth + op_margin) * 100.0)
    # 森口亮正統：目標價 = EPS × 合理PER（不疊加過度成長複利，避免估值失真）
    target_price_2 = round2(eps_next * base_per) if eps_next else None
    floor_price = round2(eps * per_low) if eps else None
    return {
        "rule_of_40": rule_of_40,
        "rule_of_40_pass": rule_of_40 is not None and rule_of_40 >= RULE_OF_40_THRESHOLD,
        "target_price_2": target_price_2,
        "floor_price": floor_price,
        "base_per": round2(base_per),
    }


# ===== CTA 六策投票（劉承彥《CTA 111 技》唐奇安突破主軸） =====
def compute_cta_vote(ind):
    close = _last(ind["Close"])
    prev_high20 = _last(ind["high20"].shift(1))
    prev_low20 = _last(ind["low20"].shift(1))
    ma20 = _last(ind["ma20"])
    ma60 = _last(ind["ma60"])
    macd = _last(ind["macd"])
    macd_signal = _last(ind["macd_signal"])
    rsi = _last(ind["rsi14"])
    adx = _last(ind["adx"])
    plus_di = _last(ind["plus_di"])
    minus_di = _last(ind["minus_di"])

    long_v = 0
    short_v = 0
    # 1. 唐奇安 20 高/低突破
    if close is not None and prev_high20 is not None:
        if close >= prev_high20:
            long_v += 1
        elif prev_low20 is not None and close <= prev_low20:
            short_v += 1
    # 2. MA20 vs MA60
    if ma20 is not None and ma60 is not None:
        if ma20 > ma60:
            long_v += 1
        elif ma20 < ma60:
            short_v += 1
    # 3. MACD 線
    if macd is not None and macd_signal is not None:
        if macd > macd_signal:
            long_v += 1
        elif macd < macd_signal:
            short_v += 1
    # 4. RSI14
    if rsi is not None:
        if rsi > 50:
            long_v += 1
        elif rsi < 50:
            short_v += 1
    # 5. 收盤 vs MA20
    if close is not None and ma20 is not None:
        if close > ma20:
            long_v += 1
        elif close < ma20:
            short_v += 1
    # 6. ADX 趨勢
    if adx is not None and plus_di is not None and minus_di is not None:
        if adx > 20 and plus_di > minus_di:
            long_v += 1
        elif adx > 20 and minus_di > plus_di:
            short_v += 1
    return {"long": long_v, "short": short_v, "score": f"{long_v}/6 看多"}


def compute_taker_buy_ratio(ind):
    """近 5 日 Taker 主動買盤比（%）。無 taker 欄位時以「上漲日成交量佔比」代理。"""
    tail = ind.tail(5)
    if "taker_base" in ind.columns and "Volume" in ind.columns:
        buy = tail["taker_base"].sum()
        total = tail["Volume"].sum()
        return round2(buy / total * 100.0) if total else None
    if "Volume" in ind.columns and "Open" in ind.columns:
        up = (tail["Close"] > tail["Open"]).astype(float)
        buy = (tail["Volume"] * up).sum()
        total = tail["Volume"].sum()
        return round2(buy / total * 100.0) if total else None
    return None


# ===== 單一標的分析（四大模型分流） =====
def _base_record(model, symbol, name, category, ind, source):
    close = _last(ind["Close"])
    prev_close = _last(ind["Close"].shift(1))
    atr = _last(ind["atr14"])
    vol = _last(ind["vol20"])
    price = round2(close)
    change_pct = round2((close / prev_close - 1.0) * 100.0) if close and prev_close else None
    slip = EXPECTED_SLIPPAGE_PCT.get(category, 0.15)
    return {
        "symbol": symbol,
        "name": name,
        "model": model,
        "model_label": MODEL_LABEL.get(model, model),
        "category": category,
        "price": price,
        "change_pct": change_pct,
        "data_source": source,
        "data_source_status": "Live" if "Live" in source else "Cached",
        "signal": None,
        "market_regime": None,
        "suggested_weight_pct": 0.0,
        "ma20": round2(_last(ind["ma20"])),
        "ma25": round2(_last(ind["ma25"])),
        "ma60": round2(_last(ind["ma60"])),
        "ma200": round2(_last(ind["ma200"])),
        "rsi14": round2(_last(ind["rsi14"])),
        "atr14": round2(atr),
        "high20": round2(_last(ind["high20"])),
        "volatility_20d_pct": round2(vol * 100.0) if vol is not None else None,
        "breakout_entry_price": price,
        "ideal_buy_price": price,
        "current_defense_sl": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "trailing_stop_price": None,
        "stress_slippage_pct": slip,
        "earnings_date": _earnings_date(symbol),
    }


def _order_levels(rec, atr, defense=None, tp_mult=3.0, trailing_mult=2.2):
    """填入括號單三價：防守止損 / 目標停利 / 移動停利。"""
    price = rec["price"]
    rec["current_defense_sl"] = round2(defense)
    rec["stop_loss_price"] = round2(defense)
    if price is not None and atr is not None:
        rec["take_profit_price"] = round2(price + tp_mult * atr)
        rec["trailing_stop_price"] = round2(price - trailing_mult * atr)


def _analyze_stocks(rec, ind, info):
    close = rec["price"]
    ma25 = rec["ma25"]
    ma25_ago = _last(ind["ma25_20d_ago"])
    rsi = rec["rsi14"]
    atr = rec["atr14"]
    macd_hist = _last(ind["macd_hist"])
    senkou_a = _last(ind["senkou_a"])
    senkou_b = _last(ind["senkou_b"])

    # 遠藤洋上升模式：Close > MA25 且 MA25 > 20 日前的 MA25
    endo_up = (
        close is not None and ma25 is not None and close > ma25
    ) and (ma25 is not None and ma25_ago is not None and ma25 > ma25_ago)

    # 茂爺爺日線三合一：站上雲帶 + MACD 柱 > 0 + RSI 40~70（或 <35 超賣反彈）
    cloud_above = (
        close is not None and senkou_a is not None and senkou_b is not None
        and close > senkou_a and close > senkou_b
    )
    macd_ok = macd_hist is not None and macd_hist > 0
    rsi_ok = rsi is not None and (40.0 <= rsi <= 70.0 or rsi < 35.0)
    fujimoto_count = int(cloud_above) + int(macd_ok) + int(rsi_ok)

    valuation = compute_moriguchi_valuation(rec["symbol"], info)

    # 出場防線：跌破 MA25 或跌破 Close - 2.0 × ATR14
    atr_defense = round2(close - STOCK_DEFENSE_ATR_MULT * atr) if close is not None and atr is not None else None
    broke = (close is not None and ma25 is not None and close < ma25) or (
        atr_defense is not None and close is not None and close < atr_defense
    )

    if broke:
        rec["signal"] = SIGNAL_STOCK_STOP
        rec["market_regime"] = "🔴 跌破防守"
    elif not endo_up:
        rec["signal"] = SIGNAL_STOCK_WATCH
        rec["market_regime"] = "⚪ 趨勢未立"
    elif fujimoto_count >= 3:
        rec["signal"] = SIGNAL_STOCK_BUY
        rec["market_regime"] = "🟢 遠藤洋上升模式"
    else:
        rec["signal"] = SIGNAL_STOCK_HOLD
        rec["market_regime"] = "🟢 遠藤洋上升模式"

    defense = None
    if ma25 is not None and atr_defense is not None:
        defense = max(ma25, atr_defense)
    elif atr_defense is not None:
        defense = atr_defense
    elif ma25 is not None:
        defense = ma25
    _order_levels(rec, atr, defense=defense)

    rec["endo_uptrend"] = bool(endo_up)
    rec["endo_25ma_status"] = "✅ 通過" if endo_up else "❌ 未通過"
    rec["fujimoto_3in1_count"] = fujimoto_count
    rec["fujimoto_3in1_status"] = (
        f"✅ 三合一達成 ({fujimoto_count}/3)"
        if fujimoto_count >= 3 else f"❌ 未達成 ({fujimoto_count}/3)"
    )
    rec["rule_of_40"] = valuation["rule_of_40"]
    rec["rule_of_40_pass"] = valuation["rule_of_40_pass"]
    rec["target_price_2"] = valuation["target_price_2"]
    rec["floor_price"] = valuation["floor_price"]
    rec["atr_defense_sl"] = atr_defense
    rec["ma25_stop"] = rec["ma25"]
    return rec


def _analyze_crypto(rec, ind):
    close = rec["price"]
    ma60 = rec["ma60"]
    atr = rec["atr14"]
    high20 = _last(ind["high20"])
    prev_high20 = _last(ind["high20"].shift(1))

    vote = compute_cta_vote(ind)
    taker = compute_taker_buy_ratio(ind)

    h4_closes = fetch_binance_klines_4h(rec["symbol"]) if not OFFLINE_MODE else None
    h4_close = h4_closes[-1] if h4_closes else None
    h4_ma20 = round2(sum(h4_closes[-20:]) / 20.0) if h4_closes and len(h4_closes) >= 20 else None
    h4_bull = (h4_close > h4_ma20) if (h4_close is not None and h4_ma20 is not None) else None

    daily_bull = (close is not None and ma60 is not None and close > ma60)
    chandelier = round2(high20 - CTA_CHANDELIER_ATR_MULT * atr) if high20 is not None and atr is not None else None
    broke_chandelier = chandelier is not None and close is not None and close < chandelier

    if close is None or ma60 is None:
        rec["signal"] = SIGNAL_CRYPTO_BEAR
        rec["market_regime"] = "🔴 熊市空頭防禦 (100% 空手轉現金)"
    elif close < ma60:
        rec["signal"] = SIGNAL_CRYPTO_BEAR
        rec["market_regime"] = "🔴 熊市空頭防禦 (100% 空手轉現金)"
    else:
        bull_zone = h4_bull if h4_bull is not None else True
        if not bull_zone:
            rec["signal"] = SIGNAL_CRYPTO_WATCH
            rec["market_regime"] = "🟡 日線多・4H 未共振"
        elif broke_chandelier:
            rec["signal"] = SIGNAL_CRYPTO_STOP
            rec["market_regime"] = "🟢 牛市多頭區"
        else:
            rec["market_regime"] = "🟢 牛市多頭區"
            breakout = prev_high20 is not None and close >= prev_high20
            taker_ok = taker is not None and taker > CTA_TAKER_THRESHOLD
            cta_entry = breakout or (vote["long"] >= CTA_VOTE_THRESHOLD and taker_ok)
            rec["signal"] = SIGNAL_CRYPTO_BUY if cta_entry else SIGNAL_CRYPTO_BULL

    _order_levels(rec, atr, defense=chandelier)

    rec["d1_ma60_filter"] = "🟢 站上 MA60" if daily_bull else "🔴 跌破 MA60"
    rec["cta_vote_score"] = vote["score"]
    rec["cta_vote_long"] = vote["long"]
    rec["cta_vote_short"] = vote["short"]
    rec["high20"] = round2(high20)
    rec["breakout_entry_price"] = round2(prev_high20) if prev_high20 is not None else rec["price"]
    rec["taker_buy_ratio_5d"] = taker
    rec["chandelier_stop"] = chandelier
    rec["mtf_4h_1d_resonance"] = (
        "✅ 4H+1D 共振" if h4_bull is True else
        ("❌ 4H 未共振" if h4_bull is False else "N/A (離線)")
    )
    rec["cta_breakout_price_20d"] = round2(prev_high20) if prev_high20 is not None else None
    return rec


def _analyze_income(rec, ind, info, macro):
    close = rec["price"]
    ma200 = rec["ma200"]
    atr = rec["atr14"]
    category = rec["category"]

    dy_frac = to_float(info.get("trailingAnnualDividendYield"))
    us10y = macro.get("us10y_yield")
    dy_pct = round2(dy_frac * 100.0) if dy_frac is not None else None

    if category == "Bonds":
        # 美債看 10Y 殖利率相對 200 日均值之乖離
        us10y_mean = macro.get("us10y_200d_mean")
        spread = round2(us10y - us10y_mean) if (us10y is not None and us10y_mean is not None) else None
    else:
        spread = round2(dy_pct - us10y) if (dy_pct is not None and us10y is not None) else None

    if close is None or ma200 is None:
        rec["signal"] = SIGNAL_INCOME_PAUSE
        rec["market_regime"] = "⚪ 跌破年線"
    elif close < ma200:
        rec["signal"] = SIGNAL_INCOME_PAUSE
        rec["market_regime"] = "⚪ 跌破年線"
    else:
        rec["market_regime"] = "🟢 站上年線"
        if category == "Bonds":
            # 長債站回生命線（Close > MA200）即視為可配置
            rec["signal"] = SIGNAL_INCOME_BUY
        else:
            attractive = spread is not None and spread > 0.0
            rec["signal"] = SIGNAL_INCOME_BUY if attractive else SIGNAL_INCOME_HOLD

    # 防守止損：站上年線者以 MA200 為生命線，跌破年線者以 3% 防守緩衝
    defense = ma200 if (close is not None and ma200 is not None and close > ma200) else round2(close * 0.97) if close else None
    _order_levels(rec, atr, defense=defense, tp_mult=2.0, trailing_mult=1.5)

    rec["dividend_yield_pct"] = dy_pct
    rec["yield_spread"] = spread
    rec["yield_spread_label"] = "10Y 乖離 (點)" if category == "Bonds" else "殖利率利差 (%)"
    rec["ma200_position"] = "🟢 站上年線" if (close is not None and ma200 is not None and close > ma200) else "⚪ 跌破年線"
    return rec


def _analyze_macro(rec, ind):
    close = rec["price"]
    ma200 = rec["ma200"]
    atr = rec["atr14"]
    ret_63 = _last(ind["ret_63"])
    ret_126 = _last(ind["ret_126"])
    above_ma200 = close is not None and ma200 is not None and close > ma200

    rec["above_ma200"] = bool(above_ma200)
    rec["ma200_position"] = "🟢 站上年線" if above_ma200 else "⚪ 跌破年線"
    rec["ret_3m_pct"] = round2(ret_63 * 100.0) if ret_63 is not None else None
    rec["ret_6m_pct"] = round2(ret_126 * 100.0) if ret_126 is not None else None
    rec["momentum_score"] = round2((ret_63 + ret_126) / 2.0) if (ret_63 is not None and ret_126 is not None) else None
    rec["abs_ok"] = False  # 由 apply_dual_momentum 於得知 SGOV 6m 報酬後覆寫
    defense = ma200 if above_ma200 else round2(close * 0.96) if close else None
    _order_levels(rec, atr, defense=defense)
    return rec


def apply_dual_momentum(records, sgov_ret_126):
    """Gary Antonacci 雙動能：絕對動能（站上 MA200 + 跑贏 SGOV）+ 相對動能排名。"""
    macro_risk = [r for r in records if r["model"] == "Macro" and r["symbol"] not in CASH_PARKING_SYMBOLS]
    for r in macro_risk:
        beats_sgov = (
            r.get("ret_6m_pct") is not None and sgov_ret_126 is not None
            and r["ret_6m_pct"] > sgov_ret_126
        )
        r["excess_return_vs_sgov"] = round2(r["ret_6m_pct"] - sgov_ret_126) if (r.get("ret_6m_pct") is not None and sgov_ret_126 is not None) else None
        r["abs_ok"] = bool(r.get("above_ma200")) and beats_sgov

    ranked = sorted(
        macro_risk,
        key=lambda r: r.get("momentum_score") if r.get("momentum_score") is not None else -999.0,
        reverse=True,
    )
    for rank, r in enumerate(ranked, start=1):
        r["momentum_rank"] = rank

    chosen = next((r for r in ranked if r.get("abs_ok")), None)
    for r in macro_risk:
        if r is chosen:
            r["signal"] = SIGNAL_MACRO_BUY
            r["market_regime"] = "🟢 絕對動能通過"
        elif r.get("abs_ok"):
            r["signal"] = SIGNAL_MACRO_HOLD
            r["market_regime"] = "🟢 絕對動能通過"
        elif r.get("above_ma200"):
            r["signal"] = SIGNAL_MACRO_NEUTRAL
            r["market_regime"] = "⚪ 動能不足"
        else:
            r["signal"] = SIGNAL_MACRO_PARK
            r["market_regime"] = "⚪ 跌破年線・移轉 SGOV"

    for r in records:
        if r["symbol"] in CASH_PARKING_SYMBOLS:
            r["signal"] = SIGNAL_SGOV_POOL
            r["market_regime"] = "💵 現金停泊資金池"
    return records


# ===== 建議倉位（逆波動率風險平價 + 防守訊號歸零 → SGOV） =====
def assign_weights(records):
    for r in records:
        vol = r.get("volatility_20d_pct")
        inv_vol = 1.0 / max(vol, 0.5) if vol else 1.0 / 2.0
        r["_inv_vol"] = inv_vol
    freed = 0.0
    for r in records:
        if r["symbol"] in CASH_PARKING_SYMBOLS:
            continue
        if r.get("signal") not in BUY_SIGNALS:
            freed += r["_inv_vol"]
            r["_inv_vol"] = 0.0
    for r in records:
        if r["symbol"] in CASH_PARKING_SYMBOLS:
            r["_inv_vol"] += freed
    total = sum(r["_inv_vol"] for r in records)
    sgov_parking = 0.0
    for r in records:
        w = (r["_inv_vol"] / total * 100.0) if total > 0 else 0.0
        r["suggested_weight_pct"] = round2(w)
        r.pop("_inv_vol", None)
        if r["symbol"] in CASH_PARKING_SYMBOLS:
            sgov_parking = r["suggested_weight_pct"]
    return sgov_parking


# ===== 單一標的分析入口 =====
def analyze(model, symbol, name, macro):
    category = FINE_CATEGORY_OF_SYMBOL.get(symbol, model)
    is_crypto = model == "Crypto"
    df, source = load_market_data(symbol, is_crypto)
    if df is None or df.empty:
        return {
            "symbol": symbol, "name": name, "model": model,
            "model_label": MODEL_LABEL.get(model, model), "category": category,
            "price": None, "change_pct": None, "data_source": "Failed",
            "data_source_status": "Failed", "signal": "⚪ 資料不足・觀望",
            "market_regime": "⚪ 資料不足", "suggested_weight_pct": 0.0,
        }
    ind = compute_indicators(df)
    rec = _base_record(model, symbol, name, category, ind, source)
    if model == "Stocks":
        info = load_fundamentals(symbol)
        rec = _analyze_stocks(rec, ind, info)
    elif model == "Crypto":
        rec = _analyze_crypto(rec, ind)
    elif model == "Income":
        info = load_fundamentals(symbol)
        rec = _analyze_income(rec, ind, info, macro)
    else:  # Macro
        rec = _analyze_macro(rec, ind)
    return rec


# ===== 摘要與帳本 =====
def build_summary(records):
    signal_counts = {}
    model_counts = {}
    for r in records:
        signal_counts[r["signal"]] = signal_counts.get(r["signal"], 0) + 1
        model_counts[r["model"]] = model_counts.get(r["model"], 0) + 1
    return {
        "total_assets": len(records),
        "signal_counts": signal_counts,
        "model_counts": model_counts,
    }


def append_signal_ledger(records):
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    rows = []
    for r in records:
        rows.append({
            "date": today,
            "symbol": r["symbol"],
            "model": r["model"],
            "close_price": r.get("price"),
            "signal": r.get("signal"),
            "suggested_weight_pct": r.get("suggested_weight_pct"),
            "rule_of_40": r.get("rule_of_40"),
            "target_price_2": r.get("target_price_2"),
            "floor_price": r.get("floor_price"),
            "yield_spread": r.get("yield_spread"),
            "momentum_rank": r.get("momentum_rank"),
            "cta_vote_score": r.get("cta_vote_score"),
        })
    new_df = pd.DataFrame(rows, columns=LEDGER_COLUMNS)
    existing = None
    if os.path.exists(LEDGER_FILE):
        try:
            existing = pd.read_csv(LEDGER_FILE)
            if list(existing.columns) != LEDGER_COLUMNS:
                existing = None
        except Exception:
            existing = None
    if existing is not None and not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df
    combined = combined.drop_duplicates(subset=["date", "symbol"], keep="last")
    combined.to_csv(LEDGER_FILE, index=False, encoding="utf-8")


def build_ledger_summary():
    if not os.path.exists(LEDGER_FILE):
        return {"total_signals": 0}
    try:
        df = pd.read_csv(LEDGER_FILE)
        by_symbol = df["symbol"].value_counts().to_dict() if "symbol" in df.columns else {}
        return {"total_signals": int(len(df)), "by_symbol": {k: int(v) for k, v in by_symbol.items()}}
    except Exception:
        return {"total_signals": 0}


# ===== 主程式 =====
def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    os.makedirs(HISTORY_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"=== 四大正統量化模型引擎（De-bloat 版） === 離線模式={OFFLINE_MODE}")

    macro = fetch_macro_overview()

    # 第一階段：逐一分析四大模型標的
    records = []
    for model in ("Stocks", "Crypto", "Income", "Macro"):
        for symbol, name in ASSET_UNIVERSE[model].items():
            rec = analyze(model, symbol, name, macro)
            records.append(rec)

    # 取得 SGOV 6 個月報酬，作為雙動能絕對動能基準
    sgov = next((r for r in records if r["symbol"] in CASH_PARKING_SYMBOLS), None)
    sgov_ret_126 = sgov.get("ret_6m_pct") if sgov else None

    records = apply_dual_momentum(records, sgov_ret_126)
    sgov_parking = assign_weights(records)

    # 依四大模型分組輸出
    assets = {}
    for model in ("Stocks", "Crypto", "Income", "Macro"):
        assets[model] = [r for r in records if r["model"] == model]

    live_count = sum(1 for r in records if r.get("data_source_status") == "Live")
    fallback_count = sum(1 for r in records if r.get("data_source_status") == "Cached")
    failed = [r["symbol"] for r in records if r.get("data_source_status") == "Failed"]

    output = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "summary": build_summary(records),
        "macro_overview": macro,
        "sgov_parking_pct": round2(sgov_parking),
        "ledger_summary": build_ledger_summary(),
        "system_health": {
            "live_count": live_count,
            "fallback_count": fallback_count,
            "failed_symbols": failed,
            "degraded": bool(failed),
        },
        "assets": assets,
    }

    for path in (OUTPUT_FILE, ROOT_OUTPUT_FILE):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

    append_signal_ledger(records)

    print(f"✅ 已輸出 {OUTPUT_FILE} 與 {ROOT_OUTPUT_FILE}（{len(records)} 檔標的）")
    print(f"   SGOV 現金池停泊比例：{round2(sgov_parking)}%")
    print(f"   訊號分布：{output['summary']['signal_counts']}")
    if failed:
        print(f"   ⚠️ 抓取失敗：{failed}")


if __name__ == "__main__":
    main()

