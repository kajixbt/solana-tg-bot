"""
Smart Priority Fee Calculator
Fetches current network conditions and suggests optimal fees.
User can override with custom values.
"""

import os, asyncio, time
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
import logging

logger = logging.getLogger(__name__)

RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# Cache priority fee recommendations (update every 10s)
_fee_cache = {"fee": 5000, "timestamp": 0, "network_state": "normal"}
_cache_ttl = 10


async def get_current_priority_fee() -> tuple[int, str]:
    """
    Fetch current priority fee recommendation from RPC.
    Returns: (fee_in_lamports, network_state_description)
    
    Network states:
    - low: < 1000 lamports recommended (slow network)
    - normal: 1000-50000 lamports (standard)
    - high: 50000-200000 lamports (congested)
    - critical: > 200000 lamports (very congested, MEV wars)
    """
    global _fee_cache
    
    now = time.time()
    if now - _fee_cache["timestamp"] < _cache_ttl:
        return _fee_cache["fee"], _fee_cache["network_state"]
    
    try:
        async with AsyncClient(RPC_URL) as client:
            # Get recent blockhash (indicates recent slot activity)
            bh = await client.get_latest_blockhash()
            
            # Try to get priority fee estimate (Jito endpoint has this)
            # For now, use a heuristic based on network load
            resp = await client.get_recent_prioritization_fees([])
            
            if resp.value:
                # Get median of recent fees
                fees = [int(f.prioritization_fee) for f in resp.value]
                fees.sort()
                median_fee = fees[len(fees) // 2] if fees else 5000
                
                # Determine network state
                if median_fee < 1000:
                    state = "🟢 Low (slow network)"
                    suggested = 5000
                elif median_fee < 50000:
                    state = "🟡 Normal (standard)"
                    suggested = max(median_fee, 5000)
                elif median_fee < 200000:
                    state = "🔴 High (congested)"
                    suggested = max(median_fee, 25000)
                else:
                    state = "🚨 Critical (MEV wars)"
                    suggested = max(median_fee, 100000)
                
                _fee_cache.update({
                    "fee": int(suggested),
                    "timestamp": now,
                    "network_state": state,
                    "median": median_fee,
                })
                return int(suggested), state
    except Exception as e:
        logger.warning(f"Could not fetch priority fee: {e}, using cached value")
    
    return _fee_cache["fee"], _fee_cache["network_state"]


def get_suggested_fee_for_trade(sol_amount: float, network_fee: int) -> tuple[int, str]:
    """
    Suggest priority fee based on trade size + network conditions.
    
    Returns: (suggested_lamports, description)
    """
    # Base multiplier on trade size
    if sol_amount < 0.05:
        multiplier = 0.5  # Small trades = 50% of network fee
    elif sol_amount < 0.1:
        multiplier = 0.75  # Medium = 75%
    elif sol_amount < 0.5:
        multiplier = 1.0  # Standard = 100%
    elif sol_amount < 2.0:
        multiplier = 1.5  # Large = 150%
    else:
        multiplier = 2.0  # Very large = 200%
    
    suggested = int(network_fee * multiplier)
    
    # Cap at reasonable maximums
    if suggested > 500000:
        suggested = 500000
        desc = f"⚠️ Capped at 500k lamports (~0.0005 SOL)"
    else:
        sol_cost = suggested / 1e9
        desc = f"💡 Suggested: {suggested:,} lamports (~${sol_cost:.6f})"
    
    return suggested, desc


def format_fee_display(lamports: int) -> str:
    """Format lamports as SOL for display."""
    sol = lamports / 1e9
    if sol < 0.000001:
        return f"{lamports} lamports"
    return f"{lamports:,} lamports (~${sol:.8f})"


# Presets for quick selection
PRIORITY_FEE_PRESETS = {
    "low": (5000, "🟢 Low (2-5s)"),
    "normal": (25000, "🟡 Normal (1-2s)"),
    "high": (100000, "🔴 High (instant)"),
    "critical": (250000, "🚨 Critical (MEV protected)"),
}
