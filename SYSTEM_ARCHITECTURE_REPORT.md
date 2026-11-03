# 多資產量化交易系統 · 系統架構盤點報告
### SYSTEM ARCHITECTURE REPORT

> 生成日期：2026-11-03
> 最高原則：**100% 純本金現貨・零槓桿（Max Leverage = 1.0x）・零數據月費（數據與交易分離）**

---

## 1. 後端檔案與各模組職責清單

| 檔案 | 行數 | 職責 |
|---|---|---|
| `config.py` | 199 | 六大資產標的池（16 檔）、ATR/MA/唐奇安參數、六大領域出場法則、現金停泊池、IBKR 連線與 10 道安全閘門常數、禁槓桿清單、離線預覽本金 |
| `quant_engine.py` | 4358 | 核心量化引擎：抓取行情、計算技術指標、六大名家評分、CTA 六策投票、六大資產專屬模型、1 年回測、歷史驗證帳本、產出 `latest_signals.json` |
| `ibkr_bridge.py` | 902 | IBKR 100% 純本金安全下單橋接器：10 道防坑閘門、原子性 GTC 括號單、`--dry-run`/`--check-account`/`--execute` 三模式 |
| `binance_spot_bridge.py` | 338 | Binance 純現貨橋接器：鎖死 `/api/v3` 現貨端點、伺服器時間同步（offset_ms）、LOT_SIZE/stepSize/tickSize 無條件捨去精度校準 |
| `notifier.py` | 393 | Discord Webhook 訊號推播：讀取 `latest_signals.json` 排版六大類別 Embed 報告 |
| `.github/workflows/daily_quant.yml` | 55 | GitHub Actions 排程（亞洲盤後 16:35 / 美股盤後 21:35 UTC），自動執行引擎與推播並提交數據 |
| `.htaccess` | — | Apache 安全規則：禁止目錄瀏覽、阻擋 `.git`/`.env`/`.py`/`.md`/`.log`、僅放行 HTML/CSS/JS/JSON/圖片 |
| `requirements.txt` | 5 | `yfinance`、`pandas`、`numpy`、`requests`、`python-dotenv` |

### 模組職責細節

**quant_engine.py 核心函式**
- `fetch_macro_overview()` — 免金鑰抓取 10Y 美債殖利率、DXY、VIX、幣圈恐慌貪婪指數。
- `load_market_data()` — 依類別選資料源：Crypto→Binance、其餘→yfinance，失敗自動 fallback 本地 CSV。
- `compute_indicators()` — MA3/8/15/20/25/33/45/60/120/200、RSI14、ATR14、唐奇安、布林、MACD、一目均衡、量比。
- `compute_master_score()` — 森口亮「每日 5 分鐘綜合評分」0~100（趨勢 35 + 動能 25 + 回測風控 40）。
- `_five_dim_scores_fundamental()/_technical()` — 森口亮五維度評分（成長/獲利/低估/安全/暴漲）。
- `compute_cta_ensemble()` — 劉承彥 CTA 六策投票（突破/均線/布林/RSI/ATR/乖離）。
- `compute_fujimoto_micro_signals()` — 藤本茂 6 大選股重點。
- `compute_nomad_micro_signals()` — 遊牧民交減陰線 + 買後三大原則。
- `compute_endo_micro_signals()` — 遠藤洋上升模式/小型翻倍股檢核。
- `compute_domain_model()` — 六大資產專屬模型（asset_model_type + domain_metrics）。
- `compute_suggested_weights()` — 逆波動率 Risk-Parity 建議權重。
- `apply_risk_adjustments()` — 高波恐慌盤折半 + 跨資產相關性折減。
- `enforce_defensive_zero_weights()` — 防禦訊號強制歸零並移轉至 SGOV。
- `append_signal_ledger()` / `_evaluate_historical_ledger()` — 歷史訊號帳本與 MFE/MAE/30/90/180 天驗證。
- `build_summary()` — 訊號分布 / 類別分布統計。

**ibkr_bridge.py 十道閘門**（見第 6 節詳列）
`validate_zero_cost_cash_order()` 依序執行閘門 1~10，`place_bracket_order()` 以母單+停利 transmit=False、止損 transmit=True 原子性打包。

**binance_spot_bridge.py**
`assert_spot_only()` 硬性攔截 `/fapi/`、`/dapi/`、`/sapi/v1/margin/`；`round_step_size()/round_tick_size()` 以 `Decimal` `ROUND_DOWN` 無條件捨去。

---

## 2. 前端架構、分頁邏輯與視覺組件清單

### 2.1 檔案與設計語言
- `index.html` 與 `public/index.html` 內容**完全一致**（單一來源，`fc /b` 驗證）。
- 色彩：象牙白 `#FAF8F5` + 香檳金 `#C5A059` + 深炭灰 `#1E293B`。
- 資料載入採多路徑 fallback：`./public/data/*` → `./data/*` → `../public/data/*`（根目錄與 public 目錄皆可正確讀取）。

### 2.2 頂部全域控制台（Global Header & Control Bar）
| 組件 | 說明 |
|---|---|
| 系統標題 / 最後更新時間 | `#subtitle` 顯示 `latest_signals.json.generated_at` |
| 系統健康度徽章 | `#healthRow`：即時連線數（live_count）vs 降級快取數（fallback_count） |
| 宏觀四維溫度計 | `#macroGrid`：10Y 美債殖利率、DXY、VIX（含區間）、幣圈恐慌貪婪（含分類） |
| ⚡ 今日異動焦點快訊 | `#alertBanner`：交減陰線🔥、主力級 A+、🚨跌破止損防線 + 數據源降級提示 |
| 實盤資金試算器 | 總本金、單筆容忍虧損 %、歷史已實現損益、🌱單利 / 🚀複利切換、雙安全狀態列 |

### 2.3 第一層：9 大主分頁（Primary Navigation Tabs）
`renderMainTabs()` 依 `tabDefs()` 產生：

| 鍵 | 分頁 | 內容 |
|---|---|---|
| 1 | 🏠 戰情總覽 | SGOV 現金池停泊比例、今日焦點 Top 3、森口亮可排序綜合評分總表 |
| 2 | 🪙 加密貨幣 | BTC-USD / ETH-USD / SOL-USD |
| 3 | 📈 成長個股 | NVDA / AAPL / 2330.TW |
| 4 | 🏢 收息信託 | VNQ / O / 0823.HK |
| 5 | 📜 美債與現金 | TLT / IEF / SGOV |
| 6 | 🧺 指數 ETF | VOO / QQQ |
| 7 | 🥇 黃金避險 | GLD / IAU |
| 8 | 📒 歷史驗證帳本 | 16 檔歷史訊號、MFE/MAE、30/90/180 天前向報酬、勝率 |
| 9 | 📖 實戰操作指南 | 6 大實戰教學模組（見任務 3） |

### 2.4 第二層：子分頁膠囊（Sub-Tabs）
`renderSubTabs()` 於資產類別分頁內產生 `[☷ 同類並排總覽]` + 各標的獨立膠囊按鈕。
- 並排摘要卡（`compareCardHtml()`）正面**嚴格 8 指標**：現價與漲跌、主訊號、市場狀態、五師評分、CTA 六策投票、現價防守止損、目標停利、含滑點安全倉位。
- 深潛視圖（`deepCardHtml()`）：SVG 水平價格攻防比例尺、60 日走勢 + 回測淨值、CTA 6 子策略燈號、資產專屬 domain_metrics、IBKR / Binance 委託預覽卡。

### 2.5 視覺組件清單
| 函式 | 用途 |
|---|---|
| `sparklineSVG()` | 60 日收盤價折線 SVG |
| `equitySVG()` | 回測淨值曲線（正規化） |
| `gaugeSVG()` | 水平價格攻防比例尺（預測低點/防守止損/現價/突破買入/目標停利/R/R 比） |
| `ctaLightsHtml()` | CTA 六策燈號矩陣 |
| `domainSectionsHtml()` | 專屬 domain_metrics（嚴格過濾 null） |
| `ibkrOrderCardHtml()` | IBKR GTC 括號單預覽（含閘門 1/8/9 攔截） |
| `binanceSpotCardHtml()` | Binance 純現貨精度校正預覽 |
| `overviewHtml()` | 戰情總覽（現金池 + Top3 + 可排序總表） |
| `ledgerTableHtml()` | 歷史驗證帳本雙層表格 |

### 2.6 鍵盤快捷鍵
- `1`~`9`：切換主分頁
- `←` `→`：切換同類子分頁標的

---

## 3. 交易手法、六大資產專屬模型（asset_model_type）與計算公式

### 3.1 六大名家交易手法
| 名家 | 觸發邏輯 | 對應欄位/函式 |
|---|---|---|
| cis 順勢 | 順勢突破、ATR 吊燈停利、永不攤平 | `trailing_stop_price`、`near_20d_high_breakout` |
| 藤本茂 | 1:2:6 加減碼階梯、RSI+MACD+一目均衡、6 大價值配息重點 | `fujimoto_6_checks`、`compute_fujimoto_micro_signals()` |
| 遊牧民 | MA3/8/15/33/45/120、量縮<25% 交減陰線、+1%~3% 短打 / -5% 鐵律停損 | `nomad_signal`、`compute_nomad_micro_signals()` |
| 遠藤洋 | 上升模式確立才進場、小型翻倍股檢核 | `endo_entry_rule`、`compute_endo_micro_signals()` |
| 森口亮 | Excel 複利目標本益比、目標價②、預測低點、40% 法則、五維度 A+ | `compute_master_score()`、`_five_dim_scores_*()` |
| 劉承彥 | CTA 六策投票、4H+1D 共振、Taker 主動買盤、單利/複利控管 | `compute_cta_ensemble()`、`taker_buy_ratio_5d` |

### 3.2 六大資產專屬模型（asset_model_type）
| asset_model_type | 類別 | 專屬指標區塊（domain_metrics title） |
|---|---|---|
| `crypto_momentum_nomad` | Crypto | 遊牧民短打雷達、短打價位、幣圈恐慌貪婪共振、CTA 六策投票矩陣 |
| `stock_fundamental_tech` | Stocks | 森口亮 Excel 估值、遠藤洋上升模式、茂爺爺 6 大重點、遊牧民交減陰線 |
| `reit_yield_nav` | REITs | 股息殖利率利差模型、每股淨資產 NAV、茂爺爺 6 大重點、RSI 超賣買點 |
| `bond_macro_duration` | Bonds | 10Y 美債殖利率宏觀壓力、MA200 均值回歸乖離率、茂爺爺 RSI、波段支撐壓力 |
| `cash_parking_pool` | Bonds(SGOV) | 現金停泊資金池（全市場建議現金停泊比例） |
| `etf_index_allocation` | ETFs | MA200 年線乖離率、VIX→藤本茂 1:2:6 倉位階梯、1 年回測績效、波段通道 |
| `gold_macro_turtle` | Gold | 宏觀避險雙因子、cis 海龜突破通道 |
| `generic` | 其他 | 空（fallback） |

### 3.3 核心計算公式
- **森口亮綜合評分** = 趨勢位階 35 分（MA3/8/15/45 + 雲帶）+ 動能量能 25 分（RSI+MACD+量比）+ 回測風控 40 分（Sharpe/PF/MDD/OOS）。
- **五維度評分**：成長性、獲利性、低估性、安全性、暴漲性（基本面版採 EPS/營收/毛利/本益比；無財報標的採波動率分位數 + MA200 乖離映射），平均 ≥ 9 且符合條件標記 `is_a_plus`。
- **CTA 六策**：突破（唐奇安 20 高/10 低）、均線（MA8 vs MA25）、布林（中軌+開口）、RSI（35 穿越/50~70 強勢）、ATR 通道（MA20±1.2×ATR14）、乖離率（BIAS_20）；多空票數 → `cta_vote_score`。
- **嚴格止損價** = 建議買入價 − 2×ATR14；長線資產（ETFs/REITs/Bonds）取「固定 % 停損」與「−3.2×ATR14」較寬者。
- **目標停利價** = 建議買入價 + 3×ATR14。
- **建議倉位** = 逆波動率 Risk-Parity 權重 → 高波恐慌盤折半 → 相關性折減 → 防禦訊號歸零移轉 SGOV。
- **含滑點安全倉位** = 單筆容忍虧損 ÷（有效止損距離% + 極端滑點%）。
- **40% 法則** = 營收成長率 + 營業利益率 ≥ 40%。
- **MFE/MAE**：期間最高漲幅 / 期間最大回撤，用於 30/90/180 天前向驗證。

### 3.4 訊號定義
| 訊號 | 說明 |
|---|---|
| 🟢 強烈買進 / 低檔加碼 | 突破/動能明確轉多 |
| 🔵 趨勢多頭 / 續抱 | 多頭趨勢延續 |
| 🟡 價值收息買點 | REITs/Bonds 殖利率 ≥ 4% |
| 🔥 爆量長紅突破 | 量 > 月均量×1.8 且紅 K 實體倍增 |
| 🟡 左側超賣試單 | 超賣但非主升段（嚴控 1/9 倉） |
| ⚪ 區間觀望 / 等待突破 | 中性 |
| 🔴 減碼 / 停利警戒 | 空頭/過熱 |
| 💵 現金停泊資金池 | SGOV 專屬 |

---

## 4. API 與資料來源、重試與降級機制

### 4.1 資料來源
| 來源 | 用途 | 標的 |
|---|---|---|
| Yahoo Finance (`yfinance`) | 歷史日 K、基本面（殖利率/本益比/PBR/市值） | 非 Crypto 標的 |
| Binance Official API (`/api/v3/klines`) | Crypto 日 K（優先） | BTC/ETH/SOL |
| Binance `/api/v3/time`、`/exchangeInfo` | 時間同步、精度規則 | Crypto |
| Binance `/fapi/v1` 公開資金費率、`/api/v3/aggTrades` | 資金費率情緒、Taker 主動買盤 | Crypto |
| 免金鑰公開 API | 10Y 美債殖利率、DXY、VIX、恐慌貪婪指數 | 宏觀總覽 |
| IB Gateway / TWS（`ib_insync`） | 帳戶現金餘額、持倉、送單（唯連線時） | 交易執行 |

### 4.2 重試與降級機制（裝甲護欄）
1. **指數退避 + jitter 重試**：yfinance/Binance 抓取失敗時以 `YFINANCE_RETRY_MAX` 指數退避重試。
2. **本地 CSV 快取降級**：連線失敗自動 fallback `./data/history/{symbol}.csv`（`SOURCE_CACHE`），系統絕不當機。
3. **後廚斷供報警（system_health）**：`live_count`（即時連線）vs `fallback_count`（降級快取）vs `failed_symbols`。
4. **單一標的隔離**：單檔抓取失敗不影響其他標的。
5. **禮貌延遲**：每標的間隔 `POLITE_SLEEP_SECONDS`，避免觸發頻率限制。
6. **快報價交叉驗證**：`fast_info` 即時報價乖離 > 2% 時改用即時報價。
7. **資料去重與斷層修復**：`save_history()` 依日期去重並修復拆股/除權息斷層；幣安 `fill_binance_gaps()` 補齊缺口。
8. **Binance 現貨端點白名單**：`assert_spot_only()` 硬性攔截期貨/槓桿借貸接口。

### 4.3 資料流
```
quant_engine.py (Yahoo/Binance) ──► data/history/*.csv (快取)
        │                                  │
        ▼                                  ▼
public/data/latest_signals.json ◄── data/signal_ledger.csv (歷史驗證)
        │
        ├──► index.html / public/index.html (前端渲染)
        ├──► notifier.py ──► Discord Webhook
        ├──► ibkr_bridge.py ──► public/data/ibkr_order_plan.json
        └──► binance_spot_bridge.py ──► public/data/binance_spot_plan.json
```

---

## 5. 本地資料庫（CSV / JSON）結構與欄位字典

### 5.1 `data/history/{symbol}.csv`（16 檔 × 1000 日）
| 欄位 | 型別 | 說明 |
|---|---|---|
| Date | date | 交易日 |
| Open / High / Low / Close | float | 未還原真實交易價 OHLC |
| Adj Close | float | 還原權息收盤價（用於報酬計算） |
| Volume | int | 成交量 |
| Dividends | float | 每股股利 |
| Stock Splits | float | 拆股/反拆股係數 |

### 5.2 `data/custom_estimates.json`
```json
{
  "_instructions": "自訂財報覆寫檔",
  "defaults": { "base_per": 15.0, "compound_years": 3 },
  "overrides": {
    "NVDA": { "eps_current", "eps_next", "base_per", "compound_years",
              "per_high_avg", "per_low_avg", "rev_growth_3p", "op_margin",
              "pbr", "dividend_yield", "earnings_date" }
  }
}
```
欄位留 `null` 或刪除時自動以 yfinance 抓取計算。

### 5.3 `data/signal_ledger.csv`（歷史訊號驗證帳本）
| 欄位 | 說明 |
|---|---|
| date / symbol | 訊號日期與標的 |
| close_price | 收盤價 |
| master_score | 五師綜合評分 |
| five_dim_avg | 五維度平均 |
| position_stage | 倉位階段（0 觀望 / 1 試單 / 2 加碼 / 3 滿倉） |
| target_price_2 | 目標價② |
| floor_price | 預測低點 |
| nomad_signal | 遊牧民交減陰線訊號 |

### 5.4 `public/data/latest_signals.json`（核心輸出）
**頂層欄位**
| 欄位 | 說明 |
|---|---|
| generated_at | 產生時間 |
| summary | total_assets / signal_counts / category_counts |
| macro_overview | us10y_yield、dxy、vix、vix_zone、crypto_fear_greed、classification |
| ledger_summary | total_signals、evaluated、overall（avg_mfe_pct/avg_mae_pct/win_rate_pct）、by_symbol、rows |
| system_health | live_count、fallback_count、failed_symbols、degraded |
| assets | { Crypto: [], Stocks: [], REITs: [], Bonds: [], ETFs: [], Gold: [] } |

**單一標的（asset）欄位字典**
| 欄位 | 型別 | 說明 |
|---|---|---|
| symbol / name / category | str | 代號 / 中文名 / 類別 |
| data_source / data_source_status | str | 資料來源與健康狀態 |
| last_bar_date / is_bar_closed / bar_close_status | — | 最後 K 棒與收盤狀態 |
| price / change_pct | float | 現價與漲跌% |
| ma3/8/15/20/25/33/45/60/120/200 | float | 多重均線 |
| rsi14 / atr14 / high20 / boll_upper / boll_lower | float | RSI / ATR / 唐奇安 / 布林 |
| vol_ratio / volume_status | float/str | 量比與量能狀態 |
| tenkan_sen / kijun_sen / senkou_a / senkou_b / ichimoku_cloud | — | 一目均衡 |
| macd / macd_signal / macd_hist | float | MACD |
| trailing_stop_price / trailing_stop_status | — | ATR 吊燈移動停利 |
| current_defense_sl / breakout_entry_price / breakout_sl | float | 現價防守止損 / 突破買入 / 突破止損 |
| market_regime | str | 市場狀態（箱型/多頭/空頭等） |
| ofi_5d / ofi_direction / vwap_20d_bias | — | 訂單流與 VWAP 乖離 |
| corr_60d / corr_penalty | float | 60 日相關性與折減 |
| sparkline_60d / backtest_equity_60d | array | 60 日走勢與淨值 |
| near_20d_high_breakout | bool | 20 日高點突破 |
| position_stage | str | 倉位階段 |
| master_score | int | 森口亮綜合評分 0~100 |
| dividend_yield / trailing_pe / price_to_book / pbr | float | 殖利率 / 本益比 / 股淨比 |
| signal / horizon / reference_model / action_reason | str | 主訊號與依據 |
| ideal_buy_price / stop_loss_price / take_profit_price | float | 建議買入 / 嚴格止損 / 目標停利 |
| suggested_weight_pct | float | 建議倉位權重 |
| exit_rule_desc | str | 出場法則說明 |
| vol_drop_prev / max_vol_10d / max_vol_10d_ratio | — | 量縮 / 10 日最大量 |
| nomad_signal / nomad_tp_1pct/3pct / nomad_sl_5pct | — | 遊牧民訊號與短打價位 |
| weekly_bullish / fujimoto_6_checks / fujimoto_checks_detail | — | 茂爺爺 6 大重點 |
| endo_entry_rule / endo_uptrend / endo_small_cap_ok / endo_turnover_ok / endo_insider_ok | — | 遠藤洋檢核 |
| market_cap / insider_ownership / daily_turnover_val | float | 市值 / 內部持股 / 成交額 |
| expected_slippage_pct / stress_slippage_pct / effective_stop_distance_pct | float | 滑點緩衝 |
| stop_loss_triggered / defensive_zero_weight | bool | 止損觸發 / 防禦歸零 |
| position_plan | obj | capital / max_loss_pct / safe_invest_amount / safe_shares / invest_amount / max_loss_amount |
| cta_vote_score / cta_vote_long / cta_vote_short / cta_net / cta_strategies | — | CTA 六策投票 |
| taker_buy_ratio_5d / taker_buy_sentiment | — | Taker 主動買盤 |
| mtf_4h_ma20 / mtf_4h_rsi / mtf_4h_trend | — | 4H+1D 共振 |
| funding_rate_pct / funding_rate_label | — | 資金費率 |
| overfit_check / history_gap_count / history_gap_days | — | 樣本外穩健 / 缺口 |
| backtest_strategy_1y | obj | win_rate、strategy_return_1y、trade_count_1y、last_exit_reason、max_drawdown_pct、sharpe_ratio、profit_factor、oos_return_6m、equity_curve_60d |
| fast_quote_price / quote_cross_verified | — | 快報價交叉驗證 |
| valuation_model | str | 估值模型描述 |
| eps_mid/current/next / base_per / compound_years / profit_growth_rate / target_per / per_high_avg / per_low_avg | float | 森口亮估值輸入 |
| target_price_1/2 / upside_1/2 / floor_price / downside_risk | float | 目標價 / 上漲率 / 預測低點 / 下跌風險 |
| rule_of_40 / rule_of_40_met | float/bool | 40% 法則 |
| earnings_date / check_90d / check_180d | date | 財報日與驗證日 |
| five_dim_scores | obj | growth / profitability / undervaluation / safety / explosive / average / grade / is_a_plus |
| asset_model_type | str | 六大資產專屬模型 |
| domain_metrics | array | [ { title, items: [ {label, value, kind} ] } ] |

### 5.5 `public/data/ibkr_order_plan.json`
| 欄位 | 說明 |
|---|---|
| generated_at / mode / data_decoupled | 產生時間 / dry-run / 數據交易分離 |
| cash_balances | USD / HKD 現金餘額 |
| portfolio_anomaly_alerts | 閘門 10 異常巡檢 |
| config | 十道閘門常數快照 |
| summary | total / approved / blocked / skipped |
| orders[] | symbol、name、category、currency、signal、limit_price、stop_price、tp_price、quantity、estimated_cost、status、gate、reason、action |

### 5.6 `public/data/binance_spot_plan.json`
| 欄位 | 說明 |
|---|---|
| generated_at / mode / spot_base | 產生時間 / 純本金模式 / 現貨端點 |
| forbidden_endpoints | 硬性禁止端點 |
| time_sync | server_ms、offset_ms、status |
| cash_usd | 離線 USDT 本金 |
| orders[] | symbol、pair、signal、market_regime、suggested_weight_pct、cta_vote_score、step_size、tick_size、min_notional、status、reason |

---

*報告完畢 — 本報告由全專案深度掃描實際程式碼產出，所有欄位與公式均對齊現行 `latest_signals.json` 與前端渲染邏輯。*




