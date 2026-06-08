"""
Solana wallet helpers — key generation, balance fetching, token lookups
"""

import base58
import asyncio
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from spl.token.constants import TOKEN_PROGRAM_ID  # bundled inside the solana package
import aiohttp
import os

RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# Well-known token mints → symbols (fallback for display)
KNOWN_TOKENS = {
    "So11111111111111111111111111111111111111112":  "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB":  "USDT",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263":  "BONK",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN":   "JUP",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So":   "mSOL",
}


class SolanaWallet:
    def __init__(self, private_key_b58: str):
        raw = base58.b58decode(private_key_b58)
        self._keypair = Keypair.from_bytes(raw)
        self.private_key_b58 = private_key_b58

    @classmethod
    def generate(cls) -> "SolanaWallet":
        kp = Keypair()
        pk_b58 = base58.b58encode(bytes(kp)).decode()
        return cls(pk_b58)

    @property
    def public_key(self) -> str:
        return str(self._keypair.pubkey())

    @property
    def keypair(self) -> Keypair:
        return self._keypair

    async def get_balances(self) -> dict[str, str]:
        """Returns SOL + all SPL token balances."""
        async with AsyncClient(RPC_URL) as client:
            pubkey = Pubkey.from_string(self.public_key)

            # SOL balance
            sol_resp = await client.get_balance(pubkey)
            sol_lamports = sol_resp.value
            sol_ui = sol_lamports / 1e9

            balances = {f"SOL": f"{sol_ui:.4f}"}

            # SPL tokens
            try:
                token_resp = await client.get_token_accounts_by_owner_json_parsed(
                    pubkey,
                    opts={"programId": str(TOKEN_PROGRAM_ID)},
                )
                for acct in token_resp.value:
                    info = acct.account.data.parsed["info"]
                    mint = info["mint"]
                    ui_amount = info["tokenAmount"]["uiAmountString"]
                    decimals = info["tokenAmount"]["decimals"]
                    if float(ui_amount) > 0:
                        symbol = KNOWN_TOKENS.get(mint, mint[:8] + "...")
                        balances[symbol] = ui_amount
            except Exception:
                pass  # no SPL tokens or RPC issue

            return balances

    async def get_token_balance(self, mint_or_symbol: str) -> float:
        """Returns raw token balance for a given mint address."""
        mint = _resolve_mint(mint_or_symbol)
        async with AsyncClient(RPC_URL) as client:
            pubkey = Pubkey.from_string(self.public_key)
            resp = await client.get_token_accounts_by_owner_json_parsed(
                pubkey,
                opts={"mint": mint},
            )
            if not resp.value:
                raise ValueError(f"No token account found for {mint}")
            info = resp.value[0].account.data.parsed["info"]
            return float(info["tokenAmount"]["uiAmountString"])


def _resolve_mint(token: str) -> str:
    """Resolve symbol (e.g. 'BONK') to mint address, or return as-is if already an address."""
    reverse = {v: k for k, v in KNOWN_TOKENS.items()}
    return reverse.get(token.upper(), token)
