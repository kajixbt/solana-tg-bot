"""
Token metadata resolver — Phase 1 rewrite
- Single shared aiohttp session
- No permanent caching of failures
- Cache expiration (1 hour for success, 30s retry for failures)
- DexScreener + Pump.fun fallbacks
- Stores to DB via storage module
"""

import asyncio
import aiohttp
import ssl
import certifi
import logging
import time

logger = logging.getLogger(__name__)

# ── Cache ─────────────────────────────────────────────────────────────────────
# {mint: {"symbol": str, "name": str, "cached_at": float, "failed": bool}}
_cache: dict[str, dict] = {}

CACHE_TTL         = 3600   # 1 hour for successful lookups
FAILURE_RETRY_TTL = 30     # retry failed lookups after 30s

# ── Known tokens (never expire) ───────────────────────────────────────────────
KNOWN: dict[str, dict] = {
    "So11111111111111111111111111111111111111112":    {"symbol": "SOL",   "name": "Solana"},
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {"symbol": "USDC",  "name": "USD Coin"},
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB":  {"symbol": "USDT",  "name": "Tether"},
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263":  {"symbol": "BONK",  "name": "Bonk"},
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN":   {"symbol": "JUP",   "name": "Jupiter"},
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So":   {"symbol": "mSOL",  "name": "Marinade SOL"},
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs":  {"symbol": "ETH",   "name": "Ethereum (Wormhole)"},
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E":  {"symbol": "BTC",   "name": "Bitcoin (Wormhole)"},
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R":  {"symbol": "RAY",   "name": "Raydium"},
    "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE":   {"symbol": "ORCA",  "name": "Orca"},
    "WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk":    {"symbol": "WEN",   "name": "Wen"},
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm":  {"symbol": "WIF",   "name": "dogwifhat"},
    "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5":   {"symbol": "MEW",   "name": "cat in a dogs world"},
}
for mint, meta in KNOWN.items():
    _cache[mint] = {**meta, "cached_at": float("inf"), "failed": False}

# ── Shared session ────────────────────────────────────────────────────────────
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()

async def _get_session() -> aiohttp.ClientSession:
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            ssl_ctx  = ssl.create_default_context(cafile=certifi.where())
            conn     = aiohttp.TCPConnector(ssl=ssl_ctx, ttl_dns_cache=300, limit=20)
            timeout  = aiohttp.ClientTimeout(total=8)
            _session = aiohttp.ClientSession(connector=conn, timeout=timeout)
        return _session

async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None

# ── Public API ────────────────────────────────────────────────────────────────

async def get_token_meta(mint: str) -> dict:
    """
    Returns {symbol, name} for a mint.
    Priority: memory cache → Jupiter token API → DexScreener → Pump.fun → fallback
    Never permanently caches failures.
    """
    # Check cache
    cached = _cache.get(mint)
    if cached:
        age = time.time() - cached["cached_at"]
        if cached["failed"]:
            if age < FAILURE_RETRY_TTL:
                # Return best available rather than 'Unknown'
                return {"symbol": cached.get("symbol", mint[:6] + "..."),
                        "name":   cached.get("name",   mint[:12] + "...")}
            # else: retry
        elif age < CACHE_TTL or cached["cached_at"] == float("inf"):
            return {"symbol": cached["symbol"], "name": cached["name"]}

    # Try sources in order
    result = (
        await _from_jupiter(mint) or
        await _from_dexscreener(mint) or
        await _from_pumpfun(mint)
    )

    if result:
        _cache[mint] = {**result, "cached_at": time.time(), "failed": False}
        # Persist to storage DB
        try:
            import storage
            storage.save_token_meta(mint, result["symbol"], result["name"])
        except Exception:
            pass
        return result

    # All failed — cache failure but keep any prior symbol we had
    prior = _cache.get(mint, {})
    _cache[mint] = {
        "symbol":    prior.get("symbol", mint[:6] + "..."),
        "name":      prior.get("name",   mint[:12] + "..."),
        "cached_at": time.time(),
        "failed":    True,
    }
    return {"symbol": _cache[mint]["symbol"], "name": _cache[mint]["name"]}


async def get_tokens_meta(mints: list[str]) -> dict[str, dict]:
    """Batch fetch with concurrency limit."""
    sem     = asyncio.Semaphore(5)
    async def fetch(mint):
        async with sem:
            return mint, await get_token_meta(mint)
    results = await asyncio.gather(*[fetch(m) for m in mints], return_exceptions=True)
    out = {}
    for item in results:
        if isinstance(item, Exception):
            continue
        mint, meta = item
        out[mint] = meta
    return out


def preload_from_db():
    """Load saved metadata from storage into cache at startup."""
    try:
        import storage
        saved = storage.get_all_token_meta()
        for mint, sym, name in saved:
            if mint not in _cache:
                _cache[mint] = {"symbol": sym, "name": name,
                                "cached_at": time.time(), "failed": False}
        logger.info(f"Preloaded {len(saved)} token metadata entries from DB")
    except Exception as e:
        logger.warning(f"Could not preload token metadata: {e}")


# ── Data sources ──────────────────────────────────────────────────────────────

async def _from_jupiter(mint: str) -> dict | None:
    """Jupiter token API — best for listed tokens."""
    try:
        session = await _get_session()
        async with session.get(
            f"https://lite-api.jup.ag/tokens/v1/token/{mint}"
        ) as r:
            if r.status == 200:
                data = await r.json()
                sym  = (data.get("symbol") or "").strip()
                name = (data.get("name")   or "").strip()
                if sym:
                    return {"symbol": sym, "name": name or sym}
    except Exception as e:
        logger.debug(f"Jupiter meta failed {mint[:8]}: {e}")
    return None


async def _from_dexscreener(mint: str) -> dict | None:
    """DexScreener — great for new/unverified tokens."""
    try:
        session = await _get_session()
        async with session.get(
            f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
        ) as r:
            if r.status == 200:
                data  = await r.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                if pairs:
                    # Use most liquid pair
                    pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                    tok  = pair.get("baseToken", {})
                    sym  = (tok.get("symbol") or "").strip()
                    name = (tok.get("name")   or "").strip()
                    if sym:
                        return {"symbol": sym, "name": name or sym}
    except Exception as e:
        logger.debug(f"DexScreener meta failed {mint[:8]}: {e}")
    return None


async def _from_pumpfun(mint: str) -> dict | None:
    """Pump.fun — for freshly launched memecoins not yet on DEXes."""
    try:
        session = await _get_session()
        async with session.get(
            f"https://frontend-api.pump.fun/coins/{mint}"
        ) as r:
            if r.status == 200:
                data = await r.json()
                sym  = (data.get("symbol") or "").strip()
                name = (data.get("name")   or "").strip()
                if sym:
                    return {"symbol": sym, "name": name or sym}
    except Exception as e:
        logger.debug(f"Pump.fun meta failed {mint[:8]}: {e}")
    return None
