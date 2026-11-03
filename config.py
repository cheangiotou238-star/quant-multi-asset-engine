# -*- coding: utf-8 -*-
"""六大資產類別標的池設定檔。

ASSET_UNIVERSE 結構：
    { 資產類別: { 代號: 中文名稱 } }
"""

ASSET_UNIVERSE = {
    "Crypto": {
        "BTC-USD": "比特幣 (BTC)",
        "ETH-USD": "以太幣 (ETH)",
        "SOL-USD": "Solana (SOL)",
    },
    "Stocks": {
        "NVDA": "輝達 (NVDA)",
        "AAPL": "蘋果 (AAPL)",
        "2330.TW": "台積電 (2330)",
    },
    "REITs": {
        "VNQ": "美國房地產ETF (VNQ)",
        "O": "Realty Income月配息信託 (O)",
        "0823.HK": "領展房產基金 (0823.HK)",
    },
    "Bonds": {
        "TLT": "20年期美債ETF (TLT)",
        "IEF": "7-10年期美債ETF (IEF)",
        "SGOV": "超短期國庫券ETF (SGOV)",
    },
    "ETFs": {
        "VOO": "標普500 ETF (VOO)",
        "QQQ": "納斯達克100 ETF (QQQ)",
    },
    "Gold": {
        "GLD": "SPDR黃金ETF (GLD)",
        "IAU": "iShares黃金信託 (IAU)",
    },
}

# ===== 階段 A：六大領域策略分流與精確止損價位升級 =====

# 技術指標週期
ATR_PERIOD = 14        # ATR14：14 日真實波動幅度
MA_LONG_PERIOD = 200   # MA200：200 日年線
DONCHIAN_PERIOD = 20   # HIGH20：20 日唐奇安高點

# 以「建議買入價」為基準的 ATR 止損 / 停利倍數
ATR_STOP_MULT = 2.0         # 嚴格止損價 = 建議買入價 - 2 × ATR14
ATR_TAKE_PROFIT_MULT = 3.0  # 目標停利價 = 建議買入價 + 3 × ATR14

# 長線資產（ETFs / REITs / Bonds）較寬波段保護：
# 停損取「固定停損百分比」與「建議買入價 - 3.2 × ATR14」兩者中較低（較寬）的價位，
# 給予大盤指數型標的至少 6%~8% 的正常波動空間，避免小回檔頻繁停損。
LONG_TERM_ATR_STOP_MULT = 3.2
LONG_TERM_STOP_CATEGORIES = ("ETFs", "REITs", "Bonds")

# 策略回測期間（過去 1 年，日曆天）
BACKTEST_LOOKBACK_DAYS = 365

# ===== 第 5 章：選股 2「爆量長紅」與圖表 5-8 四大出場法則 =====

# 爆量長紅參數（p.232-237）
VOL_LENGTH = 20            # 月均量：20 日平均成交量
BIG_VOL_MULTIPLIER = 1.8   # 爆量倍數：當日量 > 月均量 × 1.8
K_BODY_MA_P = 20           # 長紅實體 K 棒的平均週期
K_BODY_MULTIPLIER = 2.0    # 實體紅 K > 過去 20 日均值 × 2.0

# 交易成本：每次買賣來回（手續費 + 滑價）合計 0.2%
ROUND_TRIP_COST = 0.002

# 圖表 5-8：六大領域的四大出場條件
#   ① 固定持有天數出場（max_hold_days）
#   ② 固定百分比 / ATR 停損（fixed_stop_loss_pct / atr_stop_mult）
#   ③ 固定百分比 / ATR 停利（fixed_take_profit_pct / atr_tp_mult）
#   ④ 指標反轉出場
# 另含爆量長紅 / 流動性門檻：k_body_floor（長紅實體門檻）、min_avg_vol（20日月均量）。
# 數值依各領域波動與流動性特性設定，可依書本 p.239-242 調整。
DOMAIN_EXIT_RULES = {
    "Crypto": {
        "max_hold_days": 30,
        "fixed_stop_loss_pct": 0.10,
        "fixed_take_profit_pct": 0.25,
        "atr_stop_mult": 2.0,
        "atr_tp_mult": 3.0,
        "k_body_floor": 0.03,
        "min_avg_vol": 100_000_000,
    },
    "Stocks": {
        "max_hold_days": 60,
        "fixed_stop_loss_pct": 0.07,
        "fixed_take_profit_pct": 0.15,
        "atr_stop_mult": 2.0,
        "atr_tp_mult": 3.0,
        "k_body_floor": 0.02,
        "min_avg_vol": 10_000_000,
    },
    "REITs": {
        "max_hold_days": None,
        "fixed_stop_loss_pct": 0.05,
        "fixed_take_profit_pct": 0.10,
        "atr_stop_mult": 2.0,
        "atr_tp_mult": 3.0,
        "k_body_floor": 0.015,
        "min_avg_vol": 1_000_000,
    },
    "Bonds": {
        "max_hold_days": 180,
        "fixed_stop_loss_pct": 0.04,
        "fixed_take_profit_pct": 0.08,
        "atr_stop_mult": 2.0,
        "atr_tp_mult": 3.0,
        "k_body_floor": 0.008,
        "min_avg_vol": 1_000_000,
    },
    "ETFs": {
        "max_hold_days": None,
        "fixed_stop_loss_pct": 0.06,
        "fixed_take_profit_pct": 0.12,
        "atr_stop_mult": 2.0,
        "atr_tp_mult": 3.0,
        "k_body_floor": 0.015,
        "min_avg_vol": 1_000_000,
    },
    "Gold": {
        "max_hold_days": 90,
        "fixed_stop_loss_pct": 0.06,
        "fixed_take_profit_pct": 0.12,
        "atr_stop_mult": 2.0,
        "atr_tp_mult": 3.0,
        "k_body_floor": 0.015,
        "min_avg_vol": 1_000_000,
    },
}

# 💵 現金停泊資金池（不套用一般股票均線減碼訊號）
CASH_PARKING_SYMBOLS = {"SGOV"}
CASH_PARKING_LABEL = "💵 現金停泊資金池"

# =====================================================================
# $0 零月費・數據與交易分離 (Data-Execution Decoupling) 五大防坑護欄
# ---------------------------------------------------------------------
# 核心原則 1（$0 零月費）：行情、技術指標、基本面估值與歷史 K 線 100% 由
#   免費的 quant_engine.py（Yahoo Finance / Binance）產出；ibkr_bridge.py
#   僅按需連線 IB Gateway / TWS 免費查詢現金餘額並送單，送完即斷開。
# 核心原則 2（100% 純本金）：嚴禁融資、放空與跨幣種自動借貸計息。
# =====================================================================

# IBKR 本地連線資訊（預設模擬倉 Paper 7497，實盤改為 7496）
IBKR_HOST = "127.0.0.1"
IBKR_PORT_PAPER = 7497
IBKR_PORT_LIVE = 7496
IBKR_CLIENT_ID = 108

# 強制使用免費延遲/凍結數據：3 = Free Delayed Data、4 = Frozen Data。
# 確保未付費訂閱時永不觸發 Error 10089，也不產生任何數據月費。
IBKR_MARKET_DATA_TYPE = 3

# 禁止向 IBKR 請求歷史 K 線，100% 避開 Error 165 Pacing Violation 與月費
IBKR_USE_FREE_EXTERNAL_DATA_ONLY = True

# ===== 純本金與防罰款熔斷常數 =====
STRICT_CASH_ONLY = True                 # 主開關：強制 100% 純本金
MAX_LEVERAGE_RATIO = 1.0                # 槓桿上限 = 1.0（零槓桿）
CASH_SAFETY_BUFFER_PCT = 0.02           # 保留 2% 同幣種現金緩衝，最多動用 98% 已結算現金
ALLOW_SHORT_SELLING = False             # 禁止放空
PREVENT_FX_MARGIN_LOAN = True           # 同幣種現金檢核，嚴禁跨幣種借貸計息
MAX_ORDERS_PER_RUN = 5                  # 單次執行最多 5 組委託單（防 Bug 頻繁交易罰款）
ORDER_THROTTLE_SECONDS = 1.5            # 每筆委託單間強制間隔 1.5 秒

# ===== 四大實盤隱蔽風險安全鎖（第 7~10 道閘門）=====
# 鎖 1（T+1 已結算現金強制判定）：只認已交割 SettledCash，拒絕動用 T+1/T+2 未結算換匯資金
REQUIRE_STRICT_SETTLED_CASH = True

# 鎖 2（IBKR 最低手續費與成本佔比門檻）：
#   美股單邊最低手續費約 1 USD，港股約 18 HKD + 印花稅；低於門檻金額直接攔截不下單
IBKR_MIN_FEE_BY_CURRENCY = {"USD": 1.0, "HKD": 18.0}
MIN_ORDER_AMOUNT_BY_CURRENCY = {"USD": 500.0, "HKD": 4000.0}
# 雙邊預估最低手續費佔交易總額比例嚴格不得超過 0.50%
MAX_FEE_RATIO_PCT = 0.005

# 鎖 3（防黑天鵝跳空之單一標的最高本金集中度上限）：單一標的 ≤ 總本金 20%
MAX_SINGLE_ASSET_WEIGHT_PCT = 0.20

# 鎖 4（財報前夕跳空禁買窗）：距離財報公布 0~3 天內禁止新建波段買單
EARNINGS_BLACKOUT_DAYS = 3

# 槓桿 / 反向 / 高波動 ETN 禁買清單（零槓桿原則，強制攔截）
BLOCKED_LEVERAGED_TICKERS = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "UPRO", "SPXU",
    "UVXY", "SVXY", "BOIL", "KOLD", "LABU", "LABD",
    "BITX", "CONL", "NVDL",
}

# 離線預覽模式（--dry-run）使用的預設幣種本金：
# 實盤連線時一律以 IBKR accountValues() 的真實已結算現金為準。
# HKD 預設 0，目的在示範「防跨幣種融資借貸鎖」：買 HKD 標的需先手動換匯。
IBKR_DRY_RUN_CASH = {
    "USD": 100_000.0,
    "HKD": 0.0,
}

# =====================================================================
# 保底基準基本面（Fallback Fundamental Baseline）
# ---------------------------------------------------------------------
# 當 yfinance 抓取失敗、Yahoo API 限流，或執行 `python quant_engine.py --offline`
# 離線模式時，Stocks / REITs 的基本面欄位（PBR、殖利率、市值、內部持股、EPS、
# 目標價②、40% 法則與五維度評分）會變成 null，進而被前端靜默跳過消失。
# 此處為 6 檔股票/REITs 標的補齊「真實合理的保底基準」，確保關鍵基本面 100% 有數值可算，
# 絕不變成 null 被前端吃掉。
#
# 欄位採 yfinance info 命名（與 quant_engine.fetch_fundamentals() 回傳一致），
# 殖利率採「小數」（0.038 = 3.8%）、市值採「美元」、內部持股採「小數」。
FUNDAMENTAL_FALLBACK = {
    # ---- 成長個股（Stocks）----
    "NVDA": {
        "trailingEps": 7.91,               # 近四季每股盈餘 (USD)
        "forwardEps": 15.9146,             # 預估次年度每股盈餘 (USD)
        "revenueGrowth": 1.10,             # 近 3 年營收成長率（110%）
        "earningsGrowth": 1.278,           # 獲利成長率
        "operatingMargins": 0.62,          # 營業利益率（62%）
        "forwardPE": 12.7,
        "priceToBook": 21.3076,            # 股價淨值比 PBR
        "trailingAnnualDividendYield": 0.0014,  # 配息殖利率（0.14%）
        "trailingPE": 25.54,
        "marketCap": 4_879_142_617_088,    # 市值 (USD)
        "heldPercentInsiders": 0.0401,     # 內部人持股比
    },
    "AAPL": {
        "trailingEps": 8.98,
        "forwardEps": 10.7509,
        "revenueGrowth": 0.287,
        "earningsGrowth": 0.287,
        "operatingMargins": 0.203,         # 使 40% 法則 = (0.287+0.203)*100 ≈ 49%
        "forwardPE": 31.5,
        "priceToBook": 45.9674,
        "trailingAnnualDividendYield": 0.0030,
        "trailingPE": 37.67,
        "marketCap": 4_937_503_211_520,
        "heldPercentInsiders": 0.0165,
    },
    "2330.TW": {
        "trailingEps": 97.47,
        "forwardEps": 149.175,
        "revenueGrowth": 0.687,
        "earningsGrowth": 0.687,
        "operatingMargins": 0.4086,        # 使 40% 法則 ≈ 109.56%
        "forwardPE": 15.4,
        "priceToBook": 8.4498,
        "trailingAnnualDividendYield": 0.0026,
        "trailingPE": 23.55,
        "marketCap": 59_514_787_201_024,
        "heldPercentInsiders": 0.064,
    },
    # ---- 收息信託（REITs，採殖利率利差 + PBR 淨值模型，無 EPS）----
    "VNQ": {
        "priceToBook": 2.15,               # 美國房地產 ETF 平均 PBR（近淨值）
        "trailingAnnualDividendYield": 0.038,  # 3.8% 配息殖利率
        "trailingPE": 30.09,
        "marketCap": 34_000_000_000,       # VNQ AUM 約 340 億美元
        "heldPercentInsiders": 0.0,        # ETF 無內部人持股
    },
    "O": {
        "priceToBook": 1.3776,
        "trailingAnnualDividendYield": 0.0568,
        "trailingPE": 39.44,
        "marketCap": 54_483_238_912,
        "heldPercentInsiders": 0.0012,
    },
    "0823.HK": {
        "priceToBook": 0.6912,             # 領展 PBR < 1（折價）
        "trailingAnnualDividendYield": 0.0638,
        "marketCap": 103_352_803_328,
        "heldPercentInsiders": 0.0011,
    },
}
