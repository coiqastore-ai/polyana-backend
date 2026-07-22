from fastapi import APIRouter, HTTPException, Depends
from config import REFERRAL_PERCENT, REFERRAL_HOLD_HOURS, STAR_RUB_RATE, PRICE_AI_INVITE, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, YOOKASSA_VAT_CODE
import core
from db import get_db, track
from auth import get_current_user
import json, logging
import httpx

log = logging.getLogger("polyana")

router = APIRouter()


async def _get_balance(db, uid: int) -> int:
    return await db.fetchval(
        "SELECT balance FROM user_balance WHERE telegram_user_id=$1", uid
    ) or 0


async def _credit(db, uid: int, amount: int, kind: str, ref: str | None = None,
                  meta: dict | None = None) -> int:
    """Add funds. Idempotent when `ref` is given (unique on kind+ref)."""
    import asyncpg
    meta_json = json.dumps(meta) if meta else None
    try:
        async with db.transaction():
            row = await db.fetchrow(
                """
                INSERT INTO user_balance (telegram_user_id, balance) VALUES ($1,$2)
                ON CONFLICT (telegram_user_id)
                DO UPDATE SET balance = user_balance.balance + $2, updated_at = NOW()
                RETURNING balance
                """,
                uid, amount,
            )
            bal = row["balance"]
            await db.execute(
                "INSERT INTO payment_txns (telegram_user_id, kind, amount, balance_after, ref, meta) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                uid, kind, amount, bal, ref, meta_json,
            )
            return bal
    except asyncpg.UniqueViolationError:
        # Already processed (duplicate ref) — transaction rolled back, no double credit
        return await _get_balance(db, uid)


async def _debit(db, uid: int, amount: int, kind: str, meta: dict | None = None) -> tuple[int | None, int | None]:
    """Subtract funds atomically. Returns (new_balance, txn_id), or (None, None)."""
    meta_json = json.dumps(meta) if meta else None
    async with db.transaction():
        bal = await db.fetchval(
            "SELECT balance FROM user_balance WHERE telegram_user_id=$1 FOR UPDATE", uid
        ) or 0
        if bal < amount:
            return None, None
        new_bal = bal - amount
        await db.execute(
            "UPDATE user_balance SET balance=$2, updated_at=NOW() WHERE telegram_user_id=$1",
            uid, new_bal,
        )
        txn_id = await db.fetchval(
            "INSERT INTO payment_txns (telegram_user_id, kind, amount, balance_after, meta) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id",
            uid, kind, -amount, new_bal, meta_json,
        )
        return new_bal, txn_id


async def _accrue_referral_bonus(db, referee_id: int, spend: int, source_ref: str) -> None:
    """If the referee was referred, schedule a matured-in-24h bonus for the referrer."""
    referrer = await db.fetchval(
        "SELECT referrer_id FROM referrals WHERE referee_id=$1", referee_id
    )
    if not referrer or referrer == referee_id:
        return
    bonus = spend * REFERRAL_PERCENT // 100
    if bonus <= 0:
        return
    await db.execute(
        """
        INSERT INTO referral_bonuses (referrer_id, referee_id, source_ref, amount, available_at)
        VALUES ($1,$2,$3,$4, NOW() + ($5 || ' hours')::interval)
        ON CONFLICT (source_ref) DO NOTHING
        """,
        referrer, referee_id, source_ref, bonus, str(REFERRAL_HOLD_HOURS),
    )


async def _referral_maturation_loop():
    """Credit matured referral bonuses to referrers. Runs every 10 minutes."""
    import asyncio
    while True:
        try:
            if core.pool is not None:
                async with core.pool.acquire() as db:
                    rows = await db.fetch(
                        "SELECT id, referrer_id, amount FROM referral_bonuses "
                        "WHERE NOT paid AND available_at <= NOW() LIMIT 200"
                    )
                    for r in rows:
                        await _credit(db, r["referrer_id"], r["amount"], "referral_bonus",
                                      ref=f"refbonus:{r['id']}")
                        await db.execute(
                            "UPDATE referral_bonuses SET paid=TRUE WHERE id=$1", r["id"])
                        try:
                            await core.bot.send_message(
                                r["referrer_id"],
                                f"💰 Реферальный бонус +{int(r['amount']/100)} ₽ зачислен на баланс!")
                        except Exception:
                            pass
        except Exception:
            log.exception("referral maturation loop error")
        await asyncio.sleep(600)


@router.get("/api/balance")
async def get_balance_endpoint(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    bal = await _get_balance(db, user_id)
    return {"balance": bal, "balance_rub": round(bal / 100, 2)}


async def _get_bot_username() -> str:
    if core._bot_username is None:
        try:
            me = await core.bot.get_me()
            core._bot_username = me.username or ""
        except Exception:
            core._bot_username = ""
    return core._bot_username


@router.get("/api/referral")
async def referral_info(user_id: int = Depends(get_current_user), db=Depends(get_db)):
    username = await _get_bot_username()
    link = f"https://t.me/{username}?start=ref_{user_id}" if username else ""
    invited = await db.fetchval(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1", user_id) or 0
    earned = await db.fetchval(
        "SELECT COALESCE(SUM(amount),0) FROM referral_bonuses WHERE referrer_id=$1 AND paid", user_id) or 0
    pending = await db.fetchval(
        "SELECT COALESCE(SUM(amount),0) FROM referral_bonuses WHERE referrer_id=$1 AND NOT paid", user_id) or 0
    return {
        "link": link,
        "percent": REFERRAL_PERCENT,
        "invited": invited,
        "earned_rub": round(earned / 100, 2),
        "pending_rub": round(pending / 100, 2),
    }


# ── YooKassa top-up ───────────────────────────────────────────────────────────

_TOPUP_AMOUNTS = {100, 200, 500, 1000}   # rubles (minimum top-up 100 ₽)


# Top-up DISABLED: no paid feature currently consumes balance, so accepting money
# would be money-in / nothing-out. Endpoints kept (410) so old clients fail cleanly.
@router.post("/api/balance/topup")
async def create_topup(body: dict, user_id: int = Depends(get_current_user)):
    raise HTTPException(410, "Пополнение временно отключено")


@router.post("/api/balance/topup-stars")
async def create_topup_stars(body: dict, user_id: int = Depends(get_current_user)):
    raise HTTPException(410, "Пополнение временно отключено")


@router.post("/api/yookassa/webhook")
async def yookassa_webhook(body: dict, db=Depends(get_db)):
    """YooKassa server-to-server notification. We DO NOT trust the body — we
    re-fetch the authoritative payment from the API before crediting, so a
    forged webhook can't credit anyone."""
    obj = body.get("object") or {}
    pid = obj.get("id")
    if not pid or not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        return {"ok": True}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.yookassa.ru/v3/payments/{pid}",
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            )
        real = r.json()
    except Exception:
        log.exception("yookassa verify failed for %s", pid)
        return {"ok": True}

    if real.get("status") != "succeeded":
        return {"ok": True}
    try:
        uid = int((real.get("metadata") or {}).get("user_id") or 0)
        kopecks = int(round(float(real["amount"]["value"]) * 100))
    except Exception:
        return {"ok": True}
    if uid and kopecks > 0:
        new_bal = await _credit(db, uid, kopecks, "topup_yookassa", ref=pid,
                                meta={"amount_rub": real["amount"]["value"]})
        await track(uid, "payment_succeeded",
                    props={"kopecks": kopecks, "method": "yookassa", "ref": pid})
        try:
            await core.bot.send_message(
                uid, f"✅ Баланс пополнен на {int(kopecks/100)} ₽.\nТекущий баланс: {int(new_bal/100)} ₽"
            )
        except Exception:
            pass
    return {"ok": True}
