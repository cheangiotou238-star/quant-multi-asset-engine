# -*- coding: utf-8 -*-
"""Binance 100% 純本金・零槓桿現貨橋接器（Spot Cash Only）。

最高實盤鐵律：100% 純本金現貨模式（Spot Cash Only），嚴禁任何期貨合約
（Futures/Perpetual）、嚴禁槓桿借貸（Margin/Leverage）、嚴禁放空！

本橋接器嚴格鎖死現貨端點（https://api.binance.com/api/v3/...），程式碼中硬性
禁止呼叫任何 /fapi/（U 本位合約）、/dapi/（幣本位合約）或 /sapi/v1/margin/
（槓桿借貸）下單接口。

技巧 110（本地與伺服器時間差同步 time_offset_ms）：
    連線時先請求 /api/v3/time，計算 serverTime - localTime 偏移量（毫秒），
    防止實盤 API 簽名發生 -1021 Timestamp outside of recvWindow 錯誤。

技巧 109（下單帳務數值精度校正 LOT_SIZE / stepSize / tickSize / minNotional）：
    從 /api/v3/exchangeInfo（或內建安全預設表）讀取精度規則，實作
    round_step_size / round_tick_size（Decimal 無條件捨去 ROUND_DOWN），確保
    買入顆數乘以價格絕不超出帳上可用 USDT 現貨餘額，且不觸發 -1013 Filter failure。

用法：
    python binance_spot_bridge.py --dry-run    # 預設：讀 latest_signals.json 產出精度校正現貨委託預覽
    python binance_spot_bridge.py --sync-time  # 唯讀同步本地與伺服器時間差
"""

import argparse
import datetime
import json
import math
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN

import requests

# ===== 嚴格鎖死現貨端點（唯讀 + 下單預覽皆走 /api/v3/） =====
BINANCE_SPOT_BASE = "https://api.binance.com/api/v3"
SPOT_TIME_URL = f"{BINANCE_SPOT_BASE}/time"
SPOT_EXCHANGE_INFO_URL = f"{BINANCE_SPOT_BASE}/exchangeInfo"

# 硬性禁止呼叫的非現貨端點標記（期貨 / 幣本位合約 / 槓桿借貸）
FORBIDDEN_ENDPOINT_MARKERS = ("/fapi/", "/dapi/", "/sapi/v1/margin/")

SIGNALS_FILE = os.path.join(".", "public", "data", "latest_signals.json")
PLAN_FILE = os.path.join(".", "public", "data", "binance_spot_plan.json")
# 前端雙路徑讀取：同時輸出 public/data/ 與 data/ 兩份，避免根目錄部署時 404 HTML。
PLAN_FILE_ROOT = os.path.join(".", "data", "binance_spot_plan.json")

# 明確看多 / 試單主訊號（與 quant_engine.py 四大正統策略的幣圈看多訊號一致）
BULLISH_SIGNALS = {
    "🟢 CTA 順勢突破買進",
}

SYMBOL_MAP = {"BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT", "SOL-USD": "SOLUSDT"}

# 技巧 109：內建安全預設精度表（LOT_SIZE stepSize / tickSize / minNotional）
FALLBACK_FILTERS = {
    "BTCUSDT": {"stepSize": "0.00001", "tickSize": "0.01", "minNotional": "10.0"},
    "ETHUSDT": {"stepSize": "0.0001", "tickSize": "0.01", "minNotional": "10.0"},
    "SOLUSDT": {"stepSize": "0.01", "tickSize": "0.01", "minNotional": "10.0"},
}

# 100% 純本金・零槓桿防護常數
CASH_BUFFER_PCT = 0.02
MAX_SINGLE_WEIGHT_PCT = 0.20
DEFAULT_CASH_USD = 100_000.0


# ===== 純本金現貨端點守門 =====
def assert_spot_only(url):
    """硬性攔截任何非現貨端點，避免誤用期貨/幣本位合約/槓桿借貸接口。"""
    if not url:
        raise ValueError("空 URL，拒絕執行")
    low = url.lower()
    for marker in FORBIDDEN_ENDPOINT_MARKERS:
        if marker in low:
            raise ValueError(
                f"🚫 拒絕呼叫非現貨端點（{marker}）：{url}。"
                f"本橋接器 100% 純本金現貨，嚴禁期貨/合約/槓桿借貸。"
            )
    if not low.startswith("https://api.binance.com/api/v3"):
        raise ValueError(f"🚫 拒絕呼叫現貨端點以外的 API：{url}")
    return True


def to_decimal(value):
    """安全轉 Decimal，失敗回傳 None。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_float(value):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ===== 技巧 110：本地與伺服器時間差同步 =====
def sync_server_time():
    """請求 /api/v3/time，回傳 (server_ms, offset_ms, error)。"""
    try:
        assert_spot_only(SPOT_TIME_URL)
        local_ms = int(time.time() * 1000)
        resp = requests.get(SPOT_TIME_URL, timeout=5)
        resp.raise_for_status()
        server_ms = int(resp.json().get("serverTime", 0))
        offset_ms = server_ms - local_ms
        return server_ms, offset_ms, None
    except Exception as exc:
        return None, 0, str(exc)


# ===== 技巧 109：LOT_SIZE / tickSize / minNotional 精度校正 =====
def round_step_size(qty, step_size):
    """依 stepSize 以 Decimal 無條件捨去（ROUND_DOWN）截斷顆數。"""
    q = to_decimal(qty)
    step = to_decimal(step_size)
    if q is None or step is None or step <= 0:
        return None
    return (q // step) * step


def round_tick_size(price, tick_size):
    """依 tickSize 以 Decimal 無條件捨去（ROUND_DOWN）截斷價格。"""
    p = to_decimal(price)
    tick = to_decimal(tick_size)
    if p is None or tick is None or tick <= 0:
        return None
    return (p // tick) * tick


def load_exchange_info():
    """唯讀請求 /api/v3/exchangeInfo，解析 symbol 的 LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL。"""
    try:
        assert_spot_only(SPOT_EXCHANGE_INFO_URL)
        resp = requests.get(SPOT_EXCHANGE_INFO_URL, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        filters_by_symbol = {}
        for s in payload.get("symbols") or []:
            sym = s.get("symbol")
            entry = {}
            for f in s.get("filters") or []:
                ft = f.get("filterType")
                if ft == "LOT_SIZE":
                    entry["stepSize"] = f.get("stepSize")
                    entry["minQty"] = f.get("minQty")
                elif ft == "PRICE_FILTER":
                    entry["tickSize"] = f.get("tickSize")
                elif ft == "MIN_NOTIONAL":
                    entry["minNotional"] = f.get("minNotional")
            if sym:
                filters_by_symbol[sym] = entry
        return filters_by_symbol
    except Exception as exc:
        print(f"    [warn] exchangeInfo 抓取失敗，改用內建安全預設表：{exc}")
        return None


def get_symbol_filters(pair, exchange_info):
    """取得 pair 的精度規則，缺漏時退回內建安全預設表。"""
    if exchange_info and pair in exchange_info:
        return exchange_info[pair]
    return FALLBACK_FILTERS.get(pair, FALLBACK_FILTERS.get("BTCUSDT", {}))


def load_signals():
    with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_crypto_assets(data):
    assets = data.get("assets") or {}
    for record in assets.get("Crypto") or []:
        yield record


# ===== 乾跑預覽：產出精度校正後純現貨委託計畫 =====
def build_spot_plan(records, cash_usd, time_sync, exchange_info):
    orders = []
    for it in records:
        symbol = it.get("symbol")
        pair = SYMBOL_MAP.get(symbol)
        if not pair:
            continue
        signal = it.get("signal") or ""
        weight = _to_float(it.get("suggested_weight_pct"))
        price = _to_float(it.get("price"))
        filters = get_symbol_filters(pair, exchange_info)
        step_size = filters.get("stepSize", "0.00001")
        tick_size = filters.get("tickSize", "0.01")
        min_notional = to_decimal(filters.get("minNotional")) or Decimal("10.0")

        base = {
            "symbol": symbol,
            "pair": pair,
            "signal": signal,
            "market_regime": it.get("market_regime"),
            "suggested_weight_pct": weight,
            "cta_vote_score": it.get("cta_vote_score"),
            "step_size": step_size,
            "tick_size": tick_size,
            "min_notional": str(min_notional),
        }

        if weight is None or weight <= 0:
            base["status"] = "skipped"
            base["reason"] = "減碼/空頭主跌/跌破防守止損或建議倉位 0%，禁止新建多單"
            orders.append(base)
            continue

        if signal not in BULLISH_SIGNALS:
            base["status"] = "skipped"
            base["reason"] = f"主訊號非明確看多（{signal}），不新建現貨買單"
            orders.append(base)
            continue

        if price is None or price <= 0:
            base["status"] = "blocked"
            base["reason"] = "缺少有效現價，無法試算"
            orders.append(base)
            continue

        alloc_usdt = Decimal(str(cash_usd)) * Decimal(str(weight / 100.0)) * Decimal(str(1 - CASH_BUFFER_PCT))
        raw_qty = alloc_usdt / Decimal(str(price))
        qty = round_step_size(raw_qty, step_size)
        limit_price = round_tick_size(price, tick_size)

        if qty is None or limit_price is None:
            base["status"] = "blocked"
            base["reason"] = "stepSize/tickSize 精度解析失敗"
            orders.append(base)
            continue

        notional = qty * limit_price
        base["raw_qty"] = str(raw_qty)
        base["rounded_qty"] = str(qty)
        base["raw_price"] = str(price)
        base["rounded_price"] = str(limit_price)
        base["notional_usdt"] = str(notional)

        if notional < min_notional:
            base["status"] = "blocked"
            base["reason"] = f"名目金額 {notional} USDT 低於最小名目金額 {min_notional} USDT（-1013 Filter failure 防護）"
            orders.append(base)
            continue

        cap_notional = Decimal(str(cash_usd)) * Decimal(str(MAX_SINGLE_WEIGHT_PCT))
        if notional > cap_notional:
            qty_capped = round_step_size(cap_notional / limit_price, step_size)
            base["rounded_qty"] = str(qty_capped)
            base["notional_usdt"] = str(qty_capped * limit_price)
            base["capped_20pct"] = True
            base["status"] = "preview"
            base["reason"] = "🛡️ 已套用單一標的 20% 防跳空上限"
        else:
            base["capped_20pct"] = False
            base["status"] = "preview"
            base["reason"] = "✅ 精度校正完成，委託預覽就緒（零槓桿純現貨）"

        orders.append(base)

    return {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "100% 純本金現貨 Spot Cash Only（零槓桿・零借貸・零放空）",
        "spot_base": BINANCE_SPOT_BASE,
        "forbidden_endpoints": list(FORBIDDEN_ENDPOINT_MARKERS),
        "time_sync": time_sync,
        "cash_usd": cash_usd,
        "orders": orders,
    }


def run_dry_run(args):
    data = load_signals()
    records = list(iter_crypto_assets(data))

    server_ms, offset_ms, sync_err = sync_server_time()
    time_sync = {
        "server_ms": server_ms,
        "offset_ms": offset_ms,
        "status": "🟢 時間同步完成" if sync_err is None else f"🟠 時間同步失敗（{sync_err}），offset_ms=0",
    }
    if args.sync_time:
        print(json.dumps(time_sync, ensure_ascii=False, indent=2))
        return

    exchange_info = load_exchange_info()
    plan = build_spot_plan(records, args.cash_usd, time_sync, exchange_info)

    for path in (PLAN_FILE, PLAN_FILE_ROOT):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

    print(f"✅ 純現貨委託預覽已輸出至 {PLAN_FILE} 與 {PLAN_FILE_ROOT}")
    print(f"   模式：{plan['mode']}")
    print(f"   時間同步：{time_sync['status']}（offset_ms={offset_ms}）")
    print("   委託預覽：")
    for o in plan["orders"]:
        if o["status"] == "preview":
            print(
                f"     {o['symbol']} {o['signal']}｜顆數 {o['rounded_qty']}（stepSize={o['step_size']}）"
                f"｜限價 {o['rounded_price']}（tickSize={o['tick_size']}）｜名目 {o['notional_usdt']} USDT"
            )
        else:
            print(f"     {o['symbol']} [{o['status']}] {o['reason']}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # 開機自檢：現貨端點白名單（嚴禁期貨/幣本位合約/槓桿借貸）
    assert_spot_only(BINANCE_SPOT_BASE)

    parser = argparse.ArgumentParser(description="Binance 100% 純本金・零槓桿現貨橋接器")
    parser.add_argument("--dry-run", action="store_true", help="產出精度校正純現貨委託預覽（預設）")
    parser.add_argument("--sync-time", action="store_true", help="唯讀同步本地與伺服器時間差")
    parser.add_argument("--cash-usd", type=float, default=DEFAULT_CASH_USD, help="離線預覽 USDT 本金")
    args = parser.parse_args()

    run_dry_run(args)


if __name__ == "__main__":
    main()
