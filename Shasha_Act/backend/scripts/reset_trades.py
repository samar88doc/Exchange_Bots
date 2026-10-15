#!/usr/bin/env python3
"""Clear simulated paper trades, orders, fills, events, and outbox in Shasha_Act."""
from __future__ import annotations

import asyncio
import os
import sys

# Ensure backend root is in sys.path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)

from sqlalchemy import text
from app.config import get_settings
from app.db import make_engine, make_session_factory


async def reset_trades(confirm: bool = False) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    if not confirm:
        print("This will reset all trades, orders, fills, and signal events in Shasha_Act.")
        print("Use --yes to confirm deletion.")
        return

    async with session_factory() as session:
        # PostgreSQL truncate isolated tables
        await session.execute(text("""
            TRUNCATE TABLE
                shasha_act_fills,
                shasha_act_orders,
                shasha_act_trades,
                shasha_act_events,
                shasha_act_outbox,
                shasha_act_audit_events
            CASCADE;
        """))
        await session.commit()
        print("✅ Successfully cleared all Shasha_Act trades, orders, fills, and events!")
        print("   Risk controls and Alembic version were preserved.")


if __name__ == "__main__":
    confirm = "--yes" in sys.argv or "-y" in sys.argv
    asyncio.run(reset_trades(confirm=confirm))
