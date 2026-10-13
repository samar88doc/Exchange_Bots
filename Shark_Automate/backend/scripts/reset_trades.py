#!/usr/bin/env python3
"""Clear test trades, orders, positions, signals, and reset paper balance."""
import asyncio
import os
import sys

# Ensure backend root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text
from app.core.config import get_settings
from app.db.session import init_engine, get_session_factory
from app.models.entities import PaperBalance


async def reset_paper_trades():
    settings = get_settings()
    init_engine(settings)
    session_factory = get_session_factory()
    
    async with session_factory() as session:
        # Clear trade and execution tables
        await session.execute(text("""
            TRUNCATE TABLE 
                fills, 
                orders, 
                position_events, 
                trades, 
                position_reservations, 
                positions, 
                signals, 
                webhook_events, 
                audit_logs 
            CASCADE;
        """))
        
        # Reset paper balance to initial capital
        balance = (await session.execute(select(PaperBalance))).scalars().first()
        initial_cap = settings.paper_initial_balance
        if balance:
            balance.balance = initial_cap
            balance.initial_balance = initial_cap
            balance.locked_margin = 0
            balance.realized_pnl = 0
        else:
            session.add(PaperBalance(
                asset=settings.account_unit,
                balance=initial_cap,
                initial_balance=initial_cap,
                locked_margin=0,
                realized_pnl=0
            ))
        await session.commit()
        print(f"Successfully cleared all trades and reset paper balance to {initial_cap} {settings.account_unit}!")


if __name__ == "__main__":
    asyncio.run(reset_paper_trades())
