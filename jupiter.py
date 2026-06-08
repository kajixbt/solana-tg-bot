"""
Jupiter with Priority Fees + Jito MEV Protection
"""

import os, ssl, base64, aiohttp, asyncio, certifi, logging
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed
from solana_utils import KNOWN_TOKENS, _resolve_mint
from jito import send_bundle, calculate_priority_fee, calculate_tip

logger = logging.getLogger(__name__)

JUPITER_QUOTE_URLS = [
    "https://lite-api.jup.ag/swap/v1/quote",
    "https://quote-api.jup.ag/v6/quote",
]
JUPITER_SWAP_URLS = [
    "https://lite-api.jup.ag/swap/v1/swap",
    "https://quote-api.jup.ag/v6/swap",
]
JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v2"
RPC_URL   = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOL_MINT  = "So11111111111111111111111111111111111111112"

# Minimum sensible priority fee — avoids 422 from fee=0 or near-zero
MIN_PRIORITY_FEE = 25_000   # lamports (~$0.000025)

def _ssl_ctx():
    return ssl.create_default_context(cafile=certifi.where())

def _connector():
    return aiohttp.TCPConnector(ssl=_ssl_ctx(), ttl_dns_cache=300, limit=10)

async def _get(urls, params):
    last_err = None
    for url in urls:
        for _ in range(2):
            try:
                async with aiohttp.ClientSession(connector=_connector()) as s:
                    async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status != 200:
                            raise RuntimeError(f"HTTP {r.status}")
                        return await r.json()
            except (aiohttp.ClientConnectorError, aiohttp.ServerConnectionError) as e:
                last_err = e
                await asyncio.sleep(1)
            except RuntimeError:
                raise
    raise RuntimeError(f"All Jupiter endpoints failed")

async def _post(urls, payload):
    last_err = None
    for url in urls:
        for _ in range(2):
            try:
                async with aiohttp.ClientSession(connector=_connector()) as s:
                    async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status != 200:
                            body = await r.text()
                            raise RuntimeError(f"HTTP {r.status}: {body[:200]}")
                        return await r.json()
            except (aiohttp.ClientConnectorError, aiohttp.ServerConnectionError) as e:
                last_err = e
                await asyncio.sleep(1)
            except RuntimeError:
                raise
    raise RuntimeError(f"All Jupiter endpoints failed")


class JupiterClient:

    async def get_quote(self, input_mint, output_mint, slippage_bps=100,
                        amount_sol=None, amount_tokens=None):
        input_mint  = _resolve_mint(input_mint)
        output_mint = _resolve_mint(output_mint)
        if amount_sol is not None:
            amount_raw = int(amount_sol * 1e9)
        elif amount_tokens is not None:
            decimals   = await self._get_token_decimals(input_mint)
            amount_raw = int(amount_tokens * (10 ** decimals))
        else:
            raise ValueError("Provide amount_sol or amount_tokens")

        params = {"inputMint": input_mint, "outputMint": output_mint,
                  "amount": str(amount_raw), "slippageBps": str(slippage_bps),
                  "onlyDirectRoutes": "false", "asLegacyTransaction": "false"}
        data = await _get(JUPITER_QUOTE_URLS, params)

        in_dec  = await self._get_token_decimals(input_mint)
        out_dec = await self._get_token_decimals(output_mint)
        return {
            "_raw":             data,
            "input_mint":       input_mint,
            "output_mint":      output_mint,
            "in_amount_ui":     int(data["inAmount"])  / (10 ** in_dec),
            "out_amount_ui":    round(int(data["outAmount"]) / (10 ** out_dec), 6),
            "in_symbol":        KNOWN_TOKENS.get(input_mint,  input_mint[:8]),
            "out_symbol":       KNOWN_TOKENS.get(output_mint, output_mint[:8]),
            "price_impact_pct": float(data.get("priceImpactPct", 0)) * 100,
            "slippage_bps":     slippage_bps,
        }

    async def execute_swap(self, wallet, quote, use_mev_protection=True,
                           priority_fee_lamports=None, on_confirm=None):
        """
        Broadcast the swap and return the signature IMMEDIATELY (~1s).
        Confirmation runs in the background.

        on_confirm: optional async callable(sig: str, err: str | None)
                    called once the tx lands or fails/times out.
        """
        # ── Priority fee ──────────────────────────────────────────────────────
        if priority_fee_lamports is not None:
            fee = max(int(priority_fee_lamports), MIN_PRIORITY_FEE)
        else:
            raw = calculate_priority_fee(quote["in_amount_ui"])
            fee = max(int(raw * 1e9) if raw < 1.0 else int(raw), MIN_PRIORITY_FEE)

        logger.info(f"Priority fee: {fee} lamports")

        # ── Build & sign ──────────────────────────────────────────────────────
        swap_payload = {
            "quoteResponse":             quote["_raw"],
            "userPublicKey":             wallet.public_key,
            "wrapAndUnwrapSol":          True,
            "dynamicComputeUnitLimit":   True,
            "prioritizationFeeLamports": fee,
        }
        swap_data = await _post(JUPITER_SWAP_URLS, swap_payload)
        raw_tx    = base64.b64decode(swap_data["swapTransaction"])
        tx        = VersionedTransaction.from_bytes(raw_tx)
        signed    = VersionedTransaction(tx.message, [wallet.keypair])
        tx_bytes  = bytes(signed)

        sig = None

        # ── Try Jito first ────────────────────────────────────────────────────
        if use_mev_protection:
            try:
                jito_sig = await send_bundle(tx_bytes)
                if jito_sig:
                    logger.info(f"Sent via Jito: {jito_sig[:8]}")
                    sig = jito_sig
            except Exception as e:
                logger.warning(f"Jito failed, falling back to RPC: {e}")

        # ── Broadcast via RPC ─────────────────────────────────────────────────
        if not sig:
            async with AsyncClient(RPC_URL) as client:
                opts   = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                result = await client.send_raw_transaction(tx_bytes, opts=opts)
                sig    = str(result.value)
                logger.info(f"Sent via RPC: {sig[:8]}")

        # ── Background confirmation (non-blocking) ────────────────────────────
        async def _confirm_bg(signature: str):
            try:
                async with AsyncClient(RPC_URL) as client:
                    await self._confirm_transaction(client, signature, timeout=60)
                logger.info(f"Confirmed: {signature[:8]}")
                if on_confirm:
                    await on_confirm(signature, None)
            except TimeoutError:
                logger.warning(f"Confirmation timeout: {signature[:8]}")
                if on_confirm:
                    await on_confirm(signature, "timeout")
            except Exception as e:
                logger.error(f"Confirmation error {signature[:8]}: {e}")
                if on_confirm:
                    await on_confirm(signature, str(e))

        asyncio.create_task(_confirm_bg(sig))  # returns immediately

        return sig  # bot gets this in ~1s, not 10-30s

    async def get_price(self, token):
        mint  = _resolve_mint(token)
        data  = await _get([JUPITER_PRICE_URL], {"ids": mint})
        entry = data.get("data", {}).get(mint)
        if not entry:
            raise ValueError(f"No price data for {token}")
        return {
            "mint":      mint,
            "symbol":    entry.get("mintSymbol", KNOWN_TOKENS.get(mint, mint[:8])),
            "price_usd": float(entry["price"]),
        }

    async def _get_token_decimals(self, mint):
        known = {
            "So11111111111111111111111111111111111111112":   9,
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 6,
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB":  6,
            "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263":  5,
            "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN":   6,
            "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So":   9,
        }
        if mint in known:
            return known[mint]
        from solders.pubkey import Pubkey
        async with AsyncClient(RPC_URL) as client:
            resp = await client.get_account_info_json_parsed(Pubkey.from_string(mint))
            info = resp.value
            if info and hasattr(info.data, "parsed"):
                return info.data.parsed["info"]["decimals"]
        return 6

    async def _confirm_transaction(self, client, sig, timeout=30):
        from solders.signature import Signature
        signature = Signature.from_string(sig)
        for _ in range(timeout):
            await asyncio.sleep(1)
            resp   = await client.get_signature_statuses([signature])
            status = resp.value[0]
            if status is not None:
                if status.err:
                    raise RuntimeError(f"Transaction failed: {status.err}")
                if status.confirmation_status in ("confirmed", "finalized"):
                    return
        raise TimeoutError(f"Not confirmed after {timeout}s")
