# -*- coding: utf-8 -*-
"""跨市場多資產量化分析引擎。

功能：
1. 自動建立 ./data/history/ 與 ./public/data/ 資料夾。
2. 透過 yfinance 抓取六大資產標的過去 2 年日 K 線（OHLCV）與基本面資訊。
3. 將歷史數據防重複存入 ./data/history/{symbol}.csv。
4. 計算 MA20、MA60、MA200、RSI14、ATR14、20 日唐奇安高點（HIGH20）與
   20 日布林通道上下軌。
5. 依六大資產類別實作獨立領域策略（evaluate_domain_strategy），輸出週期
   定位、建議買入價、ATR 嚴格止損價、目標停利價與過去 1 年回測績效。
6. 產生量化交易訊號（Signal）；SGOV 獨立識別為現金停泊資金池。
7. 匯出最新報價、漲跌幅、技術指標、殖利率與訊號至
   ./public/data/latest_signals.json。
"""

import datetime
import json
import math
import os
import sys
import time

import pandas as pd
import requests
import yfinance as yf

from config import (
    ASSET_UNIVERSE,
    ATR_PERIOD,
    MA_LONG_PERIOD,
    DONCHIAN_PERIOD,
    ATR_STOP_MULT,
    ATR_TAKE_PROFIT_MULT,
    LONG_TERM_ATR_STOP_MULT,
    LONG_TERM_STOP_CATEGORIES,
    BACKTEST_LOOKBACK_DAYS,
    CASH_PARKING_SYMBOLS,
    CASH_PARKING_LABEL,
    VOL_LENGTH,
    BIG_VOL_MULTIPLIER,
    K_BODY_MA_P,
    K_BODY_MULTIPLIER,
    ROUND_TRIP_COST,
    DOMAIN_EXIT_RULES,
)

HISTORY_DIR = os.path.join(".", "data", "history")
OUTPUT_DIR = os.path.join(".", "public", "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "latest_signals.json")

# 本地 CSV 容量防膨脹鎖：每檔只保留最近 N 個交易日（約 4 年日線，足夠計算 MA200 與 1 年回測）
MAX_HISTORY_ROWS = 1000

# 訊號定義
SIGNAL_GREEN = "🟢 強烈買進 / 低檔加碼"
SIGNAL_BLUE = "🔵 趨勢多頭 / 續抱"
SIGNAL_YELLOW = "🟡 價值收息買點"
SIGNAL_RED = "🔴 減碼 / 停利警戒"
SIGNAL_NEUTRAL = "⚪ 區間觀望"
SIGNAL_FIRE = "🔥 爆量長紅突破 (強勢買進)"

# 價值收息訊號適用的資產類別
INCOME_CATEGORIES = ("REITs", "Bonds")
# 價值收息所需的年化殖利率門檻（yfinance 回傳為小數，0.04 = 4%）
INCOME_YIELD_THRESHOLD = 0.04


# ===== 建議資金配置權重（逆波動率 Risk-Parity + 宏觀利率調節）=====
SUGGESTED_WEIGHT_MIN_VOL = 0.005        # 波動率下限（0.5%/日），避免現金類標的權重無限放大
SUGGESTED_WEIGHT_FALLBACK_VOL = 0.02    # 資料缺失時的預設日波動率（2%）
BOND_TILT_NEUTRAL_YIELD = 4.0           # 中性 10 年期美債殖利率基準（%）
BOND_TILT_SENSITIVITY = 0.10            # 每 1% 利率變動，債券類權重調整 ±10%
BOND_TILT_MIN = 0.6
BOND_TILT_MAX = 1.5


# ===== 數據源可靠性：幣安官方 API + yfinance 三大硬傷防護 =====
# 加密貨幣：改接幣安官方正式公開 REST API（$0 免費、免金鑰、一級數據源）
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
CRYPTO_BINANCE_SYMBOLS = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
    "SOL-USD": "SOLUSDT",
}
# 資料來源標示
SOURCE_BINANCE = "Binance Official API"
SOURCE_YAHOO = "Yahoo Finance (Cleaned)"
SOURCE_CACHE = "Local CSV Cache"
# 【硬傷1】禮貌延遲（每檔標的抓取間隔）與最多失敗重試次數
POLITE_SLEEP_SECONDS = 0.4
YFINANCE_RETRY_MAX = 2
# 【硬傷3】髒數據清洗門檻：單日漲跌幅超過 45% 視為異常（如小數點錯位多一個零）
DIRTY_DATA_MAX_DAILY_CHANGE = 0.45
# 【硬傷4】yfinance 連線超時保護（秒）：所有抓取帶 timeout，避免卡死
YFINANCE_TIMEOUT_SECONDS = 15


# ===== 五位日韓操盤名家融合參數 =====
# 遊牧民（Nomad）：非典型均線週期與量能雷達
NOMAD_MA_PERIODS = (3, 8, 15, 33, 45)
# 藤本茂：一目均衡表週期
ICHIMOKU_TENKAN_PERIOD = 9
ICHIMOKU_KIJUN_PERIOD = 26
ICHIMOKU_SENKOU_B_PERIOD = 52
ICHIMOKU_SHIFT = 26
# 藤本茂：MACD 參數
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
# 遠藤洋：吊燈移動停利 = 近 20 日最高價 - TRAILING_STOP_ATR_MULT × ATR14
TRAILING_STOP_ATR_MULT = 2.2
# 遠藤洋時間成本過濾：跌破 MA45 且量比 < 0.8 視為「地底低量（禁買）」
ENDOH_LOW_VOL_RATIO = 0.8
# cis：主升段量能門檻
CIS_MAIN_WAVE_VOL_RATIO = 1.5
# 森口亮：每日 5 分鐘綜合評分（0~100）
MASTER_SCORE_MAX = 100

# 藤本茂「1:2:6 法則」倉位階段
STAGE_0 = "0 - 觀望 / 破線停損（cis鐵律：嚴禁攤平）"
STAGE_1 = "1 - 偵察試單 (1/9 倉位)"
STAGE_2 = "2 - 順勢加碼 (1+2 = 3/9 倉位)"
STAGE_3 = "3 - 主升滿倉 (1+2+6 = 9/9 倉位)"
ENDOH_LOW_VOL_LABEL = "地底低量（禁買）"


# ===== 免金鑰全球宏觀與情緒指標（Macro & Sentiment） =====
# 以 yfinance 抓取全球定價錨（無需 API 金鑰），另以免費公開 API 抓取幣圈恐慌貪婪指數。
MACRO_TICKERS = {
    "^TNX": "us10y_yield",   # 美國 10 年期公債殖利率（單位：%）
    "DX-Y.NYB": "dxy",       # 美元指數 DXY
    "^VIX": "vix",           # CBOE 恐慌指數
}

FNG_CLASS_ZH = {
    "Extreme Fear": "極度恐慌",
    "Fear": "恐慌",
    "Neutral": "中性",
    "Greed": "貪婪",
    "Extreme Greed": "極度貪婪",
}
FNG_API_URL = "https://api.alternative.me/fng/?limit=1"


def vix_zone(vix):
    """依 VIX 數值回傳市場情緒區間：<15 樂觀、15~25 正常、>25 恐慌避險。"""
    if vix is None:
        return None
    if vix < 15:
        return "樂觀"
    if vix <= 25:
        return "正常"
    return "恐慌避險"


def _make_yfinance_session(timeout=YFINANCE_TIMEOUT_SECONDS):
    """建立帶連線/讀取超時的 yfinance session，避免抓取卡死。

    yfinance 1.x 優先以 curl_cffi 進行瀏覽器 TLS 偽裝（避免被 Yahoo 擋）；
    此處沿用同一後端：curl_cffi Session 直接帶 timeout，requests Session 則
    掛上帶預設 timeout 的 HTTPAdapter。任何後端都會被強制套用超時。
    """
    try:
        from curl_cffi import requests as _curl_requests
        return _curl_requests.Session(impersonate="chrome", timeout=timeout)
    except Exception:
        pass

    from requests.adapters import HTTPAdapter

    class _TimeoutHTTPAdapter(HTTPAdapter):
        def __init__(self, *args, **kwargs):
            self._default_timeout = kwargs.pop("timeout", timeout)
            super().__init__(*args, **kwargs)

        def send(self, request, **kwargs):
            kwargs.setdefault("timeout", self._default_timeout)
            return super().send(request, **kwargs)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    adapter = _TimeoutHTTPAdapter(timeout=timeout)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_macro_overview():
    """抓取免金鑰全球宏觀與情緒指標，回傳 dict 供 latest_signals.json 使用。"""
    overview = {
        "us10y_yield": None,
        "us10y_yield_label": "美國 10 年期公債殖利率",
        "dxy": None,
        "dxy_label": "美元指數 DXY",
        "vix": None,
        "vix_label": "CBOE 恐慌指數 (VIX)",
        "vix_zone": None,
        "crypto_fear_greed": None,
        "crypto_fear_greed_label": "幣圈恐慌貪婪指數",
        "crypto_fear_greed_classification": None,
    }

    for ticker, key in MACRO_TICKERS.items():
        try:
            data = yf.Ticker(ticker, session=_make_yfinance_session()).history(
                period="5d", interval="1d", auto_adjust=True,
                timeout=YFINANCE_TIMEOUT_SECONDS,
            )
            if data is not None and not data.empty:
                closes = data["Close"].astype(float).dropna()
                if not closes.empty:
                    overview[key] = round(float(closes.iloc[-1]), 4)
        except Exception as exc:
            print(f"    [warn] 宏觀指標 {ticker} 抓取失敗: {exc}")

    overview["vix_zone"] = vix_zone(overview["vix"])

    # 幣圈恐慌貪婪指數（免費公開 API，逾時或失敗顯示 N/A）
    try:
        resp = requests.get(FNG_API_URL, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        entries = (payload or {}).get("data") or []
        if entries:
            raw_value = to_float(entries[0].get("value"))
            if raw_value is not None:
                overview["crypto_fear_greed"] = int(round(raw_value))
                raw_class = entries[0].get("value_classification")
                overview["crypto_fear_greed_classification"] = (
                    FNG_CLASS_ZH.get(raw_class, raw_class) if raw_class else None
                )
    except Exception as exc:
        print(f"    [warn] 幣圈恐慌貪婪指數抓取失敗: {exc}")

    return overview


def ensure_dirs():
    """建立需要的資料夾。"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def to_float(value):
    """將數值轉為 float，若為 None / NaN / inf 則回傳 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def normalize_yield(value):
    """將 yfinance 的 dividendYield 正規化為小數（0.0641 表示 6.41%）。

    yfinance 不同版本對 dividendYield 的表示方式不一：可能回傳小數
    （0.0641）或百分比（6.41）。此處統一轉為小數，以利後續判斷與顯示。
    """
    f = to_float(value)
    if f is None:
        return None
    # 殖利率不可能 >= 100%（小數即 >= 1.0），若 > 1 代表是百分比格式，需除以 100
    if f > 1.0:
        return f / 100.0
    return f


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """以 Wilder 平滑法計算 RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # 當平均跌幅為 0（一路走升）時 RSI 定義為 100
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi


def compute_atr(high, low, close, period=ATR_PERIOD):
    """以 Wilder 平滑法計算 ATR（真實波動幅度）。"""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def compute_macd(close: pd.Series, fast: int = MACD_FAST, slow: int = MACD_SLOW,
                 signal: int = MACD_SIGNAL):
    """以 EMA 計算 MACD(12,26,9)，回傳 (macd, macd_signal, macd_hist)。"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    macd_hist = macd - macd_signal
    return macd, macd_signal, macd_hist


def compute_indicators(df):
    """將歷史 OHLCV 轉為策略所需技術指標 DataFrame。"""
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    open_ = df["Open"].astype(float)
    volume = df["Volume"].astype(float)

    ind = pd.DataFrame(index=df.index)
    ind["close"] = close
    ind["high"] = high
    ind["low"] = low
    ind["open"] = open_
    ind["volume"] = volume
    ind["ma20"] = close.rolling(20, min_periods=20).mean()
    ind["ma60"] = close.rolling(60, min_periods=60).mean()
    ind["ma200"] = close.rolling(MA_LONG_PERIOD, min_periods=MA_LONG_PERIOD).mean()
    ind["rsi14"] = compute_rsi(close, 14)
    ind["atr14"] = compute_atr(high, low, close, ATR_PERIOD)
    ind["high20"] = high.rolling(DONCHIAN_PERIOD, min_periods=DONCHIAN_PERIOD).max()
    std20 = close.rolling(20, min_periods=20).std()
    ind["boll_upper"] = ind["ma20"] + 2.0 * std20
    ind["boll_lower"] = ind["ma20"] - 2.0 * std20

    # 第 5 章：月均量流動性與爆量長紅（p.228, p.232-237）
    ind["vol_ma20"] = volume.rolling(VOL_LENGTH, min_periods=VOL_LENGTH).mean()
    ind["vol_ratio"] = volume / ind["vol_ma20"]
    # 標準化實體 K 棒：紅 K（Close > Open）為正，黑 K 為負
    ind["k_body_range"] = (close - open_) / open_
    # 20 日平均實體大小（取絕對值，代表平均振幅）
    ind["k_body_range_ma20"] = (
        ind["k_body_range"].abs().rolling(K_BODY_MA_P, min_periods=K_BODY_MA_P).mean()
    )

    # ===== 遊牧民（Nomad）：非典型均線與量能雷達 =====
    for period in NOMAD_MA_PERIODS:
        ind[f"ma{period}"] = close.rolling(period, min_periods=period).mean()

    # ===== 藤本茂：MACD(12,26,9) 柱狀圖 =====
    ind["macd"], ind["macd_signal"], ind["macd_hist"] = compute_macd(close)

    # ===== 藤本茂：一目均衡表（Ichimoku Cloud） =====
    tenkan_high = high.rolling(ICHIMOKU_TENKAN_PERIOD, min_periods=ICHIMOKU_TENKAN_PERIOD).max()
    tenkan_low = low.rolling(ICHIMOKU_TENKAN_PERIOD, min_periods=ICHIMOKU_TENKAN_PERIOD).min()
    ind["tenkan_sen"] = (tenkan_high + tenkan_low) / 2.0
    kijun_high = high.rolling(ICHIMOKU_KIJUN_PERIOD, min_periods=ICHIMOKU_KIJUN_PERIOD).max()
    kijun_low = low.rolling(ICHIMOKU_KIJUN_PERIOD, min_periods=ICHIMOKU_KIJUN_PERIOD).min()
    ind["kijun_sen"] = (kijun_high + kijun_low) / 2.0
    ind["senkou_a"] = ((ind["tenkan_sen"] + ind["kijun_sen"]) / 2.0).shift(ICHIMOKU_SHIFT)
    senkou_b_high = high.rolling(ICHIMOKU_SENKOU_B_PERIOD, min_periods=ICHIMOKU_SENKOU_B_PERIOD).max()
    senkou_b_low = low.rolling(ICHIMOKU_SENKOU_B_PERIOD, min_periods=ICHIMOKU_SENKOU_B_PERIOD).min()
    ind["senkou_b"] = ((senkou_b_high + senkou_b_low) / 2.0).shift(ICHIMOKU_SHIFT)
    return ind


def classify_horizon(latest):
    """依價格與均線結構判斷短 / 中 / 長線定位。"""
    close = to_float(latest.get("close"))
    ma20 = to_float(latest.get("ma20"))
    ma60 = to_float(latest.get("ma60"))
    ma200 = to_float(latest.get("ma200"))
    if close is None or ma20 is None or ma60 is None:
        return "短線"
    if ma200 is not None:
        if close > ma200 and ma20 > ma60 > ma200:
            return "長線多頭"
        if close < ma200 and ma20 < ma60 < ma200:
            return "長線空頭"
    if ma20 > ma60:
        return "中線多頭"
    if ma20 < ma60:
        return "中線空頭"
    return "短線觀望"


def ichimoku_cloud_position(close, senkou_a, senkou_b):
    """判斷現價位於一目均衡表雲帶的位階：雲上 / 雲中 / 雲下。"""
    if close is None or senkou_a is None or senkou_b is None:
        return None
    cloud_top = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)
    if close > cloud_top:
        return "雲上"
    if close < cloud_bottom:
        return "雲下"
    return "雲中"


def compute_trailing_stop_price(high20, atr14):
    """遠藤洋吊燈移動停利價 = 近 20 日最高價 - 2.2 × ATR14。"""
    if high20 is None or atr14 is None:
        return None
    return high20 - TRAILING_STOP_ATR_MULT * atr14


def compute_position_stage(close, ma3, ma8, ma15, ma33, ma45, macd, macd_hist,
                           rsi, vol_ratio, cloud_pos, tenkan_sen, kijun_sen,
                           trailing_stop_price, near_20d_high_breakout):
    """藤本茂「1:2:6 法則」倉位階段 + cis 禁攤平風控。

    依序判定（優先權高 → 低）：
      - 破線停損（cis 鐵律：嚴禁攤平）
      - 主升滿倉（cis 強勢順勢段）
      - 順勢加碼
      - 偵察試單
    """
    if close is None:
        return STAGE_0

    # 遠藤洋時間成本過濾：地底低量禁買
    if (
        ma45 is not None
        and vol_ratio is not None
        and close < ma45
        and vol_ratio < ENDOH_LOW_VOL_RATIO
    ):
        return STAGE_0

    # cis 鐵律：破線停損，嚴禁攤平
    if trailing_stop_price is not None and close < trailing_stop_price:
        return STAGE_0
    if ma15 is not None and macd is not None and close < ma15 and macd < 0:
        return STAGE_0

    # 主升滿倉：cis 強勢順勢段（站上 MA3/MA8、量能放大且創波段高點）
    above_ma3_ma8 = (
        ma3 is not None and ma8 is not None and close > ma3 and close > ma8
    )
    if (
        above_ma3_ma8
        and vol_ratio is not None
        and vol_ratio >= CIS_MAIN_WAVE_VOL_RATIO
        and near_20d_high_breakout
    ):
        return STAGE_3

    # 順勢加碼：站穩 MA15 與雲帶之上、MACD 多頭且浮盈擴大
    above_ma15 = ma15 is not None and close > ma15
    macd_bull = (
        macd is not None and macd_hist is not None and macd > 0 and macd_hist > 0
    )
    if above_ma15 and cloud_pos == "雲上" and macd_bull:
        return STAGE_2

    # 偵察試單：RSI<30 觸及 MA45 超賣反彈，或剛站上 MA33 / 一目均衡表轉強
    oversold_bounce = (
        rsi is not None and rsi < 30 and ma45 is not None and close >= ma45
    )
    stood_above_ma33 = ma33 is not None and close > ma33
    ichimoku_turn = (
        tenkan_sen is not None and kijun_sen is not None and tenkan_sen > kijun_sen
    )
    if oversold_bounce or stood_above_ma33 or ichimoku_turn:
        return STAGE_1

    return STAGE_0


def compute_master_score(close, ma3, ma8, ma15, ma45, cloud_pos, rsi, macd,
                         macd_hist, vol_ratio, backtest):
    """森口亮「每日 5 分鐘綜合評分」0~100 整數。

    - 趨勢位階（MA3/8/15/45 與雲帶）：35 分
    - 動能與量能（RSI / MACD / vol_ratio）：25 分
    - 回測風控績效（Sharpe / Profit Factor / MDD / 近半年 OOS）：40 分
    """
    backtest = backtest or {}

    # ===== 趨勢位階：35 分 =====
    trend_score = 0.0
    if close is not None:
        for ma, weight in ((ma3, 4), (ma8, 4), (ma15, 6), (ma45, 6)):
            if ma is not None and close > ma:
                trend_score += weight
    trend_score += {"雲上": 15, "雲中": 8}.get(cloud_pos, 0)  # max = 4+4+6+6+15 = 35

    # ===== 動能與量能：25 分 =====
    rsi_score = 0
    if rsi is not None:
        if 50 <= rsi <= 70:
            rsi_score = 10
        elif 40 <= rsi < 50 or 70 < rsi <= 80:
            rsi_score = 7
        elif 30 <= rsi < 40 or 80 < rsi <= 90:
            rsi_score = 4
        else:
            rsi_score = 2

    macd_score = 0
    if macd is not None and macd_hist is not None:
        if macd > 0 and macd_hist > 0:
            macd_score = 10
        elif macd > 0 or macd_hist > 0:
            macd_score = 5
        else:
            macd_score = 0

    vol_score = 0
    if vol_ratio is not None:
        if 1.0 <= vol_ratio <= 2.5:
            vol_score = 5
        elif vol_ratio > 2.5:
            vol_score = 4
        elif 0.7 <= vol_ratio < 1.0:
            vol_score = 3
        else:
            vol_score = 1

    momentum_score = rsi_score + macd_score + vol_score  # max = 10+10+5 = 25

    # ===== 回測風控績效：40 分 =====
    sharpe = to_float(backtest.get("sharpe_ratio"))
    profit_factor = to_float(backtest.get("profit_factor"))
    mdd = to_float(backtest.get("max_drawdown_pct"))
    oos = to_float(backtest.get("oos_return_6m"))

    sharpe_score = 0.0
    if sharpe is not None:
        # Sharpe 1.0 → 10 分、0 → 5 分、-1 → 0 分（線性夾在 0~10）
        sharpe_score = max(0.0, min(10.0, (sharpe + 1.0) * 5.0))

    pf_score = 0
    if profit_factor is not None:
        if profit_factor >= 2.0:
            pf_score = 10
        elif profit_factor >= 1.5:
            pf_score = 8
        elif profit_factor >= 1.2:
            pf_score = 6
        elif profit_factor >= 1.0:
            pf_score = 5
        elif profit_factor >= 0.8:
            pf_score = 3
        else:
            pf_score = 0

    mdd_score = 0
    if mdd is not None:
        abs_mdd = abs(mdd)
        if abs_mdd <= 0.05:
            mdd_score = 10
        elif abs_mdd <= 0.10:
            mdd_score = 8
        elif abs_mdd <= 0.15:
            mdd_score = 6
        elif abs_mdd <= 0.20:
            mdd_score = 4
        elif abs_mdd <= 0.30:
            mdd_score = 2
        else:
            mdd_score = 0

    oos_score = 0
    if oos is not None:
        if oos >= 0.10:
            oos_score = 10
        elif oos >= 0.05:
            oos_score = 8
        elif oos >= 0.0:
            oos_score = 6
        elif oos >= -0.05:
            oos_score = 4
        elif oos >= -0.10:
            oos_score = 2
        else:
            oos_score = 0

    risk_score = sharpe_score + pf_score + mdd_score + oos_score  # max = 40

    total = trend_score + momentum_score + risk_score
    score = int(round(total))
    return max(0, min(MASTER_SCORE_MAX, score))


def domain_signal_series(category, symbol, ind):
    """依資產類別產生歷史多空訊號（1 = 看多 / 0 = 觀望）。"""
    close = ind["close"]
    ma20 = ind["ma20"]
    ma60 = ind["ma60"]
    ma200 = ind["ma200"]
    rsi = ind["rsi14"]
    high20 = ind["high20"]

    if symbol in CASH_PARKING_SYMBOLS:
        return pd.Series(1.0, index=ind.index)

    if category == "Crypto":
        breakout = close > high20.shift(1)
        trend = (close > ma20) & (ma20 > ma60)
        return (breakout | trend).astype(float)

    if category == "Stocks":
        return ((close > ma200) & (ma20 > ma60)).astype(float)

    if category == "REITs":
        return ((rsi < 45) & (close < ma20)).astype(float)

    if category == "Bonds":
        return ((close > ma200) & (ma20 > ma60)).astype(float)

    if category == "ETFs":
        return ((close > ma200) & (close > ma20) & (rsi > 50)).astype(float)

    if category == "Gold":
        breakout = close > high20.shift(1)
        pullback = (close > ma200) & (rsi < 45)
        return (breakout | pullback).astype(float)

    return pd.Series(0.0, index=ind.index)


def _indicator_reversal_exit(category, sub, i, sig, today_sig):
    """依資產類別判斷「④ 指標反轉出場」是否成立（p.239-240 降低交易頻率）。

    - ETFs：僅收盤跌破 MA200（年線牛熊分界）或 RSI14 > 82 極端過熱才反轉。
    - REITs / Bonds：僅收盤跌破 MA200 × 0.97 或 RSI14 > 78 才反轉。
    - Stocks / Gold：僅同時跌破 MA60 且 MA20 < MA60，或 RSI14 > 78 才反轉。
    - Crypto 及其餘類別：沿用進場訊號 1→0 反轉。
    """
    if category == "Crypto":
        return i > 0 and float(sig.iloc[i - 1]) == 1.0 and today_sig == 0.0

    close = to_float(sub["close"].iloc[i])
    if close is None:
        return False
    ma20 = to_float(sub["ma20"].iloc[i]) if "ma20" in sub.columns else None
    ma60 = to_float(sub["ma60"].iloc[i]) if "ma60" in sub.columns else None
    ma200 = to_float(sub["ma200"].iloc[i]) if "ma200" in sub.columns else None
    rsi = to_float(sub["rsi14"].iloc[i]) if "rsi14" in sub.columns else None

    if category == "ETFs":
        return (ma200 is not None and close < ma200) or (rsi is not None and rsi > 82)
    if category in ("REITs", "Bonds"):
        return (ma200 is not None and close < ma200 * 0.97) or (rsi is not None and rsi > 78)
    if category in ("Stocks", "Gold"):
        bear_ma = (
            ma60 is not None and ma20 is not None and close < ma60 and ma20 < ma60
        )
        return bear_ma or (rsi is not None and rsi > 78)

    # 其餘類別（含現金停泊資金池）：沿用訊號反轉
    return i > 0 and float(sig.iloc[i - 1]) == 1.0 and today_sig == 0.0


def backtest_strategy(signal, ind, rules=None, category=None):
    """四大出場邏輯 + 扣費回測模擬器。

    以領域訊號進場（0→1），依圖表 5-8 四大出場條件依序檢查：
        ① 固定持有天數出場
        ② 固定百分比 / ATR 停損
        ③ 固定百分比 / ATR 停利
        ④ 指標反轉出場（依資產類別區分門檻，降低過度交易頻率）
    每次交易結算扣除 ROUND_TRIP_COST（手續費 + 滑價）。

    回傳 dict：win_rate / strategy_return_1y / trade_count_1y / last_exit_reason，
    以及機構級風控指標 max_drawdown_pct / sharpe_ratio / profit_factor / oos_return_6m。
    """
    result = {
        "win_rate": None,
        "strategy_return_1y": None,
        "trade_count_1y": 0,
        "last_exit_reason": "無交易",
        "max_drawdown_pct": None,
        "sharpe_ratio": None,
        "profit_factor": None,
        "oos_return_6m": None,
    }
    if signal is None or ind is None or ind.empty:
        return result

    close = ind["close"]
    sig = signal.reindex(close.index).fillna(0.0).astype(float)

    last_date = close.index[-1]
    start_date = last_date - pd.Timedelta(days=BACKTEST_LOOKBACK_DAYS)
    mask = close.index >= start_date
    sig = sig[mask]
    sub = ind.loc[mask]
    if sub.empty or len(sub) < 2:
        return result

    close_bt = sub["close"].astype(float)
    high_bt = sub["high"].astype(float) if "high" in sub.columns else None
    low_bt = sub["low"].astype(float) if "low" in sub.columns else None
    atr_bt = sub["atr14"].astype(float) if "atr14" in sub.columns else None

    rules = rules or {}

    def _f(series, i):
        if series is None:
            return None
        return to_float(series.iloc[i])

    trades = []  # (net_return, exit_reason, entry_idx, exit_idx)
    entry_idx = None
    entry_price = None
    entry_atr = None

    n = len(sub)
    for i in range(n):
        today_sig = float(sig.iloc[i])
        today_close = float(close_bt.iloc[i])

        # 進場：訊號由 0 → 1
        if entry_idx is None:
            if today_sig == 1.0 and (i == 0 or float(sig.iloc[i - 1]) == 0.0):
                entry_idx = i
                entry_price = today_close
                entry_atr = _f(atr_bt, i)
            continue

        # 持有中：依四大出場條件檢查
        exit_reason = None
        hold_days = i - entry_idx

        # ① 固定持有天數出場
        max_hold = rules.get("max_hold_days")
        if max_hold is not None and hold_days >= max_hold:
            exit_reason = "① 固定持有天數出場"

        # ② 固定百分比 / ATR 停損
        #    長線資產（ETFs / REITs / Bonds）改用較寬波段保護：
        #    取「固定停損百分比」與「進場價 - 3.2 × ATR」較低的價位，
        #    給予大盤指數型標的至少 6%~8% 的正常波動空間，避免頻繁停損。
        if exit_reason is None:
            stop_levels = []
            sl_pct = rules.get("fixed_stop_loss_pct")
            if sl_pct is not None:
                stop_levels.append(entry_price * (1.0 - sl_pct))
            if entry_atr is not None:
                if category in LONG_TERM_STOP_CATEGORIES:
                    stop_levels.append(entry_price - LONG_TERM_ATR_STOP_MULT * entry_atr)
                else:
                    sl_mult = rules.get("atr_stop_mult")
                    if sl_mult is not None:
                        stop_levels.append(entry_price - sl_mult * entry_atr)
            if stop_levels:
                # 長線資產取較低（較寬）停損價；其餘取較高（較嚴格）停損價
                stop_price = (
                    min(stop_levels)
                    if category in LONG_TERM_STOP_CATEGORIES
                    else max(stop_levels)
                )
                today_low = _f(low_bt, i)
                if (today_low is not None and today_low <= stop_price) or today_close <= stop_price:
                    exit_reason = "② 停損出場"

        # ③ 固定百分比 / ATR 停利（取較低停利價，較易達成者先觸發）
        if exit_reason is None:
            tp_levels = []
            tp_pct = rules.get("fixed_take_profit_pct")
            if tp_pct is not None:
                tp_levels.append(entry_price * (1.0 + tp_pct))
            tp_mult = rules.get("atr_tp_mult")
            if entry_atr is not None and tp_mult is not None:
                tp_levels.append(entry_price + tp_mult * entry_atr)
            if tp_levels:
                tp_price = min(tp_levels)
                today_high = _f(high_bt, i)
                if (today_high is not None and today_high >= tp_price) or today_close >= tp_price:
                    exit_reason = "③ 停利出場"

        # ④ 指標反轉出場（依資產類別區分門檻，降低過度交易頻率）
        if exit_reason is None and _indicator_reversal_exit(category, sub, i, sig, today_sig):
            exit_reason = "④ 指標反轉出場"

        if exit_reason is not None:
            net = today_close / entry_price - 1.0 - ROUND_TRIP_COST
            trades.append((net, exit_reason, entry_idx, i))
            entry_idx = None
            entry_price = None
            entry_atr = None

    # 收盤仍未平倉的部位：以最後收盤價結算並標記「持有中」
    if entry_idx is not None:
        net = float(close_bt.iloc[-1]) / entry_price - 1.0 - ROUND_TRIP_COST
        trades.append((net, "持有中（尚未出場）", entry_idx, n - 1))

    if not trades:
        return result

    result["trade_count_1y"] = len(trades)
    result["last_exit_reason"] = trades[-1][1]
    result["win_rate"] = round(
        sum(1 for r, *_ in trades if r > 0) / len(trades), 4
    )

    cumulative = 1.0
    for r, *_ in trades:
        cumulative *= 1.0 + r
    result["strategy_return_1y"] = round(cumulative - 1.0, 4)

    result.update(_compute_risk_metrics(close_bt, trades))
    return result


def _compute_risk_metrics(close_bt, trades):
    """以每日資金曲線計算 4 個機構級風控指標（純 pandas，零額外套件）。

    - max_drawdown_pct：資金曲線自高點回落的最大百分比（小數，負值表示回撤）。
    - sharpe_ratio：以日報酬年化（×√252）的風險調整後收益。
    - profit_factor：總獲利 / 總虧損絕對值（無虧損=9.99、無獲利=0.0）。
    - oos_return_6m：最近 126 個交易日的樣本外累積報酬（小數）。
    """
    metrics = {
        "max_drawdown_pct": None,
        "sharpe_ratio": None,
        "profit_factor": None,
        "oos_return_6m": None,
    }
    if not trades:
        return metrics

    nets = [t[0] for t in trades]

    # 獲利因子：總獲利 / 總虧損絕對值（無虧損=9.99、無獲利=0.0）
    gross_profit = sum(r for r in nets if r > 0)
    gross_loss = abs(sum(r for r in nets if r < 0))
    if gross_loss > 0:
        metrics["profit_factor"] = round(gross_profit / gross_loss, 4)
    elif gross_profit > 0:
        metrics["profit_factor"] = 9.99
    else:
        metrics["profit_factor"] = 0.0

    # 每日標記到市場資金曲線（進場 / 出場皆以當日收盤價結算）
    n = len(close_bt)
    equity = [1.0] * n
    cash = 1.0
    shares = 0.0
    entry_equity = 1.0
    ti = 0
    for i in range(n):
        px = float(close_bt.iloc[i])
        if ti < len(trades) and trades[ti][2] == i:
            # 今日收盤價進場（進場與出場必為不同交易日）
            entry_equity = cash
            shares = cash / px if px else 0.0
            cash = 0.0
            equity[i] = entry_equity
            continue
        if ti < len(trades) and trades[ti][3] == i:
            # 今日收盤價出場
            cash = entry_equity * (1.0 + trades[ti][0])
            shares = 0.0
            equity[i] = cash
            ti += 1
            continue
        equity[i] = shares * px if shares > 0 else cash

    equity_series = pd.Series(equity, index=close_bt.index, dtype=float)

    # 最大回撤（負值：自高點回落的幅度）
    running_peak = equity_series.cummax()
    drawdown = equity_series / running_peak - 1.0
    mdd = float(drawdown.min())
    if math.isfinite(mdd):
        metrics["max_drawdown_pct"] = round(mdd, 4)

    # 年化夏普比率（252 個交易日）
    daily_returns = equity_series.pct_change().dropna()
    if len(daily_returns) >= 2:
        std = float(daily_returns.std())
        if std and std > 0:
            metrics["sharpe_ratio"] = round(
                float(daily_returns.mean()) / std * math.sqrt(252), 4
            )

    # 最近 126 個交易日樣本外累積報酬
    lookback = min(126, len(equity_series))
    if lookback >= 2:
        start_equity = float(equity_series.iloc[-lookback])
        if start_equity and start_equity > 0:
            metrics["oos_return_6m"] = round(
                float(equity_series.iloc[-1]) / start_equity - 1.0, 4
            )

    return metrics


def _round_price(value):
    """價格四捨五入至 4 位小數；None / NaN / inf 回傳 None。"""
    f = to_float(value)
    return round(f, 4) if f is not None else None


def volume_analysis(latest, rules):
    """回傳 (volume_status, big_vol_red_candle)。

    月均量流動性濾網（p.228, p.236）：VOL_MA20、VOL_RATIO、LIQUIDITY_OK。
    爆量長紅偵測（p.232-237）：量比、標準化實體紅 K 與 20 日均值比對。
    """
    rules = rules or {}
    vol_ratio = to_float(latest.get("vol_ratio"))
    k_body = to_float(latest.get("k_body_range"))
    k_body_ma = to_float(latest.get("k_body_range_ma20"))
    vol_ma20 = to_float(latest.get("vol_ma20"))

    min_avg_vol = rules.get("min_avg_vol")
    k_body_floor = rules.get("k_body_floor")

    liquidity_ok = min_avg_vol is None or (
        vol_ma20 is not None and vol_ma20 >= min_avg_vol
    )
    big_vol = vol_ratio is not None and vol_ratio >= BIG_VOL_MULTIPLIER
    long_red = (
        k_body is not None
        and k_body_ma is not None
        and k_body_floor is not None
        and k_body > k_body_floor
        and k_body > K_BODY_MULTIPLIER * k_body_ma
    )
    fire = liquidity_ok and big_vol and long_red

    if not liquidity_ok:
        status = "流動性不足（20日均量未達門檻）"
    elif fire:
        status = f"🔥 爆量長紅突破（量比 {vol_ratio:.2f}）"
    elif big_vol:
        status = f"爆量未長紅（量比 {vol_ratio:.2f}）"
    elif vol_ratio is not None:
        status = f"量能正常（量比 {vol_ratio:.2f}）"
    else:
        status = "量能資料不足"

    return status, fire


def build_exit_rule_desc(rules, category=None):
    """圖表 5-8 四大出場條件摘要。"""
    rules = rules or {}
    max_hold = rules.get("max_hold_days")
    sl_pct = rules.get("fixed_stop_loss_pct")
    tp_pct = rules.get("fixed_take_profit_pct")
    sl_mult = rules.get("atr_stop_mult")
    tp_mult = rules.get("atr_tp_mult")

    # 長線資產改用較寬波段保護（3.2 × ATR），出場法則文字同步反映
    if category in LONG_TERM_STOP_CATEGORIES:
        sl_mult = LONG_TERM_ATR_STOP_MULT

    sl = (
        f"-{sl_pct * 100:.1f}% / -{sl_mult}×ATR"
        if sl_pct is not None and sl_mult is not None
        else "-"
    )
    tp = (
        f"+{tp_pct * 100:.1f}% / +{tp_mult}×ATR"
        if tp_pct is not None and tp_mult is not None
        else "+"
    )
    hold_desc = f"① 持有 ≤ {int(max_hold)} 日" if max_hold else "① 長期持有（不設天數）"
    reversal_desc = {
        "ETFs": "④ 收盤跌破 MA200 或 RSI>82",
        "REITs": "④ 收盤跌破 MA200×0.97 或 RSI>78",
        "Bonds": "④ 收盤跌破 MA200×0.97 或 RSI>78",
        "Stocks": "④ 跌破 MA60 且 MA20<MA60 或 RSI>78",
        "Gold": "④ 跌破 MA60 且 MA20<MA60 或 RSI>78",
        "Crypto": "④ 指標反轉",
    }.get(category, "④ 指標反轉")

    parts = [
        hold_desc,
        f"② 停損 {sl}",
        f"③ 停利 {tp}",
        reversal_desc,
    ]
    return "｜".join(parts)


def _fmt_pct(v):
    if v is None:
        return "N/A"
    return f"{v * 100:.1f}%"


def _fmt_num(v):
    if v is None:
        return "N/A"
    return f"{v:.2f}"


def evaluate_domain_strategy(category, symbol, name, ind, dividend_yield=None,
                             volume_status=None, big_vol_red_candle=False):
    """依六大資產類別實作獨立策略，輸出完整決策欄位。"""
    rules = DOMAIN_EXIT_RULES.get(category, {})

    result = {
        "horizon": None,
        "reference_model": None,
        "action_reason": None,
        "ideal_buy_price": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "volume_status": volume_status,
        "exit_rule_desc": build_exit_rule_desc(rules, category),
        "backtest_strategy_1y": {
            "win_rate": None,
            "strategy_return_1y": None,
            "trade_count_1y": 0,
            "last_exit_reason": "無交易",
            "max_drawdown_pct": None,
            "sharpe_ratio": None,
            "profit_factor": None,
            "oos_return_6m": None,
        },
    }

    if ind is None or ind.empty:
        return result

    # 💵 現金停泊資金池：獨立識別，不套用股票均線減碼訊號
    if symbol in CASH_PARKING_SYMBOLS:
        close_series = ind["close"].dropna()
        last_close = _round_price(float(close_series.iloc[-1])) if not close_series.empty else None
        bt = backtest_strategy(pd.Series(1.0, index=ind.index), ind, rules=None, category=None)
        result.update({
            "horizon": "現金停泊",
            "reference_model": CASH_PARKING_LABEL,
            "action_reason": "超短期國庫券 ETF，視為現金等價物，不套用均線減碼訊號",
            "ideal_buy_price": last_close,
            "stop_loss_price": None,
            "take_profit_price": None,
            "volume_status": "現金停泊",
            "exit_rule_desc": "現金停泊（不套用出場法則）",
            "backtest_strategy_1y": {
                "win_rate": None,
                "strategy_return_1y": bt["strategy_return_1y"],
                "trade_count_1y": 0,
                "last_exit_reason": "現金停泊（無出場）",
                "max_drawdown_pct": bt.get("max_drawdown_pct"),
                "sharpe_ratio": bt.get("sharpe_ratio"),
                "profit_factor": bt.get("profit_factor"),
                "oos_return_6m": bt.get("oos_return_6m"),
            },
        })
        return result

    signal = domain_signal_series(category, symbol, ind)
    result["backtest_strategy_1y"] = backtest_strategy(signal, ind, rules, category=category)

    latest = ind.iloc[-1]
    prev = ind.iloc[-2] if len(ind) > 1 else latest
    close = to_float(latest.get("close"))
    ma20 = to_float(latest.get("ma20"))
    ma60 = to_float(latest.get("ma60"))
    ma200 = to_float(latest.get("ma200"))
    rsi = to_float(latest.get("rsi14"))
    atr = to_float(latest.get("atr14"))
    high20 = to_float(latest.get("high20"))
    prev_high20 = to_float(prev.get("high20"))

    if volume_status is None:
        volume_status, big_vol_red_candle = volume_analysis(latest, rules)
        result["volume_status"] = volume_status

    result["horizon"] = classify_horizon(latest)

    ideal_buy = None
    if category == "Crypto":
        result["reference_model"] = "唐奇安突破 + 均線動能（ATR 嚴格止損）"
        if high20 is not None and prev_high20 is not None and close is not None and close > prev_high20:
            result["action_reason"] = "收盤突破 20 日唐奇安高點，動能轉強"
            ideal_buy = high20
        elif close is not None and ma20 is not None and ma60 is not None and close > ma20 and ma20 > ma60:
            result["action_reason"] = "短中均線多頭排列，延續上行趨勢"
            ideal_buy = ma20
        else:
            result["action_reason"] = "未達突破或動能門檻，暫不追價"
            ideal_buy = ma20
    elif category == "Stocks":
        result["reference_model"] = "年線趨勢 + MA20 回檔買點（ATR 嚴格止損）"
        if close is not None and ma200 is not None and close > ma200:
            if ma20 is not None and ma60 is not None and ma20 > ma60:
                result["action_reason"] = "站上年線且均線多頭，回檔至 MA20 可佈局"
            else:
                result["action_reason"] = "站上年線但短線動能不足，等待回穩"
            ideal_buy = ma20
        else:
            result["action_reason"] = "跌破年線，趨勢轉弱，等待站回年線"
            ideal_buy = ma200
    elif category == "REITs":
        result["reference_model"] = "高殖利率 + RSI 超賣均值回歸（ATR 嚴格止損）"
        if rsi is not None and ma20 is not None and close is not None and rsi < 45 and close < ma20:
            result["action_reason"] = "RSI 超賣且跌破 MA20，殖利率價值浮現，均值回歸買點"
            ideal_buy = close
        elif dividend_yield is not None and dividend_yield > INCOME_YIELD_THRESHOLD and rsi is not None and rsi < 50:
            result["action_reason"] = "殖利率高於 4% 且動能偏弱，收息價值買點"
            ideal_buy = close
        else:
            result["action_reason"] = "殖利率與動能未達門檻，觀望"
            ideal_buy = close
    elif category == "Bonds":
        result["reference_model"] = "年線趨勢 + 均線多頭（利率循環，ATR 嚴格止損）"
        if close is not None and ma200 is not None and close > ma200 and ma20 is not None and ma60 is not None and ma20 > ma60:
            result["action_reason"] = "站上年線且均線多頭，利率下行趨勢延續"
            ideal_buy = ma20
        elif close is not None and ma200 is not None and close < ma200:
            result["action_reason"] = "跌破年線，殖利率上行壓力，觀望"
            ideal_buy = ma200
        else:
            result["action_reason"] = "趨勢未明，觀望"
            ideal_buy = ma20
    elif category == "ETFs":
        result["reference_model"] = "年線趨勢 + 布林中軌順勢（RSI 動能，ATR 嚴格止損）"
        if close is not None and ma200 is not None and close > ma200 and ma20 is not None and rsi is not None and close > ma20 and rsi > 50:
            result["action_reason"] = "站上年線且 RSI 動能向上，順勢持有"
            ideal_buy = ma20
        elif close is not None and ma200 is not None and close > ma200:
            result["action_reason"] = "年線上方但動能偏弱，等待回穩"
            ideal_buy = ma20
        else:
            result["action_reason"] = "跌破年線，觀望"
            ideal_buy = ma200
    elif category == "Gold":
        result["reference_model"] = "避險動能 + 唐奇安突破/年線回檔（ATR 嚴格止損）"
        if high20 is not None and prev_high20 is not None and close is not None and close > prev_high20:
            result["action_reason"] = "突破 20 日高點，避險動能轉強"
            ideal_buy = high20
        elif close is not None and ma200 is not None and rsi is not None and close > ma200 and rsi < 45:
            result["action_reason"] = "年線上方 RSI 超賣，避險回檔買點"
            ideal_buy = ma200
        else:
            result["action_reason"] = "避險動能未明，觀望"
            ideal_buy = ma200
    else:
        result["reference_model"] = "跨市場動能模型（ATR 嚴格止損）"
        result["action_reason"] = "依動能與均線結構研判"
        ideal_buy = ma20

    if big_vol_red_candle:
        result["action_reason"] = "爆量長紅突破，量價齊揚，強勢買進"
        ideal_buy = close

    result["ideal_buy_price"] = _round_price(ideal_buy)
    if ideal_buy is not None and atr is not None:
        result["take_profit_price"] = _round_price(ideal_buy + ATR_TAKE_PROFIT_MULT * atr)
        # 長線資產（ETFs / REITs / Bonds）改用較寬波段保護：
        # 取「固定停損百分比」與「建議買入價 - 3.2 × ATR」較低的價位，
        # 避免大盤指數 VOO/QQQ 因 2% 小回檔就頻繁停損。
        if category in LONG_TERM_STOP_CATEGORIES:
            stop_candidates = []
            sl_pct = rules.get("fixed_stop_loss_pct")
            if sl_pct is not None:
                stop_candidates.append(ideal_buy * (1.0 - sl_pct))
            stop_candidates.append(ideal_buy - LONG_TERM_ATR_STOP_MULT * atr)
            result["stop_loss_price"] = _round_price(min(stop_candidates))
        else:
            result["stop_loss_price"] = _round_price(ideal_buy - ATR_STOP_MULT * atr)

    return result


def save_history(symbol: str, df: pd.DataFrame):
    """將歷史數據防重複（依日期去重）寫入 CSV。"""
    if df is None or df.empty:
        return
    df = df.copy()
    # 移除時區，避免與既有 CSV 合併時出現 tz-aware / tz-naive 混用問題
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index.name = "Date"

    csv_path = os.path.join(HISTORY_DIR, f"{symbol}.csv")
    if os.path.exists(csv_path):
        try:
            old = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            if old.index.tz is not None:
                old.index = old.index.tz_localize(None)
            df = pd.concat([old, df])
        except Exception as exc:  # 讀取失敗則直接重建
            print(f"    [warn] 讀取既有歷史失敗，重建 {symbol}: {exc}")

    df = df[~df.index.duplicated(keep="last")]
    df.sort_index(inplace=True)
    # 容量防膨脹鎖：合併去重後只保留最近 MAX_HISTORY_ROWS 個交易日
    df = df.tail(MAX_HISTORY_ROWS)
    df.to_csv(csv_path, encoding="utf-8")

def compute_signal(category, close, prev_close, ma20, ma60, rsi,
                   boll_lower, prev_boll_lower, dividend_yield,
                   big_vol_red_candle=False):
    """依優先順序判斷量化訊號，回傳訊號字串。"""
    if close is None:
        return SIGNAL_NEUTRAL

    # 🔥 爆量長紅突破（第 5 章選股 2）：優先於所有均線 / RSI 訊號
    if big_vol_red_candle:
        return SIGNAL_FIRE

    # 🟢 強烈買進 / 低檔加碼：收盤 > MA60 且 RSI < 40，或跌破布林下軌後站回
    above_ma60_oversold = (
        ma60 is not None and rsi is not None and close > ma60 and rsi < 40
    )
    broke_lower_band = (
        prev_close is not None
        and prev_boll_lower is not None
        and boll_lower is not None
        and prev_close <= prev_boll_lower
        and close > boll_lower
    )
    if above_ma60_oversold or broke_lower_band:
        return SIGNAL_GREEN

    # 🔵 趨勢多頭 / 續抱：MA20 > MA60 且收盤 > MA20
    if (
        ma20 is not None
        and ma60 is not None
        and ma20 > ma60
        and close > ma20
    ):
        return SIGNAL_BLUE

    # 🟡 價值收息買點（僅 REITs 與 Bonds）：年化殖利率 > 4% 且 RSI < 50
    if (
        category in INCOME_CATEGORIES
        and dividend_yield is not None
        and dividend_yield > INCOME_YIELD_THRESHOLD
        and rsi is not None
        and rsi < 50
    ):
        return SIGNAL_YELLOW

    # 🔴 減碼 / 停利警戒：RSI > 75 或收盤跌破 MA20
    if (rsi is not None and rsi > 75) or (ma20 is not None and close < ma20):
        return SIGNAL_RED

    # ⚪ 區間觀望
    return SIGNAL_NEUTRAL


def clean_dirty_data(df):
    """【硬傷3】髒數據清洗器。

    剔除以下異常 K 棒，避免技術指標與回測被髒數據污染：
      1. Close <= 0
      2. High < Low（或 High / Low 非正數）
      3. 單日漲跌幅超過 DIRTY_DATA_MAX_DAILY_CHANGE（如小數點錯位多一個零）
    """
    if df is None or df.empty:
        return df
    df = df.copy()
    for col in ("Open", "High", "Low", "Close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if not {"Close", "High", "Low"}.issubset(df.columns):
        return df

    valid = (
        (df["Close"] > 0)
        & (df["High"] > 0)
        & (df["Low"] > 0)
        & (df["High"] >= df["Low"])
    )
    prev_close = df["Close"].shift(1)
    daily_change = (df["Close"] / prev_close - 1.0).abs()
    valid &= daily_change.fillna(0.0) <= DIRTY_DATA_MAX_DAILY_CHANGE

    cleaned = df[valid]
    removed = len(df) - len(cleaned)
    if removed:
        print(f"    [clean] 剔除 {removed} 筆髒數據（Close<=0 / High<Low / 單日漲跌>45%）")
    return cleaned


def fetch_binance_klines(symbol):
    """【幣安官方 API】抓取加密貨幣日 K 線（OHLCV）。

    直接請求幣安官方正式公開接口，回傳最精準的一級數據源日 K 線；
    若連線失敗（如網路地區限制），由呼叫端自動 fallback 至 yfinance。
    """
    pair = CRYPTO_BINANCE_SYMBOLS.get(symbol)
    if not pair:
        raise ValueError(f"{symbol} 沒有對應的 Binance 交易對")

    resp = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": pair, "interval": "1d", "limit": 500},
        timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{symbol} Binance 回傳空資料或格式異常")

    df = pd.DataFrame(rows, columns=[
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ])
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    df.index = pd.to_datetime([int(row[0]) for row in rows], unit="ms")
    df.index.name = "Date"
    return clean_dirty_data(df)


def fetch_yfinance_history(symbol, retries=YFINANCE_RETRY_MAX):
    """【硬傷1】以禮貌延遲 + 失敗重試抓取 yfinance 歷史，回傳 (df_adj, df_raw)。

    df_adj（auto_adjust=True）供技術指標與回測；df_raw（auto_adjust=False）供真實報價。
    抓取失敗最多重試 retries 次，每次重試前延遲 POLITE_SLEEP_SECONDS 秒。
    """
    session = _make_yfinance_session()
    ticker = yf.Ticker(symbol, session=session)
    last_error = None
    for attempt in range(retries + 1):
        try:
            df_adj = ticker.history(
                period="2y", interval="1d", auto_adjust=True,
                timeout=YFINANCE_TIMEOUT_SECONDS,
            )
            df_raw = ticker.history(
                period="2y", interval="1d", auto_adjust=False,
                timeout=YFINANCE_TIMEOUT_SECONDS,
            )
            if df_adj is not None and not df_adj.empty and df_raw is not None and not df_raw.empty:
                return clean_dirty_data(df_adj), clean_dirty_data(df_raw)
            last_error = "無歷史資料"
        except Exception as exc:
            last_error = str(exc)
        if attempt < retries:
            print(
                f"    [warn] {symbol} yfinance 第 {attempt + 1} 次抓取失敗"
                f"（{last_error}），{POLITE_SLEEP_SECONDS}s 後重試"
            )
            time.sleep(POLITE_SLEEP_SECONDS)
    print(f"    [warn] {symbol} yfinance 抓取失敗（已重試 {retries} 次）: {last_error}")
    return None, None


def fetch_fundamentals(symbol):
    """抓取基本面資訊（殖利率 / 本益比 / 股價淨值比），失敗回傳空 dict。"""
    try:
        return yf.Ticker(symbol, session=_make_yfinance_session()).info or {}
    except Exception as exc:
        print(f"    [warn] {symbol} 基本面取得失敗: {exc}")
        return {}


def load_local_history_csv(symbol):
    """【硬傷2】讀取本地歷史快取，確保 yfinance 失敗時系統絕不當機。"""
    csv_path = os.path.join(HISTORY_DIR, f"{symbol}.csv")
    if not os.path.exists(csv_path):
        return None
    try:
        df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
    except Exception as exc:
        print(f"    [warn] {symbol} 讀取本地歷史失敗: {exc}")
        return None
    if df is None or df.empty:
        return None

    if not {"Close", "High", "Low"}.issubset(df.columns):
        return None
    cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[cols].copy()
    df.index.name = "Date"
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return clean_dirty_data(df)


def load_market_data(category, symbol):
    """依類別選擇資料源，回傳 (df_adj, df_raw, data_source)。

    - Crypto：幣安官方 API 優先，失敗 fallback 至 yfinance，再 fallback 本地快取。
    - 其餘：yfinance 優先，失敗 fallback 本地快取。
    """
    if category == "Crypto":
        try:
            df = fetch_binance_klines(symbol)
            if df is not None and not df.empty:
                return df, df.copy(), SOURCE_BINANCE
            print(f"    [warn] {symbol} Binance 回傳空資料，改採 yfinance")
        except Exception as exc:
            print(f"    [warn] {symbol} Binance 連線失敗（可能地區限制）: {exc}，改採 yfinance")

        df_adj, df_raw = fetch_yfinance_history(symbol)
        if df_adj is not None and not df_adj.empty:
            return df_adj, df_raw, SOURCE_YAHOO

        df = load_local_history_csv(symbol)
        if df is not None and not df.empty:
            return df, df.copy(), SOURCE_CACHE
        return None, None, None

    df_adj, df_raw = fetch_yfinance_history(symbol)
    if df_adj is not None and not df_adj.empty and df_raw is not None and not df_raw.empty:
        return df_adj, df_raw, SOURCE_YAHOO

    df = load_local_history_csv(symbol)
    if df is not None and not df.empty:
        return df, df.copy(), SOURCE_CACHE
    return None, None, None


def fetch_and_analyze(category, symbol, name):
    """抓取單一標的歷史與基本面，計算指標並回傳最新一筆記錄。"""
    record = {
        "symbol": symbol,
        "name": name,
        "category": category,
        "data_source": None,
        "price": None,
        "change_pct": None,
        "ma20": None,
        "ma60": None,
        "ma200": None,
        "rsi14": None,
        "atr14": None,
        "high20": None,
        "boll_upper": None,
        "boll_lower": None,
        "ma3": None,
        "ma8": None,
        "ma15": None,
        "ma33": None,
        "ma45": None,
        "vol_ratio": None,
        "tenkan_sen": None,
        "kijun_sen": None,
        "senkou_a": None,
        "senkou_b": None,
        "ichimoku_cloud": None,
        "macd": None,
        "macd_signal": None,
        "macd_hist": None,
        "trailing_stop_price": None,
        "endoh_time_cost_filter": None,
        "near_20d_high_breakout": False,
        "position_stage": STAGE_0,
        "master_score": None,
        "dividend_yield": None,
        "trailing_pe": None,
        "price_to_book": None,
        "signal": SIGNAL_NEUTRAL,
        "horizon": None,
        "reference_model": None,
        "action_reason": None,
        "ideal_buy_price": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "suggested_weight_pct": None,
        "volume_status": None,
        "exit_rule_desc": None,
        "backtest_strategy_1y": {
            "win_rate": None,
            "strategy_return_1y": None,
            "trade_count_1y": 0,
            "last_exit_reason": "無交易",
            "max_drawdown_pct": None,
            "sharpe_ratio": None,
            "profit_factor": None,
            "oos_return_6m": None,
        },
    }

    try:
        # 依類別選擇資料源：Crypto 幣安優先，其餘 yfinance 優先；
        # 抓取失敗自動 fallback 至本地快取（./data/history/{symbol}.csv）。
        df_adj, df_raw, data_source = load_market_data(category, symbol)
        record["data_source"] = data_source

        if df_adj is None or df_adj.empty or df_raw is None or df_raw.empty:
            print(f"    [warn] {symbol} 無可用歷史資料（yfinance 與本地快取皆失敗）")
            return record

        # 對齊兩組資料的交易日
        common = df_adj.index.intersection(df_raw.index)
        df_adj = df_adj.loc[common]
        df_raw = df_raw.loc[common]
        if df_adj.empty or df_raw.empty:
            print(f"    [warn] {symbol} 歷史資料日期無法對齊")
            return record

        # 僅 Yahoo 來源寫回本地快取（幣安官方資料不與 yfinance 快取混用）
        if data_source == SOURCE_YAHOO:
            save_history(symbol, df_raw)

        # 基本面資訊（僅 Yahoo 來源具備完整基本面；幣安/本地快取設為 None）
        if data_source == SOURCE_YAHOO:
            info = fetch_fundamentals(symbol)
            record["dividend_yield"] = normalize_yield(info.get("dividendYield"))
            record["trailing_pe"] = to_float(info.get("trailingPE"))
            record["price_to_book"] = to_float(info.get("priceToBook"))
        else:
            record["dividend_yield"] = None
            record["trailing_pe"] = None
            record["price_to_book"] = None

        # 以還原價計算所有技術指標與回測
        ind = compute_indicators(df_adj)
        ind = ind[ind["close"].notna()].copy()
        if ind.empty:
            print(f"    [warn] {symbol} 指標資料不足")
            return record

        latest = ind.iloc[-1]
        rules = DOMAIN_EXIT_RULES.get(category, {})

        # 未還原現價（真實看盤報價）
        raw_close = df_raw["Close"].astype(float).dropna()
        if len(raw_close) < 2:
            print(f"    [warn] {symbol} 收盤資料不足")
            return record
        raw_last_close = to_float(raw_close.iloc[-1])
        raw_prev_close = to_float(raw_close.iloc[-2])

        last_close = to_float(latest.get("close"))
        adj_prev_close = to_float(ind["close"].iloc[-2]) if len(ind) > 1 else None
        last_ma20 = to_float(latest.get("ma20"))
        last_ma60 = to_float(latest.get("ma60"))
        last_ma200 = to_float(latest.get("ma200"))
        last_rsi = to_float(latest.get("rsi14"))
        last_atr = to_float(latest.get("atr14"))
        last_high20 = to_float(latest.get("high20"))
        last_boll_upper = to_float(latest.get("boll_upper"))
        last_boll_lower = to_float(latest.get("boll_lower"))
        prev_boll_lower = to_float(ind["boll_lower"].iloc[-2]) if len(ind) > 1 else None

        record["price"] = round(raw_last_close, 4) if raw_last_close is not None else None
        record["change_pct"] = (
            round((raw_last_close / raw_prev_close - 1.0) * 100.0, 2)
            if raw_last_close is not None and raw_prev_close
            else None
        )
        record["ma20"] = round(last_ma20, 4) if last_ma20 is not None else None
        record["ma60"] = round(last_ma60, 4) if last_ma60 is not None else None
        record["ma200"] = round(last_ma200, 4) if last_ma200 is not None else None
        record["rsi14"] = round(last_rsi, 2) if last_rsi is not None else None
        record["atr14"] = round(last_atr, 4) if last_atr is not None else None
        record["high20"] = round(last_high20, 4) if last_high20 is not None else None
        record["boll_upper"] = round(last_boll_upper, 4) if last_boll_upper is not None else None
        record["boll_lower"] = round(last_boll_lower, 4) if last_boll_lower is not None else None

        # 第 5 章：月均量流動性濾網 + 爆量長紅偵測
        volume_status, big_vol_red_candle = volume_analysis(latest, rules)
        record["volume_status"] = volume_status

        # 💵 現金停泊資金池：不套用一般股票均線減碼訊號
        if symbol in CASH_PARKING_SYMBOLS:
            record["signal"] = CASH_PARKING_LABEL
        else:
            record["signal"] = compute_signal(
                category, last_close, adj_prev_close, last_ma20, last_ma60, last_rsi,
                last_boll_lower, prev_boll_lower, record["dividend_yield"],
                big_vol_red_candle=big_vol_red_candle,
            )

        strategy = evaluate_domain_strategy(
            category, symbol, name, ind, record["dividend_yield"],
            volume_status=volume_status, big_vol_red_candle=big_vol_red_candle,
        )
        record.update(strategy)

        # ===== 五位日韓操盤名家：五師指標、倉位階段與綜合評分 =====
        prev_ind = ind.iloc[-2] if len(ind) > 1 else latest
        prev_high20 = to_float(prev_ind.get("high20"))

        last_ma3 = to_float(latest.get("ma3"))
        last_ma8 = to_float(latest.get("ma8"))
        last_ma15 = to_float(latest.get("ma15"))
        last_ma33 = to_float(latest.get("ma33"))
        last_ma45 = to_float(latest.get("ma45"))
        last_vol_ratio = to_float(latest.get("vol_ratio"))
        last_tenkan = to_float(latest.get("tenkan_sen"))
        last_kijun = to_float(latest.get("kijun_sen"))
        last_senkou_a = to_float(latest.get("senkou_a"))
        last_senkou_b = to_float(latest.get("senkou_b"))
        last_macd = to_float(latest.get("macd"))
        last_macd_signal = to_float(latest.get("macd_signal"))
        last_macd_hist = to_float(latest.get("macd_hist"))

        cloud_pos = ichimoku_cloud_position(last_close, last_senkou_a, last_senkou_b)
        trailing_stop = compute_trailing_stop_price(last_high20, last_atr)
        near_20d_high_breakout = bool(
            last_close is not None and prev_high20 is not None and last_close > prev_high20
        )
        endoh_filter = (
            ENDOH_LOW_VOL_LABEL
            if (
                last_ma45 is not None
                and last_vol_ratio is not None
                and last_close is not None
                and last_close < last_ma45
                and last_vol_ratio < ENDOH_LOW_VOL_RATIO
            )
            else None
        )

        record["ma3"] = round(last_ma3, 4) if last_ma3 is not None else None
        record["ma8"] = round(last_ma8, 4) if last_ma8 is not None else None
        record["ma15"] = round(last_ma15, 4) if last_ma15 is not None else None
        record["ma33"] = round(last_ma33, 4) if last_ma33 is not None else None
        record["ma45"] = round(last_ma45, 4) if last_ma45 is not None else None
        record["vol_ratio"] = round(last_vol_ratio, 4) if last_vol_ratio is not None else None
        record["tenkan_sen"] = round(last_tenkan, 4) if last_tenkan is not None else None
        record["kijun_sen"] = round(last_kijun, 4) if last_kijun is not None else None
        record["senkou_a"] = round(last_senkou_a, 4) if last_senkou_a is not None else None
        record["senkou_b"] = round(last_senkou_b, 4) if last_senkou_b is not None else None
        record["ichimoku_cloud"] = cloud_pos
        record["macd"] = round(last_macd, 6) if last_macd is not None else None
        record["macd_signal"] = round(last_macd_signal, 6) if last_macd_signal is not None else None
        record["macd_hist"] = round(last_macd_hist, 6) if last_macd_hist is not None else None
        record["trailing_stop_price"] = round(trailing_stop, 4) if trailing_stop is not None else None
        record["endoh_time_cost_filter"] = endoh_filter
        record["near_20d_high_breakout"] = near_20d_high_breakout
        record["position_stage"] = compute_position_stage(
            last_close, last_ma3, last_ma8, last_ma15, last_ma33, last_ma45,
            last_macd, last_macd_hist, last_rsi, last_vol_ratio, cloud_pos,
            last_tenkan, last_kijun, trailing_stop, near_20d_high_breakout,
        )
        record["master_score"] = compute_master_score(
            last_close, last_ma3, last_ma8, last_ma15, last_ma45, cloud_pos,
            last_rsi, last_macd, last_macd_hist, last_vol_ratio,
            record.get("backtest_strategy_1y"),
        )

        # 除權息還原校正：以當日未還原現價比例，將還原價位換算回真實看盤報價
        if last_close and raw_last_close:
            price_ratio = raw_last_close / last_close
            for key in ("ideal_buy_price", "stop_loss_price", "take_profit_price", "trailing_stop_price"):
                if record.get(key) is not None:
                    record[key] = round(record[key] * price_ratio, 4)
    except Exception as exc:
        print(f"    [error] {symbol} 處理失敗: {exc}")

    return record

def _bond_tilt(us10y_yield):
    """依 10 年期美債殖利率計算債券類別權重調節係數。

    以 4% 為中性基準：殖利率越高，債券票息越具吸引力，故調高債券類權重；
    反之殖利率越低，債券吸引力下降，調低債券類權重。調節幅度夾在 0.6~1.5 倍。
    """
    if us10y_yield is None:
        return 1.0
    tilt = 1.0 + (us10y_yield - BOND_TILT_NEUTRAL_YIELD) * BOND_TILT_SENSITIVITY
    return max(BOND_TILT_MIN, min(BOND_TILT_MAX, tilt))


def compute_suggested_weights(records, macro_overview):
    """以逆波動率 Risk-Parity 思維計算 16 檔建議資金配置權重（總和 100%）。

    - 單檔波動率以「ATR14 / 現價」代理（日波動率）。
    - 權重 ∝ 1 / 波動率，並套用波動率下限避免現金類標的（如 SGOV）權重無限放大。
    - 債券類別權重再乘上宏觀利率調節係數（10 年期美債殖利率越高，債券權重越重）。
    - 最後正規化為百分比並四捨五入至小數 2 位，尾差補到最大權重標的確保總和恰為 100%。
    """
    macro = macro_overview or {}
    tnx = macro.get("us10y_yield")
    bond_tilt = _bond_tilt(tnx)

    raw = {}
    for r in records:
        sym = r.get("symbol")
        price = to_float(r.get("price"))
        atr = to_float(r.get("atr14"))
        vol_pct = (atr / price) if (price and atr) else None
        if vol_pct is None or vol_pct <= 0:
            vol_pct = SUGGESTED_WEIGHT_FALLBACK_VOL
        vol_pct = max(vol_pct, SUGGESTED_WEIGHT_MIN_VOL)
        inv_vol = 1.0 / vol_pct
        if r.get("category") == "Bonds":
            inv_vol *= bond_tilt
        raw[sym] = inv_vol

    total = sum(raw.values())
    if total <= 0:
        return {r.get("symbol"): None for r in records}

    pct = {sym: round(v / total * 100.0, 2) for sym, v in raw.items()}
    # 四捨五入可能造成總和 99.9x 或 100.0x，尾差補到最大權重標的
    diff = round(100.0 - sum(pct.values()), 2)
    if diff and pct:
        max_sym = max(pct, key=lambda k: pct[k])
        pct[max_sym] = round(pct[max_sym] + diff, 2)
    return pct


def build_summary(records):
    """產生統計摘要（總數、訊號分布、類別分布）。"""
    signal_counts = {}
    category_counts = {}
    for r in records:
        sig = r["signal"]
        signal_counts[sig] = signal_counts.get(sig, 0) + 1
        cat = r["category"]
        category_counts[cat] = category_counts.get(cat, 0) + 1
    return {
        "total_assets": len(records),
        "signal_counts": signal_counts,
        "category_counts": category_counts,
    }


def main():
    # 強制 UTF-8 輸出，避免 Windows cp950 主控台無法編碼 emoji
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ensure_dirs()
    print("== 量化引擎啟動 ==")

    assets = {}
    all_records = []
    for category, symbols in ASSET_UNIVERSE.items():
        print(f"\n[{category}]")
        assets[category] = []
        for symbol, name in symbols.items():
            print(f"  - {symbol} ({name})")
            record = fetch_and_analyze(category, symbol, name)
            assets[category].append(record)
            all_records.append(record)
            time.sleep(POLITE_SLEEP_SECONDS)  # 【硬傷1】禮貌延遲，避免觸發 Yahoo 頻率限制

    summary = build_summary(all_records)

    print("\n== 全球宏觀與市場情緒溫度計 ==")
    macro_overview = fetch_macro_overview()
    tnx = macro_overview.get("us10y_yield")
    dxy = macro_overview.get("dxy")
    vix = macro_overview.get("vix")
    fng = macro_overview.get("crypto_fear_greed")
    print(
        f"    10年美債殖利率: {f'{tnx:.2f}%' if tnx is not None else 'N/A'} ｜ "
        f"美元指數 DXY: {f'{dxy:.2f}' if dxy is not None else 'N/A'} ｜ "
        f"VIX: {f'{vix:.2f}' if vix is not None else 'N/A'}"
        f"（{macro_overview.get('vix_zone') or 'N/A'}） ｜ "
        f"幣圈恐慌貪婪: {f'{int(fng)}' if fng is not None else 'N/A'}"
        f"（{macro_overview.get('crypto_fear_greed_classification') or 'N/A'}）"
    )

    # 建議資金配置權重（逆波動率 Risk-Parity + 宏觀利率調節，16 檔總和 100%）
    weights = compute_suggested_weights(all_records, macro_overview)
    for r in all_records:
        r["suggested_weight_pct"] = weights.get(r["symbol"])

    payload = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summary,
        "macro_overview": macro_overview,
        "assets": assets,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 訊號已輸出至 {OUTPUT_FILE}")
    print("   訊號分布：")
    for sig, cnt in sorted(summary["signal_counts"].items(), key=lambda x: -x[1]):
        print(f"     {sig}：{cnt} 檔")

    print("\n== 16 檔標的總表（停損價 / 建議倉位 / 勝率 / 交易次數 / 累積報酬 / MDD / 夏普 / 獲利因子 / 近半年報酬）==")
    print(f"{'標的':<10} {'資料來源':<20} {'停損價':>10} {'建議倉位':>9} {'勝率':>8} {'交易次數':>8} {'累積報酬':>10} {'MDD':>9} {'夏普':>7} {'獲利因子':>8} {'近半年':>9}")
    print("-" * 128)
    for r in all_records:
        bt = r.get("backtest_strategy_1y") or {}
        sl = r.get("stop_loss_price")
        sl_txt = f"{sl:.4f}" if sl is not None else "N/A"
        w = r.get("suggested_weight_pct")
        w_txt = f"{w:.2f}%" if w is not None else "N/A"
        print(
            f"{r['symbol']:<10} {str(r.get('data_source') or 'N/A'):<20} "
            f"{sl_txt:>10} {w_txt:>9} "
            f"{_fmt_pct(bt.get('win_rate')):>8} "
            f"{str(bt.get('trade_count_1y')):>8} "
            f"{_fmt_pct(bt.get('strategy_return_1y')):>10} "
            f"{_fmt_pct(bt.get('max_drawdown_pct')):>9} "
            f"{_fmt_num(bt.get('sharpe_ratio')):>7} "
            f"{_fmt_num(bt.get('profit_factor')):>8} "
            f"{_fmt_pct(bt.get('oos_return_6m')):>9}"
        )
    print("== 量化引擎完成 ==")


if __name__ == "__main__":
    main()


