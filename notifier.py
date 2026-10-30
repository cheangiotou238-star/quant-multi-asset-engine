# -*- coding: utf-8 -*-
"""Discord 訊號推播工具。

讀取 .env 中的 DISCORD_WEBHOOK_URL，以及 ./public/data/latest_signals.json，
將六大資產類別的最新價格、RSI、均線趨勢、殖利率與買賣訊號，
排版成精美的 Discord Embed 報告發送到 Discord 頻道。
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
SIGNALS_FILE = os.path.join(".", "public", "data", "latest_signals.json")

# 香檳金
GOLD = 0xC9A227

CATEGORY_ICON = {
    "Crypto": "🪙",
    "Stocks": "📈",
    "REITs": "🏢",
    "Bonds": "📜",
    "ETFs": "🧺",
    "Gold": "🥇",
}


def load_signals():
    with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def fmt_num(v, decimals=2):
    if v is None:
        return "N/A"
    return f"{v:.{decimals}f}"


def fmt_change(v):
    if v is None:
        return "N/A"
    return f"{v:+.2f}%"


def fmt_yield(v):
    if v is None:
        return "N/A"
    return f"{v * 100:.2f}%"


def fmt_weight(v):
    """建議倉位權重（已為百分比數值，如 6.25 表示 6.25%）。"""
    if v is None:
        return "N/A"
    return f"{v:.2f}%"


def fmt_rate(v):
    """將勝率 / 累積報酬（小數）轉為百分比字串。"""
    if v is None:
        return "N/A"
    return f"{v * 100:.1f}%"


def fmt_pct_signed(v):
    """將小數報酬 / 回撤轉為帶正負號的百分比字串（-0.1234 -> -12.34%）。"""
    if v is None:
        return "N/A"
    return f"{v * 100:+.2f}%"


def fmt_ratio(v):
    """比率（夏普 / 獲利因子）格式化，保留 2 位小數。"""
    if v is None:
        return "N/A"
    return f"{v:.2f}"


def fmt_score(v):
    """五師綜合評分（0~100 整數）格式化。"""
    if v is None:
        return "N/A"
    return f"{int(round(v))}"


def fmt_vol_ratio(v):
    """量能倍數格式化（當日量 / 20 日均量）。"""
    if v is None:
        return "N/A"
    return f"{v:.2f}x"


def fmt_int(v):
    """整數（交易次數）格式化，None 回傳 N/A。"""
    if v is None:
        return "N/A"
    return str(v)


def ma_trend(ma20, ma60):
    if ma20 is None or ma60 is None:
        return "N/A"
    if ma20 > ma60:
        return "📈 多頭排列"
    if ma20 < ma60:
        return "📉 空頭排列"
    return "➖ 糾結"


def build_macro_block(macro):
    """組出「全球宏觀與市場情緒溫度計」文字區塊。"""
    if not macro:
        return None

    def num(v, suffix=""):
        if v is None:
            return "N/A"
        return f"{v:.2f}{suffix}"

    vix_val = macro.get("vix")
    vix_txt = f"{vix_val:.2f}" if vix_val is not None else "N/A"
    vix_zone = macro.get("vix_zone")
    fng = macro.get("crypto_fear_greed")
    fng_txt = f"{int(fng)}" if fng is not None else "N/A"
    fng_cls = macro.get("crypto_fear_greed_classification")

    lines = [
        f"🇺🇸 10年期美債殖利率：**{num(macro.get('us10y_yield'), '%')}**",
        f"💵 美元指數 DXY：**{num(macro.get('dxy'))}**",
        f"😨 VIX 恐慌指數：**{vix_txt}**" + (f"（{vix_zone}）" if vix_zone else ""),
        f"🪙 幣圈恐慌貪婪：**{fng_txt}**" + (f"（{fng_cls}）" if fng_cls else ""),
    ]
    return "\n".join(lines)


def build_leaderboard(data):
    """森口亮前段班：依五師綜合評分排序，挑出最高贏面標的。"""
    ranked = []
    for items in data.get("assets", {}).values():
        ranked.extend(items or [])
    ranked.sort(key=lambda it: -(it.get("master_score") or -1))
    top = ranked[:5]
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, it in enumerate(top):
        medal = medals[i] if i < len(medals) else f"#{i + 1}"
        lines.append(
            f"{medal} **{it.get('name')}**（{it.get('symbol')}）"
            f" 評分 **{fmt_score(it.get('master_score'))}**\n"
            f"　📦 {it.get('position_stage') or 'N/A'}｜"
            f"量能 **{fmt_vol_ratio(it.get('vol_ratio'))}**｜"
            f"移動停利 **{fmt_num(it.get('trailing_stop_price'), 2)}**"
        )
    return "\n".join(lines)


def build_embeds(data):
    embeds = []
    summary = data.get("summary", {})

    # 標題 Embed：產生時間 + 訊號分布摘要
    desc_lines = [f"🕒 產生時間：{data.get('generated_at', 'N/A')}", ""]
    signal_counts = summary.get("signal_counts", {})
    for sig, cnt in sorted(signal_counts.items(), key=lambda x: -x[1]):
        desc_lines.append(f"{sig}：**{cnt}** 檔")
    header = {
        "title": "📊 六大資產量化戰情室",
        "description": "\n".join(desc_lines),
        "color": GOLD,
        "footer": {"text": "Quant Multi-Asset Engine · 資料來源 Binance / Yahoo Finance / Local Cache"},
    }
    macro_block = build_macro_block(data.get("macro_overview"))
    header_fields = []
    if macro_block:
        header_fields.append({
            "name": "🌍 全球宏觀與市場情緒溫度計",
            "value": macro_block,
            "inline": False,
        })
    leaderboard = build_leaderboard(data)
    if leaderboard:
        header_fields.append({
            "name": "🏆 五師綜合評分前段班（森口亮 5 分鐘戰情室）",
            "value": leaderboard,
            "inline": False,
        })
    if header_fields:
        header["fields"] = header_fields
    embeds.append(header)

    # 每個資產類別一個 Embed
    for category, items in data.get("assets", {}).items():
        fields = []
        exit_rule_desc = None
        for it in items:
            name = (
                f"{it.get('signal', '')} "
                f"{it.get('name', '')}（{it.get('symbol', '')}）"
            )
            bt = it.get("backtest_strategy_1y") or {}
            # 出場法則為「領域」共用，顯示於類別 Embed 描述，避免每檔重複撐爆 6000 字限制
            exit_rule_desc = it.get("exit_rule_desc") or exit_rule_desc
            value = (
                f"💰 **{fmt_num(it.get('price'), 2)}**（{fmt_change(it.get('change_pct'))}）"
                f" RSI **{fmt_num(it.get('rsi14'), 2)}**\n"
                f"📈 {ma_trend(it.get('ma20'), it.get('ma60'))}"
                f" MA20 **{fmt_num(it.get('ma20'), 2)}** MA60 **{fmt_num(it.get('ma60'), 2)}**\n"
                f"📐 ATR **{fmt_num(it.get('atr14'), 2)}** MA200 **{fmt_num(it.get('ma200'), 2)}**"
                f" HIGH20 **{fmt_num(it.get('high20'), 2)}** 殖利 **{fmt_yield(it.get('dividend_yield'))}**\n"
                f"⏱ {it.get('horizon') or 'N/A'}｜🎯 買 **{fmt_num(it.get('ideal_buy_price'), 2)}**"
                f" 停損 **{fmt_num(it.get('stop_loss_price'), 2)}** 停利 **{fmt_num(it.get('take_profit_price'), 2)}**\n"
                f"🏆 勝率 **{fmt_rate(bt.get('win_rate'))}** 報酬 **{fmt_rate(bt.get('strategy_return_1y'))}**"
                f" 交易 **{fmt_int(bt.get('trade_count_1y'))}** 次\n"
                f"📉 最大回撤 **{fmt_pct_signed(bt.get('max_drawdown_pct'))}**"
                f" 夏普 **{fmt_ratio(bt.get('sharpe_ratio'))}**"
                f" 獲利因子 **{fmt_ratio(bt.get('profit_factor'))}**\n"
                f"🕰 近半年報酬 **{fmt_pct_signed(bt.get('oos_return_6m'))}**\n"
                f"🎯 五師評分 **{fmt_score(it.get('master_score'))}**｜"
                f"📦 {it.get('position_stage') or 'N/A'}\n"
                f"📈 量能倍數 **{fmt_vol_ratio(it.get('vol_ratio'))}**｜"
                f"🛡 移動停利 **{fmt_num(it.get('trailing_stop_price'), 2)}**\n"
                f"📦 建議倉位 **{fmt_weight(it.get('suggested_weight_pct'))}**｜"
                f"🚪 {bt.get('last_exit_reason') or 'N/A'}｜📊 {it.get('volume_status') or 'N/A'}\n"
                f"🔭 {it.get('nomad_signal') or '—'}｜短打 +1% **{fmt_num(it.get('nomad_tp_1pct'), 2)}** +3% **{fmt_num(it.get('nomad_tp_3pct'), 2)}** -5% **{fmt_num(it.get('nomad_sl_5pct'), 2)}**\n"
                f"🧓 茂爺爺 PBR **{fmt_num(it.get('pbr'), 2)}** 殖利 **{fmt_yield(it.get('dividend_yield'))}** 6大 **{fmt_int(it.get('fujimoto_6_checks'))}/6**｜遠藤洋 {it.get('endo_entry_rule') or '—'}\n"
                f"📝 {it.get('action_reason') or 'N/A'}"
            )
            fields.append({"name": name, "value": value, "inline": False})
        embeds.append({
            "title": f"{CATEGORY_ICON.get(category, '')} {category}",
            "description": f"📐 出場法則：{exit_rule_desc or 'N/A'}",
            "color": GOLD,
            "fields": fields,
        })

    return embeds


def send(embeds):
    payload = {"embeds": embeds}
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=30)
    resp.raise_for_status()
    return resp


def main():
    # 強制 UTF-8 輸出，避免 Windows cp950 主控台無法編碼 emoji
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
