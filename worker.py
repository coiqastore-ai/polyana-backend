"""Polyana worker: activates matured referral rewards.

Runs as a separate process (polyana-worker.service).
Uses PostgreSQL advisory lock to prevent parallel execution.
Idempotent — safe to restart at any time.

Usage:
    python worker.py

Environment variables (same as main.py):
    DATABASE_URL - PostgreSQL connection string
"""
import os
import asyncio
import logging
import json
from datetime import datetime, timezone

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(message)s")
log = logging.getLogger("polyana-worker")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
LOCK_KEY = 12345  # Advisory lock key for this worker

# Config (same as main.py)
REFERRAL_HOLD_DAYS = int(os.environ.get("REFERRAL_HOLD_DAYS", "7"))


async def get_db():
    return await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, command_timeout=30)


async def activate_pending_rewards(pool):
    """Find and activate matured pending referral rewards."""
    async with pool.acquire() as db:
        # Find users with matured pending rewards
        rows = await db.fetch(
            "SELECT DISTINCT referrer_user_id FROM referral_rewards "
            "WHERE status='pending' AND available_at <= NOW() LIMIT 100"
        )
        total_activated = 0
        for r in rows:
            uid = r["referrer_user_id"]
            # Activate pending rewards for this user
            async with db.transaction():
                # Find matured rewards
                rewards = await db.fetch(
                    "SELECT id, reward_points FROM referral_rewards "
                    "WHERE referrer_user_id=$1 AND status='pending' AND available_at <= NOW() "
                    "FOR UPDATE",
                    uid
                )
                if not rewards:
                    continue

                total_points = sum(reward["reward_points"] for reward in rewards)

                # Debit pending, credit bonus
                await db.execute(
                    "UPDATE wallets SET "
                    "pending_bonus_points = pending_bonus_points - $2, "
                    "bonus_points = bonus_points + $2, "
                    "updated_at = NOW() "
                    "WHERE user_id = $1",
                    uid, total_points
                )

                # Update reward statuses
                reward_ids = [reward["id"] for reward in rewards]
                await db.execute(
                    "UPDATE referral_rewards SET status='available', activated_at=NOW(), updated_at=NOW() "
                    "WHERE id = ANY($1)",
                    reward_ids
                )

                # Ledger entry
                try:
                    await db.execute(
                        "INSERT INTO wallet_ledger "
                        "(user_id, wallet_type, amount, transaction_type, reference_type, "
                        "balance_after, metadata) "
                        "VALUES ($1, 'bonus', $2, 'referral_activated', 'referral_rewards', $3, $4)",
                        uid, total_points,
                        json.dumps({"reward_ids": [str(rid) for rid in reward_ids]}),
                        (await db.fetchrow("SELECT bonus_points FROM wallets WHERE user_id=$1", uid))["bonus_points"]
                    )
                except asyncpg.UniqueViolationError:
                    pass

                total_activated += len(rewards)
                log.info("activated %d rewards (%d points) for user %s", len(rewards), total_points, uid)

        return total_activated


async def send_activation_notifications(pool, activated_users):
    """Send notifications to users whose rewards were activated."""
    # This is handled in the main maturation loop in main.py
    # Worker only does the DB work; notifications are sent by the main process
    pass


async def run_worker():
    """Main worker loop with advisory lock."""
    if not DATABASE_URL:
        log.error("DATABASE_URL not set")
        return

    pool = await get_db()
    log.info("Worker started, connecting to database...")

    while True:
        try:
            async with pool.acquire() as db:
                # Try to acquire advisory lock (non-blocking)
                acquired = await db.fetchval(
                    "SELECT pg_try_advisory_lock($1)", LOCK_KEY
                )
                if not acquired:
                    log.debug("Another worker instance is running, skipping...")
                    await asyncio.sleep(60)
                    continue

                try:
                    # Run the activation
                    count = await activate_pending_rewards(pool)
                    if count > 0:
                        log.info("Activated %d matured rewards", count)
                finally:
                    # Release the lock
                    await db.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)

        except Exception:
            log.exception("Worker error")

        # Sleep between runs (5 minutes)
        await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(run_worker())
