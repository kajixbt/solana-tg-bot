"""
Solana Trading Bot — Phases 3-10
Dashboard, TP/SL, DCA, Alerts, Copy Trading, Analytics, Pump.fun
"""

import os, asyncio, logging, datetime, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from solana_utils import SolanaWallet
from jupiter import JupiterClient
from token_meta import get_token_meta, get_tokens_meta, preload_from_db
import storage
from orders import monitor_orders
from alerts import monitor_alerts, monitor_tp_sl, monitor_dca
from copy_trading import monitor_copy_trading
from dashboard import build_dashboard, build_analytics_report

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
SOL_MINT = "So11111111111111111111111111111111111111112"

# ── States ────────────────────────────────────────────────────────────────────
(S_BUY_TOKEN, S_BUY_AMOUNT,
 S_SELL_TOKEN, S_SELL_AMOUNT,
 S_IMP_KEY, S_WAL_LABEL,
 S_PIN_SET, S_PIN_CHECK,
 S_LIM_TOKEN, S_LIM_PRICE, S_LIM_AMOUNT,
 S_PRICE_TOKEN, S_WATCH_TOKEN,
 S_ALERT_TOKEN, S_ALERT_COND, S_ALERT_PRICE,
 S_TP_TOKEN, S_TP_PRICE,
 S_SL_TOKEN, S_SL_PRICE,
 S_DCA_TOKEN, S_DCA_AMOUNT, S_DCA_ORDERS, S_DCA_INTERVAL,
 S_COPY_ADDR, S_COPY_LABEL, S_COPY_SOL,
 S_PUMP_TOKEN) = range(28)

# ── Helpers ───────────────────────────────────────────────────────────────────

def uid(u: Update) -> int:
    return (u.message or u.callback_query).from_user.id

def get_wallet(ctx, user_id):
    pk = ctx.user_data.get("private_key") or storage.load_wallet(user_id)
    return SolanaWallet(pk) if pk else None

def pin_ok(ctx, user_id):
    return not storage.has_pin(user_id) or ctx.user_data.get("pin_ok", False)

def on(v): return "🟢" if v else "🔴"

def msg_of(u: Update):
    return u.message or u.callback_query.message

async def ack(u: Update):
    if u.callback_query:
        await u.callback_query.answer()

def main_kb(user_id=None, ab_enabled=False):
    ab_label = "🤖 Auto Buy: ON" if ab_enabled else "🤖 Auto Buy: OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard",  callback_data="dashboard"),
         InlineKeyboardButton("📈 Buy",        callback_data="buy"),
         InlineKeyboardButton("📉 Sell",       callback_data="sell")],
        [InlineKeyboardButton("💼 Holdings",   callback_data="holdings"),
         InlineKeyboardButton("🎯 TP/SL",      callback_data="tpsl_menu"),
         InlineKeyboardButton("🔄 DCA",        callback_data="dca_menu")],
        [InlineKeyboardButton("💱 Price",      callback_data="price_menu"),
         InlineKeyboardButton("👁 Watchlist",  callback_data="watchlist"),
         InlineKeyboardButton("🔔 Alerts",     callback_data="alerts_menu")],
        [InlineKeyboardButton("📋 Orders",     callback_data="orders"),
         InlineKeyboardButton("📜 History",    callback_data="history"),
         InlineKeyboardButton("📊 Analytics",  callback_data="analytics")],
        [InlineKeyboardButton("🔄 Copy Trade", callback_data="copy_menu"),
         InlineKeyboardButton("🔑 Wallets",    callback_data="wallets"),
         InlineKeyboardButton("⚙️ Settings",   callback_data="settings")],
        [InlineKeyboardButton(ab_label,        callback_data="autobuy_menu")],
    ])

BOT_COMMANDS = [
    BotCommand("start",      "Main menu"),
    BotCommand("dashboard",  "Portfolio dashboard"),
    BotCommand("buy",        "Buy a token"),
    BotCommand("sell",       "Sell a token"),
    BotCommand("holdings",   "Holdings & live PnL"),
    BotCommand("price",      "Token price"),
    BotCommand("tp",         "Set take profit"),
    BotCommand("sl",         "Set stop loss"),
    BotCommand("dca",        "Set up DCA"),
    BotCommand("alert",      "Set price alert"),
    BotCommand("alerts",     "View alerts"),
    BotCommand("watch",      "Add to watchlist"),
    BotCommand("watchlist",  "View watchlist"),
    BotCommand("orders",     "Limit orders"),
    BotCommand("limit",      "New limit order"),
    BotCommand("copy",       "Copy a wallet"),
    BotCommand("copylist",   "Copy trading list"),
    BotCommand("analytics",  "Trade analytics"),
    BotCommand("history",    "Trade history"),
    BotCommand("wallets",    "Manage wallets"),
    BotCommand("newwallet",  "Generate wallet"),
    BotCommand("importwallet","Import wallet"),
    BotCommand("settings",   "Settings"),
    BotCommand("setpin",     "Set PIN"),
    BotCommand("autobuy_setup", "Configure auto-buy"),
    BotCommand("autobuy",    "Toggle auto-buy ON/OFF"),
    BotCommand("help",       "Help"),
]

# ── /start ────────────────────────────────────────────────────────────────────

async def start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = uid(u)
    w = get_wallet(ctx, user_id)
    wl = f"✅ `{w.public_key[:8]}...{w.public_key[-4:]}`" if w else "⚠️ No wallet — /newwallet"
    ab_config = storage.get_auto_buy_config(user_id)
    ab_enabled = bool(ab_config and ab_config.get("enabled"))
    await u.message.reply_text(
        f"🤖 *Solana Trading Bot*\n\n{wl}\n\n_Jupiter • 0% bot fee_",
        parse_mode="Markdown", reply_markup=main_kb(user_id, ab_enabled))

async def start_from_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Return to main menu from a callback button"""
    await ack(u)
    user_id = uid(u)
    w = get_wallet(ctx, user_id)
    wl = f"✅ `{w.public_key[:8]}...{w.public_key[-4:]}`" if w else "⚠️ No wallet — /newwallet"
    ab_config = storage.get_auto_buy_config(user_id)
    ab_enabled = bool(ab_config and ab_config.get("enabled"))
    await u.callback_query.message.reply_text(
        f"🤖 *Solana Trading Bot*\n\n{wl}\n\n_Jupiter • 0% bot fee_",
        parse_mode="Markdown", reply_markup=main_kb(user_id, ab_enabled))

async def help_cmd(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "📖 *Commands*\n\n"
        "/dashboard — Portfolio overview\n/buy /sell — Trade tokens\n"
        "/holdings — Holdings + PnL\n/price <tok> — Price lookup\n"
        "/tp /sl — Take profit / Stop loss\n/dca — Dollar cost average\n"
        "/alert — Price alerts\n/watch — Watchlist\n"
        "/limit — Limit orders\n/copy — Copy trading\n"
        "/analytics — Trade stats\n/history — Trade history\n"
        "/wallets /newwallet /importwallet — Wallet mgmt\n"
        "/settings /setpin — Config",
        parse_mode="Markdown")

# ── Dashboard ─────────────────────────────────────────────────────────────────

async def dashboard(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u)
    user_id = uid(u)
    wallet  = get_wallet(ctx, user_id)
    if not wallet: await m.reply_text("⚠️ No wallet."); return
    await m.reply_text("⏳ Building dashboard...")
    try:
        text, held = await build_dashboard(user_id, wallet)
        buttons    = []
        for h in held:
            buttons.append([
                InlineKeyboardButton(f"📈 Buy more {h['symbol']}",  callback_data=f"quickbuy_{h['mint']}_{h['symbol']}"),
                InlineKeyboardButton(f"📉 Sell {h['symbol']}",      callback_data=f"selltoken_{h['mint']}_{h['symbol']}"),
            ])
        buttons.append([
            InlineKeyboardButton("🔄 Refresh", callback_data="dashboard"),
            InlineKeyboardButton("📊 Analytics", callback_data="analytics"),
        ])
        await m.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        await m.reply_text(f"❌ {e}")

# ── Analytics ─────────────────────────────────────────────────────────────────

async def analytics(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u)
    await m.reply_text(
        "📊 *Analytics* — pick period:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Today",      callback_data="analytics_day"),
            InlineKeyboardButton("This Week",  callback_data="analytics_week"),
            InlineKeyboardButton("This Month", callback_data="analytics_month"),
            InlineKeyboardButton("All Time",   callback_data="analytics_all"),
        ]])
    )

async def show_analytics(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    period = query.data.split("_")[1]
    try:
        text = await build_analytics_report(uid(u), period)
        await query.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await query.message.reply_text(f"❌ {e}")

# ── Wallet ────────────────────────────────────────────────────────────────────

async def new_wallet(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    w = SolanaWallet.generate()
    ctx.user_data.update(pending_key=w.private_key_b58, pending_pubkey=w.public_key)
    await u.message.reply_text(
        f"🆕 *New wallet*\n\n📬 `{w.public_key}`\n\n"
        f"🔑 *(Save & delete!)*\n`{w.private_key_b58}`\n\nLabel?",
        parse_mode="Markdown")
    return S_WAL_LABEL

async def import_wallet_cmd(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔑 Send private key:\n⚠️ _Delete after._", parse_mode="Markdown")
    return S_IMP_KEY

async def import_wallet_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        w = SolanaWallet(u.message.text.strip())
        ctx.user_data.update(pending_key=u.message.text.strip(), pending_pubkey=w.public_key)
        await u.message.delete()
        await u.message.reply_text(f"✅ `{w.public_key}`\n\nLabel?", parse_mode="Markdown")
        return S_WAL_LABEL
    except Exception as e:
        await u.message.reply_text(f"❌ {e}"); return ConversationHandler.END

async def wallet_label_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    label = u.message.text.strip()[:20]
    key   = ctx.user_data.pop("pending_key", None)
    pub   = ctx.user_data.pop("pending_pubkey", "")
    if not key: await u.message.reply_text("❌ Expired."); return ConversationHandler.END
    storage.save_wallet(uid(u), label, key)
    ctx.user_data["private_key"] = key
    await u.message.reply_text(f"✅ *{label}* saved!\n`{pub[:8]}...`", parse_mode="Markdown")
    return ConversationHandler.END

async def wallets_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u)
    wallets, active = storage.list_wallets(uid(u))
    if not wallets: await m.reply_text("No wallets. /newwallet"); return
    buttons = [[InlineKeyboardButton(("✅ " if l == active else "") + l,
                callback_data=f"sw_{l}")] for l in wallets]
    await m.reply_text("🔑 *Wallets:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def switch_wallet(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    label = query.data[3:]
    if storage.set_active_wallet(uid(u), label):
        ctx.user_data["private_key"] = storage.load_wallet(uid(u), label)
        await query.message.reply_text(f"✅ Switched to *{label}*", parse_mode="Markdown")

# ── PIN ───────────────────────────────────────────────────────────────────────

async def setpin_cmd(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔒 Enter 4+ digit PIN:"); return S_PIN_SET

async def pin_set_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pin = u.message.text.strip()
    if not pin.isdigit() or len(pin) < 4:
        await u.message.reply_text("❌ Min 4 digits."); return S_PIN_SET
    storage.set_pin(uid(u), pin); await u.message.delete()
    ctx.user_data["pin_ok"] = True
    await u.message.reply_text("✅ PIN set!"); return ConversationHandler.END

async def pin_check_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.delete()
    if storage.check_pin(uid(u), u.message.text.strip()):
        ctx.user_data["pin_ok"] = True
        await u.message.reply_text("✅ Correct. Retry action.")
    else:
        await u.message.reply_text("❌ Wrong PIN.")
    return ConversationHandler.END

# ── Balance ───────────────────────────────────────────────────────────────────

async def balance(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u)
    w = get_wallet(ctx, uid(u))
    if not w: await m.reply_text("⚠️ No wallet."); return
    await m.reply_text("⏳ Fetching...")
    try:
        bals = await w.get_balances()
        _, active = storage.list_wallets(uid(u))
        lines = [f"💼 *{active}*\n`{w.public_key}`\n"]
        for tok, amt in bals.items():
            lines.append(f"• *{tok}*: `{amt}`")
        await m.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await m.reply_text(f"❌ {e}")

# ── Holdings ──────────────────────────────────────────────────────────────────

async def holdings(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u)
    user_id = uid(u)
    held    = storage.get_holdings(user_id)
    if not held: await m.reply_text("💼 No holdings yet."); return
    await m.reply_text("⏳ Fetching prices...")
    jupiter = JupiterClient()
    mints   = [h["mint"] for h in held]
    metas, prices = await asyncio.gather(
        get_tokens_meta(mints),
        _prices(jupiter, mints),
    )
    lines = ["💼 *Holdings*\n"]; buttons = []
    for h in held:
        meta  = metas.get(h["mint"]) or {"symbol": h["symbol"], "name": h["symbol"]}
        sym   = meta["symbol"] if "..." not in meta["symbol"] else h["symbol"]
        name  = meta["name"]   if "..." not in meta["name"]   else sym
        price = prices.get(h["mint"])
        if price:
            pnl   = storage.get_pnl(user_id, h["mint"], price)
            unr   = pnl.get("unrealized_pnl", 0)
            pct   = pnl.get("pnl_pct", 0)
            sign  = "+" if pct >= 0 else ""
            arrow = "🟢" if pct >= 0 else "🔴"
            lines.append(
                f"{arrow} *{name}* `{sym}`\n"
                f"   `{h['token_amount']:.4f}` | Entry `${h['avg_entry_usd']:.8f}` | Now `${price:.8f}`\n"
                f"   PnL: `{sign}${unr:.4f}` ({sign}{pct:.2f}%) | Value: `${h['token_amount']*price:.4f}`\n"
            )
        else:
            lines.append(f"⚪ *{name}* `{sym}` — `{h['token_amount']:.4f}`\n")
        buttons.append([
            InlineKeyboardButton(f"📉 Sell {sym}",  callback_data=f"selltoken_{h['mint']}_{sym}"),
            InlineKeyboardButton(f"🎯 TP/SL",        callback_data=f"tpsl_token_{h['mint']}_{sym}"),
        ])
    await m.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def _prices(jupiter, mints):
    results = await asyncio.gather(*[jupiter.get_price(m) for m in mints], return_exceptions=True)
    return {m: r["price_usd"] for m, r in zip(mints, results) if not isinstance(r, Exception)}

# ── BUY ───────────────────────────────────────────────────────────────────────

async def buy_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u)
    await msg_of(u).reply_text("📈 *Buy* — token address or symbol:", parse_mode="Markdown")
    return S_BUY_TOKEN

async def buy_token_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from solana_utils import _resolve_mint
    await u.message.reply_text("🔍 Looking up...")
    mint = _resolve_mint(u.message.text.strip())
    meta = await get_token_meta(mint)
    ctx.user_data.update(buy_token=mint, buy_sym=meta["symbol"], buy_name=meta["name"])
    sol_bal = ""
    try:
        w = get_wallet(ctx, uid(u))
        if w:
            b = await w.get_balances()
            sol_bal = f"\n_Balance: {b.get('SOL','?')} SOL_"
    except Exception: pass
    await u.message.reply_text(
        f"📈 *{meta['name']} ({meta['symbol']})*{sol_bal}\n\nSOL to spend?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("0.1",    callback_data="bamt_0.1"),
            InlineKeyboardButton("0.25",   callback_data="bamt_0.25"),
            InlineKeyboardButton("0.5",    callback_data="bamt_0.5"),
        ],[
            InlineKeyboardButton("1",      callback_data="bamt_1.0"),
            InlineKeyboardButton("2",      callback_data="bamt_2.0"),
            InlineKeyboardButton("5",      callback_data="bamt_5.0"),
        ],[
            InlineKeyboardButton("Custom", callback_data="bamt_custom"),
        ]])
    )
    return S_BUY_AMOUNT

async def buy_amount_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if u.callback_query:
        await u.callback_query.answer()
        raw = u.callback_query.data.split("_")[1]; m = u.callback_query.message
        if raw == "custom": await m.reply_text("Type SOL amount:"); return S_BUY_AMOUNT
    else:
        raw = u.message.text.strip(); m = u.message
    user_id = uid(u)
    w = get_wallet(ctx, user_id)
    if not w: await m.reply_text("⚠️ No wallet."); return ConversationHandler.END
    if not pin_ok(ctx, user_id):
        ctx.user_data["_buy_raw"] = raw
        await m.reply_text("🔒 PIN:"); return S_PIN_CHECK
    try:
        sol   = float(raw)
        mint  = ctx.user_data["buy_token"]
        sym   = ctx.user_data.get("buy_sym", "?")
        name  = ctx.user_data.get("buy_name", "?")
        slip  = storage.get_setting(user_id, "slippage", 1.0)
        auto_confirm = storage.get_setting(user_id, "auto_confirm", False)
        await m.reply_text(f"🔍 Quote for {name}...")
        quote = await JupiterClient().get_quote(
            input_mint=SOL_MINT, output_mint=mint,
            amount_sol=sol, slippage_bps=int(slip * 100))
        ctx.user_data["pending_quote"] = quote

        # AUTO-CONFIRM: skip confirmation and execute immediately
        if auto_confirm:
            await m.reply_text(f"⚡ Auto-confirm ON — buying {name}...")
            tx_sig = None
            try:
                tx_sig = await JupiterClient().execute_swap(w, quote)
                price_usd = 0.0
                try: price_usd = (await JupiterClient().get_price(quote["output_mint"]))["price_usd"]
                except Exception: pass
                storage.record_trade(user_id, "buy", sym, quote["output_mint"],
                                     quote["out_amount_ui"], quote["in_amount_ui"], price_usd, tx_sig)
                await m.reply_text(
                    f"✅ *Bought {quote['out_amount_ui']} {sym}!*\n_{name}_\n\n"
                    f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
                    parse_mode="Markdown", disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🎯 Set TP/SL", callback_data=f"tpsl_token_{quote['output_mint']}_{sym}"),
                        InlineKeyboardButton("✖️ Skip",       callback_data="cancel"),
                    ]])
                )
            except Exception as e:
                if tx_sig:
                    await m.reply_text(f"⚠️ Sent!\n🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
                                       parse_mode="Markdown", disable_web_page_preview=True)
                else: await m.reply_text(f"❌ {e}")
            return ConversationHandler.END

        await m.reply_text(
            f"📊 *{name} ({sym})*\n\n"
            f"• Spend: `{sol} SOL`\n• Get: `{quote['out_amount_ui']} {sym}`\n"
            f"• Impact: `{quote['price_impact_pct']:.3f}%`\n• Slip: `{slip}%`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Buy",    callback_data="confirm_buy"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
            ]])
        )
    except Exception as e:
        await m.reply_text(f"❌ {e}")
    return ConversationHandler.END

async def confirm_buy(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    user_id = uid(u); w = get_wallet(ctx, user_id); quote = ctx.user_data.get("pending_quote")
    if not w or not quote: await query.message.reply_text("❌ Expired."); return
    sym  = ctx.user_data.get("buy_sym",  quote["out_symbol"])
    name = ctx.user_data.get("buy_name", sym)
    await query.message.reply_text(f"⏳ Buying {name}...")
    tx_sig = None
    try:
        tx_sig = await JupiterClient().execute_swap(w, quote)
        price_usd = 0.0
        try: price_usd = (await JupiterClient().get_price(quote["output_mint"]))["price_usd"]
        except Exception: pass
        storage.record_trade(user_id, "buy", sym, quote["output_mint"],
                             quote["out_amount_ui"], quote["in_amount_ui"], price_usd, tx_sig)
        # Ask if user wants to set TP/SL
        await query.message.reply_text(
            f"✅ *Bought {quote['out_amount_ui']} {sym}!*\n_{name}_\n\n"
            f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
            parse_mode="Markdown", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎯 Set TP/SL", callback_data=f"tpsl_token_{quote['output_mint']}_{sym}"),
                InlineKeyboardButton("✖️ Skip",       callback_data="cancel"),
            ]])
        )
    except Exception as e:
        if tx_sig:
            await query.message.reply_text(f"⚠️ Sent!\n🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
                                           parse_mode="Markdown", disable_web_page_preview=True)
        else: await query.message.reply_text(f"❌ {e}")

async def quickbuy(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Buy more of a token already in holdings."""
    query = u.callback_query; await query.answer()
    parts = query.data.split("_", 2)  # quickbuy_MINT_SYM
    mint, sym = parts[1], parts[2]
    meta = await get_token_meta(mint)
    ctx.user_data.update(buy_token=mint, buy_sym=meta["symbol"], buy_name=meta["name"])
    await query.message.reply_text(
        f"📈 *Buy more {meta['name']} ({meta['symbol']})*\n\nSOL to spend?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("0.1",  callback_data="bamt_0.1"),
            InlineKeyboardButton("0.25", callback_data="bamt_0.25"),
            InlineKeyboardButton("0.5",  callback_data="bamt_0.5"),
            InlineKeyboardButton("1",    callback_data="bamt_1.0"),
            InlineKeyboardButton("Custom", callback_data="bamt_custom"),
        ]])
    )

# ── SELL ──────────────────────────────────────────────────────────────────────

async def sell_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u); user_id = uid(u)
    held = storage.get_holdings(user_id)
    if held:
        await m.reply_text("⏳ Loading...")
        metas = await get_tokens_meta([h["mint"] for h in held])
        buttons = []
        for h in held:
            meta = metas.get(h["mint"]) or {"symbol": h["symbol"], "name": h["symbol"]}
            sym  = meta["symbol"] if "..." not in meta["symbol"] else h["symbol"]
            name = meta["name"]   if "..." not in meta["name"]   else sym
            buttons.append([InlineKeyboardButton(
                f"{name} ({sym}) — {h['token_amount']:.4f}",
                callback_data=f"selltoken_{h['mint']}_{sym}"
            )])
        buttons.append([InlineKeyboardButton("Enter address manually", callback_data="sell_manual")])
        await m.reply_text("📉 *Sell:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await m.reply_text("📉 Token address or symbol:"); return S_SELL_TOKEN
    return ConversationHandler.END

async def sell_token_selected(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    parts = query.data.split("_", 2)
    mint, sym = parts[1], parts[2]
    meta = await get_token_meta(mint)
    sym  = meta["symbol"] if "..." not in meta["symbol"] else sym
    name = meta["name"]   if "..." not in meta["name"]   else sym
    ctx.user_data.update(sell_token=mint, sell_sym=sym, sell_name=name)
    user_id = uid(u); w = get_wallet(ctx, user_id)
    bal = 0.0
    try:
        if w: bal = await w.get_token_balance(mint)
    except Exception:
        held = storage.get_holdings(user_id)
        bal  = next((h["token_amount"] for h in held if h["mint"] == mint), 0.0)
    ctx.user_data["sell_bal"] = bal
    await query.message.reply_text(
        f"📉 *{name} ({sym})*\nBalance: `{bal:.6f}`\n\nHow much?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("25%",    callback_data="samt_25"),
            InlineKeyboardButton("50%",    callback_data="samt_50"),
            InlineKeyboardButton("75%",    callback_data="samt_75"),
            InlineKeyboardButton("100%",   callback_data="samt_100"),
        ],[
            InlineKeyboardButton("Custom", callback_data="samt_custom"),
        ]])
    )

async def sell_token_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from solana_utils import _resolve_mint
    mint = _resolve_mint(u.message.text.strip())
    meta = await get_token_meta(mint)
    sym  = meta["symbol"]; name = meta["name"]
    ctx.user_data.update(sell_token=mint, sell_sym=sym, sell_name=name)
    bal = 0.0
    try:
        w = get_wallet(ctx, uid(u))
        if w: bal = await w.get_token_balance(mint)
    except Exception: pass
    ctx.user_data["sell_bal"] = bal
    await u.message.reply_text(
        f"📉 *{name} ({sym})*\nBalance: `{bal:.6f}`\n\nHow much?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("25%",  callback_data="samt_25"),
            InlineKeyboardButton("50%",  callback_data="samt_50"),
            InlineKeyboardButton("75%",  callback_data="samt_75"),
            InlineKeyboardButton("100%", callback_data="samt_100"),
        ]])
    )
    return S_SELL_AMOUNT

async def sell_amt_selected(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    if query.data == "samt_custom":
        await query.message.reply_text("Type amount:")
        ctx.user_data["_sell_custom"] = True; return
    pct = int(query.data.split("_")[1])
    amount = ctx.user_data.get("sell_bal", 0) * pct / 100
    user_id = uid(u)
    auto_confirm = storage.get_setting(user_id, "auto_confirm", False)
    if auto_confirm:
        token = ctx.user_data.get("sell_token")
        sym   = ctx.user_data.get("sell_sym", "?")
        name  = ctx.user_data.get("sell_name", sym)
        slip  = storage.get_setting(user_id, "slippage", 1.0)
        w = get_wallet(ctx, user_id)
        if not w: await query.message.reply_text("⚠️ No wallet."); return
        try:
            await query.message.reply_text(f"⚡ Auto-confirm ON — selling {pct}% {name}...")
            quote = await JupiterClient().get_quote(
                input_mint=token, output_mint=SOL_MINT,
                amount_tokens=amount, slippage_bps=int(slip * 100))
            ctx.user_data.update(pending_quote=quote, sell_amount=amount)
            tx_sig = None
            tx_sig = await JupiterClient().execute_swap(w, quote)
            price_usd = 0.0
            try: price_usd = (await JupiterClient().get_price(quote["input_mint"]))["price_usd"]
            except Exception: pass
            storage.record_trade(user_id, "sell", sym, quote["input_mint"],
                                 amount, quote["out_amount_ui"], price_usd, tx_sig)
            await query.message.reply_text(
                f"✅ *Sold {name}!*\nGot: `{quote['out_amount_ui']} SOL`\n\n"
                f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
                parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            if tx_sig:
                await query.message.reply_text(f"⚠️ Sent!\n🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
                                               parse_mode="Markdown", disable_web_page_preview=True)
            else: await query.message.reply_text(f"❌ {e}")
        return
    await _sell_quote(query.message, ctx, uid(u), amount)

async def sell_custom_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.pop("_sell_custom", False): return ConversationHandler.END
    try: amount = float(u.message.text.strip())
    except ValueError: await u.message.reply_text("❌ Invalid."); return ConversationHandler.END
    user_id = uid(u)
    auto_confirm = storage.get_setting(user_id, "auto_confirm", False)
    if auto_confirm:
        # Execute directly without showing confirmation prompt
        token = ctx.user_data.get("sell_token")
        sym   = ctx.user_data.get("sell_sym", "?")
        name  = ctx.user_data.get("sell_name", sym)
        slip  = storage.get_setting(user_id, "slippage", 1.0)
        w = get_wallet(ctx, user_id)
        if not w: await u.message.reply_text("⚠️ No wallet."); return ConversationHandler.END
        try:
            await u.message.reply_text(f"⚡ Auto-confirm ON — selling {name}...")
            quote = await JupiterClient().get_quote(
                input_mint=token, output_mint=SOL_MINT,
                amount_tokens=amount, slippage_bps=int(slip * 100))
            ctx.user_data.update(pending_quote=quote, sell_amount=amount)
            tx_sig = None
            tx_sig = await JupiterClient().execute_swap(w, quote)
            price_usd = 0.0
            try: price_usd = (await JupiterClient().get_price(quote["input_mint"]))["price_usd"]
            except Exception: pass
            storage.record_trade(user_id, "sell", sym, quote["input_mint"],
                                 amount, quote["out_amount_ui"], price_usd, tx_sig)
            await u.message.reply_text(
                f"✅ *Sold {name}!*\nGot: `{quote['out_amount_ui']} SOL`\n\n"
                f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
                parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            if tx_sig:
                await u.message.reply_text(f"⚠️ Sent!\n🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
                                           parse_mode="Markdown", disable_web_page_preview=True)
            else: await u.message.reply_text(f"❌ {e}")
        return ConversationHandler.END
    await _sell_quote(u.message, ctx, uid(u), amount)
    return ConversationHandler.END

async def _sell_quote(m, ctx, user_id, amount):
    token = ctx.user_data.get("sell_token")
    sym   = ctx.user_data.get("sell_sym", "?")
    name  = ctx.user_data.get("sell_name", sym)
    slip  = storage.get_setting(user_id, "slippage", 1.0)
    if not pin_ok(ctx, user_id):
        ctx.user_data["_sell_amount"] = amount; await m.reply_text("🔒 PIN:"); return
    try:
        await m.reply_text(f"🔍 Quote for {amount:.4f} {sym}...")
        quote = await JupiterClient().get_quote(
            input_mint=token, output_mint=SOL_MINT,
            amount_tokens=amount, slippage_bps=int(slip * 100))
        ctx.user_data.update(pending_quote=quote, sell_amount=amount)
        await m.reply_text(
            f"📊 *Sell {name} ({sym})*\n\n"
            f"• Sell: `{amount:.4f} {sym}`\n• Get: `{quote['out_amount_ui']} SOL`\n"
            f"• Impact: `{quote['price_impact_pct']:.3f}%`\n• Slip: `{slip}%`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Sell",    callback_data="confirm_sell"),
                InlineKeyboardButton("❌ Cancel",  callback_data="cancel"),
            ]])
        )
    except Exception as e:
        await m.reply_text(f"❌ {e}")

async def confirm_sell(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    user_id = uid(u); w = get_wallet(ctx, user_id); quote = ctx.user_data.get("pending_quote")
    if not w or not quote: await query.message.reply_text("❌ Expired."); return
    sym  = ctx.user_data.get("sell_sym",  quote["in_symbol"])
    name = ctx.user_data.get("sell_name", sym)
    await query.message.reply_text(f"⏳ Selling {name}...")
    tx_sig = None
    try:
        tx_sig = await JupiterClient().execute_swap(w, quote)
        price_usd = 0.0
        try: price_usd = (await JupiterClient().get_price(quote["input_mint"]))["price_usd"]
        except Exception: pass
        storage.record_trade(user_id, "sell", sym, quote["input_mint"],
                             ctx.user_data.get("sell_amount", quote["in_amount_ui"]),
                             quote["out_amount_ui"], price_usd, tx_sig)
        await query.message.reply_text(
            f"✅ *Sold {name}!*\nGot: `{quote['out_amount_ui']} SOL`\n\n"
            f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
            parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        if tx_sig:
            await query.message.reply_text(f"⚠️ Sent!\n🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
                                           parse_mode="Markdown", disable_web_page_preview=True)
        else: await query.message.reply_text(f"❌ {e}")

# ── Price ─────────────────────────────────────────────────────────────────────

async def price_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); await msg_of(u).reply_text("💱 Token address or symbol:")
    return S_PRICE_TOKEN

async def price_token_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from solana_utils import _resolve_mint
    try:
        mint    = _resolve_mint(u.message.text.strip())
        meta, p = await asyncio.gather(get_token_meta(mint), JupiterClient().get_price(mint))
        sym     = meta["symbol"] if "..." not in meta["symbol"] else p.get("symbol","?")
        await u.message.reply_text(
            f"💱 *{meta['name']} ({sym})*\n\n`${p['price_usd']:.10f}`",
            parse_mode="Markdown")
    except Exception as e:
        await u.message.reply_text(f"❌ {e}")
    return ConversationHandler.END

async def price_cmd(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from solana_utils import _resolve_mint
    if not ctx.args: await u.message.reply_text("Usage: /price <token>"); return
    try:
        mint    = _resolve_mint(ctx.args[0])
        meta, p = await asyncio.gather(get_token_meta(mint), JupiterClient().get_price(mint))
        sym     = meta["symbol"] if "..." not in meta["symbol"] else p.get("symbol","?")
        await u.message.reply_text(
            f"💱 *{meta['name']} ({sym})*\n`${p['price_usd']:.10f}`",
            parse_mode="Markdown")
    except Exception as e:
        await u.message.reply_text(f"❌ {e}")

# ── TP/SL ─────────────────────────────────────────────────────────────────────

async def tpsl_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u); user_id = uid(u)
    active = storage.get_open_tp_sl(user_id)
    held   = storage.get_holdings(user_id)
    lines  = ["🎯 *Take Profit / Stop Loss*\n"]
    if active:
        for o in active:
            t = "🎯 TP" if o["type"] == "tp" else "🛡 SL"
            lines.append(f"{t} *{o['symbol']}* @ `${o['target_price']:.10f}` — `{o['token_amount']:.4f}`")
    else:
        lines.append("_No active TP/SL orders_")
    buttons = []
    if held:
        for h in held:
            buttons.append([InlineKeyboardButton(
                f"🎯 Set TP/SL for {h['symbol']}",
                callback_data=f"tpsl_token_{h['mint']}_{h['symbol']}"
            )])
    buttons.append([InlineKeyboardButton("❌ Close all TP/SL", callback_data="tpsl_closeall")])
    await m.reply_text("\n".join(lines), parse_mode="Markdown",
                       reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)

async def tpsl_for_token(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    parts = query.data.split("_", 3)   # tpsl_token_MINT_SYM
    mint, sym = parts[2], parts[3]
    ctx.user_data.update(tpsl_mint=mint, tpsl_sym=sym)
    # Get current price and holdings
    try:
        p    = await JupiterClient().get_price(mint)
        held = storage.get_holdings(uid(u))
        bal  = next((h["token_amount"] for h in held if h["mint"] == mint), 0.0)
        pnl  = storage.get_pnl(uid(u), mint, p["price_usd"])
        entry = pnl.get("avg_entry_usd", 0)
        tp20  = round(entry * 1.20, 10)
        tp50  = round(entry * 1.50, 10)
        tp100 = round(entry * 2.00, 10)
        sl10  = round(entry * 0.90, 10)
        sl20  = round(entry * 0.80, 10)
        ctx.user_data["tpsl_bal"] = bal
        await query.message.reply_text(
            f"🎯 *TP/SL — {sym}*\n\n"
            f"Entry: `${entry:.10f}` | Now: `${p['price_usd']:.10f}`\n"
            f"Balance: `{bal:.4f} {sym}`\n\n"
            f"*Take Profit:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"TP +20% (${tp20:.8f})",  callback_data=f"settp_{mint}_{sym}_{tp20}"),
                 InlineKeyboardButton(f"TP +50% (${tp50:.8f})",  callback_data=f"settp_{mint}_{sym}_{tp50}"),
                 InlineKeyboardButton(f"TP 2x (${tp100:.8f})",   callback_data=f"settp_{mint}_{sym}_{tp100}")],
                [InlineKeyboardButton("Custom TP price",          callback_data=f"settp_custom_{mint}_{sym}")],
                [InlineKeyboardButton(f"SL -10% (${sl10:.8f})",  callback_data=f"setsl_{mint}_{sym}_{sl10}"),
                 InlineKeyboardButton(f"SL -20% (${sl20:.8f})",  callback_data=f"setsl_{mint}_{sym}_{sl20}")],
                [InlineKeyboardButton("Custom SL price",          callback_data=f"setsl_custom_{mint}_{sym}")],
            ])
        )
    except Exception as e:
        await query.message.reply_text(f"❌ {e}")

async def set_tp(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    parts = query.data.split("_")   # settp_MINT_SYM_PRICE
    if parts[1] == "custom":
        mint, sym = parts[2], parts[3]
        ctx.user_data.update(tp_mint=mint, tp_sym=sym)
        await query.message.reply_text(f"🎯 Enter TP price in USD for {sym}:")
        return S_TP_PRICE
    mint, sym, price = parts[1], parts[2], float(parts[3])
    await _save_tp(query.message, ctx, uid(u), mint, sym, price)

async def tp_price_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: price = float(u.message.text.strip())
    except ValueError: await u.message.reply_text("❌ Invalid."); return S_TP_PRICE
    mint = ctx.user_data.get("tp_mint"); sym = ctx.user_data.get("tp_sym", "?")
    await _save_tp(u.message, ctx, uid(u), mint, sym, price)
    return ConversationHandler.END

async def _save_tp(m, ctx, user_id, mint, sym, price):
    held    = storage.get_holdings(user_id)
    amount  = next((h["token_amount"] for h in held if h["mint"] == mint), 0.0)
    slip    = storage.get_setting(user_id, "slippage", 1.0)
    storage.add_tp_sl(user_id, mint, sym, "tp", amount, price, int(slip * 100))
    await m.reply_text(
        f"✅ *Take Profit set!*\n\n• {sym} | Sell `{amount:.4f}` @ `${price:.10f}`",
        parse_mode="Markdown")

async def set_sl(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    parts = query.data.split("_")   # setsl_MINT_SYM_PRICE
    if parts[1] == "custom":
        mint, sym = parts[2], parts[3]
        ctx.user_data.update(sl_mint=mint, sl_sym=sym)
        await query.message.reply_text(f"🛡 Enter SL price in USD for {sym}:")
        return S_SL_PRICE
    mint, sym, price = parts[1], parts[2], float(parts[3])
    await _save_sl(query.message, ctx, uid(u), mint, sym, price)

async def sl_price_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: price = float(u.message.text.strip())
    except ValueError: await u.message.reply_text("❌ Invalid."); return S_SL_PRICE
    mint = ctx.user_data.get("sl_mint"); sym = ctx.user_data.get("sl_sym", "?")
    await _save_sl(u.message, ctx, uid(u), mint, sym, price)
    return ConversationHandler.END

async def _save_sl(m, ctx, user_id, mint, sym, price):
    held   = storage.get_holdings(user_id)
    amount = next((h["token_amount"] for h in held if h["mint"] == mint), 0.0)
    slip   = storage.get_setting(user_id, "slippage", 1.0)
    storage.add_tp_sl(user_id, mint, sym, "sl", amount, price, int(slip * 100))
    await m.reply_text(
        f"✅ *Stop Loss set!*\n\n• {sym} | Sell `{amount:.4f}` @ `${price:.10f}`",
        parse_mode="Markdown")

# ── DCA ───────────────────────────────────────────────────────────────────────

async def dca_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u); user_id = uid(u)
    active = storage.get_active_dca(user_id)
    lines  = ["🔄 *DCA Orders*\n"]
    buttons = []
    if active:
        for d in active:
            rem = d["total_orders"] - d["executed_count"]
            lines.append(
                f"• *{d['symbol']}* — `{d['sol_per_order']} SOL` every `{d['interval_secs']//3600}h`\n"
                f"  Progress: `{d['executed_count']}/{d['total_orders']}` ({rem} remaining)"
            )
            buttons.append([InlineKeyboardButton(f"❌ Stop {d['symbol']} DCA",
                                                  callback_data=f"stopdca_{d['id']}")])
    else:
        lines.append("_No active DCA orders_")
    buttons.append([InlineKeyboardButton("➕ New DCA", callback_data="dca_new")])
    await m.reply_text("\n".join(lines), parse_mode="Markdown",
                       reply_markup=InlineKeyboardMarkup(buttons))

async def dca_new(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    await query.message.reply_text("🔄 *New DCA* — token address or symbol:", parse_mode="Markdown")
    return S_DCA_TOKEN

async def dca_token_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from solana_utils import _resolve_mint
    mint = _resolve_mint(u.message.text.strip())
    meta = await get_token_meta(mint)
    ctx.user_data.update(dca_mint=mint, dca_sym=meta["symbol"], dca_name=meta["name"])
    await u.message.reply_text(
        f"🔄 *DCA {meta['name']} ({meta['symbol']})*\n\nSOL per order? (e.g. `0.1`)",
        parse_mode="Markdown")
    return S_DCA_AMOUNT

async def dca_amount_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["dca_sol"] = float(u.message.text.strip())
    except ValueError: await u.message.reply_text("❌ Invalid."); return S_DCA_AMOUNT
    await u.message.reply_text("How many orders total? (e.g. `10`)")
    return S_DCA_ORDERS

async def dca_orders_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["dca_total"] = int(u.message.text.strip())
    except ValueError: await u.message.reply_text("❌ Invalid."); return S_DCA_ORDERS
    await u.message.reply_text(
        "Interval between orders?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("1 hour",   callback_data="dcaiv_3600"),
            InlineKeyboardButton("4 hours",  callback_data="dcaiv_14400"),
            InlineKeyboardButton("12 hours", callback_data="dcaiv_43200"),
            InlineKeyboardButton("1 day",    callback_data="dcaiv_86400"),
            InlineKeyboardButton("1 week",   callback_data="dcaiv_604800"),
        ]])
    )
    return S_DCA_INTERVAL

async def dca_interval_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if u.callback_query:
        await u.callback_query.answer()
        secs = int(u.callback_query.data.split("_")[1]); m = u.callback_query.message
    else:
        try: secs = int(u.message.text.strip()) * 3600
        except ValueError: await u.message.reply_text("❌ Invalid."); return S_DCA_INTERVAL
        m = u.message
    user_id = uid(u)
    mint  = ctx.user_data["dca_mint"]; sym = ctx.user_data["dca_sym"]
    sol   = ctx.user_data["dca_sol"];  total = ctx.user_data["dca_total"]
    slip  = storage.get_setting(user_id, "slippage", 1.0)
    storage.add_dca_order(user_id, mint, sym, sol, total, secs, int(slip * 100))
    hours = secs // 3600
    await m.reply_text(
        f"✅ *DCA set up!*\n\n"
        f"• Token: *{ctx.user_data['dca_name']}* ({sym})\n"
        f"• Amount: `{sol} SOL` per order\n"
        f"• Orders: `{total}` total\n"
        f"• Interval: every `{hours}h`\n"
        f"• Total: `{sol * total} SOL`",
        parse_mode="Markdown")
    return ConversationHandler.END

async def stop_dca(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    dca_id = int(query.data.split("_")[1])
    storage.close_dca(dca_id, "cancelled")
    await query.message.reply_text("✅ DCA stopped.")

# ── Alerts ────────────────────────────────────────────────────────────────────

async def alerts_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u); user_id = uid(u)
    alerts = storage.get_open_alerts(user_id)
    lines  = ["🔔 *Price Alerts*\n"]
    buttons = []
    if alerts:
        for a in alerts:
            cond = "📈 >" if a["condition"] == "above" else "📉 <"
            lines.append(f"{cond} *{a['symbol']}* `${a['target_price']:.10f}`")
            buttons.append([InlineKeyboardButton(f"❌ Delete {a['symbol']} alert",
                                                  callback_data=f"delalert_{a['id']}")])
    else:
        lines.append("_No active alerts_")
    buttons.append([InlineKeyboardButton("➕ New alert", callback_data="alert_new")])
    await m.reply_text("\n".join(lines), parse_mode="Markdown",
                       reply_markup=InlineKeyboardMarkup(buttons))

async def alert_new(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    await query.message.reply_text("🔔 *New Alert* — token address or symbol:", parse_mode="Markdown")
    return S_ALERT_TOKEN

async def alert_token_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from solana_utils import _resolve_mint
    mint = _resolve_mint(u.message.text.strip())
    meta = await get_token_meta(mint)
    ctx.user_data.update(alert_mint=mint, alert_sym=meta["symbol"])
    try:
        p = await JupiterClient().get_price(mint)
        current = f"\nCurrent: `${p['price_usd']:.10f}`"
    except Exception: current = ""
    await u.message.reply_text(
        f"🔔 *{meta['name']} ({meta['symbol']})*{current}\n\nAlert when price is:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📈 Above", callback_data="alertcond_above"),
            InlineKeyboardButton("📉 Below", callback_data="alertcond_below"),
        ]])
    )
    return S_ALERT_COND

async def alert_cond_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    ctx.user_data["alert_cond"] = query.data.split("_")[1]
    sym = ctx.user_data.get("alert_sym", "token")
    await query.message.reply_text(f"💵 Target price in USD for {sym}?")
    return S_ALERT_PRICE

async def alert_price_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: price = float(u.message.text.strip())
    except ValueError: await u.message.reply_text("❌ Invalid."); return S_ALERT_PRICE
    user_id = uid(u)
    mint    = ctx.user_data["alert_mint"]
    sym     = ctx.user_data["alert_sym"]
    cond    = ctx.user_data["alert_cond"]
    storage.add_alert(user_id, mint, sym, cond, price)
    arrow = "📈 above" if cond == "above" else "📉 below"
    await u.message.reply_text(
        f"✅ *Alert set!*\n\nNotify when *{sym}* goes {arrow} `${price:.10f}`",
        parse_mode="Markdown")
    return ConversationHandler.END

async def del_alert(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    storage.delete_alert(uid(u), int(query.data.split("_")[1]))
    await query.message.reply_text("✅ Alert deleted.")

# ── Watchlist ─────────────────────────────────────────────────────────────────

async def watch_cmd(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        await _add_watch(u, ctx.args[0]); return ConversationHandler.END
    await u.message.reply_text("👁 Token address or symbol:"); return S_WATCH_TOKEN

async def watch_token_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _add_watch(u, u.message.text.strip()); return ConversationHandler.END

async def _add_watch(u, raw):
    from solana_utils import _resolve_mint
    try:
        mint = _resolve_mint(raw); meta = await get_token_meta(mint)
        storage.add_to_watchlist(uid(u), mint, meta["symbol"], meta["name"])
        await msg_of(u).reply_text(f"👁 *{meta['name']} ({meta['symbol']})* added!", parse_mode="Markdown")
    except Exception as e:
        await msg_of(u).reply_text(f"❌ {e}")

async def watchlist_cmd(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u); user_id = uid(u)
    wl = storage.get_watchlist(user_id)
    if not wl: await m.reply_text("👁 Watchlist empty.\n/watch <token>"); return
    await m.reply_text("⏳ Fetching prices...")
    prices = await _prices(JupiterClient(), [w["mint"] for w in wl])
    lines  = ["👁 *Watchlist*\n"]; buttons = []
    for w in wl:
        p = prices.get(w["mint"])
        pline = f"`${p:.10f}`" if p else "_n/a_"
        lines.append(f"• *{w['name']}* ({w['symbol']}) — {pline}")
        buttons.append([
            InlineKeyboardButton(f"📈 Buy {w['symbol']}",    callback_data=f"wbuy_{w['mint']}_{w['symbol']}"),
            InlineKeyboardButton(f"🔔 Alert",                callback_data=f"walert_{w['mint']}_{w['symbol']}"),
            InlineKeyboardButton(f"❌ Remove",               callback_data=f"wremove_{w['mint']}"),
        ])
    await m.reply_text("\n".join(lines), parse_mode="Markdown",
                       reply_markup=InlineKeyboardMarkup(buttons))

async def watchlist_buy(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    parts = query.data.split("_", 2); mint, sym = parts[1], parts[2]
    meta  = await get_token_meta(mint)
    ctx.user_data.update(buy_token=mint, buy_sym=meta["symbol"], buy_name=meta["name"])
    await query.message.reply_text(
        f"📈 *Buy {meta['name']}*\n\nSOL to spend?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("0.1",  callback_data="bamt_0.1"),
            InlineKeyboardButton("0.5",  callback_data="bamt_0.5"),
            InlineKeyboardButton("1",    callback_data="bamt_1.0"),
            InlineKeyboardButton("Custom", callback_data="bamt_custom"),
        ]])
    )

async def watchlist_alert(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    parts = query.data.split("_", 2); mint, sym = parts[1], parts[2]
    ctx.user_data.update(alert_mint=mint, alert_sym=sym)
    await query.message.reply_text(
        f"🔔 Alert for *{sym}* when price is:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📈 Above", callback_data="alertcond_above"),
            InlineKeyboardButton("📉 Below", callback_data="alertcond_below"),
        ]])
    )
    return S_ALERT_PRICE

async def watchlist_remove(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    storage.remove_from_watchlist(uid(u), query.data.split("_")[1])
    await query.message.reply_text("✅ Removed.")

# ── Copy Trading ──────────────────────────────────────────────────────────────

async def copy_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u); user_id = uid(u)
    targets = storage.get_copy_targets(user_id)
    lines   = ["🔄 *Copy Trading*\n"]
    buttons = []
    if targets:
        for t in targets:
            lines.append(
                f"• *{t['label']}*\n"
                f"  `{t['wallet_address'][:8]}...{t['wallet_address'][-4:]}`\n"
                f"  Max: `{t['max_sol_per_trade']} SOL` | Sell: `{t['sell_percentage']}%`"
            )
            buttons.append([InlineKeyboardButton(f"❌ Stop copying {t['label']}",
                                                  callback_data=f"stopcopy_{t['wallet_address']}")])
    else:
        lines.append("_Not copying anyone_")
    buttons.append([InlineKeyboardButton("➕ Copy a wallet", callback_data="copy_new")])
    await m.reply_text("\n".join(lines), parse_mode="Markdown",
                       reply_markup=InlineKeyboardMarkup(buttons))

async def copy_new(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    await query.message.reply_text(
        "🔄 *Copy Wallet*\n\nSend the Solana wallet address to copy:",
        parse_mode="Markdown")
    return S_COPY_ADDR

async def copy_addr_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    addr = u.message.text.strip()
    try:
        from solders.pubkey import Pubkey
        Pubkey.from_string(addr)  # validate
        ctx.user_data["copy_addr"] = addr
        await u.message.reply_text("Label for this wallet? (e.g. `whale1`)")
        return S_COPY_LABEL
    except Exception:
        await u.message.reply_text("❌ Invalid Solana address."); return S_COPY_ADDR

async def copy_label_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["copy_label"] = u.message.text.strip()[:20]
    await u.message.reply_text("Max SOL per copied trade? (e.g. `0.1`)\n_Limits your exposure per trade._",
                               parse_mode="Markdown")
    return S_COPY_SOL

async def copy_sol_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: max_sol = float(u.message.text.strip())
    except ValueError: await u.message.reply_text("❌ Invalid."); return S_COPY_SOL
    user_id = uid(u)
    addr    = ctx.user_data["copy_addr"]
    label   = ctx.user_data["copy_label"]
    if storage.add_copy_target(user_id, addr, label, max_sol):
        await u.message.reply_text(
            f"✅ *Now copying {label}!*\n\n"
            f"Address: `{addr[:8]}...{addr[-4:]}`\n"
            f"Max per trade: `{max_sol} SOL`\n\n"
            f"_Bot will mirror their buys and sells automatically._",
            parse_mode="Markdown")
    else:
        await u.message.reply_text("❌ Already copying this wallet.")
    return ConversationHandler.END

async def stop_copy(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    storage.remove_copy_target(uid(u), query.data.split("_")[1])
    await query.message.reply_text("✅ Stopped copying.")

# ── Limit orders ──────────────────────────────────────────────────────────────

async def limit_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u)
    await msg_of(u).reply_text("📌 *Limit Order* — token address or symbol:", parse_mode="Markdown")
    return S_LIM_TOKEN

async def limit_token_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from solana_utils import _resolve_mint
    mint = _resolve_mint(u.message.text.strip())
    meta = await get_token_meta(mint)
    ctx.user_data.update(lim_mint=mint, lim_sym=meta["symbol"], lim_name=meta["name"])
    await u.message.reply_text(
        f"📌 *{meta['name']} ({meta['symbol']})*\n\nTarget price in USD?", parse_mode="Markdown")
    return S_LIM_PRICE

async def limit_price_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["lim_price"] = float(u.message.text.strip())
    except ValueError: await u.message.reply_text("❌ Invalid."); return S_LIM_PRICE
    sym = ctx.user_data.get("lim_sym", "tokens")
    await u.message.reply_text(f"`buy 0.1` — spend SOL  or  `sell 50000` — sell {sym}:", parse_mode="Markdown")
    return S_LIM_AMOUNT

async def limit_amount_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = u.message.text.strip().lower().split()
    if len(raw) != 2 or raw[0] not in ("buy", "sell"):
        await u.message.reply_text("❌ Format: `buy 0.1` or `sell 50000`", parse_mode="Markdown")
        return S_LIM_AMOUNT
    side, amount = raw[0], float(raw[1])
    user_id = uid(u)
    mint    = ctx.user_data["lim_mint"]
    sym     = ctx.user_data.get("lim_sym","?")
    name    = ctx.user_data.get("lim_name", sym)
    storage.add_limit_order(user_id, {
        "side": side, "mint": mint, "symbol": sym,
        "sol_amount":       amount if side == "buy"  else 0,
        "token_amount":     amount if side == "sell" else 0,
        "target_price_usd": ctx.user_data["lim_price"],
        "slippage_bps":     int(storage.get_setting(user_id, "slippage", 1.0) * 100),
    })
    await u.message.reply_text(
        f"✅ *Limit order set!*\n\n• *{name}* ({sym})\n• {side.upper()} `{amount}` @ `${ctx.user_data['lim_price']:.10f}`",
        parse_mode="Markdown")
    return ConversationHandler.END

async def orders_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u); orders = storage.get_open_orders(uid(u))
    if not orders:
        await m.reply_text("📋 No orders.\n/limit to place one.",
                           reply_markup=InlineKeyboardMarkup([[
                               InlineKeyboardButton("➕ New", callback_data="limit")
                           ]]))
        return
    lines = ["📋 *Open orders*\n"]; buttons = []
    for o in orders:
        side = "🟢 BUY" if o["side"] == "buy" else "🔴 SELL"
        lines.append(f"{side} *{o['symbol']}* @ `${o['target_price_usd']:.10f}`")
        buttons.append([InlineKeyboardButton(f"❌ Cancel {o['symbol']}", callback_data=f"cancelorder_{o['id']}")])
    await m.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def cancel_order(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    storage.close_order(uid(u), int(query.data.split("_")[1]), "cancelled")
    await query.message.reply_text("✅ Cancelled.")

# ── History ───────────────────────────────────────────────────────────────────

async def history(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u); user_id = uid(u)
    trades = storage.get_trades(user_id, limit=10)
    if not trades: await m.reply_text("📜 No trades yet."); return
    metas = await get_tokens_meta(list({t["mint"] for t in trades}))
    lines = ["📜 *Last 10 trades*\n"]
    for t in trades:
        meta = metas.get(t["mint"]) or {"symbol": t["symbol"], "name": t["symbol"]}
        sym  = meta["symbol"] if "..." not in meta["symbol"] else t["symbol"]
        name = meta["name"]   if "..." not in meta["name"]   else sym
        dt   = datetime.datetime.fromtimestamp(t["ts"]).strftime("%m/%d %H:%M")
        side = "🟢 BUY" if t["side"] == "buy" else "🔴 SELL"
        lines.append(
            f"{side} *{name}* ({sym})\n"
            f"   `{t['token_amount']:.4f}` | `{t['sol_amount']:.4f} SOL` | _{dt}_\n"
            f"   [tx](https://solscan.io/tx/{t['tx']})")
    await m.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

# ── PnL ───────────────────────────────────────────────────────────────────────

async def pnl_menu(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u)
    held  = storage.get_holdings(uid(u))
    if not held: await m.reply_text("📊 No holdings."); return
    metas = await get_tokens_meta([h["mint"] for h in held])
    buttons = [[InlineKeyboardButton(
        f"{metas.get(h['mint'],{'name':h['symbol']})['name']} ({metas.get(h['mint'],{'symbol':h['symbol']})['symbol']})",
        callback_data=f"pnl_{h['mint']}"
    )] for h in held]
    await m.reply_text("📊 *PnL:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def show_pnl(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    mint  = query.data[4:]
    try:
        meta, p = await asyncio.gather(get_token_meta(mint), JupiterClient().get_price(mint))
        sym     = meta["symbol"] if "..." not in meta["symbol"] else p.get("symbol","?")
        pnl     = storage.get_pnl(uid(u), mint, p["price_usd"])
        if not pnl: await query.message.reply_text("No data."); return
        sign  = "+" if pnl["unrealized_pnl"] >= 0 else ""
        emoji = "🟢" if pnl["pnl_pct"] >= 0 else "🔴"
        await query.message.reply_text(
            f"📊 *{meta['name']} ({sym})*\n\n"
            f"{emoji} `{sign}{pnl['pnl_pct']:.2f}%`\n"
            f"• Entry:  `${pnl['avg_entry_usd']:.10f}`\n"
            f"• Now:    `${pnl['current_price_usd']:.10f}`\n"
            f"• Hold:   `{pnl['remaining_tokens']:.4f} {sym}`\n"
            f"• Unreal: `{sign}${pnl['unrealized_pnl']:.4f}`\n"
            f"• Real:   `{'+'if pnl['realized_pnl']>=0 else ''}${pnl['realized_pnl']:.4f}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎯 Set TP/SL", callback_data=f"tpsl_token_{mint}_{sym}"),
                InlineKeyboardButton("📉 Sell",       callback_data=f"selltoken_{mint}_{sym}"),
            ]])
        )
    except Exception as e:
        await query.message.reply_text(f"❌ {e}")

# ── Settings ──────────────────────────────────────────────────────────────────

async def settings(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u); m = msg_of(u); user_id = uid(u)
    s    = storage.get_all_settings(user_id)
    slip = s.get("slippage", 1.0)
    pin  = "✅ Set" if storage.has_pin(user_id) else "❌ Not set"
    await m.reply_text(
        f"⚙️ *Settings*\n\n"
        f"Slippage: `{slip}%` | PIN: {pin}\n\n"
        f"{on(s['mev_protect'])} MEV Protection\n"
        f"{on(s['auto_confirm'])} Auto Confirm\n"
        f"{on(s['smart_slippage'])} Smart Slippage\n"
        f"{on(s['price_alerts'])} Price Alerts\n"
        f"{on(s['notifications'])} Notifications",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("0.5%",  callback_data="slip_0.5"),
             InlineKeyboardButton("1%",    callback_data="slip_1.0"),
             InlineKeyboardButton("2%",    callback_data="slip_2.0"),
             InlineKeyboardButton("5%",    callback_data="slip_5.0"),
             InlineKeyboardButton("10%",   callback_data="slip_10.0")],
            [InlineKeyboardButton(f"{on(s['mev_protect'])} MEV",       callback_data="toggle_mev_protect"),
             InlineKeyboardButton(f"{on(s['auto_confirm'])} AutoConf",  callback_data="toggle_auto_confirm")],
            [InlineKeyboardButton(f"{on(s['smart_slippage'])} SmartSlip",callback_data="toggle_smart_slippage"),
             InlineKeyboardButton(f"{on(s['price_alerts'])} Alerts",    callback_data="toggle_price_alerts")],
            [InlineKeyboardButton(f"{on(s['notifications'])} Notifs",   callback_data="toggle_notifications")],
            [InlineKeyboardButton("🔒 Set PIN", callback_data="setpin")],
        ])
    )

async def set_slippage(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    val   = float(query.data.split("_")[1])
    storage.set_setting(uid(u), "slippage", val)
    await query.message.reply_text(f"✅ Slippage: `{val}%`", parse_mode="Markdown")

async def toggle_setting_handler(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = u.callback_query; await query.answer()
    key   = query.data[7:]
    val   = storage.toggle_setting(uid(u), key)
    label = key.replace("_", " ").title()
    await query.message.reply_text(f"{on(val)} *{label}* {'enabled' if val else 'disabled'}", parse_mode="Markdown")

# ── Cancel ────────────────────────────────────────────────────────────────────

async def cancel(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ack(u)
    await msg_of(u).reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ── Router ────────────────────────────────────────────────────────────────────

async def router(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = u.callback_query.data
    simple = {
        "dashboard": dashboard, "buy": buy_start, "sell": sell_start,
        "holdings": holdings, "tpsl_menu": tpsl_menu, "dca_menu": dca_menu,
        "dca_new": dca_new, "price_menu": price_menu, "watchlist": watchlist_cmd,
        "alerts_menu": alerts_menu, "alert_new": alert_new,
        "orders": orders_menu, "history": history, "analytics": analytics,
        "copy_menu": copy_menu, "copy_new": copy_new,
        "wallets": wallets_menu, "settings": settings, "limit": limit_start,
        "confirm_buy": confirm_buy, "confirm_sell": confirm_sell,
        "cancel": cancel, "pnl_menu": pnl_menu,
        "tpsl_closeall": lambda u,c: u.callback_query.answer(),
        "autobuy_menu":     autobuy_menu_cb,
        "autobuy_cb_toggle": autobuy_toggle_cb,
        "autobuy_cb_setup":  autobuy_setup_cb,
        "start": lambda u, c: start_from_cb(u, c),
    }
    if d in simple:
        await simple[d](u, ctx)
    elif d == "setpin":
        await u.callback_query.answer()
        await u.callback_query.message.reply_text("🔒 Enter 4+ digit PIN:")
        ctx.user_data["_conv"] = "setpin"
    elif d == "sell_manual":
        await u.callback_query.answer()
        await u.callback_query.message.reply_text("Token address or symbol:")
    elif d.startswith("analytics_"): await show_analytics(u, ctx)
    elif d.startswith("slip_"):        await set_slippage(u, ctx)
    elif d.startswith("toggle_"):      await toggle_setting_handler(u, ctx)
    elif d.startswith("sw_"):          await switch_wallet(u, ctx)
    elif d.startswith("pnl_"):         await show_pnl(u, ctx)
    elif d.startswith("cancelorder_"): await cancel_order(u, ctx)
    elif d.startswith("bamt_"):        await buy_amount_recv(u, ctx)
    elif d.startswith("samt_"):        await sell_amt_selected(u, ctx)
    elif d.startswith("selltoken_"):   await sell_token_selected(u, ctx)
    elif d.startswith("quickbuy_"):    await quickbuy(u, ctx)
    elif d.startswith("tpsl_token_"):  await tpsl_for_token(u, ctx)
    elif d.startswith("settp_"):       await set_tp(u, ctx)
    elif d.startswith("setsl_"):       await set_sl(u, ctx)
    elif d.startswith("stopdca_"):     await stop_dca(u, ctx)
    elif d.startswith("delalert_"):    await del_alert(u, ctx)
    elif d.startswith("wbuy_"):        await watchlist_buy(u, ctx)
    elif d.startswith("walert_"):      await watchlist_alert(u, ctx)
    elif d.startswith("wremove_"):     await watchlist_remove(u, ctx)
    elif d.startswith("stopcopy_"):    await stop_copy(u, ctx)

# ── Auto Buy ──────────────────────────────────────────────────────────────────

async def autobuy_menu_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show auto-buy menu from main keyboard button"""
    await ack(u)
    m = msg_of(u)
    user_id = uid(u)
    config = storage.get_auto_buy_config(user_id)
    enabled = bool(config and config.get("enabled"))

    if config:
        pri_fee_display = f"{config['priority_fee_sol']:.8f}".rstrip('0').rstrip('.')
        status_text = (
            f"{'🟢 ENABLED' if enabled else '🔴 DISABLED'}\n\n"
            f"• Amount: `{config['sol_amount']} SOL`\n"
            f"• Priority Fee: `{pri_fee_display} SOL`\n"
            f"• Slippage: `{config['slippage_bps']/100}%`"
        )
    else:
        status_text = "⚠️ Not configured yet"

    toggle_label = "⏸ Turn OFF" if enabled else "▶️ Turn ON"

    await m.reply_text(
        f"🤖 *Auto Buy*\n\n{status_text}\n\n"
        f"When ON, just paste any CA and it buys instantly with your preset settings.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle_label,      callback_data="autobuy_cb_toggle"),
             InlineKeyboardButton("⚙️ Configure",    callback_data="autobuy_cb_setup")],
            [InlineKeyboardButton("🏠 Menu",         callback_data="start")],
        ])
    )

async def autobuy_toggle_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-buy from inline button"""
    await ack(u)
    m = msg_of(u)
    user_id = uid(u)
    config = storage.get_auto_buy_config(user_id)

    if not config:
        await m.reply_text(
            "❌ Auto Buy not configured yet.\n\nTap ⚙️ Configure first.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⚙️ Configure", callback_data="autobuy_cb_setup"),
            ]])
        )
        return

    enabled = storage.toggle_auto_buy(user_id)
    pri_fee_display = f"{config['priority_fee_sol']:.8f}".rstrip('0').rstrip('.')
    status = "🟢 ENABLED" if enabled else "🔴 DISABLED"
    toggle_label = "⏸ Turn OFF" if enabled else "▶️ Turn ON"

    await m.reply_text(
        f"{'🤖 ON' if enabled else '⏸ OFF'} *Auto Buy*\n\n"
        f"Status: {status}\n\n"
        f"• Amount: `{config['sol_amount']} SOL`\n"
        f"• Priority Fee: `{pri_fee_display} SOL`\n"
        f"• Slippage: `{config['slippage_bps']/100}%`\n\n"
        + ("✅ Now just paste a CA to auto-buy!" if enabled else "Auto-buy is paused."),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle_label,   callback_data="autobuy_cb_toggle"),
             InlineKeyboardButton("⚙️ Configure", callback_data="autobuy_cb_setup")],
            [InlineKeyboardButton("🏠 Menu",      callback_data="start")],
        ])
    )

    # If just turned ON, prompt for CA
    if enabled:
        await m.reply_text("📋 Paste a token CA to buy now, or just close this.")

async def autobuy_setup_cb(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Trigger auto-buy setup from inline button"""
    await ack(u)
    m = msg_of(u)
    await m.reply_text(
        "🤖 *Auto Buy Setup*\n\n"
        "Enter format: `SOL_AMOUNT PRIORITY_FEE_SOL SLIPPAGE_%`\n\n"
        "Examples:\n"
        "• `0.05 0.00005 1.0` — 0.05 SOL, 50 lamports fee, 1% slippage\n"
        "• `0.1 0.0001 0.5` — 0.1 SOL, 100 lamports fee, 0.5% slippage\n\n"
        "Reply with your settings:",
        parse_mode="Markdown")
    ctx.user_data["_setup_auto_buy"] = True

async def auto_buy_setup(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Setup auto-buy configuration"""
    await u.message.reply_text(
        "🤖 *Auto Buy Setup*\n\n"
        "Enter format: `SOL_AMOUNT PRIORITY_FEE_SOL SLIPPAGE_BPS`\n\n"
        "Examples:\n"
        "• `0.05 0.00005 1.0` — 0.05 SOL, 50 lamports fee, 1% slippage\n"
        "• `0.1 0.0001 0.5` — 0.1 SOL, 100 lamports fee, 0.5% slippage\n\n"
        "Reply with your settings:",
        parse_mode="Markdown")
    ctx.user_data["_setup_auto_buy"] = True

async def auto_buy_config_recv(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Receive and save auto-buy config"""
    if not ctx.user_data.pop("_setup_auto_buy", False):
        return

    try:
        parts = u.message.text.strip().split()
        if len(parts) < 3:
            await u.message.reply_text("❌ Format: `0.05 0.00005 1.0`")
            return

        sol_amt = float(parts[0])
        pri_fee_sol = float(parts[1])
        slip_bps = int(float(parts[2]) * 100)

        if sol_amt <= 0 or pri_fee_sol < 0:
            await u.message.reply_text("❌ Amounts must be positive")
            return

        lamports = int(pri_fee_sol * 1e9)
        pri_fee_display = f"{pri_fee_sol:.8f}".rstrip('0').rstrip('.')
        storage.save_auto_buy_config(uid(u), sol_amt, pri_fee_sol, slip_bps, enabled=False)

        await u.message.reply_text(
            f"✅ *Auto Buy Configured*\n\n"
            f"• Amount: `{sol_amt} SOL`\n"
            f"• Priority Fee: `{pri_fee_display} SOL` ({lamports:,} lamports)\n"
            f"• Slippage: `{slip_bps/100}%`\n\n"
            f"Use `/autobuy` to toggle ON/OFF\n"
            f"Then just paste CA to auto-buy!",
            parse_mode="Markdown")
    except ValueError:
        await u.message.reply_text("❌ Invalid format. Use: `0.05 0.00005 1.0`")

async def auto_buy_toggle(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-buy on/off"""
    config = storage.get_auto_buy_config(uid(u))
    if not config:
        await u.message.reply_text(
            "❌ Auto Buy not configured\n\n/autobuy_setup to configure",
            parse_mode="Markdown")
        return

    enabled = storage.toggle_auto_buy(uid(u))
    emoji = "🤖 ON" if enabled else "⏸ OFF"
    status = "🟢 ENABLED" if enabled else "🔴 DISABLED"
    sol_amt = config['sol_amount']
    pri_fee = f"{config['priority_fee_sol']:.8f}".rstrip('0').rstrip('.')
    slip_pct = config['slippage_bps'] / 100

    await u.message.reply_text(
        f"{emoji} *Auto Buy*\n\n"
        f"• Amount: `{sol_amt} SOL`\n"
        f"• Priority Fee: `{pri_fee} SOL`\n"
        f"• Slippage: `{slip_pct}%`\n\n"
        f"Status: {status}\n\n"
        f"Now just paste CA to auto-buy!",
        parse_mode="Markdown")

async def auto_buy_execute(u: Update, ctx: ContextTypes.DEFAULT_TYPE, token_address: str):
    """Execute auto-buy with preset config"""
    from solana_utils import _resolve_mint

    user_id = uid(u)
    config = storage.get_auto_buy_config(user_id)

    if not config or not config.get("enabled"):
        return False

    try:
        mint = _resolve_mint(token_address)
        meta = await get_token_meta(mint)
        w = get_wallet(ctx, user_id)

        if not w:
            await msg_of(u).reply_text("⚠️ No wallet")
            return False

        await msg_of(u).reply_text(f"🤖 Auto-buying {meta['symbol']}...")

        quote = await JupiterClient().get_quote(
            input_mint=SOL_MINT,
            output_mint=mint,
            amount_sol=config["sol_amount"],
            slippage_bps=config["slippage_bps"])

        tx_sig = await JupiterClient().execute_swap(w, quote)

        storage.record_trade(
            user_id, "buy", meta["symbol"], mint,
            quote["out_amount_ui"], config["sol_amount"], 0, tx_sig)

        await msg_of(u).reply_text(
            f"✅ *Auto-bought {meta['symbol']}!*\n\n"
            f"• Amount: `{quote['out_amount_ui']} {meta['symbol']}`\n"
            f"• Cost: `{config['sol_amount']} SOL`\n"
            f"• Fee: `{config['priority_fee_sol']} SOL`\n"
            f"🔗 [Solscan](https://solscan.io/tx/{tx_sig})",
            parse_mode="Markdown", disable_web_page_preview=True)

        return True
    except Exception as e:
        await msg_of(u).reply_text(f"❌ {e}")
        return False

async def handle_ca_paste(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages — route to auto-buy, config recv, or sell custom"""
    text = u.message.text.strip() if u.message else ""

    import re
    is_ca = bool(re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', text))

    # If waiting for auto-buy setup config (NOT a CA)
    if ctx.user_data.get("_setup_auto_buy") and not is_ca:
        await auto_buy_config_recv(u, ctx)
        return

    # If it looks like a Solana CA — try auto-buy first
    if is_ca:
        executed = await auto_buy_execute(u, ctx, text)
        if executed:
            return

    # Fall through to sell custom if applicable
    if ctx.user_data.get("_sell_custom"):
        await sell_custom_recv(u, ctx)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()

    def conv(entry_points, states, **kw):
        return ConversationHandler(
            entry_points=entry_points, states=states,
            fallbacks=[CommandHandler("cancel", cancel)], **kw)

    TEXT = filters.TEXT & ~filters.COMMAND

    app.add_handler(conv(
        [CommandHandler("newwallet", new_wallet), CommandHandler("importwallet", import_wallet_cmd)],
        {S_IMP_KEY: [MessageHandler(TEXT, import_wallet_recv)],
         S_WAL_LABEL: [MessageHandler(TEXT, wallet_label_recv)]}))

    app.add_handler(conv(
        [CommandHandler("buy", buy_start), CallbackQueryHandler(buy_start, pattern="^buy$")],
        {S_BUY_TOKEN:  [MessageHandler(TEXT, buy_token_recv)],
         S_BUY_AMOUNT: [MessageHandler(TEXT, buy_amount_recv),
                        CallbackQueryHandler(buy_amount_recv, pattern="^bamt_")],
         S_PIN_CHECK:  [MessageHandler(TEXT, pin_check_recv)]}))

    app.add_handler(conv(
        [CommandHandler("sell", sell_start), CallbackQueryHandler(sell_start, pattern="^sell$")],
        {S_SELL_TOKEN:  [MessageHandler(TEXT, sell_token_recv)],
         S_SELL_AMOUNT: [MessageHandler(TEXT, sell_custom_recv),
                         CallbackQueryHandler(sell_amt_selected, pattern="^samt_")],
         S_PIN_CHECK:   [MessageHandler(TEXT, pin_check_recv)]}))

    app.add_handler(conv(
        [CommandHandler("setpin", setpin_cmd)],
        {S_PIN_SET: [MessageHandler(TEXT, pin_set_recv)]}))

    app.add_handler(conv(
        [CommandHandler("limit", limit_start), CallbackQueryHandler(limit_start, pattern="^limit$")],
        {S_LIM_TOKEN:  [MessageHandler(TEXT, limit_token_recv)],
         S_LIM_PRICE:  [MessageHandler(TEXT, limit_price_recv)],
         S_LIM_AMOUNT: [MessageHandler(TEXT, limit_amount_recv)]}))

    app.add_handler(conv(
        [CallbackQueryHandler(price_menu, pattern="^price_menu$")],
        {S_PRICE_TOKEN: [MessageHandler(TEXT, price_token_recv)]}))

    app.add_handler(conv(
        [CommandHandler("watch", watch_cmd)],
        {S_WATCH_TOKEN: [MessageHandler(TEXT, watch_token_recv)]}))

    app.add_handler(conv(
        [CallbackQueryHandler(alert_new, pattern="^alert_new$"),
         CommandHandler("alert", alert_new)],
        {S_ALERT_TOKEN: [MessageHandler(TEXT, alert_token_recv)],
         S_ALERT_COND:  [CallbackQueryHandler(alert_cond_recv, pattern="^alertcond_")],
         S_ALERT_PRICE: [MessageHandler(TEXT, alert_price_recv)]}))

    app.add_handler(conv(
        [CallbackQueryHandler(dca_new, pattern="^dca_new$"), CommandHandler("dca", dca_menu)],
        {S_DCA_TOKEN:    [MessageHandler(TEXT, dca_token_recv)],
         S_DCA_AMOUNT:   [MessageHandler(TEXT, dca_amount_recv)],
         S_DCA_ORDERS:   [MessageHandler(TEXT, dca_orders_recv)],
         S_DCA_INTERVAL: [MessageHandler(TEXT, dca_interval_recv),
                          CallbackQueryHandler(dca_interval_recv, pattern="^dcaiv_")]}))

    app.add_handler(conv(
        [CallbackQueryHandler(copy_new, pattern="^copy_new$"), CommandHandler("copy", copy_menu)],
        {S_COPY_ADDR:  [MessageHandler(TEXT, copy_addr_recv)],
         S_COPY_LABEL: [MessageHandler(TEXT, copy_label_recv)],
         S_COPY_SOL:   [MessageHandler(TEXT, copy_sol_recv)]}))

    app.add_handler(conv(
        [CallbackQueryHandler(set_tp, pattern="^settp_custom_")],
        {S_TP_PRICE: [MessageHandler(TEXT, tp_price_recv)]}))

    app.add_handler(conv(
        [CallbackQueryHandler(set_sl, pattern="^setsl_custom_")],
        {S_SL_PRICE: [MessageHandler(TEXT, sl_price_recv)]}))

    for cmd, fn in [
        ("start", start), ("help", help_cmd), ("dashboard", dashboard),
        ("balance", balance), ("price", price_cmd), ("history", history),
        ("orders", orders_menu), ("holdings", holdings), ("watchlist", watchlist_cmd),
        ("settings", settings), ("analytics", analytics), ("alerts", alerts_menu),
        ("copylist", copy_menu), ("tp", tpsl_menu), ("sl", tpsl_menu),
        ("autobuy_setup", auto_buy_setup), ("autobuy", auto_buy_toggle),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ca_paste))

    async def post_init(application):
        await application.bot.set_my_commands(BOT_COMMANDS)
        preload_from_db()
        asyncio.create_task(monitor_orders(application.bot))
        asyncio.create_task(monitor_alerts(application.bot))
        asyncio.create_task(monitor_tp_sl(application.bot))
        asyncio.create_task(monitor_dca(application.bot))
        asyncio.create_task(monitor_copy_trading(application.bot))

    app.post_init = post_init
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
