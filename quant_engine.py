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
import random
import sys
import time
from zoneinfo import ZoneInfo

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

# ===== 歷史訊號與 30/90/180 天驗證帳本（森口亮 Excel 四季報検証用 + 茂爺爺「記錄檢討每天交易」）=====
LEDGER_FILE = os.path.join(".", "data", "signal_ledger.csv")
LEDGER_COLUMNS = [
    "date",
    "symbol",
    "close_price",
    "master_score",
    "five_dim_avg",
    "position_stage",
    "target_price_2",
    "floor_price",
    "nomad_signal",
]

# 本地 CSV 容量防膨脹鎖：每檔只保留最近 N 個交易日（約 4 年日線，足夠計算 MA200 與 1 年回測）
MAX_HISTORY_ROWS = 1000

# 訊號定義
SIGNAL_GREEN = "🟢 強烈買進 / 低檔加碼"
SIGNAL_BLUE = "🔵 趨勢多頭 / 續抱"
SIGNAL_YELLOW = "🟡 價值收息買點"
SIGNAL_RED = "🔴 減碼 / 停利警戒"
SIGNAL_NEUTRAL = "⚪ 區間觀望"
SIGNAL_FIRE = "🔥 爆量長紅突破 (強勢買進)"
# 訊號一致性閘門（Signal Gatekeeper）：低分/空頭時取代「強烈買進」的校正訊號
SIGNAL_WAIT_BREAKOUT = "⚪ 區間觀望 / 等待突破"
SIGNAL_OVERSOLD_TRIAL = "🟡 左側超賣試單 (嚴控1/9小倉)"

# 價值收息訊號適用的資產類別
INCOME_CATEGORIES = ("REITs", "Bonds")
# 價值收息所需的年化殖利率門檻（yfinance 回傳為小數，0.04 = 4%）
INCOME_YIELD_THRESHOLD = 0.04

# ===== 真實摩擦成本與滑點（單邊）：按資產類別區分 =====
SINGLE_SIDE_COST = {
    "Stocks": 0.0015,
    "REITs": 0.0015,
    "ETFs": 0.0015,
    "Crypto": 0.0020,
    "Bonds": 0.0008,
    "Gold": 0.0008,
}

# ===== 裝甲4：極端踩踏動態滑點緩衝（Stress Slippage Buffer，單位：百分比點）=====
# 常態摩擦成本維持：股/ETF 0.15%、幣 0.20%、債/金 0.08%
EXPECTED_SLIPPAGE_PCT = {
    "Stocks": 0.15,
    "REITs": 0.15,
    "ETFs": 0.15,
    "Crypto": 0.20,
    "Bonds": 0.08,
    "Gold": 0.08,
}
# 極端踩踏滑點緩衝下限：Stocks/REITs/ETFs 0.40%、Crypto 0.80%、Bonds/Gold 0.20%
STRESS_SLIPPAGE_BASE_PCT = {
    "Stocks": 0.40,
    "REITs": 0.40,
    "ETFs": 0.40,
    "Crypto": 0.80,
    "Bonds": 0.20,
    "Gold": 0.20,
}
# 極端踩踏滑點緩衝上限：Stocks/REITs/ETFs 0.60%、Crypto 1.50%、Bonds/Gold 0.20%
STRESS_SLIPPAGE_MAX_PCT = {
    "Stocks": 0.60,
    "REITs": 0.60,
    "ETFs": 0.60,
    "Crypto": 1.50,
    "Bonds": 0.20,
    "Gold": 0.20,
}
# 高波動幣種（SOL）極端滑點緩衝固定 1.50%
STRESS_SLIPPAGE_HIGH_VOL_SYMBOLS = {"SOL-USD"}

# ===== 裝甲3：美股冬夏令時差與「未收盤半根 K 線」時間守衛 =====
# 各交易所當地交易時段：(時區, (開盤時, 開盤分), (收盤時, 收盤分))；None 表示 24/7 全天候
MARKET_SESSION_RULES = {
    "America/New_York": (9, 30, 16, 0),   # 美股/美債/黃金 ETF
    "Asia/Taipei": (9, 0, 13, 30),        # 台股
    "Asia/Hong_Kong": (9, 30, 16, 0),     # 港股
    "UTC_24_7": None,                     # 加密貨幣 24/7 全天候
}

# 未收盤 K 線標記文字
BAR_CLOSED_LABEL = "✅ 已收盤"
BAR_INTRADAY_LABEL = "⏳ 盤中即時K線 (未收盤)"

# ===== 市場狀態三態分類器 =====
REGIME_TREND = "🔥 強趨勢推進盤"
REGIME_RANGE = "⚖️ 箱型震盪盤"
REGIME_PANIC = "⚡ 高波恐慌盤"

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
# 資料來源健康狀態（裝甲1：單一標的隔離保護 + 後廚斷供報警）
DATA_SOURCE_LIVE = "🟢 即時連線 (Live)"
DATA_SOURCE_FALLBACK = "🟠 降級快取 (Cached Fallback)"
DATA_SOURCE_FAILED = "🔴 資料取得失敗"
# 【硬傷1】禮貌延遲（每檔標的抓取間隔）與最多失敗重試次數
POLITE_SLEEP_SECONDS = 0.4
YFINANCE_RETRY_MAX = 3
# 【裝甲1】指數退避重試：最多 3 次，等待 2s -> 5s -> 10s + 隨機 0.5~1.5s jitter
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
RETRY_JITTER_RANGE = (0.5, 1.5)
# 【硬傷3】髒數據清洗門檻：單日漲跌幅超過 45% 視為異常（如小數點錯位多一個零）
DIRTY_DATA_MAX_DAILY_CHANGE = 0.45
# 【硬傷4】yfinance 連線超時保護（秒）：所有抓取帶 timeout，避免卡死
YFINANCE_TIMEOUT_SECONDS = 15

# ===== 交易者自訂財報覆寫與 Excel 數學估值公式 =====
CUSTOM_ESTIMATES_PATH = os.path.join(".", "data", "custom_estimates.json")
DEFAULT_BASE_PER = 15.0          # 行業標準本益比
DEFAULT_COMPOUND_YEARS = 3       # 目標本益比複利年數 t
TARGET_PER_MIN = 8.0             # 複利目標本益比合理下限
TARGET_PER_MAX = 120.0           # 複利目標本益比合理上限
EXCEL_GRADE_A_PLUS = 8.0         # 五維度平均 >= 8.0 標記「主力級 A+」
RULE_OF_40_THRESHOLD = 40.0      # Rule of 40 達標門檻（%）

# 離線模式：python quant_engine.py --offline，僅讀本地 CSV 快取，不進行任何網路請求
OFFLINE_MODE = "--offline" in sys.argv

# 基本面估值適用之資產類別（其餘採「歷史波動率分位數 + 200日均線乖離率」映射）
FUNDAMENTAL_CATEGORIES = ("Stocks", "REITs")


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

# ===== 遊牧民（Nomad）「交減陰線」暴漲前兆與買後三大原則 =====
NOMAD_WASHOUT_VOL_RATIO = 0.25     # 今日量 <= 0.25 × 近10日最大量（量縮黑K）
NOMAD_WASHOUT_VOL_MA_RATIO = 0.6   # 連續量縮時，今日量 < 0.6 × 20日均量
NOMAD_SUPPORT_TOLERANCE = 0.03     # MA120 支撐 ±3%
NOMAD_TP_1PCT = 0.01               # 新手無條件 +1% 停利
NOMAD_TP_3PCT = 0.03               # 線圖短打 +3% 停利
NOMAD_SL_5PCT = 0.05               # -5% 誓死停損鐵律
NOMAD_NEWS_TRAP_GAIN = 0.85        # 距近60日低點上漲 > +85%
NOMAD_NEWS_TRAP_RSI = 72           # 高檔利多陷阱 RSI 門檻

# ===== 藤本茂（Fujimoto）「6大選股重點」 =====
FUJIMOTO_CHEAP_FORWARD_PE = 15.0   # 便宜估值：forward PE <= 15
FUJIMOTO_CHEAP_PBR = 1.0           # 便宜估值：PBR <= 1.0
FUJIMOTO_RSI_OVERSOLD = 30.0       # RSI 超賣買點
FUJIMOTO_RSI_OVERBOUGHT = 70.0     # RSI 過熱
FUJIMOTO_WEEKLY_MA = 4             # 週線 MA4（約日線 MA20）

# ===== 遠藤洋（Endo）「Tenbagger 檢核」 =====
ENDO_MA = 25                       # 上升模式判定均線 MA25
ENDO_MIN_DAILY_TURNOVER = 30_000_000  # 單日成交額達標（對應日股 3,000 萬圓流動性）
# 遠藤洋「小型暴漲股」市值門檻：日股 <= 300 億日圓；美股/其他 <= 100 億美元（中小型成長股）
ENDO_SMALL_CAP_JPY = 30_000_000_000   # 日股 300 億日圓以下（時価総額）
ENDO_SMALL_CAP_USD = 10_000_000_000   # 美股/其他中小型成長股：100 億美元以下


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

    if OFFLINE_MODE:
        return overview

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


# ===== 裝甲3：未收盤 K 線時間守衛 =====
def market_session_for(symbol):
    """依標的代號回傳 (時區規則鍵, 開盤時, 開盤分, 收盤時, 收盤分)。

    加密貨幣為 24/7 全天候，無「未收盤」問題，故回傳 None。
    """
    if symbol.endswith(".TW"):
        return "Asia/Taipei", MARKET_SESSION_RULES["Asia/Taipei"]
    if symbol.endswith(".HK"):
        return "Asia/Hong_Kong", MARKET_SESSION_RULES["Asia/Hong_Kong"]
    if symbol in CRYPTO_BINANCE_SYMBOLS or symbol.endswith("-USD"):
        return "UTC", MARKET_SESSION_RULES["UTC_24_7"]
    return "America/New_York", MARKET_SESSION_RULES["America/New_York"]


def bar_close_status(symbol, now=None):
    """判斷標的當前最後一根日 K 線是否已收盤。

    回傳 (is_bar_closed: bool, label: str)。
    加密貨幣 24/7 全天候視為「已收盤」；週末休市視為「已收盤」。
    """
    now = now or datetime.datetime.now()
    tz_name, session = market_session_for(symbol)
    if session is None:  # 24/7 全天候（加密貨幣）
        return True, BAR_CLOSED_LABEL

    open_h, open_m, close_h, close_m = session
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except Exception:
        return True, BAR_CLOSED_LABEL

    if local.weekday() >= 5:  # 週六/週日休市
        return True, BAR_CLOSED_LABEL

    cur_minutes = local.hour * 60 + local.minute
    open_minutes = open_h * 60 + open_m
    close_minutes = close_h * 60 + close_m
    if open_minutes <= cur_minutes < close_minutes:
        return False, BAR_INTRADAY_LABEL
    return True, BAR_CLOSED_LABEL


# ===== 裝甲4：動態壓力滑點模型與實盤安全倉位 =====
def compute_stress_slippage_pct(category, symbol, record, macro_overview=None):
    """計算極端踩踏滑點緩衝（百分比點），無壓力時回傳 0.0。

    觸發條件（任一即啟動「踩踏滑點懲罰係數 2.5x~4.0x」）：
      - market_regime == 高波恐慌盤
      - VIX > 25
      - 當日振幅 > 2 * ATR
      - 觸發止損砍倉
    """
    normal = EXPECTED_SLIPPAGE_PCT.get(category, 0.15)
    triggers = []
    if record.get("market_regime") == REGIME_PANIC:
        triggers.append("高波恐慌盤")
    vix = to_float((macro_overview or {}).get("vix"))
    if vix is not None and vix > 25:
        triggers.append(f"VIX {vix:.1f} > 25")
    if record.get("amplitude_over_2atr"):
        triggers.append("振幅 > 2×ATR")
    if record.get("stop_loss_triggered"):
        triggers.append("觸發止損砍倉")
    if not triggers:
        return 0.0

    base = STRESS_SLIPPAGE_BASE_PCT.get(category, normal)
    cap = STRESS_SLIPPAGE_MAX_PCT.get(category, normal)
    if symbol in STRESS_SLIPPAGE_HIGH_VOL_SYMBOLS:
        return cap
    # 踩踏滑點懲罰係數：觸發越多越接近 4.0x（單一觸發 2.5x）
    multiplier = min(4.0, 2.0 + 0.5 * len(triggers))
    slippage = normal * multiplier
    return round(max(base, min(cap, slippage)), 4)


def compute_effective_stop_distance_pct(record):
    """有效止損距離 % = |現價 - 防守止損價| / 現價 + stress_slippage_pct。"""
    price = to_float(record.get("price"))
    stop = to_float(record.get("current_defense_sl"))
    if stop is None:
        stop = to_float(record.get("stop_loss_price"))
    stress = to_float(record.get("stress_slippage_pct")) or 0.0
    if price is None or stop is None or price <= 0:
        return None
    return round(abs(price - stop) / price * 100.0 + stress, 4)


def compute_safe_position(record, capital=100_000.0, max_loss_pct=1.0):
    """吧台部位試算：把極端滑點緩衝算進 1% 最大虧損額度內。

    安全投入金額 = (總本金 * 單筆容忍虧損%) / 有效止損距離 %
    """
    price = to_float(record.get("price"))
    entry = to_float(record.get("breakout_entry_price"))
    if entry is None:
        entry = to_float(record.get("ideal_buy_price"))
    if entry is None:
        entry = price
    stop = to_float(record.get("current_defense_sl"))
    if stop is None:
        stop = to_float(record.get("stop_loss_price"))
    stress = to_float(record.get("stress_slippage_pct")) or 0.0
    if price is None or entry is None or stop is None or entry <= 0 or stop <= 0:
        return None

    eff_dist_pct = abs(price - stop) / price * 100.0 + stress
    if eff_dist_pct <= 0:
        return None
    risk_budget = capital * max_loss_pct / 100.0
    safe_invest = risk_budget / (eff_dist_pct / 100.0)
    shares = int(safe_invest // entry)
    invest = shares * entry
    max_loss = invest * eff_dist_pct / 100.0
    return {
        "capital": capital,
        "max_loss_pct": max_loss_pct,
        "effective_stop_distance_pct": round(eff_dist_pct, 4),
        "safe_invest_amount": round(safe_invest, 2),
        "safe_shares": shares,
        "invest_amount": round(invest, 2),
        "max_loss_amount": round(max_loss, 2),
    }


def normalize_yield(value):
    """將 yfinance 殖利率正規化為小數（0.0046 表示 0.46%、0.0641 表示 6.41%）。

    yfinance 對殖利率的表示方式隨版本與標的不同而異：
    - trailingAnnualDividendYield：固定為小數（0.0046 = 0.46%）。
    - dividendYield：部分美股 / ETF 已回傳百分比數值（0.46 = 0.46%），
      部分標的仍是小數（0.057 = 5.7%）。

    統一策略：個股 / ETF 年化殖利率理應落在 0.02%~2.0%（小數 0.0002~0.02），
    絕不可能超過 20%。因此凡數值 > 0.2 皆視為百分比格式（除以 100 還原），
    僅 0 < 值 <= 0.2 視為小數直接採用。
    """
    f = to_float(value)
    if f is None:
        return None
    if f <= 0.0:
        return 0.0
    # 殖利率 > 20%（小數 0.2）不合理，必為百分比格式，除以 100 還原為小數
    if f > 0.2:
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


def compute_adx(high, low, close, period=14):
    """以 Wilder 平滑法計算 ADX(14) 趨勢強度指標。"""
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(0.0, index=close.index)
    minus_dm = pd.Series(0.0, index=close.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move[(up_move > down_move) & (up_move > 0)]
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move[(down_move > up_move) & (down_move > 0)]

    tr = compute_atr(high, low, close, period=period)
    tr_smooth = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / tr_smooth
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / tr_smooth
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


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

    # ===== 市場狀態三態分類器與微觀訂單流指標 =====
    # 布林帶寬度（%）與 ADX(14) 趨勢強度
    ind["bb_width"] = (ind["boll_upper"] - ind["boll_lower"]) / ind["ma20"]
    ind["adx14"] = compute_adx(high, low, close, 14)
    # 單日訂單流不平衡（Order Flow Imbalance）與近 5 日累積淨訂單流
    hl_range = (high - low).clip(lower=1e-9)
    ind["ofi_bar"] = ((2.0 * close - high - low) / hl_range) * volume
    ind["ofi_5d"] = ind["ofi_bar"].rolling(5, min_periods=1).sum()
    ind["vol_5d_sum"] = volume.rolling(5, min_periods=1).sum()
    # 20 日成交量加權均價（VWAP_20）
    typical_price = (high + low + close) / 3.0
    ind["vwap_20"] = (
        (typical_price * volume).rolling(20, min_periods=20).sum()
        / volume.rolling(20, min_periods=20).sum()
    )

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
    # 交減陰線：MA5、MA120 半年線、前一日量變化、近10日最大量、近60日低點
    ind["ma5"] = close.rolling(5, min_periods=5).mean()
    ind["ma120"] = close.rolling(120, min_periods=120).mean()
    ind["vol_drop_prev"] = volume < volume.shift(1)
    ind["max_vol_10d"] = volume.rolling(10, min_periods=10).max()
    ind["low_60d"] = low.rolling(60, min_periods=60).min()
    # 遠藤洋：MA25 上升模式
    ind["ma25"] = close.rolling(ENDO_MA, min_periods=ENDO_MA).mean()

    # ===== 藤本茂：週 K 趨勢（週線收盤 / 週線 MA4 / 週 RSI） =====
    try:
        weekly = close.resample("W-FRI").last().dropna()
        ind["weekly_close"] = weekly.reindex(ind.index, method="ffill")
        ind["weekly_ma4"] = (
            weekly.rolling(FUJIMOTO_WEEKLY_MA, min_periods=FUJIMOTO_WEEKLY_MA).mean()
            .reindex(ind.index, method="ffill")
        )
        ind["weekly_rsi"] = compute_rsi(weekly, 14).reindex(ind.index, method="ffill")
    except Exception:
        ind["weekly_close"] = float("nan")
        ind["weekly_ma4"] = float("nan")
        ind["weekly_rsi"] = float("nan")

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


def classify_market_regime(adx, bb_width_pctl, vol_pctl, atr_pct):
    """市場狀態三態分類器：強趨勢推進盤 / 箱型震盪盤 / 高波恐慌盤。"""
    if (vol_pctl is not None and vol_pctl >= 0.85) or (atr_pct is not None and atr_pct >= 0.06):
        return REGIME_PANIC
    if adx is not None and adx >= 25:
        return REGIME_TREND
    return REGIME_RANGE


def classify_ofi_direction(ofi_5d, vol_5d):
    """近 5 日累積淨訂單流方向：主力吸籌 / 買賣均衡 / 高檔派發。"""
    if ofi_5d is None:
        return None
    threshold = 0.10 * (vol_5d or 0.0)
    if ofi_5d > threshold:
        return "🟢 主力主動吸籌"
    if ofi_5d < -threshold:
        return "🔴 高檔派發倒貨"
    return "⚪ 買賣力道均衡"


def compute_current_defense_sl(close, atr, category):
    """基於現價的防守止損價：距離至少 1.5×ATR 或 -3%~-5%。"""
    if close is None or close <= 0:
        return None
    pct = {
        "Crypto": 0.05, "Stocks": 0.05, "ETFs": 0.04,
        "REITs": 0.04, "Bonds": 0.03, "Gold": 0.03,
    }.get(category, 0.04)
    min_dist = pct * close
    if atr is not None and atr > 0:
        min_dist = max(min_dist, 1.5 * atr)
    return round(close - min_dist, 4)


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


def round_trip_cost(category):
    """回傳該資產類別的來回交易成本（單邊成本 × 2）。"""
    single = SINGLE_SIDE_COST.get(category, 0.0015)
    return 2.0 * single


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
    cost = round_trip_cost(category)

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
            net = today_close / entry_price - 1.0 - cost
            trades.append((net, exit_reason, entry_idx, i))
            entry_idx = None
            entry_price = None
            entry_atr = None

    # 收盤仍未平倉的部位：以最後收盤價結算並標記「持有中」
    if entry_idx is not None:
        net = float(close_bt.iloc[-1]) / entry_price - 1.0 - cost
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
        "equity_curve_60d": None,
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

    # 最近 60 日回測淨值曲線（供 UI 迷你曲線）
    tail60 = equity_series.tail(60)
    metrics["equity_curve_60d"] = [round(float(v), 4) for v in tail60]

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


def _detect_and_heal_split_discontinuity(symbol, new_df, old_df):
    """【裝甲2】拆股與除權息跳空斷層自動修復器。

    回傳 True 表示偵測到拆股/除權息或歷史基準不一致，應捨棄舊 CSV 快取，
    直接以最新抓回的完整歷史序列覆寫重存，杜絕「均線錯亂而誤喊暴跌停損」。
    偵測條件：
      1. 新抓回資料含非零的 Stock Splits / Dividends 欄位。
      2. 新資料單日異常跳空 > 35%。
      3. 與舊 CSV 重疊日期的收盤價不一致（超過 0.5%）。
    """
    if old_df is None or old_df.empty:
        return False

    # 1. 拆股/除權息欄位非零
    for col in ("Stock Splits", "Dividends"):
        if col in new_df.columns:
            vals = pd.to_numeric(new_df[col], errors="coerce").fillna(0.0)
            if (vals != 0.0).any():
                return True

    # 2. 新資料單日異常跳空 > 35%（無預警變動）
    close = pd.to_numeric(new_df["Close"], errors="coerce").dropna()
    if len(close) > 1:
        daily = (close / close.shift(1) - 1.0).abs()
        if (daily > 0.35).any():
            return True

    # 3. 重疊日期收盤價基準不一致
    common = new_df.index.intersection(old_df.index)
    if len(common) > 0:
        new_c = pd.to_numeric(new_df["Close"], errors="coerce")
        old_c = pd.to_numeric(old_df["Close"], errors="coerce")
        denom = old_c[common].abs().replace(0.0, float("nan"))
        diff = ((new_c[common] - old_c[common]).abs() / denom).dropna()
        if (diff > 0.005).any():
            return True
    return False


def save_history(symbol: str, df: pd.DataFrame):
    """將歷史數據防重複（依日期去重）寫入 CSV，並內建拆股/除權息斷層修復。"""
    if df is None or df.empty:
        return
    df = df.copy()
    # 移除時區，避免與既有 CSV 合併時出現 tz-aware / tz-naive 混用問題
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index.name = "Date"

    csv_path = os.path.join(HISTORY_DIR, f"{symbol}.csv")
    old = None
    if os.path.exists(csv_path):
        try:
            old = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            if old.index.tz is not None:
                old.index = old.index.tz_localize(None)
        except Exception as exc:  # 讀取失敗則直接重建
            print(f"    [warn] 讀取既有歷史失敗，重建 {symbol}: {exc}")

    if _detect_and_heal_split_discontinuity(symbol, df, old):
        print(f"    [heal] {symbol} 偵測到拆股/除權息斷層，捨棄舊快取，以最新完整歷史覆寫")
        merged = df
    elif old is not None and not old.empty:
        merged = pd.concat([old, df])
    else:
        merged = df

    merged = merged[~merged.index.duplicated(keep="last")]
    merged.sort_index(inplace=True)
    # 容量防膨脹鎖：合併去重後只保留最近 MAX_HISTORY_ROWS 個交易日
    merged = merged.tail(MAX_HISTORY_ROWS)
    merged.to_csv(csv_path, encoding="utf-8")

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


def _downgrade_signal(record, rsi):
    """低分/空頭時，將強烈買進校正為區間觀望或左側超賣試單，確保 100% 邏輯一致。"""
    if rsi is not None and rsi < FUJIMOTO_RSI_OVERSOLD:
        record["signal"] = SIGNAL_OVERSOLD_TRIAL
        record["position_stage"] = STAGE_1
        record["action_reason"] = "左側超賣試單：RSI 超賣但五師評分不足/均線偏空，僅可 1/9 小倉偵察，嚴守止損"
    else:
        record["signal"] = SIGNAL_WAIT_BREAKOUT
        record["position_stage"] = STAGE_0
        record["action_reason"] = "未達突破或動能門檻，暫不追價"
    return record


def apply_signal_gatekeeper(record, ma20, ma60, rsi):
    """統一最終決策閘門：主訊號、五師評分、1:2:6 倉位與底部備註 100% 一致。"""
    signal = record.get("signal")
    if signal != SIGNAL_GREEN:
        return record
    score = to_float(record.get("master_score"))
    ma_bearish = ma20 is not None and ma60 is not None and ma20 < ma60
    if (score is not None and score < 45) or ma_bearish:
        return _downgrade_signal(record, rsi)
    return record


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

    # 裝甲2：拆股/除權息日為合法公司行為跳空（非髒數據），予以保留供斷層修復器偵測
    corporate_action = pd.Series(False, index=df.index)
    for col in ("Stock Splits", "Dividends"):
        if col in df.columns:
            corporate_action |= pd.to_numeric(df[col], errors="coerce").fillna(0.0) != 0.0
    valid |= corporate_action

    cleaned = df[valid]
    removed = len(df) - len(cleaned)
    if removed:
        print(f"    [clean] 剔除 {removed} 筆髒數據（Close<=0 / High<Low / 單日漲跌>45%）")
    return cleaned


def fetch_binance_klines(symbol, retries=YFINANCE_RETRY_MAX):
    """【幣安官方 API】抓取加密貨幣日 K 線（OHLCV），內建指數退避重試。

    直接請求幣安官方正式公開接口，回傳最精準的一級數據源日 K 線；
    HTTP 429/403/連線逾時/空資料皆重試（2s->5s->10s + jitter）；
    若最終仍失敗（如網路地區限制），由呼叫端自動 fallback 至 yfinance。
    """
    pair = CRYPTO_BINANCE_SYMBOLS.get(symbol)
    if not pair:
        raise ValueError(f"{symbol} 沒有對應的 Binance 交易對")

    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                BINANCE_KLINES_URL,
                params={"symbol": pair, "interval": "1d", "limit": 500},
                timeout=15,
            )
            if resp.status_code in (429, 403):
                last_error = f"HTTP {resp.status_code}"
            else:
                resp.raise_for_status()
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    df = pd.DataFrame(rows, columns=[
                        "open_time", "Open", "High", "Low", "Close", "Volume",
                        "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
                    ])
                    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
                    df.index = pd.to_datetime([int(row[0]) for row in rows], unit="ms")
                    df.index.name = "Date"
                    return clean_dirty_data(df)
                last_error = "回傳空資料或格式異常"
        except Exception as exc:
            last_error = str(exc)
        if attempt < retries:
            backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            jitter = random.uniform(*RETRY_JITTER_RANGE)
            wait = backoff + jitter
            print(
                f"    [warn] {symbol} Binance 第 {attempt + 1} 次抓取失敗"
                f"（{last_error}），{wait:.2f}s 後重試"
            )
            time.sleep(wait)

    print(f"    [warn] {symbol} Binance 抓取失敗（已重試 {retries} 次）: {last_error}")
    raise ValueError(f"{symbol} Binance 抓取失敗: {last_error}")


def fetch_yfinance_history(symbol, retries=YFINANCE_RETRY_MAX):
    """【裝甲1】以指數退避 + 隨機 jitter 抓取 yfinance 歷史，回傳 (df_adj, df_raw)。

    df_adj（auto_adjust=True）供回測；df_raw（auto_adjust=False）為未復權真實交易價，
    統一作為技術指標與止損定位的價格基準（裝甲2：對齊券商盤面 Raw Close）。
    抓取失敗最多重試 retries 次，等待 2s -> 5s -> 10s + 隨機 0.5~1.5s jitter，
    並針對 HTTP 429 / 403 / 連線逾時 / 空 DataFrame 皆觸發重試。
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
            last_error = "回傳空 DataFrame"
        except Exception as exc:
            last_error = str(exc)
        if attempt < retries:
            backoff = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            jitter = random.uniform(*RETRY_JITTER_RANGE)
            wait = backoff + jitter
            print(
                f"    [warn] {symbol} yfinance 第 {attempt + 1} 次抓取失敗"
                f"（{last_error}），{wait:.2f}s 後重試"
            )
            time.sleep(wait)
    print(f"    [warn] {symbol} yfinance 抓取失敗（已重試 {retries} 次）: {last_error}")
    return None, None


def fetch_fundamentals(symbol):
    """抓取基本面資訊（殖利率 / 本益比 / 股價淨值比），失敗回傳空 dict。"""
    if OFFLINE_MODE:
        return {}
    try:
        return yf.Ticker(symbol, session=_make_yfinance_session()).info or {}
    except Exception as exc:
        print(f"    [warn] {symbol} 基本面取得失敗: {exc}")
        return {}


def fetch_fast_quote(symbol):
    """抓取 fast_info 即時報價，用於與還原收盤價做交叉驗證。"""
    if OFFLINE_MODE:
        return {}
    try:
        fast = yf.Ticker(symbol, session=_make_yfinance_session()).fast_info
        last_price = None
        for key in ("last_price", "lastPrice"):
            try:
                last_price = to_float(getattr(fast, key, None))
            except Exception:
                last_price = None
            if last_price is not None:
                break
        previous_close = None
        for key in ("previous_close", "previousClose", "regular_market_previous_close"):
            try:
                previous_close = to_float(getattr(fast, key, None))
            except Exception:
                previous_close = None
            if previous_close is not None:
                break
        return {"last_price": last_price, "previous_close": previous_close}
    except Exception as exc:
        print(f"    [warn] {symbol} fast_info 取得失敗: {exc}")
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
    if OFFLINE_MODE:
        df = load_local_history_csv(symbol)
        if df is not None and not df.empty:
            return df, df.copy(), SOURCE_CACHE
        return None, None, None

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


# ===== Excel 數學估值公式（森口亮基本面估值 + 五維度評分） =====

_CUSTOM_ESTIMATES_CACHE = None


def load_custom_estimates():
    """讀取 data/custom_estimates.json（交易者自訂財報覆寫），回傳 (defaults, overrides)。"""
    if not os.path.exists(CUSTOM_ESTIMATES_PATH):
        return {}, {}
    try:
        with open(CUSTOM_ESTIMATES_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        print(f"    [warn] 讀取 custom_estimates.json 失敗: {exc}")
        return {}, {}
    return payload.get("defaults") or {}, payload.get("overrides") or {}


def _custom_estimates():
    global _CUSTOM_ESTIMATES_CACHE
    if _CUSTOM_ESTIMATES_CACHE is None:
        _CUSTOM_ESTIMATES_CACHE = load_custom_estimates()
    return _CUSTOM_ESTIMATES_CACHE


def _round_or_none(value, ndigits=4):
    f = to_float(value)
    return round(f, ndigits) if f is not None else None


def _clamp_value(value, lo, hi):
    if value is None:
        return None
    return max(lo, min(hi, value))


def _clamp_score(value):
    return round(max(1.0, min(10.0, value)), 2)


def _parse_earnings_date(value):
    """將 yfinance earningsDate / 自訂日期字串 / 時間戳轉為 date 物件。"""
    if value is None:
        return None
    try:
        if isinstance(value, (list, tuple)):
            for item in value:
                parsed = _parse_earnings_date(item)
                if parsed is not None:
                    return parsed
            return None
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value).date()
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.datetime.strptime(s, fmt).date()
                except ValueError:
                    continue
            return datetime.date.fromisoformat(s[:10])
    except Exception:
        return None
    return None


def _historical_vol_percentile(ind):
    """當前 60 日年化波動率，及其在近 2 年波動率區間中的分位數（0~1）。"""
    try:
        close = ind["close"].astype(float)
        returns = close.pct_change().dropna()
        if len(returns) < 60:
            return None, None
        rolling_vol = returns.rolling(60, min_periods=60).std() * math.sqrt(252)
        rolling_vol = rolling_vol.dropna()
        if rolling_vol.empty:
            return None, None
        current = float(rolling_vol.iloc[-1])
        percentile = float((rolling_vol <= current).mean())
        return max(0.0, min(1.0, percentile)), current
    except Exception:
        return None, None


def apply_fast_quote_verification(symbol, record, raw_last_close):
    """以 fast_info 即時報價交叉驗證收盤價；乖離 > 2% 時改用即時報價。"""
    quote = fetch_fast_quote(symbol)
    fast_price = quote.get("last_price")
    record["fast_quote_price"] = fast_price
    if fast_price is not None and raw_last_close is not None and raw_last_close > 0:
        diff = abs(fast_price - raw_last_close) / raw_last_close
        record["quote_cross_verified"] = diff <= 0.02
        if diff > 0.02:
            record["price"] = round(fast_price, 4)
    else:
        record["quote_cross_verified"] = False


def _five_dim_scores_fundamental(profit_growth_rate, op_margin, upside_1, upside_2,
                                 downside_risk):
    """森口亮五維度評分（基本面）：①成長性 ②獲利性 ③低估性 ④安全性 ⑤暴漲性。"""

    def _growth():
        g = profit_growth_rate
        if g is None:
            return 5.0
        if g >= 0.30: return 10.0
        if g >= 0.20: return 9.0
        if g >= 0.15: return 8.0
        if g >= 0.10: return 7.0
        if g >= 0.05: return 6.0
        if g >= 0.00: return 5.0
        if g >= -0.05: return 4.0
        if g >= -0.10: return 3.0
        if g >= -0.20: return 2.0
        return 1.0

    def _profitability():
        m = op_margin
        if m is None:
            return 5.0
        if m >= 0.40: return 10.0
        if m >= 0.30: return 9.0
        if m >= 0.25: return 8.0
        if m >= 0.20: return 7.0
        if m >= 0.15: return 6.0
        if m >= 0.10: return 5.0
        if m >= 0.05: return 4.0
        if m >= 0.00: return 3.0
        return 2.0

    def _undervaluation():
        u = upside_2
        if u is None:
            return 5.0
        if u >= 1.0: return 10.0
        if u >= 0.6: return 9.0
        if u >= 0.4: return 8.0
        if u >= 0.25: return 7.0
        if u >= 0.15: return 6.0
        if u >= 0.05: return 5.0
        if u >= 0.00: return 4.0
        if u >= -0.10: return 3.0
        if u >= -0.20: return 2.0
        return 1.0

    def _safety():
        d = downside_risk
        if d is None:
            return 5.0
        if d >= 0.0: return 10.0
        if d >= -0.05: return 9.0
        if d >= -0.10: return 8.0
        if d >= -0.15: return 7.0
        if d >= -0.20: return 6.0
        if d >= -0.25: return 5.0
        if d >= -0.30: return 4.0
        if d >= -0.40: return 3.0
        return 2.0

    def _explosive():
        u = upside_1
        if u is None:
            return 5.0
        if u >= 1.0: return 10.0
        if u >= 0.6: return 9.0
        if u >= 0.4: return 8.0
        if u >= 0.25: return 7.0
        if u >= 0.15: return 6.0
        if u >= 0.05: return 5.0
        if u >= 0.00: return 4.0
        if u >= -0.10: return 3.0
        if u >= -0.20: return 2.0
        return 1.0

    return {
        "growth": _growth(),
        "profitability": _profitability(),
        "undervaluation": _undervaluation(),
        "safety": _safety(),
        "explosive": _explosive(),
    }


def _five_dim_scores_technical(ma200_bias, rsi, vol_pctl, close_adj, high20):
    """森口亮五維度評分（無財報標的）：以波動率分位數 + 200日均線乖離率映射。"""
    bias = ma200_bias if ma200_bias is not None else 0.0
    r = rsi if rsi is not None else 50.0
    pctl = vol_pctl if vol_pctl is not None else 0.5

    growth = 5.0 + bias * 10.0 + (r - 50.0) / 10.0
    profitability = 5.0 + bias * 5.0 + (0.5 - pctl) * 4.0
    undervaluation = 5.0 - bias * 10.0
    safety = 10.0 - pctl * 9.0
    explosive = 5.0 + pctl * 5.0
    if close_adj is not None and high20 is not None and high20 > 0:
        if close_adj >= high20:
            explosive += 1.0
        elif close_adj >= high20 * 0.98:
            explosive += 0.5

    return {
        "growth": _clamp_score(growth),
        "profitability": _clamp_score(profitability),
        "undervaluation": _clamp_score(undervaluation),
        "safety": _clamp_score(safety),
        "explosive": _clamp_score(explosive),
    }


def _grade_for(average):
    if average >= 8.0:
        return "主力級 A+"
    if average >= 7.0:
        return "A級"
    if average >= 6.0:
        return "B+級"
    if average >= 5.0:
        return "B級"
    if average >= 4.0:
        return "C級"
    return "D級"


def _cash_parking_valuation(close_adj, ma200, rsi, high20, vol_pctl, ma200_bias):
    """SGOV 現金停泊資金池：無目標價 / 無停損，僅保留五維度技術評分供一致性。"""
    scores = _five_dim_scores_technical(ma200_bias, rsi, vol_pctl, close_adj, high20)
    average = max(1.0, min(10.0, round(sum(scores.values()) / 5.0, 2)))
    return {
        "valuation_model": "現金停泊資金池（無目標價 / 無停損）",
        "eps_mid": None,
        "eps_current": None,
        "eps_next": None,
        "base_per": None,
        "compound_years": None,
        "profit_growth_rate": None,
        "target_per": None,
        "per_high_avg": None,
        "per_low_avg": None,
        "target_price_1": None,
        "upside_1": None,
        "target_price_2": None,
        "upside_2": None,
        "floor_price": None,
        "downside_risk": None,
        "rule_of_40": None,
        "rule_of_40_met": False,
        "earnings_date": None,
        "check_90d": None,
        "check_180d": None,
        "five_dim_scores": {
            "growth": _round_or_none(scores["growth"], 2),
            "profitability": _round_or_none(scores["profitability"], 2),
            "undervaluation": _round_or_none(scores["undervaluation"], 2),
            "safety": _round_or_none(scores["safety"], 2),
            "explosive": _round_or_none(scores["explosive"], 2),
            "average": average,
            "grade": _grade_for(average),
            "is_a_plus": bool(average >= EXCEL_GRADE_A_PLUS),
        },
    }


def _reit_yield_spread_valuation(symbol, price, dividend_yield, pbr, us10y,
                                 ind, latest, ma200_bias, close_adj, rsi,
                                 high20, vol_pctl):
    """REITs 專屬估值：股息殖利率利差模型 + PBR 淨值折溢價。

    - 利差 = 標的殖利率 - 10Y 美債殖利率（正利差 = 收息吸引力高於無風險利率）。
    - 合理目標價：以「殖利率壓縮至 target_yield」反推（價格漲、殖利率降）。
    - 收息地板價：以「殖利率擴張至 floor_yield」反推（價格跌、殖利率升）。
    - 無殖利率時退回 MA200 ± 2×ATR 波段區間，徹底避免負 EPS × PER 的負目標價。
    """
    atr = to_float(latest.get("atr14"))
    ma200 = to_float(latest.get("ma200"))
    dy = to_float(dividend_yield)
    us10y_dec = to_float(us10y)
    if us10y_dec is not None and us10y_dec > 1.0:
        us10y_dec = us10y_dec / 100.0  # 保險：若誤傳百分比數值（4.9）轉為小數

    yield_spread = None
    target_price_1 = target_price_2 = floor_price = None
    upside_1 = upside_2 = downside_risk = None

    if dy is not None and dy > 0 and price is not None and price > 0:
        if us10y_dec is not None:
            yield_spread = dy - us10y_dec
        dps = price * dy                      # 以現價回推年化每股配息
        target_yield = max(dy * 0.85, 0.01)   # 上檔：殖利率壓縮 15%
        floor_yield = max(dy * 1.30, 0.01)    # 下檔：殖利率擴張 30%（收息地板）
        target_price_2 = dps / target_yield
        target_price_1 = target_price_2 * 1.06
        floor_price = dps / floor_yield
        if target_price_2 and price:
            upside_2 = (target_price_2 - price) / price
        if target_price_1 and price:
            upside_1 = (target_price_1 - price) / price
        if floor_price and price:
            downside_risk = (floor_price - price) / price
    elif ma200 is not None and atr is not None and price is not None and price > 0:
        target_price_2 = ma200 + 2.0 * atr
        target_price_1 = ma200 + 3.0 * atr
        floor_price = ma200 - 2.0 * atr
        upside_2 = (target_price_2 - price) / price
        upside_1 = (target_price_1 - price) / price
        downside_risk = (floor_price - price) / price

    pbr_f = to_float(pbr)
    pbr_discount = None
    if pbr_f is not None:
        pbr_discount = pbr_f < 1.0

    scores = _five_dim_scores_technical(ma200_bias, rsi, vol_pctl, close_adj, high20)
    average = max(1.0, min(10.0, round(sum(scores.values()) / 5.0, 2)))

    return {
        "valuation_model": "股息殖利率利差模型 + PBR 淨值折溢價",
        "yield_spread": _round_or_none(yield_spread, 4),
        "pbr_discount": pbr_discount,
        "eps_mid": None,
        "eps_current": None,
        "eps_next": None,
        "base_per": None,
        "compound_years": None,
        "profit_growth_rate": None,
        "target_per": None,
        "per_high_avg": None,
        "per_low_avg": None,
        "target_price_1": _round_or_none(target_price_1, 4),
        "upside_1": _round_or_none(upside_1, 4),
        "target_price_2": _round_or_none(target_price_2, 4),
        "upside_2": _round_or_none(upside_2, 4),
        "floor_price": _round_or_none(floor_price, 4),
        "downside_risk": _round_or_none(downside_risk, 4),
        "rule_of_40": None,
        "rule_of_40_met": False,
        "earnings_date": None,
        "check_90d": None,
        "check_180d": None,
        "five_dim_scores": {
            "growth": _round_or_none(scores["growth"], 2),
            "profitability": _round_or_none(scores["profitability"], 2),
            "undervaluation": _round_or_none(scores["undervaluation"], 2),
            "safety": _round_or_none(scores["safety"], 2),
            "explosive": _round_or_none(scores["explosive"], 2),
            "average": average,
            "grade": _grade_for(average),
            "is_a_plus": bool(average >= EXCEL_GRADE_A_PLUS),
        },
    }


def _compute_excel_valuation(symbol, category, price, info, ind, latest,
                             dividend_yield=None, pbr=None, us10y=None):
    """Excel 數學估值：目標價①② / 地板價 / 40%法則 / 森口亮五維度評分（1~10）。

    - Stocks：森口亮基本面數學估值（複利目標 PER + 40% 法則）。
    - REITs：股息殖利率利差模型 + PBR 淨值折溢價（不套用負 EPS × PER）。
    - SGOV（現金停泊池）：無目標價 / 無停損。
    """
    info = info or {}
    price = to_float(price)
    defaults, overrides = _custom_estimates()
    est = dict(defaults)
    est.update(overrides.get(symbol, {}))

    base_per = to_float(est.get("base_per")) or DEFAULT_BASE_PER
    compound_years = int(to_float(est.get("compound_years")) or DEFAULT_COMPOUND_YEARS)

    eps_current = to_float(est.get("eps_current"))
    if eps_current is None:
        eps_current = to_float(info.get("trailingEps"))
    eps_next = to_float(est.get("eps_next"))
    if eps_next is None:
        eps_next = to_float(info.get("forwardEps"))
    if eps_current is not None and eps_next is not None:
        eps_mid = (eps_current + eps_next) / 2.0
    else:
        eps_mid = eps_current if eps_current is not None else eps_next

    rev_growth_rate = to_float(est.get("rev_growth_3p"))
    if rev_growth_rate is None:
        rev_growth_rate = to_float(info.get("revenueGrowth"))
    op_margin = to_float(est.get("op_margin"))
    if op_margin is None:
        op_margin = to_float(info.get("operatingMargins"))
    profit_growth_rate = to_float(info.get("earningsGrowth"))
    if profit_growth_rate is None:
        profit_growth_rate = rev_growth_rate

    earnings_date = _parse_earnings_date(est.get("earnings_date") or info.get("earningsDate"))
    check_90d = earnings_date + datetime.timedelta(days=90) if earnings_date else None
    check_180d = earnings_date + datetime.timedelta(days=180) if earnings_date else None
    rule_of_40 = None
    if op_margin is not None or rev_growth_rate is not None:
        rule_of_40 = ((op_margin or 0.0) + (rev_growth_rate or 0.0)) * 100.0

    close_adj = to_float(latest.get("close"))
    ma200 = to_float(latest.get("ma200"))
    rsi = to_float(latest.get("rsi14"))
    high20 = to_float(latest.get("high20"))
    vol_pctl, _ann_vol = _historical_vol_percentile(ind)
    ma200_bias = None
    if close_adj is not None and ma200 is not None and ma200 > 0:
        ma200_bias = (close_adj - ma200) / ma200

    # 💵 現金停泊資金池：不輸出任何漲跌目標價 / 停損價
    if symbol in CASH_PARKING_SYMBOLS:
        return _cash_parking_valuation(close_adj, ma200, rsi, high20, vol_pctl, ma200_bias)

    # 🏢 REITs：股息殖利率利差模型 + PBR 淨值折溢價（廢除股票複利 PER 目標價）
    if category == "REITs":
        return _reit_yield_spread_valuation(
            symbol, price, dividend_yield, pbr, us10y, ind, latest,
            ma200_bias, close_adj, rsi, high20, vol_pctl,
        )

    # 🧱 個股：EPS <= 0 時禁用負 EPS × PER（避免負目標價），退回技術估值
    if category == "Stocks" and eps_mid is not None and eps_mid > 0 and price is not None and price > 0:
        model = "基本面數學估值（Excel 公式）"
        trailing_pe = to_float(info.get("trailingPE"))
        per_high_avg = to_float(est.get("per_high_avg"))
        if per_high_avg is None:
            per_high_avg = (trailing_pe or base_per) * 1.2
        per_low_avg = to_float(est.get("per_low_avg"))
        if per_low_avg is None:
            per_low_avg = (trailing_pe or base_per) * 0.8
        growth = profit_growth_rate if profit_growth_rate is not None else 0.0
        target_per = _clamp_value(base_per * ((1.0 + growth) ** compound_years), TARGET_PER_MIN, TARGET_PER_MAX)
        target_price_1 = eps_mid * per_high_avg
        target_price_2 = eps_mid * target_per
        floor_price = eps_mid * per_low_avg
        upside_1 = (target_price_1 - price) / price
        upside_2 = (target_price_2 - price) / price
        downside_risk = (floor_price - price) / price
        scores = _five_dim_scores_fundamental(profit_growth_rate, op_margin, upside_1, upside_2, downside_risk)
    else:
        model = "歷史波動率分位數 + 200日均線乖離率映射"
        per_high_avg = per_low_avg = target_per = None
        target_price_1 = target_price_2 = floor_price = None
        upside_1 = upside_2 = downside_risk = None
        if price is not None and price > 0:
            bias = ma200_bias if ma200_bias is not None else 0.0
            pctl = vol_pctl if vol_pctl is not None else 0.5
            vol_factor = 0.5 + pctl
            upside_2 = max(0.05, (0.20 - 0.5 * bias) * vol_factor)
            downside_risk = -max(0.05, (0.20 + 0.5 * bias) * vol_factor)
            target_price_2 = price * (1.0 + upside_2)
            target_price_1 = price * (1.0 + upside_2 * 1.35)
            floor_price = price * (1.0 + downside_risk)
            upside_1 = (target_price_1 - price) / price
        scores = _five_dim_scores_technical(ma200_bias, rsi, vol_pctl, close_adj, high20)

    average = max(1.0, min(10.0, round(sum(scores.values()) / 5.0, 2)))

    return {
        "valuation_model": model,
        "eps_mid": _round_or_none(eps_mid, 4),
        "eps_current": _round_or_none(eps_current, 4),
        "eps_next": _round_or_none(eps_next, 4),
        "base_per": _round_or_none(base_per, 2),
        "compound_years": compound_years,
        "profit_growth_rate": _round_or_none(profit_growth_rate, 4),
        "target_per": _round_or_none(target_per, 2),
        "per_high_avg": _round_or_none(per_high_avg, 2),
        "per_low_avg": _round_or_none(per_low_avg, 2),
        "target_price_1": _round_or_none(target_price_1, 4),
        "upside_1": _round_or_none(upside_1, 4),
        "target_price_2": _round_or_none(target_price_2, 4),
        "upside_2": _round_or_none(upside_2, 4),
        "floor_price": _round_or_none(floor_price, 4),
        "downside_risk": _round_or_none(downside_risk, 4),
        "rule_of_40": _round_or_none(rule_of_40, 2),
        "rule_of_40_met": bool(rule_of_40 is not None and rule_of_40 >= RULE_OF_40_THRESHOLD),
        "earnings_date": earnings_date.isoformat() if earnings_date else None,
        "check_90d": check_90d.isoformat() if check_90d else None,
        "check_180d": check_180d.isoformat() if check_180d else None,
        "five_dim_scores": {
            "growth": _round_or_none(scores["growth"], 2),
            "profitability": _round_or_none(scores["profitability"], 2),
            "undervaluation": _round_or_none(scores["undervaluation"], 2),
            "safety": _round_or_none(scores["safety"], 2),
            "explosive": _round_or_none(scores["explosive"], 2),
            "average": average,
            "grade": _grade_for(average),
            "is_a_plus": bool(average >= EXCEL_GRADE_A_PLUS),
        },
    }


def compute_nomad_micro_signals(ind, price, is_bar_closed=True):
    """遊牧民「交減陰線」暴漲前兆 + 買後三大原則 + MetaLab 假利多高檔陷阱。

    裝甲3：交減陰線需確認日線收盤量縮；當最後一根為盤中未收盤 K 線時，
    改參考前一交易日已確認收盤價，避免盤中假跌破誤觸訊號。
    """
    result = {
        "nomad_signal": None,
        "nomad_tp_1pct": None,
        "nomad_tp_3pct": None,
        "nomad_sl_5pct": None,
        "news_trap_warning": None,
        "vol_drop_prev": None,
        "max_vol_10d": None,
        "max_vol_10d_ratio": None,
    }
    if ind is None or ind.empty:
        return result

    close = ind["close"]
    open_ = ind["open"]
    volume = ind["volume"]
    vol_ma20 = ind["vol_ma20"]
    max_vol_10d = ind["max_vol_10d"]
    ma3 = ind["ma3"]
    ma5 = ind.get("ma5")
    ma8 = ind["ma8"]
    ma20 = ind["ma20"]
    ma120 = ind.get("ma120")
    high20 = ind.get("high20")

    vol_shrink_2d = (volume < volume.shift(1)) & (volume.shift(1) < volume.shift(2))
    cond_vol = (volume <= NOMAD_WASHOUT_VOL_RATIO * max_vol_10d) | (
        vol_shrink_2d & (volume < NOMAD_WASHOUT_VOL_MA_RATIO * vol_ma20)
    )
    bearish = close < open_

    band_3_8 = pd.concat([ma3, ma8], axis=1)
    in_ma3_8 = (close >= band_3_8.min(axis=1)) & (close <= band_3_8.max(axis=1))
    in_ma5_20 = pd.Series(False, index=ind.index)
    near_ma120 = pd.Series(False, index=ind.index)
    if ma5 is not None:
        band_5_20 = pd.concat([ma5, ma20], axis=1)
        in_ma5_20 = (close >= band_5_20.min(axis=1)) & (close <= band_5_20.max(axis=1))
    if ma120 is not None:
        near_ma120 = (ma120 > 0) & ((close - ma120).abs() / ma120 <= NOMAD_SUPPORT_TOLERANCE)
    in_support = in_ma3_8 | in_ma5_20 | near_ma120
    not_at_high = pd.Series(True, index=ind.index)
    if high20 is not None:
        not_at_high = close < high20

    washout = cond_vol & bearish & in_support & not_at_high
    bar_idx = -1 if is_bar_closed else -2
    today_washout = bool(washout.iloc[bar_idx]) if len(washout) > abs(bar_idx) else False
    yesterday_washout = bool(washout.iloc[bar_idx - 1]) if len(washout) > abs(bar_idx - 1) else False
    today_bullish = bool(close.iloc[bar_idx] > open_.iloc[bar_idx]) if len(ind) > abs(bar_idx) else False

    if today_washout:
        result["nomad_signal"] = "🔥 交減陰線 (量縮黑K買點)"
    elif yesterday_washout and today_bullish:
        result["nomad_signal"] = "🚀 交減陰線後轉強噴發"

    price = to_float(price)
    if price is not None:
        result["nomad_tp_1pct"] = round(price * (1.0 + NOMAD_TP_1PCT), 2)
        result["nomad_tp_3pct"] = round(price * (1.0 + NOMAD_TP_3PCT), 2)
        result["nomad_sl_5pct"] = round(price * (1.0 - NOMAD_SL_5PCT), 2)

    if "vol_drop_prev" in ind:
        result["vol_drop_prev"] = bool(ind["vol_drop_prev"].iloc[bar_idx]) if len(ind) > abs(bar_idx) else None
    mv = to_float(max_vol_10d.iloc[bar_idx]) if max_vol_10d is not None and len(ind) > abs(bar_idx) else None
    vm = to_float(vol_ma20.iloc[bar_idx]) if vol_ma20 is not None and len(ind) > abs(bar_idx) else None
    result["max_vol_10d"] = _round_or_none(mv, 2)
    result["max_vol_10d_ratio"] = _round_or_none(mv / vm if (mv is not None and vm) else None, 2)

    low_60d = ind.get("low_60d")
    rsi = ind.get("rsi14")
    if low_60d is not None and rsi is not None:
        low60 = to_float(low_60d.iloc[bar_idx])
        r = to_float(rsi.iloc[bar_idx])
        c = to_float(close.iloc[bar_idx])
        if low60 and c and r is not None and (c - low60) / low60 > NOMAD_NEWS_TRAP_GAIN and r > NOMAD_NEWS_TRAP_RSI:
            result["news_trap_warning"] = "⚠️ 高檔利多鈍化警戒 (勿當分母)"

    return result


def compute_fujimoto_micro_signals(latest, info, est, record):
    """藤本茂「6大選股重點」：PBR + 配息 + 日K/週K共振等，滿分 6 項。"""
    dividend_yield = to_float(record.get("dividend_yield"))
    pbr = to_float(record.get("price_to_book"))
    rsi = to_float(record.get("rsi14"))
    vol_ratio = to_float(record.get("vol_ratio"))
    price = to_float(record.get("price"))
    last_volume = to_float(latest.get("volume"))
    close = to_float(latest.get("close"))
    ma20 = to_float(latest.get("ma20"))

    rev_growth_rate = to_float(est.get("rev_growth_3p"))
    if rev_growth_rate is None:
        rev_growth_rate = to_float(info.get("revenueGrowth"))
    profit_growth_rate = to_float(info.get("earningsGrowth"))
    if profit_growth_rate is None:
        profit_growth_rate = rev_growth_rate
    forward_pe = to_float(info.get("forwardPE"))

    weekly_close = to_float(latest.get("weekly_close"))
    weekly_ma4 = to_float(latest.get("weekly_ma4"))
    weekly_rsi = to_float(latest.get("weekly_rsi"))
    weekly_bullish = bool(
        weekly_close is not None and weekly_ma4 is not None and weekly_rsi is not None
        and weekly_close > weekly_ma4 and weekly_rsi > 50.0
    )
    daily_bullish = bool(close is not None and ma20 is not None and close > ma20)

    daily_turnover_val = price * last_volume if (price is not None and last_volume is not None) else None

    c1 = (rev_growth_rate is not None and rev_growth_rate > 0) and (profit_growth_rate is not None and profit_growth_rate > 0)
    c2 = dividend_yield is not None and dividend_yield > 0
    c3 = (forward_pe is not None and forward_pe > 0 and forward_pe <= FUJIMOTO_CHEAP_FORWARD_PE) or (pbr is not None and pbr > 0 and pbr <= FUJIMOTO_CHEAP_PBR)
    c4 = weekly_bullish and daily_bullish
    c5 = rsi is not None and (rsi <= FUJIMOTO_RSI_OVERSOLD or (FUJIMOTO_RSI_OVERSOLD < rsi < FUJIMOTO_RSI_OVERBOUGHT))
    c6 = (vol_ratio is not None and vol_ratio >= 1.0) or (daily_turnover_val is not None and daily_turnover_val >= ENDO_MIN_DAILY_TURNOVER)

    checks = [
        ("營收獲利雙增", c1),
        ("配息", c2),
        ("估值便宜", c3),
        ("日週K共振", c4),
        ("RSI買點", c5),
        ("量能熱絡", c6),
    ]
    passed = sum(1 for _, ok in checks if ok)

    return {
        "weekly_bullish": weekly_bullish,
        "pbr": _round_or_none(pbr, 4),
        "fujimoto_6_checks": passed,
        "fujimoto_checks_detail": [{"label": lbl, "passed": bool(ok)} for lbl, ok in checks],
        "daily_turnover_val": _round_or_none(daily_turnover_val, 2),
    }


def _endo_small_cap_ok(symbol, market_cap):
    """遠藤洋「小型暴漲股」市值門檻：日股 <= 300 億日圓，其餘 <= 100 億美元（中小型成長股）。"""
    if market_cap is None or market_cap <= 0:
        return None
    if symbol and str(symbol).endswith(".T"):
        return bool(market_cap <= ENDO_SMALL_CAP_JPY)
    return bool(market_cap <= ENDO_SMALL_CAP_USD)


def compute_endo_micro_signals(latest, ind, record, info):
    """遠藤洋「穩賺三倍股 / Tenbagger」入場鐵律檢核（含小型股市值門檻 + 上升模式）。"""
    close = to_float(latest.get("close"))
    ma25 = to_float(latest.get("ma25"))
    price = to_float(record.get("price"))
    last_volume = to_float(latest.get("volume"))
    symbol = str(record.get("symbol", ""))
    info = info or {}

    daily_turnover_val = price * last_volume if (price is not None and last_volume is not None) else None
    insider_ownership = to_float(info.get("heldPercentInsiders"))
    market_cap = to_float(info.get("marketCap"))
    upside_2 = to_float(record.get("upside_2"))

    ma25_slope = None
    if "ma25" in ind and len(ind) > 1:
        cur = to_float(ind["ma25"].iloc[-1])
        prev = to_float(ind["ma25"].iloc[-2])
        if cur is not None and prev is not None:
            ma25_slope = cur - prev

    # 上升模式：Close >= MA25 且 MA25 上揚（遠藤洋入場鐵律，否則太早投資 NG）
    uptrend = bool(
        close is not None and ma25 is not None and close >= ma25
        and ma25_slope is not None and ma25_slope > 0
    )
    small_cap_ok = _endo_small_cap_ok(symbol, market_cap)
    turnover_ok = bool(daily_turnover_val is not None and daily_turnover_val >= ENDO_MIN_DAILY_TURNOVER)
    insider_ok = bool(insider_ownership is not None and insider_ownership > 0)
    upside_ok = upside_2 is not None and upside_2 >= 1.0

    entry_rule = None
    if close is None or ma25 is None:
        entry_rule = None
    elif not uptrend:
        entry_rule = "⏳ 太早投資NG！等上漲趨勢確立"
    elif small_cap_ok and upside_ok:
        entry_rule = "✅ 上升模式確立 (小型股翻倍潛力)"
    elif not small_cap_ok and upside_ok:
        entry_rule = "🟢 上升模式確立（非小型股，續抱觀察）"
    else:
        entry_rule = "🟢 上升模式確立（續抱觀察）"

    return {
        "endo_entry_rule": entry_rule,
        "endo_uptrend": uptrend,
        "endo_small_cap_ok": small_cap_ok,
        "endo_turnover_ok": turnover_ok,
        "endo_insider_ok": insider_ok,
        "ma25": _round_or_none(ma25, 4),
        "ma25_slope": _round_or_none(ma25_slope, 4),
        "daily_turnover_val": _round_or_none(daily_turnover_val, 2),
        "insider_ownership": _round_or_none(insider_ownership, 4),
        "market_cap": _round_or_none(market_cap, 2),
    }


# ===== 六大資產專屬量化模型與專屬指標區塊（domain_metrics） =====

def _dm(label, value, kind="text"):
    """建構 domain_metrics 的單一指標項目。"""
    return {"label": label, "value": value, "kind": kind}


def _ma_bias_pct(close_adj, ma200):
    """MA200 乖離率（%）。"""
    if close_adj is None or ma200 is None or ma200 == 0:
        return None
    return round((close_adj - ma200) / ma200 * 100.0, 2)


def _set_domain_metric(record, label, value):
    """依 label 更新 record["domain_metrics"] 中對應指標的 value。"""
    for section in (record.get("domain_metrics") or []):
        for item in section.get("items", []):
            if item.get("label") == label:
                item["value"] = value


def compute_domain_model(category, symbol, record, ind, latest, macro_overview=None):
    """依六大資產類別產生專屬 asset_model_type 與 domain_metrics 區塊。"""
    macro = macro_overview or {}
    close_adj = to_float(latest.get("close"))
    ma200 = to_float(record.get("ma200"))
    ma200_bias = _ma_bias_pct(close_adj, ma200)

    if category == "Stocks":
        return _domain_stocks(record)
    if category == "Crypto":
        return _domain_crypto(record, macro)
    if category == "REITs":
        return _domain_reits(record, macro, ma200_bias)
    if category == "Bonds":
        if symbol in CASH_PARKING_SYMBOLS:
            return _domain_cash_pool(record)
        return _domain_bonds(record, macro, ma200_bias)
    if category == "ETFs":
        return _domain_etfs(record, macro, ma200_bias)
    if category == "Gold":
        return _domain_gold(record, macro, close_adj, ma200)
    return {"asset_model_type": "generic", "domain_metrics": []}


def _domain_stocks(record):
    fd = record.get("five_dim_scores") or {}
    return {
        "asset_model_type": "stock_fundamental_tech",
        "domain_metrics": [
            {
                "title": "森口亮 Excel 估值",
                "items": [
                    _dm("目標價①", record.get("target_price_1"), "price"),
                    _dm("目標價②", record.get("target_price_2"), "price"),
                    _dm("預測低點", record.get("floor_price"), "price"),
                    _dm("複利目標 PER", record.get("target_per"), "num"),
                    _dm("40% 法則", record.get("rule_of_40"), "pct"),
                    _dm("五維度評分", fd.get("average"), "score"),
                    _dm("評等", fd.get("grade"), "text"),
                ],
            },
            {
                "title": "遠藤洋上升模式與小型股檢核",
                "items": [
                    _dm("上升模式", record.get("endo_uptrend"), "bool"),
                    _dm("小型股檢核", record.get("endo_small_cap_ok"), "bool"),
                    _dm("入場鐵律", record.get("endo_entry_rule"), "text"),
                ],
            },
            {
                "title": "茂爺爺 6 大重點",
                "items": [
                    _dm("PBR", record.get("price_to_book"), "num"),
                    _dm("配息率", record.get("dividend_yield"), "yield"),
                    _dm("週 K 共振", record.get("weekly_bullish"), "bool"),
                    _dm("6 大重點通過", record.get("fujimoto_6_checks"), "score6"),
                ],
            },
            {
                "title": "遊牧民交減陰線與量比",
                "items": [
                    _dm("交減陰線訊號", record.get("nomad_signal"), "signal"),
                    _dm("量能倍數", record.get("vol_ratio"), "num"),
                    _dm("前日量縮", record.get("vol_drop_prev"), "bool"),
                ],
            },
        ],
    }


def _domain_crypto(record, macro):
    fng = macro.get("crypto_fear_greed")
    fng_class = macro.get("crypto_fear_greed_classification")
    resonance = None
    if fng is not None and record.get("nomad_signal"):
        if fng <= 25:
            resonance = "🟢 恐慌 + 交減陰線 → 低吸契機"
        elif fng >= 75:
            resonance = "🔴 貪婪 + 短打 → 嚴守停利"
        else:
            resonance = "⚪ 中性 → 區間短打"
    elif fng is not None:
        resonance = fng_class or str(fng)

    close = to_float(record.get("price"))
    ma200 = to_float(record.get("ma200"))
    long_short = None
    if close is not None and ma200 is not None:
        long_short = close >= ma200

    return {
        "asset_model_type": "crypto_momentum_nomad",
        "domain_metrics": [
            {
                "title": "遊牧民短打雷達",
                "items": [
                    _dm("MA3", record.get("ma3"), "price"),
                    _dm("MA8", record.get("ma8"), "price"),
                    _dm("MA15", record.get("ma15"), "price"),
                    _dm("MA120", record.get("ma120"), "price"),
                    _dm("量能倍數", record.get("vol_ratio"), "num"),
                    _dm("前日量縮", record.get("vol_drop_prev"), "bool"),
                    _dm("交減陰線訊號", record.get("nomad_signal"), "signal"),
                ],
            },
            {
                "title": "短打價位（遊牧民）",
                "items": [
                    _dm("+1% 停利", record.get("nomad_tp_1pct"), "price"),
                    _dm("+3% 停利", record.get("nomad_tp_3pct"), "price"),
                    _dm("-5% 鐵律停損", record.get("nomad_sl_5pct"), "price"),
                ],
            },
            {
                "title": "幣圈恐慌貪婪指數共振",
                "items": [
                    _dm("恐慌貪婪指數", fng, "num"),
                    _dm("分類", fng_class, "text"),
                    _dm("共振訊號", resonance, "signal"),
                ],
            },
            {
                "title": "cis 突破與 ATR 移動停利",
                "items": [
                    _dm("20 日高點突破", record.get("near_20d_high_breakout"), "bool"),
                    _dm("HIGH20", record.get("high20"), "price"),
                    _dm("ATR 移動停利", record.get("trailing_stop_price"), "price"),
                    _dm("移動停利狀態", record.get("trailing_stop_status"), "text"),
                    _dm("上方壓力反轉價", record.get("pressure_reversal_price"), "price"),
                    _dm("MA200 長線多空", long_short, "bool"),
                ],
            },
        ],
    }


def _domain_reits(record, macro, ma200_bias):
    us10y = macro.get("us10y_yield")
    dy = to_float(record.get("dividend_yield"))
    spread = to_float(record.get("yield_spread"))
    pbr = to_float(record.get("price_to_book"))
    pbr_state = None
    if pbr is not None:
        pbr_state = "折價安全" if pbr < 1.0 else "溢價偏高"
    return {
        "asset_model_type": "reit_yield_nav",
        "domain_metrics": [
            {
                "title": "股息殖利率利差模型",
                "items": [
                    _dm("標的殖利率", dy, "yield"),
                    _dm("10Y 美債殖利率", us10y, "pct"),
                    _dm("利差 (Yield Spread)", spread, "yield"),
                ],
            },
            {
                "title": "茂爺爺 PBR 淨值折溢價",
                "items": [
                    _dm("PBR", pbr, "num"),
                    _dm("折溢價狀態", pbr_state, "text"),
                ],
            },
            {
                "title": "合理目標價與收息地板價",
                "items": [
                    _dm("合理目標價", record.get("target_price_2"), "price"),
                    _dm("收息地板價", record.get("floor_price"), "price"),
                    _dm("MA200 乖離率", ma200_bias, "pct"),
                ],
            },
        ],
    }


def _domain_cash_pool(record):
    return {
        "asset_model_type": "cash_parking_pool",
        "domain_metrics": [
            {
                "title": "現金停泊資金池",
                "items": [
                    _dm("年化配息殖利率", record.get("dividend_yield"), "yield"),
                    _dm("全市場建議現金停泊比例", record.get("suggested_weight_pct"), "pct"),
                    _dm("銀彈狀態", "隨時可轉入風險資產", "text"),
                ],
            },
        ],
    }


def _domain_bonds(record, macro, ma200_bias):
    us10y = macro.get("us10y_yield")
    rate_state = None
    if us10y is not None:
        if us10y < 4.0:
            rate_state = "寬鬆"
        elif us10y <= 5.0:
            rate_state = "中性"
        else:
            rate_state = "高壓"
    rsi = to_float(record.get("rsi14"))
    oversold = rsi is not None and rsi < 30.0
    return {
        "asset_model_type": "bond_macro_duration",
        "domain_metrics": [
            {
                "title": "10Y 美債殖利率宏觀壓力",
                "items": [
                    _dm("10Y 殖利率", us10y, "pct"),
                    _dm("殖利率狀態", rate_state, "text"),
                ],
            },
            {
                "title": "MA200 均值回歸乖離率",
                "items": [
                    _dm("MA200 年線", record.get("ma200"), "price"),
                    _dm("乖離率", ma200_bias, "pct"),
                ],
            },
            {
                "title": "茂爺爺 RSI 超賣買點與一目均衡",
                "items": [
                    _dm("RSI", rsi, "num"),
                    _dm("一目均衡", record.get("ichimoku_cloud"), "text"),
                    _dm("超賣買點", oversold, "bool"),
                ],
            },
            {
                "title": "波段支撐與壓力",
                "items": [
                    _dm("支撐", record.get("boll_lower"), "price"),
                    _dm("壓力", record.get("boll_upper"), "price"),
                ],
            },
        ],
    }


def _domain_etfs(record, macro, ma200_bias):
    vix = macro.get("vix")
    vix_zone = macro.get("vix_zone")
    position = None
    if vix is not None:
        if vix < 15:
            position = "1:2:6 主升滿倉 (9/9)"
        elif vix <= 25:
            position = "1:2:6 順勢加碼 (3/9)"
        else:
            position = "1:2:6 觀望 / 偵察試單 (1/9)"
    bt = record.get("backtest_strategy_1y") or {}
    mdd = bt.get("max_drawdown_pct")
    mdd_pct = _round_or_none(mdd * 100.0, 2) if mdd is not None else None
    return {
        "asset_model_type": "etf_index_allocation",
        "domain_metrics": [
            {
                "title": "MA200 年線乖離率",
                "items": [
                    _dm("MA200 年線", record.get("ma200"), "price"),
                    _dm("乖離率", ma200_bias, "pct"),
                ],
            },
            {
                "title": "VIX 恐慌指數 → 藤本茂 1:2:6 倉位階梯",
                "items": [
                    _dm("VIX", vix, "num"),
                    _dm("VIX 區間", vix_zone, "text"),
                    _dm("倉位階梯", position, "text"),
                ],
            },
            {
                "title": "1 年回測績效",
                "items": [
                    _dm("年化夏普", bt.get("sharpe_ratio"), "num"),
                    _dm("MDD", mdd_pct, "pct"),
                    _dm("獲利因子", bt.get("profit_factor"), "num"),
                ],
            },
            {
                "title": "波段通道高低點",
                "items": [
                    _dm("通道高點", record.get("boll_upper"), "price"),
                    _dm("通道低點", record.get("boll_lower"), "price"),
                ],
            },
        ],
    }


def _domain_gold(record, macro, close_adj, ma200):
    dxy = macro.get("dxy")
    us10y = macro.get("us10y_yield")
    dxy_state = None
    if dxy is not None:
        dxy_state = "強勢美元（金價承壓）" if dxy >= 100.0 else "弱勢美元（金價受惠）"
    rate_state = None
    if us10y is not None:
        rate_state = "高實質利率（金價承壓）" if us10y >= 4.0 else "低利率環境（金價受惠）"
    long_short = None
    if close_adj is not None and ma200 is not None:
        long_short = close_adj >= ma200
    return {
        "asset_model_type": "gold_macro_turtle",
        "domain_metrics": [
            {
                "title": "宏觀避險雙因子",
                "items": [
                    _dm("美元指數 DXY", dxy, "num"),
                    _dm("DXY 狀態", dxy_state, "text"),
                    _dm("10Y 美債殖利率", us10y, "pct"),
                    _dm("殖利率狀態", rate_state, "text"),
                ],
            },
            {
                "title": "cis 海龜突破通道",
                "items": [
                    _dm("HIGH20 突破價", record.get("high20"), "price"),
                    _dm("20 日高點突破", record.get("near_20d_high_breakout"), "bool"),
                    _dm("ATR 移動停利", record.get("trailing_stop_price"), "price"),
                    _dm("MA200 長線多空", long_short, "bool"),
                ],
            },
        ],
    }


def fetch_and_analyze(category, symbol, name, macro_overview=None):
    """抓取單一標的歷史與基本面，計算指標並回傳最新一筆記錄。"""
    record = {
        "symbol": symbol,
        "name": name,
        "category": category,
        "data_source": None,
        "data_source_status": None,
        "last_bar_date": None,
        "is_bar_closed": True,
        "bar_close_status": None,
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
        "trailing_stop_status": None,
        "pressure_reversal_price": None,
        "current_defense_sl": None,
        "breakout_entry_price": None,
        "breakout_sl": None,
        "market_regime": None,
        "ofi_5d": None,
        "ofi_direction": None,
        "vwap_20d_bias": None,
        "corr_60d": None,
        "corr_penalty": None,
        "sparkline_60d": [],
        "backtest_equity_60d": [],
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
        "ma5": None,
        "ma120": None,
        "ma25": None,
        "vol_drop_prev": None,
        "max_vol_10d": None,
        "max_vol_10d_ratio": None,
        "nomad_signal": None,
        "nomad_tp_1pct": None,
        "nomad_tp_3pct": None,
        "nomad_sl_5pct": None,
        "news_trap_warning": None,
        "weekly_bullish": None,
        "pbr": None,
        "fujimoto_6_checks": None,
        "fujimoto_checks_detail": None,
        "endo_entry_rule": None,
        "ma25_slope": None,
        "daily_turnover_val": None,
        "insider_ownership": None,
        "market_cap": None,
        "expected_slippage_pct": None,
        "stress_slippage_pct": None,
        "effective_stop_distance_pct": None,
        "amplitude_over_2atr": False,
        "stop_loss_triggered": False,
        "position_plan": None,
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
        record["data_source_status"] = (
            DATA_SOURCE_LIVE if data_source in (SOURCE_BINANCE, SOURCE_YAHOO)
            else DATA_SOURCE_FALLBACK if data_source == SOURCE_CACHE
            else DATA_SOURCE_FAILED
        )
        if df_raw is not None and not df_raw.empty:
            record["last_bar_date"] = pd.Timestamp(df_raw.index[-1]).strftime("%Y-%m-%d")

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
        info = {}
        est = {}
        if data_source == SOURCE_YAHOO:
            info = fetch_fundamentals(symbol)
            _defaults, _overrides = _custom_estimates()
            est = dict(_defaults)
            est.update(_overrides.get(symbol, {}))
            # 殖利率優先取 trailingAnnualDividendYield（固定小數），再退回 dividendYield 並做合理性判斷
            raw_yield = est.get("dividend_yield")
            if raw_yield is None:
                raw_yield = info.get("trailingAnnualDividendYield")
            if raw_yield is None:
                raw_yield = info.get("dividendYield")
            record["dividend_yield"] = normalize_yield(raw_yield)
            record["trailing_pe"] = to_float(info.get("trailingPE"))
            record["price_to_book"] = to_float(
                est.get("pbr") if est.get("pbr") is not None else info.get("priceToBook")
            )
            record["market_cap"] = to_float(info.get("marketCap"))
        else:
            record["dividend_yield"] = None
            record["trailing_pe"] = None
            record["price_to_book"] = None

        # 裝甲2：統一採用「未除權息還原之真實交易價 (auto_adjust=False)」計算技術指標與止損定位，
        # 使 MA20/MA200/current_defense_sl 與券商盤面 Raw Close 處於同一價格基準。
        ind = compute_indicators(df_raw)
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

        # 裝甲3：未收盤 K 線時間守衛（美股冬夏令 / 台股 / 港股 / 加密貨幣 24/7）
        is_bar_closed, bar_close_label = bar_close_status(symbol)
        record["is_bar_closed"] = is_bar_closed
        record["bar_close_status"] = bar_close_label
        # 盤中未收盤時，破線停損改參考前一交易日已確認收盤價，防止盤中假跌破誤觸停損
        bar_idx = -1 if is_bar_closed else -2
        confirmed_close_adj = to_float(ind["close"].iloc[bar_idx]) if len(ind) > abs(bar_idx) else None
        confirmed_close_raw = to_float(raw_close.iloc[bar_idx]) if len(raw_close) > abs(bar_idx) else None

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
        # fast_info 報價交叉驗證（僅 Yahoo 來源）：與還原收盤價乖離 > 2% 時改用即時報價
        record["fast_quote_price"] = None
        record["quote_cross_verified"] = False
        if data_source == SOURCE_YAHOO:
            apply_fast_quote_verification(symbol, record, raw_last_close)
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

        # ===== Excel 數學估值：目標價①② / 地板價 / 40%法則 / 森口亮五維度評分 =====
        record.update(_compute_excel_valuation(
            symbol, category, record["price"], info, ind, latest,
            dividend_yield=record.get("dividend_yield"),
            pbr=record.get("price_to_book"),
            us10y=(macro_overview or {}).get("us10y_yield"),
        ))

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
        last_ma5 = to_float(latest.get("ma5"))
        last_ma8 = to_float(latest.get("ma8"))
        last_ma15 = to_float(latest.get("ma15"))
        last_ma25 = to_float(latest.get("ma25"))
        last_ma33 = to_float(latest.get("ma33"))
        last_ma45 = to_float(latest.get("ma45"))
        last_ma120 = to_float(latest.get("ma120"))
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
        record["ma5"] = round(last_ma5, 4) if last_ma5 is not None else None
        record["ma8"] = round(last_ma8, 4) if last_ma8 is not None else None
        record["ma15"] = round(last_ma15, 4) if last_ma15 is not None else None
        record["ma25"] = round(last_ma25, 4) if last_ma25 is not None else None
        record["ma33"] = round(last_ma33, 4) if last_ma33 is not None else None
        record["ma45"] = round(last_ma45, 4) if last_ma45 is not None else None
        record["ma120"] = round(last_ma120, 4) if last_ma120 is not None else None
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
        # 裝甲3：破線停損改參考已確認收盤價，防止盤中假跌破誤觸停損
        decision_close = (
            confirmed_close_adj if (confirmed_close_adj is not None and not is_bar_closed) else last_close
        )
        record["position_stage"] = compute_position_stage(
            decision_close, last_ma3, last_ma8, last_ma15, last_ma33, last_ma45,
            last_macd, last_macd_hist, last_rsi, last_vol_ratio, cloud_pos,
            last_tenkan, last_kijun, trailing_stop, near_20d_high_breakout,
        )
        record["master_score"] = compute_master_score(
            last_close, last_ma3, last_ma8, last_ma15, last_ma45, cloud_pos,
            last_rsi, last_macd, last_macd_hist, last_vol_ratio,
            record.get("backtest_strategy_1y"),
        )

        # ===== 訊號一致性閘門：低分/空頭時禁止「強烈買進」，校正主訊號與倉位 =====
        apply_signal_gatekeeper(record, last_ma20, last_ma60, last_rsi)

        # ===== 市場狀態三態分類器與微觀訂單流 / VWAP 乖離 =====
        adx14 = to_float(latest.get("adx14"))
        bb_width_series = ind["bb_width"].dropna()
        bb_width_pctl = None
        if not bb_width_series.empty:
            last_bbw = to_float(bb_width_series.iloc[-1])
            if last_bbw is not None:
                bb_width_pctl = float((bb_width_series <= last_bbw).mean())
        vol_pctl, _ann_vol = _historical_vol_percentile(ind)
        atr_pct = (last_atr / last_close) if (last_atr is not None and last_close) else None
        record["market_regime"] = classify_market_regime(adx14, bb_width_pctl, vol_pctl, atr_pct)

        ofi_5d_val = to_float(latest.get("ofi_5d"))
        vol_5d_val = to_float(latest.get("vol_5d_sum"))
        record["ofi_5d"] = round(ofi_5d_val, 2) if ofi_5d_val is not None else None
        record["ofi_direction"] = classify_ofi_direction(ofi_5d_val, vol_5d_val)

        vwap_20 = to_float(latest.get("vwap_20"))
        vwap_bias = None
        if last_close is not None and vwap_20 is not None and vwap_20 > 0:
            vwap_bias = (last_close - vwap_20) / vwap_20 * 100.0
        record["vwap_20d_bias"] = round(vwap_bias, 2) if vwap_bias is not None else None

        # ===== 三大微觀實戰訊號：遊牧民交減陰線 × 藤本茂6大重點 × 遠藤洋Tenbagger =====
        record.update(compute_nomad_micro_signals(ind, record["price"], is_bar_closed=is_bar_closed))
        record.update(compute_fujimoto_micro_signals(latest, info, est, record))
        record.update(compute_endo_micro_signals(latest, ind, record, info))

        # ===== 六大資產專屬量化模型與專屬指標區塊（asset_model_type + domain_metrics）=====
        domain = compute_domain_model(category, symbol, record, ind, latest, macro_overview)
        record["asset_model_type"] = domain["asset_model_type"]
        record["domain_metrics"] = domain["domain_metrics"]

        # 💵 現金停泊資金池：禁止顯示漲跌目標價與 -5% 停損
        if symbol in CASH_PARKING_SYMBOLS:
            for _key in ("nomad_tp_1pct", "nomad_tp_3pct", "nomad_sl_5pct"):
                record[_key] = None

        # 除權息還原校正：以當日未還原現價比例，將還原價位換算回真實看盤報價
        raw_atr = last_atr
        if last_close and raw_last_close:
            price_ratio = raw_last_close / last_close
            for key in ("ideal_buy_price", "stop_loss_price", "take_profit_price", "trailing_stop_price"):
                if record.get(key) is not None:
                    record[key] = round(record[key] * price_ratio, 4)
            raw_atr = last_atr * price_ratio if last_atr is not None else None

        # ===== 防守止損 / 突破防線 / 移動停利狀態（基準價不再錯位） =====
        close_price = to_float(record.get("price"))
        if symbol in CASH_PARKING_SYMBOLS:
            record["current_defense_sl"] = None
            record["breakout_entry_price"] = None
            record["breakout_sl"] = None
        else:
            record["current_defense_sl"] = compute_current_defense_sl(close_price, raw_atr, category)
            ideal_buy = to_float(record.get("ideal_buy_price"))
            if ideal_buy is not None and close_price is not None and ideal_buy > close_price:
                record["breakout_entry_price"] = ideal_buy
                record["breakout_sl"] = record.get("stop_loss_price")
            else:
                record["breakout_entry_price"] = None
                record["breakout_sl"] = None

        ts = to_float(record.get("trailing_stop_price"))
        # 裝甲3：移動停利破線判定同樣參考已確認收盤價，避免盤中假跌破
        break_close = (
            confirmed_close_raw if (confirmed_close_raw is not None and not is_bar_closed) else close_price
        )
        if break_close is not None and ts is not None:
            if break_close < ts:
                record["trailing_stop_status"] = "未觸發 (現價低於多頭防線)"
                record["pressure_reversal_price"] = ts
            else:
                record["trailing_stop_status"] = "已站上多頭防線（移動停利生效）"
                record["pressure_reversal_price"] = ts
        else:
            record["trailing_stop_status"] = None
            record["pressure_reversal_price"] = None

        # ===== 裝甲4：極端踩踏動態滑點緩衝與實盤安全倉位 =====
        raw_high = to_float(df_raw["High"].astype(float).dropna().iloc[-1])
        raw_low = to_float(df_raw["Low"].astype(float).dropna().iloc[-1])
        record["amplitude_over_2atr"] = bool(
            raw_high is not None and raw_low is not None
            and raw_atr is not None and raw_atr > 0
            and (raw_high - raw_low) > 2.0 * raw_atr
        )
        defense_sl = to_float(record.get("current_defense_sl"))
        record["stop_loss_triggered"] = bool(
            close_price is not None and defense_sl is not None and close_price < defense_sl
        )
        record["expected_slippage_pct"] = EXPECTED_SLIPPAGE_PCT.get(category, 0.15)
        record["stress_slippage_pct"] = compute_stress_slippage_pct(
            category, symbol, record, macro_overview
        )
        record["effective_stop_distance_pct"] = compute_effective_stop_distance_pct(record)
        record["position_plan"] = compute_safe_position(record)

        # 60 日收盤價陣列（SVG 迷你走勢圖）與回測淨值小曲線
        spark = ind["close"].dropna().tail(60)
        record["sparkline_60d"] = [round(float(v), 4) for v in spark]
        record["backtest_equity_60d"] = (record.get("backtest_strategy_1y") or {}).get("equity_curve_60d")
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


def _load_returns_series(symbol):
    """讀取本地歷史 CSV 的每日收盤報酬率序列（供跨資產相關性計算）。"""
    path = os.path.join(HISTORY_DIR, f"{symbol}.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        close = pd.to_numeric(df["Close"], errors="coerce").dropna()
        if len(close) < 30:
            return None
        return close.pct_change().dropna()
    except Exception:
        return None


def compute_corr_penalty(symbol, category, returns_map):
    """計算標的與同類別（等權基準）近 60 日滾動相關係數。"""
    ser = returns_map.get(symbol)
    if ser is None:
        return None
    peers = [s for s in ASSET_UNIVERSE.get(category, {}) if s != symbol and s in returns_map]
    if not peers:
        return None
    frames = [returns_map[s].rename(s) for s in peers]
    try:
        bench = pd.concat(frames, axis=1, join="inner").mean(axis=1).dropna()
    except Exception:
        return None
    if bench.empty:
        return None
    common = ser.index.intersection(bench.index)
    if len(common) < 30:
        return None
    a = ser.loc[common].tail(60)
    b = bench.loc[common].tail(60)
    if len(a) < 30:
        return None
    return to_float(a.corr(b))


def corr_penalty_value(corr, macro_overview):
    """高相關同向波動且市場風險偏高時，回傳倉位折減比例（0~0.2）。"""
    if corr is None:
        return None
    macro = macro_overview or {}
    vix = to_float(macro.get("vix"))
    fng = to_float(macro.get("crypto_fear_greed"))
    risk_high = (vix is not None and vix > 25.0) or (fng is not None and fng >= 75.0)
    if not risk_high:
        return 0.0
    if corr > 0.7:
        return 0.20
    if corr > 0.6:
        return 0.10
    return 0.0


def apply_risk_adjustments(records, macro_overview):
    """依市場狀態（高波恐慌盤折半）與跨資產相關性（corr_penalty）微調建議倉位。"""
    returns_map = {}
    for r in records:
        sym = r.get("symbol")
        ser = _load_returns_series(sym)
        if ser is not None:
            returns_map[sym] = ser

    for r in records:
        sym = r.get("symbol")
        corr = compute_corr_penalty(sym, r.get("category"), returns_map)
        penalty = corr_penalty_value(corr, macro_overview)
        r["corr_60d"] = round(corr, 4) if corr is not None else None
        r["corr_penalty"] = penalty

        regime = r.get("market_regime")
        regime_factor = 0.5 if regime == REGIME_PANIC else 1.0
        r["regime_weight_factor"] = regime_factor

        base = to_float(r.get("suggested_weight_pct"))
        if base is not None:
            factor = regime_factor * (1.0 - (penalty or 0.0))
            r["suggested_weight_pct"] = round(base * factor, 2)


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


def append_signal_ledger(records):
    """將最新訊號追加寫入 data/signal_ledger.csv（以 date + symbol 去重）。

    落實茂爺爺第 5 大投資術「記錄、檢討每天的交易」與森口亮 Excel
    「四季報検証用」的歷史追蹤：每次執行產出最新訊號時，把當日各標的
    關鍵訊號存成一列，供 _evaluate_historical_ledger 計算後續驗證績效。
    """
    if not records:
        return
    os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
    today = datetime.date.today().isoformat()

    existing = set()
    if os.path.exists(LEDGER_FILE):
        try:
            prev = pd.read_csv(LEDGER_FILE, dtype=str)
            for _, row in prev.iterrows():
                existing.add((str(row.get("date", "")).strip(), str(row.get("symbol", "")).strip()))
        except Exception:
            existing = set()

    new_rows = []
    for rec in records:
        symbol = str(rec.get("symbol", ""))
        if (today, symbol) in existing:
            continue
        five_dim = rec.get("five_dim_scores") or {}
        five_dim_avg = to_float(five_dim.get("average"))
        new_rows.append({
            "date": today,
            "symbol": symbol,
            "close_price": _round_or_none(rec.get("price"), 4),
            "master_score": _round_or_none(rec.get("master_score"), 2),
            "five_dim_avg": _round_or_none(five_dim_avg, 2),
            "position_stage": rec.get("position_stage"),
            "target_price_2": _round_or_none(rec.get("target_price_2"), 4),
            "floor_price": _round_or_none(rec.get("floor_price"), 4),
            "nomad_signal": rec.get("nomad_signal"),
        })
        existing.add((today, symbol))

    if not new_rows:
        print(f"    signal_ledger 今日（{today}）已記錄，無新增")
        return

    header = not os.path.exists(LEDGER_FILE) or os.path.getsize(LEDGER_FILE) == 0
    pd.DataFrame(new_rows, columns=LEDGER_COLUMNS).to_csv(
        LEDGER_FILE, mode="a", header=header, index=False, encoding="utf-8"
    )
    print(f"    signal_ledger 追加 {len(new_rows)} 筆（{today}）")


def _evaluate_historical_ledger():
    """讀取 data/signal_ledger.csv 與 data/history/*.csv，計算歷史訊號驗證摘要。

    每個歷史訊號發出後迄今：
      - MFE%（期間最高價漲幅） = 訊號日後最高價 / 進場價 - 1
      - MAE%（期間最低價回撤） = 訊號日後最低價 / 進場價 - 1
      - 目前累積報酬率（%）   = 最新收盤 / 進場價 - 1
      - 另提供 30 / 90 / 180 天前向驗證報酬。
    """
    summary = {
        "total_signals": 0,
        "evaluated": 0,
        "overall": {
            "avg_mfe_pct": None,
            "avg_mae_pct": None,
            "avg_current_return_pct": None,
            "win_rate_pct": None,
        },
        "by_symbol": {},
        "rows": [],
    }
    if not os.path.exists(LEDGER_FILE):
        return summary

    try:
        ledger = pd.read_csv(LEDGER_FILE, dtype={"symbol": str})
    except Exception:
        return summary
    if ledger.empty:
        return summary

    ledger["date"] = pd.to_datetime(ledger["date"], errors="coerce")
    ledger["close_price"] = pd.to_numeric(ledger["close_price"], errors="coerce")

    history_cache = {}

    def _load_history(symbol):
        if symbol in history_cache:
            return history_cache[symbol]
        df = None
        path = os.path.join(HISTORY_DIR, f"{symbol}.csv")
        if os.path.exists(path):
            try:
                df = pd.read_csv(path, index_col=0, parse_dates=True)
                cols = [c for c in ("High", "Low", "Close") if c in df.columns]
                if cols:
                    df = df[cols].astype(float).dropna()
            except Exception:
                df = None
        history_cache[symbol] = df
        return df

    rows = []
    for _, row in ledger.iterrows():
        symbol = str(row.get("symbol", ""))
        sig_date = row.get("date")
        if pd.isna(sig_date):
            continue
        df = _load_history(symbol)
        if df is None or df.empty:
            continue

        # 日期對齊：針對非週末交易的股票/REITs/債券/ETF/黃金，自動對齊該標的
        # CSV 中「最近一個有效交易日」收盤價，避免週末訊號日找不到交易日而漏算。
        prior = df.index[df.index <= sig_date]
        if prior.empty:
            continue
        ref_date = prior[-1]
        entry = to_float(df.loc[ref_date, "Close"])
        if entry is None or entry <= 0:
            continue

        # 績效自「訊號日（對齊後）之後」開始計算
        future = df[df.index > ref_date]
        if future.empty:
            # 尚無前向資料（訊號為最新交易日）：以 0% 計入，確保 16 檔全數納入帳本
            mfe = 0.0
            mae = 0.0
            cur = 0.0
            fwd30 = fwd90 = fwd180 = None
        else:
            mfe = to_float((future["High"].max() / entry - 1.0) * 100.0)
            mae = to_float((future["Low"].min() / entry - 1.0) * 100.0)
            cur = to_float((future["Close"].iloc[-1] / entry - 1.0) * 100.0)

            def _fwd_return(days):
                target = ref_date + pd.Timedelta(days=days)
                sub = df[df.index >= target]
                if sub.empty:
                    return None
                return to_float((sub["Close"].iloc[0] / entry - 1.0) * 100.0)

            fwd30 = _fwd_return(30)
            fwd90 = _fwd_return(90)
            fwd180 = _fwd_return(180)

        rows.append({
            "date": sig_date.strftime("%Y-%m-%d"),
            "symbol": symbol,
            "mfe_pct": _round_or_none(mfe, 2),
            "mae_pct": _round_or_none(mae, 2),
            "current_return_pct": _round_or_none(cur, 2),
            "return_30d_pct": _round_or_none(fwd30, 2),
            "return_90d_pct": _round_or_none(fwd90, 2),
            "return_180d_pct": _round_or_none(fwd180, 2),
        })

    summary["total_signals"] = len(ledger)
    summary["evaluated"] = len(rows)

    per_symbol = {}
    for r in rows:
        bucket = per_symbol.setdefault(r["symbol"], {
            "signals": 0, "mfe_pct": [], "mae_pct": [], "current_return_pct": [],
        })
        bucket["signals"] += 1
        if r["mfe_pct"] is not None:
            bucket["mfe_pct"].append(r["mfe_pct"])
        if r["mae_pct"] is not None:
            bucket["mae_pct"].append(r["mae_pct"])
        if r["current_return_pct"] is not None:
            bucket["current_return_pct"].append(r["current_return_pct"])

    by_symbol = {}
    for sym, bucket in per_symbol.items():
        wins = sum(1 for v in bucket["current_return_pct"] if v > 0)
        by_symbol[sym] = {
            "signals": bucket["signals"],
            "avg_mfe_pct": _round_or_none(sum(bucket["mfe_pct"]) / len(bucket["mfe_pct"]) if bucket["mfe_pct"] else None, 2),
            "avg_mae_pct": _round_or_none(sum(bucket["mae_pct"]) / len(bucket["mae_pct"]) if bucket["mae_pct"] else None, 2),
            "avg_current_return_pct": _round_or_none(sum(bucket["current_return_pct"]) / len(bucket["current_return_pct"]) if bucket["current_return_pct"] else None, 2),
            "win_rate_pct": _round_or_none(wins / len(bucket["current_return_pct"]) * 100.0 if bucket["current_return_pct"] else None, 2),
        }
    summary["by_symbol"] = by_symbol

    all_mfe = [r["mfe_pct"] for r in rows if r["mfe_pct"] is not None]
    all_mae = [r["mae_pct"] for r in rows if r["mae_pct"] is not None]
    all_cur = [r["current_return_pct"] for r in rows if r["current_return_pct"] is not None]
    wins_all = sum(1 for v in all_cur if v > 0)
    summary["overall"] = {
        "avg_mfe_pct": _round_or_none(sum(all_mfe) / len(all_mfe) if all_mfe else None, 2),
        "avg_mae_pct": _round_or_none(sum(all_mae) / len(all_mae) if all_mae else None, 2),
        "avg_current_return_pct": _round_or_none(sum(all_cur) / len(all_cur) if all_cur else None, 2),
        "win_rate_pct": _round_or_none(wins_all / len(all_cur) * 100.0 if all_cur else None, 2),
    }

    # 僅保留最近 200 筆明細，避免 JSON 過度膨脹
    summary["rows"] = rows[-200:]
    return summary


def main():
    # 強制 UTF-8 輸出，避免 Windows cp950 主控台無法編碼 emoji
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ensure_dirs()
    print("== 量化引擎啟動 ==")

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

    assets = {}
    all_records = []
    for category, symbols in ASSET_UNIVERSE.items():
        print(f"\n[{category}]")
        assets[category] = []
        for symbol, name in symbols.items():
            print(f"  - {symbol} ({name})")
            record = fetch_and_analyze(category, symbol, name, macro_overview)
            assets[category].append(record)
            all_records.append(record)
            time.sleep(POLITE_SLEEP_SECONDS)  # 【硬傷1】禮貌延遲，避免觸發 Yahoo 頻率限制

    summary = build_summary(all_records)

    # 建議資金配置權重（逆波動率 Risk-Parity + 宏觀利率調節，16 檔總和 100%）
    weights = compute_suggested_weights(all_records, macro_overview)
    for r in all_records:
        r["suggested_weight_pct"] = weights.get(r["symbol"])

    # 市場狀態（高波恐慌盤折半）與跨資產相關性（corr_penalty）風險折減
    apply_risk_adjustments(all_records, macro_overview)

    for r in all_records:
        if r.get("asset_model_type") == "cash_parking_pool":
            _set_domain_metric(r, "全市場建議現金停泊比例", r["suggested_weight_pct"])

    # ===== 歷史訊號與 30/90/180 天驗證帳本：追加當日訊號並計算 MFE / MAE / 累積報酬 =====
    append_signal_ledger(all_records)
    ledger_summary = _evaluate_historical_ledger()
    print(
        f"    歷史訊號帳本：共 {ledger_summary.get('total_signals', 0)} 筆，"
        f"已驗證 {ledger_summary.get('evaluated', 0)} 筆"
    )

    # ===== 裝甲1：後廚斷供報警（system_health）=====
    live_count = sum(1 for r in all_records if r.get("data_source") in (SOURCE_BINANCE, SOURCE_YAHOO))
    fallback_count = sum(1 for r in all_records if r.get("data_source") == SOURCE_CACHE)
    failed_symbols = [
        r["symbol"] for r in all_records
        if r.get("data_source") is None or r.get("data_source_status") == DATA_SOURCE_FAILED
    ]
    system_health = {
        "live_count": live_count,
        "fallback_count": fallback_count,
        "failed_symbols": failed_symbols,
        "degraded": fallback_count > 0,
    }

    payload = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summary,
        "macro_overview": macro_overview,
        "ledger_summary": ledger_summary,
        "system_health": system_health,
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


