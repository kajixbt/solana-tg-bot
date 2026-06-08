"""
Phase 8 — Copy Trading
Monitors target wallets on-chain and mirrors trades.
Uses Solana RPC getSignaturesForAddress + transaction parsing.
"""

import asyncio, logging, time
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from telegram import Bot
from jupiter import JupiterClient
from solana_utils import SolanaWallet, RPC_URL
import storage

logger = logging.getLogger(__name__)
SOL_MINT = "So11111111111111111111111111111111111111112"

# Track last seen signature per wallet to avoid duplicate copies
_last_sig: dict[str, str] = {}


async def monitor_copy_trading(bot: Bot):
    """Poll copy targets every 10s for new transactions."""
    logger.info("Copy trading monitor started")
    while True:
        try:
            targets = storage.get_all_copy_targets()
            if targets:
                jupiter = JupiterClient()
                for user_id, target in targets:
                    await _check_wallet(bot, user_id, target, jupiter)
        except Exception as e:
            logger.error(f"Copy trading error: {e}")
        await asyncio.sleep(10)


async def _check_wallet(bot: Bot, user_id: int, target: dict, jupiter: JupiterClient):
    wallet_addr = target["wallet_address"]
    try:
        async with AsyncClient(RPC_URL) as client:
            pubkey = Pubkey.from_string(wallet_addr)
            resp   = await client.get_signatures_for_address(pubkey, limit=5)
            sigs   = resp.value
            if not sigs:
                return

            latest = str(sigs[0].signature)
            last   = _last_sig.get(wallet_addr)

            if last is None:
                # First poll — just record, don't copy
                _last_sig[wallet_addr] = latest
                return

            if latest == last:
                return  # No new txns

            # New transactions found — process them
            new_sigs = []
            for sig_info in sigs:
                sig = str(sig_info.signature)
                if sig == last:
                    break
                new_sigs.append(sig)

            _last_sig[wallet_addr] = latest

            for sig in reversed(new_sigs):
                await _process_and_copy(bot, user_id, target, sig, jupiter, client)

    except Exception as e:
        logger.debug(f"Copy check failed for {wallet_addr[:8]}: {e}")


async def _process_and_copy(bot, user_id, target, sig, jupiter, client):
    """Parse tx, detect swap direction, mirror it."""
    try:
        resp = await client.get_transaction(
            sig, encoding="jsonParsed", max_supported_transaction_version=0
        )
        tx = resp.value
        if not tx or not tx.transaction:
            return

        # Detect token changes from pre/post token balances
        meta    = tx.transaction.meta
        if not meta:
            return

        pre  = {b.account_index: b for b in (meta.pre_token_balances  or [])}
        post = {b.account_index: b for b in (meta.post_token_balances or [])}

        # Find what increased (bought) and what decreased (sold)
        bought_mint = sold_mint = None
        for idx, pb in post.items():
            pre_amt  = float(pre.get(idx,  type('', (), {'ui_token_amount': type('', (), {'ui_amount': 0})()})()).ui_token_amount.ui_amount or 0)
            post_amt = float(pb.ui_token_amount.ui_amount or 0)
            diff     = post_amt - pre_amt
            if diff > 0 and pb.mint != SOL_MINT:
                bought_mint = pb.mint
            elif diff < 0 and pb.mint != SOL_MINT:
                sold_mint = pb.mint

        if not bought_mint and not sold_mint:
            return

        # Determine trade side
        if bought_mint and bought_mint != SOL_MINT:
            side = "buy"
            mint = bought_mint
        elif sold_mint and sold_mint != SOL_MINT:
            side = "sell"
            mint = sold_mint
        else:
            return

        # Check position sizing settings
        max_sol = target.get("max_sol_per_trade", 0.1)
        pk      = storage.load_wallet(user_id)
        if not pk:
            return

        my_wallet = SolanaWallet(pk)

        if side == "buy":
            await _copy_buy(bot, user_id, my_wallet, target, mint, max_sol, jupiter, sig)
        else:
            await _copy_sell(bot, user_id, my_wallet, target, mint, jupiter, sig)

    except Exception as e:
        logger.debug(f"Copy parse error {sig[:8]}: {e}")


async def _copy_buy(bot, user_id, wallet, target, mint, sol_amount, jupiter, orig_sig):
    from token_meta import get_token_meta
    try:
        meta  = await get_token_meta(mint)
        sym   = meta["symbol"]
        quote = await jupiter.get_quote(
            input_mint=SOL_MINT, output_mint=mint,
            amount_sol=sol_amount,
            slippage_bps=target.get("slippage_bps", 200),
        )
        tx_sig = await jupiter.execute_swap(wallet, quote)
        storage.record_trade(
            user_id, "buy", sym, mint,
            quote["out_amount_ui"], sol_amount, 0, tx_sig
        )
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🔄 *Copy Trade — BUY*\n\n"
                f"• Copied: `{target['label']}`\n"
                f"• Token:  *{meta['name']}* ({sym})\n"
                f"• Spent:  `{sol_amount} SOL`\n"
                f"• Got:    `{quote['out_amount_ui']} {sym}`\n"
                f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})"
            ),
            parse_mode="Markdown", disable_web_page_preview=True,
        )
    except Exception as e:
        await bot.send_message(chat_id=user_id, text=f"❌ Copy buy failed: {e}")


async def _copy_sell(bot, user_id, wallet, target, mint, jupiter, orig_sig):
    from token_meta import get_token_meta
    try:
        meta    = await get_token_meta(mint)
        sym     = meta["symbol"]
        balance = await wallet.get_token_balance(mint)
        if balance <= 0:
            return
        pct    = target.get("sell_percentage", 100)
        amount = balance * pct / 100
        quote  = await jupiter.get_quote(
            input_mint=mint, output_mint=SOL_MINT,
            amount_tokens=amount,
            slippage_bps=target.get("slippage_bps", 200),
        )
        tx_sig = await jupiter.execute_swap(wallet, quote)
        storage.record_trade(
            user_id, "sell", sym, mint,
            amount, quote["out_amount_ui"], 0, tx_sig
        )
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🔄 *Copy Trade — SELL*\n\n"
                f"• Copied: `{target['label']}`\n"
                f"• Token:  *{meta['name']}* ({sym})\n"
                f"• Sold:   `{amount:.4f} {sym}`\n"
                f"• Got:    `{quote['out_amount_ui']} SOL`\n"
                f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})"
            ),
            parse_mode="Markdown", disable_web_page_preview=True,
        )
    except Exception as e:
        await bot.send_message(chat_id=user_id, text=f"❌ Copy sell failed: {e}")
