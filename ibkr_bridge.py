# -*- coding: utf-8 -*-
"""$0 零月費・數據與交易分離 (Data-Execution Decoupling) 的 IBKR 100% 純本金安全下單橋接系統。

核心原則 1（$0 零月費，數據與交易分離）：
    在產生被動收入前「不訂閱任何 IBKR 付費市場數據，也不租用 VPS」。所有行情、
    技術指標、基本面估值與歷史 K 線 100% 由免費的 quant_engine.py（Yahoo Finance
    / Binance）產出至 public/data/latest_signals.json；本橋接器僅負責在本地按需
    連線 IB Gateway / TWS，免費查詢帳戶各幣種現金餘額並發送限價括號委託單
    （Bracket Orders），發單完即斷開連線（On-Demand Connect & Disconnect）。

核心原則 2（100% 純本金、零槓桿、零外匯借貸利息）：
    嚴禁任何融資（Margin）、放空（Short）與跨幣種自動借貸。

用法：
    python ibkr_bridge.py --dry-run           # 離線模擬純本金委託單計畫（預設）
    python ibkr_bridge.py --check-account     # 唯讀連線查詢 USD/HKD 現金餘額與持倉
    python ibkr_bridge.py --execute           # 連線 TWS 通過 10 道閘門後實際送括號單
"""

import argparse
import datetime
import json
import math
import os
import sys
import time

# 動態導入 ib_insync：未安裝或本地未開 TWS 時自動進入離線模擬預覽模式，不報錯崩潰
try:
    from ib_insync import IB, Stock, LimitOrder, StopOrder  # noqa: F401

    IB_INSYNC_AVAILABLE = True
except Exception:  # pragma: no cover - 環境未裝 ib_insync 時走離線模式
    IB = None
    Stock = None
    LimitOrder = None
    StopOrder = None
    IB_INSYNC_AVAILABLE = False

from config import (
    IBKR_HOST,
    IBKR_PORT_PAPER,
    IBKR_CLIENT_ID,
    IBKR_MARKET_DATA_TYPE,
    IBKR_USE_FREE_EXTERNAL_DATA_ONLY,
    STRICT_CASH_ONLY,
    MAX_LEVERAGE_RATIO,
    CASH_SAFETY_BUFFER_PCT,
    ALLOW_SHORT_SELLING,
    PREVENT_FX_MARGIN_LOAN,
    MAX_ORDERS_PER_RUN,
    ORDER_THROTTLE_SECONDS,
    REQUIRE_STRICT_SETTLED_CASH,
    IBKR_MIN_FEE_BY_CURRENCY,
    MIN_ORDER_AMOUNT_BY_CURRENCY,
    MAX_FEE_RATIO_PCT,
    MAX_SINGLE_ASSET_WEIGHT_PCT,
    EARNINGS_BLACKOUT_DAYS,
    BLOCKED_LEVERAGED_TICKERS,
    IBKR_DRY_RUN_CASH,
)

SIGNALS_FILE = os.path.join(".", "public", "data", "latest_signals.json")
ORDER_PLAN_FILE = os.path.join(".", "public", "data", "ibkr_order_plan.json")
ORDER_LOG_FILE = os.path.join(".", "ibkr_order_log.txt")

# 明確看多 / 試單主訊號（對應 quant_engine.py 的訊號定義，訊號過濾閘門用）
BULLISH_SIGNALS = {
    "🟢 強烈買進 / 低檔加碼",
    "🔵 趨勢多頭 / 續抱",
    "🟡 價值收息買點",
    "🔥 爆量長紅突破 (強勢買進)",
    "🟡 左側超賣試單 (嚴控1/9小倉)",
}

# 港股每手股數（預設 100 股）
HK_LOT_SIZE = 100

# 每筆委託單的手續費成本緩衝（0.1%），與訊號 stress_slippage_pct 相加後計入買入總額
COMMISSION_BUFFER_PCT = 0.001


def _to_float(value):
    """安全轉 float，失敗或非有限數回傳 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _fmt_money(value):
    if value is None:
        return "N/A"
    return f"{value:,.2f}"


def load_signals():
    """讀取免費數據源 quant_engine.py 產出的最新訊號。"""
    with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_assets(data):
    """把 latest_signals.json 的 assets 字典攤平成單一記錄清單。"""
    assets = data.get("assets") or {}
    known = ("Crypto", "Stocks", "REITs", "Bonds", "ETFs", "Gold")
    for category in known:
        for record in assets.get(category) or []:
            yield record
    for category, records in assets.items():
        if category in known:
            continue
        for record in records:
            yield record


def _currency_of(symbol):
    """標的計價幣種：.HK 為 HKD，美股/ETF/美債/黃金 ETF 皆為 USD。"""
    if symbol.endswith(".HK"):
        return "HKD"
    return "USD"


def _settled_cash(cash_balances, currency):
    """讀取該幣種已交割現金 SettledCash（dry-run 純數字時即視為該值）。"""
    entry = cash_balances.get(currency, 0.0) if cash_balances else 0.0
    if isinstance(entry, dict):
        return _to_float(entry.get("SettledCash")) or 0.0
    return _to_float(entry) or 0.0


def _cash_balance(cash_balances, currency):
    """讀取該幣種帳上總餘額 CashBalance（dry-run 純數字時即視為該值）。"""
    entry = cash_balances.get(currency, 0.0) if cash_balances else 0.0
    if isinstance(entry, dict):
        return _to_float(entry.get("CashBalance")) or 0.0
    return _to_float(entry) or 0.0


def _days_until(date_str):
    """把 'YYYY-MM-DD' 字串轉成距今天數（負值=已過），解析失敗回傳 None。"""
    if not date_str:
        return None
    try:
        d = datetime.datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (d - datetime.date.today()).days


def _is_ibkr_supported(symbol, category):
    """閘門 1：僅處理 IBKR 支援的現貨品種，攔截 Crypto、.TW 台股與槓桿 ETF。"""
    if category in ("Crypto",):
        return False
    if symbol.endswith(".TW"):
        return False
    if symbol in BLOCKED_LEVERAGED_TICKERS:
        return False
    return True


def _blocked(gate, reason, base):
    base.update(status="blocked", gate=gate, reason=reason)
    return base


def _skipped(gate, reason, base):
    base.update(status="skipped", gate=gate, reason=reason)
    return base


def validate_zero_cost_cash_order(record, cash_balances=None, positions=None):
    """十道實盤防坑檢核閘門。

    回傳 dict：
        status: "approved" / "blocked" / "skipped"
        gate:   停在哪一道閘門（approved 時為 6 表示全數通過）
        reason: 說明文字
        action: "BUY" / "SELL" / None
        quantity / limit_price / stop_price / tp_price / estimated_cost
    """
    cash_balances = cash_balances or {}
    positions = positions or {}

    symbol = record.get("symbol") or ""
    category = record.get("category") or ""
    name = record.get("name") or symbol
    signal = record.get("signal") or ""

    price = _to_float(record.get("price"))
    entry = _to_float(record.get("breakout_entry_price"))
    if entry is None:
        entry = _to_float(record.get("ideal_buy_price"))
    if entry is None:
        entry = price
    stop = _to_float(record.get("current_defense_sl"))
    if stop is None:
        stop = _to_float(record.get("stop_loss_price"))
    tp = _to_float(record.get("take_profit_price"))
    slippage = _to_float(record.get("stress_slippage_pct")) or 0.0

    currency = _currency_of(symbol)
    base = {
        "symbol": symbol,
        "name": name,
        "category": category,
        "currency": currency,
        "signal": signal,
        "limit_price": entry,
        "stop_price": stop,
        "tp_price": tp,
        "quantity": 0,
        "estimated_cost": 0.0,
        "status": "blocked",
        "gate": 0,
        "reason": "",
        "action": None,
    }

    # ----- 閘門 1：品種與禁槓桿檢查 -----
    if category in ("Crypto",):
        return _skipped(1, "Crypto 非 IBKR 現貨標的（幣圈由 Binance 執行），跳過", base)
    if symbol.endswith(".TW"):
        return _skipped(1, ".TW 台股非 IBKR 現貨標的，跳過", base)
    if symbol in BLOCKED_LEVERAGED_TICKERS:
        return _blocked(
            1,
            f"🚫 槓桿/反向 ETF 禁買清單攔截 ({symbol} ∈ BLOCKED_LEVERAGED_TICKERS)，"
            f"遵守 100% 純本金・零槓桿原則",
            base,
        )
    if not STRICT_CASH_ONLY:
        return _blocked(1, "STRICT_CASH_ONLY=False，系統停用（未開啟純本金鎖）", base)
    if MAX_LEVERAGE_RATIO > 1.0:
        return _blocked(1, f"MAX_LEVERAGE_RATIO={MAX_LEVERAGE_RATIO} > 1.0，違反零槓桿原則", base)

    pos_qty = _to_float(positions.get(symbol)) or 0.0

    # ----- 閘門 2：Signal Gatekeeper 訊號過濾 -----
    # 2a. 已持有部位且跌破防守止損 → 僅產生平倉賣單（sell_qty <= 持倉股數，禁止無券放空）
    if pos_qty > 0 and stop is not None and price is not None and price < stop:
        sell_qty = int(pos_qty)
        if sell_qty <= 0:
            return _skipped(2, "持倉股數為 0，無可平倉", base)
        base.update(
            status="approved",
            gate=2,
            action="SELL",
            quantity=sell_qty,
            limit_price=stop,
            stop_price=None,
            tp_price=None,
            estimated_cost=round(sell_qty * stop, 2),
            reason=(
                f"持有 {pos_qty:g} 股已跌破防守止損 {_fmt_money(stop)}，"
                f"僅產生平倉賣單（sell_qty={sell_qty} <= 持倉 {pos_qty:g}，禁止無券放空）"
            ),
        )
        return base

    # 2b. 買單僅限明確看多／試單，且未跌破防守止損
    if signal not in BULLISH_SIGNALS:
        return _skipped(2, f"主訊號「{signal}」非明確看多/試單，不產生買單", base)
    if stop is not None and price is not None and price < stop:
        return _skipped(2, "現價已跌破防守止損，禁止追價買入", base)

    # ----- 閘門 3：防跨幣種融資借貸鎖（Currency-Specific Cash Lock）-----
    if entry is None or entry <= 0:
        return _blocked(3, "缺少有效限價買入價 (breakout_entry_price / ideal_buy_price)", base)
    if stop is None or stop <= 0:
        return _blocked(3, "缺少有效防守止損價 (current_defense_sl)", base)

    settled = _settled_cash(cash_balances, currency)
    cash_balance = _cash_balance(cash_balances, currency)
    # 可動用本金硬上限 = min(已結算 SettledCash, 帳上總餘額 CashBalance) × (1 - 現金緩衝)
    usable = min(settled, cash_balance) * (1.0 - CASH_SAFETY_BUFFER_PCT) if PREVENT_FX_MARGIN_LOAN else min(settled, cash_balance)

    # 買入總額緩衝 = 手續費 + 極端踩踏滑點
    cost_buffer = 1.0 + COMMISSION_BUFFER_PCT + (slippage / 100.0 if slippage else 0.0)

    # ----- 閘門 4：整數股與港股每手股數對齊（先以帳上總餘額估算建議股數，供閘門 7 抓 T+1 差）-----
    lot = HK_LOT_SIZE if currency == "HKD" else 1
    sizing_cash = cash_balance * (1.0 - CASH_SAFETY_BUFFER_PCT) if PREVENT_FX_MARGIN_LOAN else cash_balance
    qty = (int(sizing_cash // (entry * cost_buffer)) // lot) * lot
    needed = qty * entry * cost_buffer
    order_notional = qty * entry

    if qty < 1:
        minimum = entry * cost_buffer * lot
        return _blocked(
            3,
            f"🚫 [防外匯借貸攔截] {symbol} 需要 {_fmt_money(minimum)} {currency}，"
            f"但帳上已結算 {currency} 現金僅 {_fmt_money(settled)}，"
            f"請先在 IBKR 手動換匯 (FX Conversion)，禁止自動借貸計息！",
            base,
        )

    # ----- 閘門 9：財報前夕跳空禁買窗 + 單一標的 20% 防跳空上限 -----
    earnings_date = record.get("earnings_date")
    if earnings_date:
        days_to_earnings = _days_until(earnings_date)
        if days_to_earnings is not None and 0 <= days_to_earnings <= EARNINGS_BLACKOUT_DAYS:
            return _blocked(
                9,
                f"📅 [財報跳空防護] 距離財報公布不足 {EARNINGS_BLACKOUT_DAYS} 天，暫停新建買單",
                base,
            )

    cap_applied = False
    total_capital = cash_balance if cash_balance > 0 else settled
    cap_notional = total_capital * MAX_SINGLE_ASSET_WEIGHT_PCT
    if order_notional > cap_notional:
        max_qty = int(cap_notional // (entry * cost_buffer))
        qty = (max_qty // lot) * lot
        if qty < 1:
            return _blocked(
                9,
                f"🛡️ [20% 單一標的防跳空上限] {symbol} 依總本金 20% 上限可買股數不足 1 整股/1 手，暫停",
                base,
            )
        cap_applied = True
        needed = qty * entry * cost_buffer
        order_notional = qty * entry

    # ----- 閘門 7：嚴格 SettledCash vs CashBalance 交割時間差防禦 -----
    if REQUIRE_STRICT_SETTLED_CASH and cash_balance >= needed and settled < needed:
        return _blocked(
            7,
            f"⏳ [T+1 交割時間差攔截] 帳上 {currency} 總餘額雖足夠，但已結算現金 "
            f"(SettledCash={_fmt_money(settled)}) 不足 {_fmt_money(needed)}！"
            f"請等待換匯/賣股完成 T+1 交割後隔日再下單，避免產生隱形過夜借貸利息。",
            base,
        )

    if PREVENT_FX_MARGIN_LOAN and needed > usable:
        return _blocked(
            3,
            f"🚫 [防外匯借貸攔截] {symbol} 需要 {_fmt_money(needed)} {currency}，"
            f"但帳上已結算 {currency} 現金僅 {_fmt_money(settled)}，"
            f"請先在 IBKR 手動換匯 (FX Conversion)，禁止自動借貸計息！",
            base,
        )

    # ----- 閘門 8：單筆最低金額與手續費佔比攔截 -----
    min_fee = IBKR_MIN_FEE_BY_CURRENCY.get(currency, 0.0)
    min_amount = MIN_ORDER_AMOUNT_BY_CURRENCY.get(currency, 0.0)
    est_roundtrip_fee = min_fee * 2.0
    if currency == "HKD":
        # 港股額外加計 0.26% 雙邊印花稅與規費
        est_roundtrip_fee += order_notional * 0.0026
    fee_pct = (est_roundtrip_fee / order_notional) if order_notional > 0 else 0.0
    if order_notional < min_amount or fee_pct > MAX_FEE_RATIO_PCT:
        return _blocked(
            8,
            f"💸 [手續費蠶食攔截] 單筆金額 {_fmt_money(order_notional)} {currency} 過小，"
            f"預估雙邊手續費佔比達 {fee_pct * 100:.2f}% (>0.50% 上限)，拒絕下單以保護本金！",
            base,
        )

    # ----- 閘門 5：GTC 括號委託單封裝（母限價買單 + 止損子單 + 停利子單）-----
    tp_note = "" if (tp is not None and tp > 0) else "（無目標停利價，僅掛母單+止損）"
    cap_note = "｜🛡️ 已套用 20% 單一標的防跳空上限" if cap_applied else ""

    # ----- 閘門 6：防罰款速率熔斷器（於 build_order_plan 統一計數與封頂）-----
    base.update(
        status="approved",
        gate=6,
        action="BUY",
        quantity=qty,
        limit_price=entry,
        stop_price=stop,
        tp_price=tp,
        estimated_cost=round(needed, 2),
        order_notional=round(order_notional, 2),
        est_roundtrip_fee=round(est_roundtrip_fee, 2),
        fee_ratio_pct=round(fee_pct * 100, 4),
        single_asset_capped=cap_applied,
        reason=(
            f"通過安全閘門：限價買入 {entry} × {qty} 股，"
            f"估動用 {currency} {_fmt_money(needed)}（≤ 可用 98% = {_fmt_money(usable)}）"
            f"{tp_note}{cap_note}"
        ),
    )
    return base


def build_order_plan(records, cash_balances, positions):
    """對所有標的跑閘門，並於閘門 6 套用單次執行 5 單熔斷上限。

    回傳 (results, anomaly_alerts)：
        results         逐筆委託單判定結果
        anomaly_alerts  閘門 10 異常持倉/碎股/負現金巡檢警報清單
    """
    anomaly_alerts = audit_portfolio_anomalies(positions, cash_balances)
    results = []
    approved_count = 0
    for record in records:
        result = validate_zero_cost_cash_order(record, cash_balances, positions)
        if result["status"] == "approved":
            if anomaly_alerts and result.get("action") == "BUY":
                # 閘門 10：偵測到負持倉/碎股/負現金，凍結所有新買單
                result.update(
                    status="blocked",
                    gate=10,
                    action=None,
                    quantity=0,
                    estimated_cost=0.0,
                    reason=(
                        "🚨 [異常持倉凍結] 偵測到負持倉/碎股/負現金，"
                        "已凍結所有新買單，請先人工處理異常持倉"
                    ),
                )
            elif approved_count >= MAX_ORDERS_PER_RUN:
                result.update(
                    status="blocked",
                    gate=6,
                    action=None,
                    quantity=0,
                    estimated_cost=0.0,
                    reason=(
                        f"🛑 已達單次執行上限 {MAX_ORDERS_PER_RUN} 組委託單"
                        f"（防 Bug 頻繁交易罰款熔斷器），本單暫緩"
                    ),
                )
            else:
                approved_count += 1
        results.append(result)
    return results, anomaly_alerts


def connect_ib(host=None, port=None, client_id=None):
    """即連即斷架構的連線入口：連上後立即切換免費數據模式。"""
    if not IB_INSYNC_AVAILABLE:
        return None, "未安裝 ib_insync（pip install ib_insync），無法連線 IB Gateway / TWS"
    host = host or IBKR_HOST
    port = port or IBKR_PORT_PAPER
    client_id = client_id or IBKR_CLIENT_ID
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=8)
    except Exception as exc:
        return None, f"連線失敗 {host}:{port} → {exc}"
    try:
        # 免費數據模式：未付費訂閱時不觸發 Error 10089、不產生數據月費
        ib.reqMarketDataType(IBKR_MARKET_DATA_TYPE)
    except Exception:
        pass
    return ib, None


def read_account_state(ib):
    """唯讀查詢帳戶各幣種現金餘額（同時抓 SettledCash 與 CashBalance）與持倉。"""
    raw_cash = {"USD": {"SettledCash": 0.0, "CashBalance": 0.0},
                "HKD": {"SettledCash": 0.0, "CashBalance": 0.0}}
    try:
        for av in ib.accountValues():
            if av.tag in ("SettledCash", "CashBalance") and av.currency in ("USD", "HKD"):
                raw_cash[av.currency][av.tag] = _to_float(av.value) or 0.0
    except Exception:
        pass

    cash_balances = {}
    for cur in ("USD", "HKD"):
        cash_balances[cur] = {
            "SettledCash": raw_cash[cur].get("SettledCash", 0.0),
            "CashBalance": raw_cash[cur].get("CashBalance", 0.0),
        }

    positions = {}
    try:
        for pos in ib.positions():
            positions[pos.contract.symbol] = _to_float(pos.position) or 0.0
    except Exception:
        pass

    return cash_balances, positions


def audit_portfolio_anomalies(positions, cash_balances):
    """閘門 10：公司行動／反向拆股「異常負持倉」巡檢器。

    掃描所有持倉數量與各幣種現金餘額：若有任一持倉 position < 0（被動放空）、
    非整數碎股、或任一幣種 CashBalance < 0（負數透支），輸出最高級別紅色警報。
    """
    alerts = []
    for sym, raw_qty in (positions or {}).items():
        q = _to_float(raw_qty)
        if q is None:
            continue
        if q < 0:
            alerts.append({
                "level": "critical",
                "type": "negative_position",
                "symbol": sym,
                "position": q,
                "message": f"🚨 偵測到負持倉（被動放空）{sym} = {q:g} 股，立即凍結所有新買單！",
            })
        elif q != int(q):
            alerts.append({
                "level": "warning",
                "type": "fractional_position",
                "symbol": sym,
                "position": q,
                "message": f"⚠️ 偵測到非整數碎股 {sym} = {q:g} 股（可能為公司行動/反向拆股殘股），請人工確認。",
            })
    for cur in ("USD", "HKD"):
        cash = _cash_balance(cash_balances, cur)
        if cash < 0:
            alerts.append({
                "level": "critical",
                "type": "negative_cash",
                "currency": cur,
                "cash_balance": cash,
                "message": f"🚨 偵測到 {cur} 現金餘額為負 ({_fmt_money(cash)})，疑似透支借貸，立即凍結所有新買單！",
            })
    return alerts


def build_contract(symbol, currency):
    """把訊號代號轉成 ib_insync 現貨合約（.HK 拆成 SEHK 交易所）。"""
    if symbol.endswith(".HK"):
        return Stock(symbol[:-3], "SEHK", currency)
    return Stock(symbol, "SMART", currency)


def place_bracket_order(ib, contract, qty, entry, stop, tp):
    """閘門 5：GTC 括號委託單（母限價買單 + 止損子單 + 停利子單）。

    三張單皆為伺服器端 GTC 單，送單後即可關閉 TWS，不怕每週斷線或手機互踢。
    """
    parent = LimitOrder("BUY", qty, entry, tif="GTC")
    parent.transmit = False
    trade_parent = ib.placeOrder(contract, parent)
    parent_id = trade_parent.order.orderId

    has_tp = tp is not None and tp > 0
    stop_order = StopOrder("SELL", qty, stop, tif="GTC")
    stop_order.parentId = parent_id
    stop_order.transmit = not has_tp
    trade_stop = ib.placeOrder(contract, stop_order)

    trade_tp = None
    if has_tp:
        tp_order = LimitOrder("SELL", qty, tp, tif="GTC")
        tp_order.parentId = parent_id
        tp_order.transmit = True
        trade_tp = ib.placeOrder(contract, tp_order)

    return [trade_parent, trade_stop, trade_tp]


def log_order(result, trades):
    """已送單紀錄寫入本地日誌（ibkr_order_log.txt 已加入 .gitignore，不外洩帳戶資訊）。"""
    entry = {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "symbol": result["symbol"],
        "action": result["action"],
        "currency": result["currency"],
        "quantity": result["quantity"],
        "limit_price": result["limit_price"],
        "stop_price": result["stop_price"],
        "tp_price": result["tp_price"],
        "order_ids": [t.order.orderId for t in trades if t is not None],
    }
    with open(ORDER_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_self_test():
    """十道閘門自檢演示（僅 console 輸出，不寫入委託單計畫，不實際送單）。"""
    print("\n" + "=" * 74)
    print("🛡️ 十道防坑閘門自檢演示（槓桿/跨幣種/整數股/每手/T+1/手續費/20%上限/財報/負持倉）")
    print("=" * 74)

    lev = {
        "symbol": "TQQQ", "name": "ProShares UltraPro QQQ (3x)", "category": "ETFs",
        "price": 70.0, "breakout_entry_price": 70.0, "current_defense_sl": 66.0,
        "take_profit_price": 80.0, "stress_slippage_pct": 0.0,
        "signal": "🟢 強烈買進 / 低檔加碼",
    }
    r = validate_zero_cost_cash_order(lev, {"USD": 100_000.0}, {})
    print(f"  [閘門1] 槓桿 ETF 攔截  TQQQ      → {r['status']}: {r['reason']}")

    hk = {
        "symbol": "0823.HK", "name": "領展房產基金", "category": "REITs",
        "price": 36.0, "breakout_entry_price": 36.0, "current_defense_sl": 34.0,
        "take_profit_price": 40.0, "stress_slippage_pct": 0.0,
        "signal": "🔵 趨勢多頭 / 續抱",
    }
    r = validate_zero_cost_cash_order(hk, {"USD": 100_000.0, "HKD": 0.0}, {})
    print(f"  [閘門3] 跨幣種現金不足攔截 0823.HK → {r['status']}: {r['reason']}")

    us = {
        "symbol": "AAPL", "name": "蘋果 (AAPL)", "category": "Stocks",
        "price": 220.0, "breakout_entry_price": 220.0, "current_defense_sl": 210.0,
        "take_profit_price": 250.0, "stress_slippage_pct": 0.5,
        "signal": "🟢 強烈買進 / 低檔加碼",
    }
    r = validate_zero_cost_cash_order(us, {"USD": 100_000.0}, {})
    print(f"  [閘門4] 美股整數股計算  AAPL     → qty={r['quantity']} 股, "
          f"估動用 ${_fmt_money(r['estimated_cost'])}, capped={r.get('single_asset_capped')}, status={r['status']}")

    hk2 = dict(hk)
    r = validate_zero_cost_cash_order(hk2, {"USD": 0.0, "HKD": 200_000.0}, {})
    print(f"  [閘門4] 港股每手 100 股對齊 0823.HK → qty={r['quantity']} 股 "
          f"(每手 {HK_LOT_SIZE}), 估動用 HKD {_fmt_money(r['estimated_cost'])}, capped={r.get('single_asset_capped')}, status={r['status']}")

    # 閘門 7：T+1 交割時間差攔截（總餘額足夠但 SettledCash 不足）
    t1 = dict(us)
    r = validate_zero_cost_cash_order(
        t1,
        {"USD": {"SettledCash": 500.0, "CashBalance": 100_000.0}},
        {},
    )
    print(f"  [閘門7] T+1 未結算現金攔截 AAPL → {r['status']}: {r['reason']}")

    # 閘門 8：手續費蠶食攔截（單筆金額低於最低門檻）
    small = {
        "symbol": "SGOV", "name": "超短期國庫券ETF", "category": "Bonds",
        "price": 50.0, "breakout_entry_price": 50.0, "current_defense_sl": 49.75,
        "take_profit_price": 51.0, "stress_slippage_pct": 0.0,
        "signal": "🟡 價值收息買點",
    }
    r = validate_zero_cost_cash_order(small, {"USD": 500.0}, {})
    print(f"  [閘門8] 最低手續費佔比攔截 SGOV → {r['status']}: {r['reason']}")

    # 閘門 9：財報前夕 3 天禁買
    er = dict(us)
    er["earnings_date"] = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
    r = validate_zero_cost_cash_order(er, {"USD": 100_000.0}, {})
    print(f"  [閘門9] 財報前夕禁買窗 AAPL → {r['status']}: {r['reason']}")

    # 閘門 10：異常負持倉巡檢
    anomalies = audit_portfolio_anomalies(
        {"AAPL": -10.0, "NVDA": 5.5},
        {"USD": {"SettledCash": 100_000.0, "CashBalance": 100_000.0},
         "HKD": {"SettledCash": 0.0, "CashBalance": -50.0}},
    )
    for a in anomalies:
        print(f"  [閘門10] 異常持倉巡檢 {a['type']} → {a['message']}")

    print(f"  [閘門6] 單次執行上限 MAX_ORDERS_PER_RUN = {MAX_ORDERS_PER_RUN}，"
          f"每單間隔 {ORDER_THROTTLE_SECONDS}s")
    print("=" * 74)


def run_dry_run(records, data, cash_balances=None):
    """離線模擬：不需開啟 TWS，讀 latest_signals.json 產出純本金整數股委託單計畫。"""
    cash = dict(cash_balances or IBKR_DRY_RUN_CASH)
    results, anomaly_alerts = build_order_plan(records, cash, {})

    approved = [r for r in results if r["status"] == "approved"]
    blocked = [r for r in results if r["status"] == "blocked"]
    skipped = [r for r in results if r["status"] == "skipped"]

    plan = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "dry-run",
        "data_decoupled": IBKR_USE_FREE_EXTERNAL_DATA_ONLY,
        "cash_balances": cash,
        "portfolio_anomaly_alerts": anomaly_alerts,
        "config": {
            "market_data_type": IBKR_MARKET_DATA_TYPE,
            "strict_cash_only": STRICT_CASH_ONLY,
            "max_leverage_ratio": MAX_LEVERAGE_RATIO,
            "cash_safety_buffer_pct": CASH_SAFETY_BUFFER_PCT,
            "allow_short_selling": ALLOW_SHORT_SELLING,
            "prevent_fx_margin_loan": PREVENT_FX_MARGIN_LOAN,
            "max_orders_per_run": MAX_ORDERS_PER_RUN,
            "order_throttle_seconds": ORDER_THROTTLE_SECONDS,
            "require_strict_settled_cash": REQUIRE_STRICT_SETTLED_CASH,
            "ibkr_min_fee_by_currency": IBKR_MIN_FEE_BY_CURRENCY,
            "min_order_amount_by_currency": MIN_ORDER_AMOUNT_BY_CURRENCY,
            "max_fee_ratio_pct": MAX_FEE_RATIO_PCT,
            "max_single_asset_weight_pct": MAX_SINGLE_ASSET_WEIGHT_PCT,
            "earnings_blackout_days": EARNINGS_BLACKOUT_DAYS,
        },
        "summary": {
            "total": len(results),
            "approved": len(approved),
            "blocked": len(blocked),
            "skipped": len(skipped),
        },
        "orders": results,
    }

    with open(ORDER_PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)

    print(f"✅ 離線模擬委託單計畫已輸出至 {ORDER_PLAN_FILE}")
    print(f"   訊號檔：{SIGNALS_FILE}（免費數據源，未向 IBKR 請求任何行情）")
    print(f"   現金餘額（離線預設）：USD {_fmt_money(cash.get('USD', 0.0))} / "
          f"HKD {_fmt_money(cash.get('HKD', 0.0))}")
    print(f"   結果：通過 {len(approved)} 組｜攔截 {len(blocked)} 組｜略過 {len(skipped)} 組")
    if anomaly_alerts:
        print("\n  🚨 [閘門 10 異常持倉巡檢] 偵測到異常，已凍結所有新買單：")
        for a in anomaly_alerts:
            print(f"     {a['message']}")
    if approved:
        print("\n  ✅ 通過閘門的委託單（純本金整數股）：")
        for r in approved:
            print(f"     {r['action']} {r['symbol']:<10} {r['currency']} × {r['quantity']:>6} 股 "
                  f"@ LMT {_fmt_money(r['limit_price'])}｜STP {_fmt_money(r['stop_price'])}｜"
                  f"TP {_fmt_money(r['tp_price'])}｜估動用 {_fmt_money(r['estimated_cost'])}")
    if blocked:
        print("\n  🚫 被防坑閘門攔截：")
        for r in blocked:
            print(f"     [{r['symbol']}] {r['reason']}")
    if skipped:
        print("\n  ⏭️ 略過（非看多訊號 / 非 IBKR 現貨）：")
        for r in skipped:
            print(f"     [{r['symbol']}] {r['reason']}")

    run_self_test()
    return plan


def check_account(port=None):
    """--check-account：唯讀連線本地 TWS，免費列出 USD/HKD 真實現金餘額與持倉。"""
    ib, err = connect_ib(port=port)
    if ib is None:
        print(f"⚠️ 無法連線 IB Gateway / TWS：{err}")
        print("   請先啟動 IB Gateway / TWS 並開啟 API 連線（Paper 7497 / Live 7496）")
        return
    try:
        cash_balances, positions = read_account_state(ib)
    except Exception as exc:
        print(f"❌ 讀取帳戶狀態失敗：{exc}")
        ib.disconnect()
        return
    ib.disconnect()

    anomaly_alerts = audit_portfolio_anomalies(positions, cash_balances)
    print("🏦 IBKR 帳戶各幣種現金餘額（唯讀查詢，免費數據模式）")
    for cur in ("USD", "HKD"):
        settled = _settled_cash(cash_balances, cur)
        cash_bal = _cash_balance(cash_balances, cur)
        print(f"   {cur}: 已結算現金 SettledCash {_fmt_money(settled)} ｜ 總餘額 CashBalance {_fmt_money(cash_bal)}")
    if positions:
        print("\n📦 目前持倉：")
        for sym, qty in positions.items():
            print(f"   {sym}: {qty:,.0f} 股")
    else:
        print("\n📦 目前無持倉")
    if anomaly_alerts:
        print("\n🚨 [閘門 10 異常持倉巡檢] 偵測到異常，已凍結所有新買單：")
        for a in anomaly_alerts:
            print(f"     {a['message']}")
    else:
        print("\n✅ 閘門 10 異常持倉巡檢：無負持倉 / 碎股 / 負現金")
    print("\n✅ 已斷開連線（唯讀查詢，未送出任何委託單）")


def execute_orders(port=None):
    """--execute：連線本地 TWS，通過十道閘門後實際送出 GTC Bracket Orders。"""
    if not STRICT_CASH_ONLY:
        print("❌ STRICT_CASH_ONLY=False，系統停用（拒絕執行）")
        return
    data = load_signals()
    records = list(iter_assets(data))

    ib, err = connect_ib(port=port)
    if ib is None:
        print(f"⚠️ 無法連線 IB Gateway / TWS：{err}")
        print("   → 改以離線模擬預覽模式（--dry-run）產出委託單計畫，不實際送單")
        run_dry_run(records, data)
        return

    try:
        cash_balances, positions = read_account_state(ib)
    except Exception as exc:
        print(f"❌ 讀取帳戶狀態失敗：{exc}")
        ib.disconnect()
        return

    results, anomaly_alerts = build_order_plan(records, cash_balances, positions)
    if anomaly_alerts:
        print("\n🚨 [閘門 10 異常持倉巡檢] 偵測到異常，已凍結所有新買單：")
        for a in anomaly_alerts:
            print(f"     {a['message']}")
    approved = [r for r in results if r["status"] == "approved"]

    if not approved:
        print("⚠️ 沒有通過安全閘門的委託單，不送任何單，直接斷線")
        run_self_test()
        ib.disconnect()
        return

    print(f"🔓 共 {len(approved)} 組委託單通過閘門，開始送單（間隔 {ORDER_THROTTLE_SECONDS}s）")
    sent = 0
    for idx, r in enumerate(approved):
        contract = build_contract(r["symbol"], r["currency"])
        try:
            trades = place_bracket_order(
                ib, contract, r["quantity"], r["limit_price"], r["stop_price"], r["tp_price"]
            )
            log_order(r, trades)
            sent += 1
            print(f"   ✅ 已送出括號單 {r['action']} {r['symbol']} × {r['quantity']} "
                  f"@ LMT {_fmt_money(r['limit_price'])}")
        except Exception as exc:
            print(f"   ❌ 送單失敗 {r['symbol']}: {exc}")
        if idx < len(approved) - 1:
            time.sleep(ORDER_THROTTLE_SECONDS)

    ib.disconnect()
    print(f"\n✅ 執行完成，共送出 {sent} 組委託單，已自動斷開連線（On-Demand Disconnect）")


def main():
    # 強制 UTF-8 輸出，避免 Windows cp950 主控台無法編碼 emoji
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="$0 零月費・數據與交易分離的 IBKR 100% 純本金安全下單橋接系統"
    )
    parser.add_argument("--dry-run", action="store_true", help="離線模擬純本金委託單計畫（預設）")
    parser.add_argument("--check-account", action="store_true", help="唯讀連線查詢 USD/HKD 現金餘額與持倉")
    parser.add_argument("--execute", action="store_true", help="連線 TWS 實際送出 GTC 括號單")
    parser.add_argument("--cash-usd", type=float, default=None, help="離線預覽 USD 本金（覆蓋預設）")
    parser.add_argument("--cash-hkd", type=float, default=None, help="離線預覽 HKD 本金（覆蓋預設）")
    parser.add_argument("--port", type=int, default=IBKR_PORT_PAPER, help="IBKR port（7497 Paper / 7496 Live）")
    args = parser.parse_args()

    # 覆蓋離線預覽本金（不影響實盤 accountValues()）
    dry_cash = dict(IBKR_DRY_RUN_CASH)
    if args.cash_usd is not None:
        dry_cash["USD"] = args.cash_usd
    if args.cash_hkd is not None:
        dry_cash["HKD"] = args.cash_hkd

    if args.execute:
        execute_orders(port=args.port)
    elif args.check_account:
        check_account(port=args.port)
    else:
        data = load_signals()
        records = list(iter_assets(data))
        run_dry_run(records, data, cash_balances=dry_cash)


if __name__ == "__main__":
    main()






