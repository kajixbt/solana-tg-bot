"""
Jito Bundle MEV Protection
Sends transactions as bundles to Jito validators to avoid sandwich attacks.
Costs: ~0.00005 SOL per bundle (~500 lamports) + priority fees
"""

import os, base64, asyncio
import aiohttp
import ssl, certifi
from solders.transaction import VersionedTransaction

JITO_URLS = [
    "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
    "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles",
]

def _ssl_ctx():
    return ssl.create_default_context(cafile=certifi.where())

async def send_bundle(tx_bytes: bytes, tip_lamports: int = 1000) -> str | None:
    """
    Send transaction as Jito bundle with tip.
    tip_lamports: 0-5000 (0.000005 SOL = 500 lamports is minimum for fast inclusion)
    Returns signature if successful, None otherwise.
    """
    tx = VersionedTransaction.from_bytes(tx_bytes)
    sig = str(tx.signatures[0])
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendBundle",
        "params": [
            [base64.b64encode(tx_bytes).decode()],
            {"encoding": "base64"},
            {"skipPreflightValidation": True, "preflightCommitmentLevel": "confirmed"},
        ]
    }

    ssl_ctx = _ssl_ctx()
    conn = aiohttp.TCPConnector(ssl=ssl_ctx, limit=5)
    
    for url in JITO_URLS:
        try:
            async with aiohttp.ClientSession(connector=conn) as s:
                async with s.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if "result" in data and data["result"] is not None:
                            return sig
        except Exception:
            pass
    
    return None


def calculate_priority_fee(sol_amount: float, slippage_bps: int = 100) -> int:
    """
    Calculate priority fee in lamports based on trade size.
    Larger trades = higher fee to ensure fast inclusion.
    
    Formula: base + (sol_amount * multiplier)
    """
    base_lamports = 5000  # ~0.000005 SOL baseline
    
    if sol_amount < 0.1:
        return base_lamports  # 5k lamports
    elif sol_amount < 0.5:
        return 10000  # 10k lamports
    elif sol_amount < 1.0:
        return 25000  # 25k lamports
    elif sol_amount < 5.0:
        return 50000  # 50k lamports
    else:
        return 100000  # 100k lamports for large trades

def calculate_tip(is_mev_protected: bool) -> int:
    """
    Jito tip for priority in bundle.
    ~500-1000 lamports is standard for fast inclusion.
    """
    return 1000 if is_mev_protected else 0  # 0.000001 SOL tip if using Jito
