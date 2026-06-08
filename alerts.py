"""
Phase 6 — Price Alerts + Phase 7 — TP/SL monitor
Runs as background tasks alongside the limit order monitor.
"""

import asyncio, logging, time
from telegram import Bot
from jupiter import JupiterClient
from solana_utils import SolanaWallet
import storage

logger = logging.getLogger(__name__)

# ── Price alerts ──────────────────────────────────────────────────────────────

async def monitor_alerts(bot: Bot):
    """Check price alerts every 20s."""
    logger.info("Alert monitor started")
    while True:
        try:
            alerts = storage.get_all_open_alerts()
            if alerts:
                jupiter = JupiterClient()
                mints   = list({a["mint"] for _, a in alerts})
                prices  = {}
                for mint in mints:
                    try:
                        p = await jupiter.get_price(mint)
                        prices[mint] = p["price_usd"]
                    except Exception:
                        pass
                for user_id, alert in alerts:
                    price = prices.get(alert["mint"])
                    if price is None:
                        continue
                    triggered = (
                        (alert["condition"] == "above" and price >= alert["target_price"]) or
                        (alert["condition"] == "below" and price <= alert["target_price"])
                    )
                    if triggered:
                        storage.close_alert(user_id, alert["id"])
                        cond = "📈 above" if alert["condition"] == "above" else "📉 below"
                        await bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🔔 *Price Alert!*\n\n"
                                f"*{alert['symbol']}* is now {cond} `${alert['target_price']:.10f}`\n"
                                f"Current: `${price:.10f}`"
                            ),
                            parse_mode="Markdown",
                        )
        except Exception as e:
            logger.error(f"Alert monitor error: {e}")
        await asyncio.sleep(20)


# ── TP/SL monitor ─────────────────────────────────────────────────────────────

async def monitor_tp_sl(bot: Bot):
    """Check take-profit and stop-loss every 15s."""
    logger.info("TP/SL monitor started")
    while True:
        try:
            tp_sl_orders = storage.get_all_open_tp_sl()
            if tp_sl_orders:
                jupiter = JupiterClient()
                mints   = list({o["mint"] for _, o in tp_sl_orders})
                prices  = {}
                for mint in mints:
                    try:
                        p = await jupiter.get_price(mint)
                        prices[mint] = p["price_usd"]
                    except Exception:
                        pass
                for user_id, order in tp_sl_orders:
                    price = prices.get(order["mint"])
                    if price is None:
                        continue
                    triggered = (
                        (order["type"] == "tp" and price >= order["target_price"]) or
                        (order["type"] == "sl" and price <= order["target_price"])
                    )
                    if triggered:
                        await _execute_tp_sl(bot, user_id, order, price, jupiter)
        except Exception as e:
            logger.error(f"TP/SL monitor error: {e}")
        await asyncio.sleep(15)


async def _execute_tp_sl(bot: Bot, user_id: int, order: dict, current_price: float, jupiter: JupiterClient):
    SOL_MINT = "So11111111111111111111111111111111111111112"
    try:
        pk = storage.load_wallet(user_id)
        if not pk:
            return
        wallet = SolanaWallet(pk)
        quote  = await jupiter.get_quote(
            input_mint=order["mint"],
            output_mint=SOL_MINT,
            amount_tokens=order["token_amount"],
            slippage_bps=order.get("slippage_bps", 200),
        )
        tx_sig = await jupiter.execute_swap(wallet, quote)
        storage.close_tp_sl(user_id, order["id"], "filled")
        storage.record_trade(
            user_id, "sell", order["symbol"], order["mint"],
            order["token_amount"], quote["out_amount_ui"], current_price, tx_sig
        )
        label  = "🎯 Take Profit" if order["type"] == "tp" else "🛡 Stop Loss"
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"{label} *triggered!*\n\n"
                f"• Token:  *{order['symbol']}*\n"
                f"• Sold:   `{order['token_amount']:.4f}`\n"
                f"• Price:  `${current_price:.10f}`\n"
                f"• Got:    `{quote['out_amount_ui']} SOL`\n"
                f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})"
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"TP/SL execute error {order['id']}: {e}")
        storage.close_tp_sl(user_id, order["id"], "failed")
        await bot.send_message(chat_id=user_id, text=f"❌ TP/SL execution failed: {e}")


# ── DCA monitor ───────────────────────────────────────────────────────────────

async def monitor_dca(bot: Bot):
    """Execute DCA orders when their interval is due."""
    logger.info("DCA monitor started")
    while True:
        try:
            now  = int(time.time())
            dcas = storage.get_due_dca_orders(now)
            if dcas:
                jupiter = JupiterClient()
                for user_id, dca in dcas:
                    await _execute_dca(bot, user_id, dca, jupiter)
        except Exception as e:
            logger.error(f"DCA monitor error: {e}")
        await asyncio.sleep(30)


async def _execute_dca(bot: Bot, user_id: int, dca: dict, jupiter: JupiterClient):
    SOL_MINT = "So11111111111111111111111111111111111111112"
    try:
        pk = storage.load_wallet(user_id)
        if not pk:
            return
        wallet = SolanaWallet(pk)
        quote  = await jupiter.get_quote(
            input_mint=SOL_MINT,
            output_mint=dca["mint"],
            amount_sol=dca["sol_per_order"],
            slippage_bps=dca.get("slippage_bps", 100),
        )
        tx_sig = await jupiter.execute_swap(wallet, quote)

        # Update DCA state
        new_count = dca["executed_count"] + 1
        remaining = dca["total_orders"] - new_count
        storage.update_dca_after_execution(dca["id"], new_count)

        storage.record_trade(
            user_id, "buy", dca["symbol"], dca["mint"],
            quote["out_amount_ui"], dca["sol_per_order"],
            (await jupiter.get_price(dca["mint"]))["price_usd"], tx_sig
        )

        status = f"({new_count}/{dca['total_orders']})"
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🔄 *DCA executed {status}*\n\n"
                f"• Bought: `{quote['out_amount_ui']} {dca['symbol']}`\n"
                f"• Spent:  `{dca['sol_per_order']} SOL`\n"
                f"• Remaining orders: `{remaining}`\n"
                f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})"
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

        if remaining <= 0:
            storage.close_dca(dca["id"], "completed")
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ *DCA completed!* All {dca['total_orders']} orders for *{dca['symbol']}* executed.",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"DCA execute error {dca['id']}: {e}")
        await bot.send_message(chat_id=user_id, text=f"❌ DCA order failed: {e}")
