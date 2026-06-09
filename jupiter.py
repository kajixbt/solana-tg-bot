"""
Jupiter with Priority Fees + Jito MEV Protection
FIXED: Fast TG confirmation (returns immediately, confirms in background)
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
                            try:
                                body = await r.json()
                                detail = body.get("error") or body.get("message") or str(body)
                            except Exception:
                                detail = await r.text()
                            logger.error(f"Jupiter POST {url} -> {r.status}: {detail}")
                            raise RuntimeError(f"HTTP {r.status}: {detail}")
                        return await r.json()
            except (aiohttp.ClientConnectorError, aiohttp.ServerConnectionError) as e:
                last_err = e
                await asyncio.sleep(1)
            except RuntimeError:
                raise
    raise RuntimeError(f"All Jupiter endpoints failed: {last_err}")


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

    async def execute_swap(self, wallet, quote, use_mev_protection=True):
        """
        Execute swap with priority fees + optional Jito MEV protection.
        ⚡ RETURNS IMMEDIATELY (1-2 sec) - confirmation in background
        """
        priority_lamports = calculate_priority_fee(quote["in_amount_ui"])
        
        swap_data = await _post(JUPITER_SWAP_URLS, {
            "quoteResponse": quote["_raw"], 
            "userPublicKey": wallet.public_key,
            "wrapAndUnwrapSol": True, 
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": int(priority_lamports),
        })
        raw_tx = base64.b64decode(swap_data["swapTransaction"])
        tx     = VersionedTransaction.from_bytes(raw_tx)
        signed = VersionedTransaction(tx.message, [wallet.keypair])
        
        sig = None
        
        # Try Jito MEV protection first
        if use_mev_protection:
            try:
                jito_sig = await send_bundle(bytes(signed))
                if jito_sig:
                    logger.info(f"Sent via Jito: {jito_sig[:8]}")
                    sig = jito_sig
                    await asyncio.sleep(0.5)  # Brief pause
            except Exception as e:
                logger.warning(f"Jito failed, using RPC: {e}")
        
        # Fall back to regular RPC
        if not sig:
            async with AsyncClient(RPC_URL) as client:
                opts   = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
                result = await client.send_raw_transaction(bytes(signed), opts=opts)
                sig    = str(result.value)
                logger.info(f"Sent via RPC: {sig[:8]}")
        
        # ✅ RETURN IMMEDIATELY - DON'T WAIT FOR CONFIRMATION
        # Confirmation happens in background async task
        asyncio.create_task(self._confirm_in_background(sig))
        
        return sig

    async def _confirm_in_background(self, sig: str):
        """Confirm transaction in background (non-blocking)"""
        try:
            async with AsyncClient(RPC_URL) as client:
                await self._confirm_transaction(client, sig, timeout=60)
            logger.info(f"✅ Confirmed: {sig[:8]}")
        except TimeoutError:
            logger.warning(f"⏱ Timeout: {sig[:8]} (likely still pending)")
        except Exception as e:
            logger.error(f"❌ Confirm error {sig[:8]}: {e}")

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
