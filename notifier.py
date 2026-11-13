# -*- coding: utf-8 -*-
"""Discord 訊號推播工具（De-bloat 版）。

讀取 .env 的 DISCORD_WEBHOOK_URL 與 ./public/data/latest_signals.json，
嚴格按照「四大正統策略分組」清爽輸出，不再印出跨資產硬套的欄位。

四大分組：
    📈 個股波段 (Stocks) → 遠藤洋 25MA 趨勢 / 茂爺爺三合一 / 森口亮估值 / ATR 止損
    🪙 加密 CTA (Crypto) → MA60 牛熊 / CTA 六策 / 4H+1D 共振 / 吊燈停損
    🏢 收息與債券 (Income) → 殖利率利差 / MA200 多空
    🌍 雙動能配置 (Macro) → SGOV 停泊比例 / 絕對動能 / 動能排名
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
SIGNALS_FILE = os.path.join(".", "public", "data", "latest_signals.json")

GOLD = 0xC9A227

GROUP_ICON = {
    "Stocks": "📈",
    "Crypto": "🪙",
    "Income": "🏢",
    "Macro": "🌍",
}
GROUP_TITLE = {
    "Stocks": "個股波段 (遠藤洋 × 茂爺爺 × 森口亮)",
    "Crypto": "加密 CTA (劉承彥《CTA 111 技》唐奇安突破)",
    "Income": "收息與債券 (無風險利差 × 200MA)",
    "Macro": "雙動能配置 (Gary Antonacci × Faber 200MA)",
}


def load_signals():
    with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def num(v, d=2):
    if v is None:
        return "N/A"
    return f"{v:,.{d}f}"


def pct(v):
    if v is None:
        return "N/A"
    return f"{v:+.2f}%"


def build_embeds(data):
    macro = data.get("macro_overview") or {}
    sgov_parking = data.get("sgov_parking_pct")
    embeds = []

    # 頂部巨觀與現金池
    header = (
        f"🏦 SGOV 現金池停泊比例 **{num(sgov_parking)}%**\n"
        f"10Y 美債 **{num(macro.get('us10y_yield'))}%**｜DXY **{num(macro.get('dxy'))}**｜"
        f"VIX **{num(macro.get('vix'))}** ({macro.get('vix_zone') or 'N/A'})\n"
        f"幣圈恐慌貪婪 **{macro.get('crypto_fear_greed')}** ({macro.get('crypto_fear_greed_classification') or 'N/A'})"
    )
    embeds.append({"title": "🌍 巨觀總覽與現金池", "description": header, "color": GOLD})

    assets = data.get("assets") or {}
    for group in ("Stocks", "Crypto", "Income", "Macro"):
        records = assets.get(group) or []
        if not records:
            continue
        lines = []
        for it in records:
            sym = it.get("symbol")
            price = num(it.get("price"))
            signal = it.get("signal") or "N/A"
            w = it.get("suggested_weight_pct")
            w_str = num(w) + "%" if w is not None else "N/A"
            line = f"**{sym}** {price}｜{signal}｜倉位 {w_str}"
            if group == "Stocks":
                line += (
                    f"\n    遠藤洋 25MA {it.get('endo_25ma_status') or 'N/A'}｜"
                    f"茂爺爺 {it.get('fujimoto_3in1_status') or 'N/A'}｜"
                    f"40% 法則 {num(it.get('rule_of_40'))}%｜目標價② {num(it.get('target_price_2'))}｜"
                    f"ATR 止損 {num(it.get('atr_defense_sl'))}"
                )
            elif group == "Crypto":
                line += (
                    f"\n    MA60 濾網 {it.get('d1_ma60_filter') or 'N/A'}｜"
                    f"CTA 六策 {it.get('cta_vote_score') or 'N/A'}｜"
                    f"4H+1D {it.get('mtf_4h_1d_resonance') or 'N/A'}｜"
                    f"Taker {num(it.get('taker_buy_ratio_5d'))}%｜"
                    f"吊燈停損 {num(it.get('chandelier_stop'))}"
                )
            elif group == "Income":
                line += (
                    f"\n    殖利率 {num(it.get('dividend_yield_pct'))}%｜"
                    f"利差 {pct(it.get('yield_spread'))}｜MA200 {it.get('ma200_position') or 'N/A'}｜"
                    f"止損 {num(it.get('current_defense_sl'))}"
                )
            else:  # Macro
                line += (
                    f"\n    MA200 {it.get('ma200_position') or 'N/A'}｜"
                    f"6月報酬 {pct(it.get('ret_6m_pct'))}｜"
                    f"相對 SGOV {pct(it.get('excess_return_vs_sgov'))}｜"
                    f"動能排名 Rank {it.get('momentum_rank') or 'N/A'}"
                )
            lines.append(line)
        embeds.append({
            "title": f"{GROUP_ICON.get(group, '')} {GROUP_TITLE.get(group, group)}",
            "description": "\n".join(lines),
            "color": GOLD,
        })

    return embeds


def send(embeds):
    payload = {"embeds": embeds}
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    resp.raise_for_status()
    return resp


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not WEBHOOK_URL:
        print("❌ 未設定 DISCORD_WEBHOOK_URL，請檢查 .env")
        return

    data = load_signals()
    embeds = build_embeds(data)
    print(f"準備發送 {len(embeds)} 個 Embed 至 Discord...")

    try:
        resp = send(embeds)
    except requests.exceptions.RequestException as exc:
        print(f"❌ 發送失敗：{exc}")
        return

    if resp.status_code in (200, 204):
        print("✅ 已成功發送至 Discord！")
    else:
        print(f"⚠️ Discord 回應狀態碼：{resp.status_code} - {resp.text}")


if __name__ == "__main__":
    main()
