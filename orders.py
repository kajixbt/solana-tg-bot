"""
Background task: monitors limit orders and executes them when price is hit.
Runs as a separate asyncio task inside the bot process.
"""

import asyncio
import logging
from telegram import Bot
from jupiter import JupiterClient
from solana_utils import SolanaWallet
import storage

logger = logging.getLogger(__name__)

async def monitor_orders(bot: Bot):
    """Poll prices every 15s and execute any triggered limit orders."""
    logger.info("Limit order monitor started")
    while True:
        try:
            open_orders = storage.get_all_open_orders()
            if open_orders:
                jupiter = JupiterClient()
                # Group by mint to minimize API calls
                mints = list({o["mint"] for _, o in open_orders})
                prices = {}
                for mint in mints:
                    try:
                        p = await jupiter.get_price(mint)
                        prices[mint] = p["price_usd"]
                    except Exception:
                        pass

                for user_id, order in open_orders:
                    mint  = order["mint"]
                    price = prices.get(mint)
                    if price is None:
                        continue

                    triggered = (
                        (order["side"] == "buy"  and price <= order["target_price_usd"]) or
                        (order["side"] == "sell" and price >= order["target_price_usd"])
                    )

                    if triggered:
                        await _execute_order(bot, user_id, order, price, jupiter)

        except Exception as e:
            logger.error(f"Order monitor error: {e}")

        await asyncio.sleep(15)


async def _execute_order(bot: Bot, user_id: int, order: dict, current_price: float, jupiter: JupiterClient):
    SOL_MINT = "So11111111111111111111111111111111111111112"
    try:
        pk = storage.load_wallet(user_id)
        if not pk:
            return
        wallet = SolanaWallet(pk)

        if order["side"] == "buy":
            quote = await jupiter.get_quote(
                input_mint=SOL_MINT,
                output_mint=order["mint"],
                amount_sol=order["sol_amount"],
                slippage_bps=order.get("slippage_bps", 100),
            )
        else:
            quote = await jupiter.get_quote(
                input_mint=order["mint"],
                output_mint=SOL_MINT,
                amount_tokens=order["token_amount"],
                slippage_bps=order.get("slippage_bps", 100),
            )

        tx_sig = await jupiter.execute_swap(wallet, quote)
        storage.close_order(user_id, order["id"], "filled")

        side_emoji = "📈" if order["side"] == "buy" else "📉"
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"{side_emoji} *Limit order filled!*\n\n"
                f"• {order['side'].title()}: `{order['symbol']}`\n"
                f"• Trigger price: `${order['target_price_usd']:.8f}`\n"
                f"• Fill price: `${current_price:.8f}`\n"
                f"🔗 [View on Solscan](https://solscan.io/tx/{tx_sig})"
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        logger.info(f"Limit order {order['id']} filled for user {user_id}")

    except Exception as e:
        logger.error(f"Failed to execute order {order['id']}: {e}")
        storage.close_order(user_id, order["id"], "failed")
        await bot.send_message(
            chat_id=user_id,
            text=f"❌ Limit order failed to execute: {e}",
        )
