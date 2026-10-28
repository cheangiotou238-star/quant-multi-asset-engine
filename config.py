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
