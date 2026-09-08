#!/usr/bin/env bash
# Read-only check that the Kalshi credentials work. Places no orders.
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/python -c "
import asyncio
from app.data.kalshi import client as kc
from app.config import settings, live_trading_available

async def main():
    print('key id configured   :', bool(settings.KALSHI_KEY_ID))
    print('private key loaded  :', bool(settings.KALSHI_PRIVATE_KEY))
    if not kc.credentials_present():
        print()
        print('No credentials found. Run ./install_kalshi_key.sh first.')
        return
    try:
        balance = await kc.balance()
        print('Kalshi says          :', balance)
        print()
        print('Credentials work. This was a read-only balance call — nothing was placed.')
    except Exception as exc:
        print()
        print('Kalshi rejected the request:', str(exc)[:300])
        print('Check the key id matches the private key, and that the key is active.')
        return
    allowed, reason = live_trading_available(settings.LIVE_TRADING_USER or None)
    print()
    print('Live trading gate    :', 'OPEN' if allowed else 'CLOSED')
    print('Reason               :', reason)

asyncio.run(main())
"
