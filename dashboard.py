"""
Phase 3/10 — Portfolio Dashboard + Analytics
Generates the main dashboard and PnL reports.
"""

import time, datetime
import storage
from jupiter import JupiterClient
from token_meta import get_tokens_meta


async def build_dashboard(user_id: int, wallet) -> tuple[str, list]:
    """Build main portfolio dashboard. Returns (text, buttons)."""
    held     = storage.get_holdings(user_id)
    jupiter  = JupiterClient()

    # Fetch all prices in parallel
    mints  = [h["mint"] for h in held] if held else []
    prices = {}
    metas  = {}
    if mints:
        import asyncio
        metas, price_results = await asyncio.gather(
            get_tokens_meta(mints),
            _fetch_prices_safe(jupiter, mints),
        )
        prices = price_results

    # Portfolio value
    total_value    = 0.0
    total_cost     = 0.0
    total_pnl      = 0.0
    holdings_lines = []

    for h in held:
        price = prices.get(h["mint"])
        meta  = metas.get(h["mint"]) or {"symbol": h["symbol"], "name": h["symbol"]}
        sym   = meta["symbol"] if "..." not in meta["symbol"] else h["symbol"]
        if price:
            val       = h["token_amount"] * price
            cost      = h["token_amount"] * h["avg_entry_usd"]
            pnl       = val - cost
            pct       = ((price - h["avg_entry_usd"]) / h["avg_entry_usd"] * 100) if h["avg_entry_usd"] else 0
            total_value += val
            total_cost  += cost
            total_pnl   += pnl
            arrow = "🟢" if pct >= 0 else "🔴"
            sign  = "+" if pct >= 0 else ""
            holdings_lines.append(
                f"{arrow} *{sym}* `{h['token_amount']:.3f}` — `${val:.3f}` ({sign}{pct:.1f}%)"
            )
        else:
            holdings_lines.append(f"⚪ *{sym}* `{h['token_amount']:.3f}`")

    # SOL balance
    sol_balance = 0.0
    try:
        bals        = await wallet.get_balances()
        sol_balance = float(bals.get("SOL", 0))
    except Exception:
        pass

    # Analytics
    now       = int(time.time())
    day_stats = storage.get_analytics(user_id, since_ts=now - 86400)
    all_stats = storage.get_analytics(user_id, since_ts=0)

    total_sign = "+" if total_pnl >= 0 else ""
    day_sign   = "+" if day_stats["total_pnl_sol"] >= 0 else ""

    lines = [
        "📊 *Portfolio Dashboard*\n",
        f"💰 SOL Balance: `{sol_balance:.4f} SOL`",
        f"💼 Token Value: `${total_value:.4f}`",
        f"📈 Unrealized PnL: `{total_sign}${total_pnl:.4f}`\n",
        f"📅 Today's PnL: `{day_sign}{day_stats['total_pnl_sol']:.4f} SOL`",
        f"🏆 Win Rate: `{all_stats['win_rate']:.1f}%` ({all_stats['wins']}W / {all_stats['losses']}L)",
        f"📋 Total Trades: `{all_stats['total_trades']}`\n",
    ]

    if holdings_lines:
        lines.append("*Holdings:*")
        lines.extend(holdings_lines)

    open_orders = storage.get_open_orders(user_id)
    if open_orders:
        lines.append(f"\n📋 Open Orders: `{len(open_orders)}`")

    tp_sl = storage.get_open_tp_sl(user_id)
    if tp_sl:
        lines.append(f"🎯 Active TP/SL: `{len(tp_sl)}`")

    dca = storage.get_active_dca(user_id)
    if dca:
        lines.append(f"🔄 Active DCA: `{len(dca)}`")

    return "\n".join(lines), held


async def build_analytics_report(user_id: int, period: str) -> str:
    """Build analytics report for given period: day/week/month/all"""
    now = int(time.time())
    periods = {"day": 86400, "week": 604800, "month": 2592000, "all": 0}
    since   = now - periods.get(period, 0) if period != "all" else 0
    stats   = storage.get_analytics(user_id, since_ts=since)
    label   = {"day": "Today", "week": "This Week", "month": "This Month", "all": "All Time"}[period]

    pnl_sign = "+" if stats["total_pnl_sol"] >= 0 else ""
    best_sym, best_pnl   = stats["best_trade"]
    worst_sym, worst_pnl = stats["worst_trade"]

    return (
        f"📊 *Analytics — {label}*\n\n"
        f"• Trades:    `{stats['total_trades']}`\n"
        f"• Buys:      `{stats['total_buy_sol']:.4f} SOL spent`\n"
        f"• Sells:     `{stats['total_sell_sol']:.4f} SOL received`\n"
        f"• PnL:       `{pnl_sign}{stats['total_pnl_sol']:.4f} SOL`\n"
        f"• Win Rate:  `{stats['win_rate']:.1f}%` ({stats['wins']}W / {stats['losses']}L)\n"
        + (f"• Best:      `+${best_pnl:.4f}` ({best_sym})\n" if best_sym else "")
        + (f"• Worst:     `-${abs(worst_pnl):.4f}` ({worst_sym})\n" if worst_sym else "")
    )


async def _fetch_prices_safe(jupiter, mints):
    import asyncio
    results = await asyncio.gather(*[jupiter.get_price(m) for m in mints], return_exceptions=True)
    return {m: r["price_usd"] for m, r in zip(mints, results) if not isinstance(r, Exception)}
