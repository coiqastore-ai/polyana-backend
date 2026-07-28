from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
import uuid
from urllib.parse import parse_qs

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("polyana-pay")


DOMAIN = os.environ["PAYMENT_SITE_DOMAIN"]
BASE_URL = f"https://{DOMAIN}"

DATABASE_URL = os.environ["DATABASE_URL"]

BOT_USERNAME = os.getenv(
    "TELEGRAM_LOGIN_BOT_USERNAME",
    "reciptesbot",
).lstrip("@")

BOT_TOKEN = os.environ["TELEGRAM_LOGIN_BOT_TOKEN"]

SESSION_SECRET = os.environ[
    "PAYMENT_SESSION_SECRET"
].encode()

SESSION_MAX_AGE = int(
    os.getenv(
        "PAYMENT_SESSION_MAX_AGE_SECONDS",
        "604800",
    )
)

TELEGRAM_LOGIN_MAX_AGE = int(
    os.getenv(
        "TELEGRAM_LOGIN_MAX_AGE_SECONDS",
        "86400",
    )
)

YOOKASSA_API_URL = os.getenv(
    "YOOKASSA_API_URL",
    "https://api.yookassa.ru/v3",
).rstrip("/")

YOOKASSA_SHOP_ID = os.environ[
    "YOOKASSA_SHOP_ID"
]

YOOKASSA_SECRET_KEY = os.environ[
    "YOOKASSA_SECRET_KEY"
]

YOOKASSA_RETURN_URL = os.getenv(
    "YOOKASSA_RETURN_URL",
    f"{BASE_URL}/payment/result",
)

WEBHOOK_TOKEN = os.environ[
    "PAYMENT_WEBHOOK_TOKEN"
]

TEST_MODE = (
    os.getenv(
        "PAYMENT_TEST_MODE",
        "true",
    ).lower()
    == "true"
)

CREDIT_ENABLED = (
    os.getenv(
        "PAYMENT_CREDIT_ENABLED",
        "false",
    ).lower()
    == "true"
)

YOOKASSA_MODE = os.getenv(
    "YOOKASSA_MODE",
    "test",
).strip().lower()

PAYMENT_LIVE_READY = (
    os.getenv(
        "PAYMENT_LIVE_READY",
        "false",
    ).lower()
    == "true"
)

PAYMENT_RECEIPT_MODE = os.getenv(
    "PAYMENT_RECEIPT_MODE",
    "full_prepayment",
).strip()

if PAYMENT_RECEIPT_MODE not in {
    "full_prepayment",
    "full_payment",
}:
    raise RuntimeError(
        "PAYMENT_RECEIPT_MODE must be "
        "full_prepayment or full_payment"
    )


app = FastAPI(
    title="ПОЛЯНА Pay",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

pool: asyncpg.Pool | None = None


def b64encode(raw: bytes) -> str:
    return (
        base64.urlsafe_b64encode(raw)
        .rstrip(b"=")
        .decode()
    )


def b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(
        value + "=" * (-len(value) % 4)
    )


def create_session(
    user_id: int,
    username: str | None,
) -> str:
    payload = {
        "uid": int(user_id),
        "username": username or "",
        "csrf": secrets.token_urlsafe(24),
        "exp": int(time.time()) + SESSION_MAX_AGE,
    }

    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    signature = hmac.new(
        SESSION_SECRET,
        raw,
        hashlib.sha256,
    ).digest()

    return (
        f"{b64encode(raw)}."
        f"{b64encode(signature)}"
    )


def read_session(
    request: Request,
) -> dict | None:
    token = request.cookies.get(
        "polyana_pay_session",
        "",
    )

    try:
        raw_part, signature_part = token.split(
            ".",
            1,
        )

        raw = b64decode(raw_part)
        signature = b64decode(signature_part)

        expected = hmac.new(
            SESSION_SECRET,
            raw,
            hashlib.sha256,
        ).digest()

        if not hmac.compare_digest(
            signature,
            expected,
        ):
            return None

        payload = json.loads(raw)

        if int(payload.get("exp", 0)) < int(
            time.time()
        ):
            return None

        return payload

    except Exception:
        return None


def verify_telegram_login(
    params: dict[str, str],
) -> dict:
    received_hash = params.get("hash", "")
    auth_date_raw = params.get(
        "auth_date",
        "",
    )
    user_id_raw = params.get("id", "")

    if (
        not received_hash
        or not auth_date_raw
        or not user_id_raw
    ):
        raise ValueError(
            "Telegram login data is incomplete"
        )

    check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(
            params.items()
        )
        if key != "hash"
    )

    secret_key = hashlib.sha256(
        BOT_TOKEN.encode()
    ).digest()

    expected_hash = hmac.new(
        secret_key,
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        received_hash,
        expected_hash,
    ):
        raise ValueError(
            "Invalid Telegram login signature"
        )

    auth_date = int(auth_date_raw)
    now = int(time.time())

    if (
        auth_date > now + 60
        or now - auth_date
        > TELEGRAM_LOGIN_MAX_AGE
    ):
        raise ValueError(
            "Telegram login data has expired"
        )

    return {
        "id": int(user_id_raw),
        "username": params.get(
            "username",
            "",
        ),
        "first_name": params.get(
            "first_name",
            "",
        ),
    }


def render_page(
    title: str,
    body: str,
) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta
  name="viewport"
  content="width=device-width,initial-scale=1"
>
<title>{html.escape(title)} — ПОЛЯНА</title>
<style>
:root {{
  color-scheme: dark;
}}

* {{
  box-sizing: border-box;
}}

body {{
  margin: 0;
  background: #101c29;
  color: #ffffff;
  font-family: Arial, sans-serif;
}}

main {{
  max-width: 560px;
  margin: 0 auto;
  padding: 42px 18px;
}}

.card,
.package {{
  background: #1b2c3d;
  border-radius: 20px;
  padding: 24px;
  margin-bottom: 16px;
}}

.package {{
  border: 1px solid #385069;
}}

h1,
h2 {{
  margin-top: 0;
}}

p {{
  color: #dce5ed;
  line-height: 1.5;
}}

small {{
  color: #9fb0bf;
}}

button,
.button {{
  display: block;
  width: 100%;
  border: 0;
  border-radius: 12px;
  padding: 14px;
  background: #67b96b;
  color: #07140a;
  font-size: 16px;
  font-weight: 700;
  text-align: center;
  text-decoration: none;
  cursor: pointer;
}}

.secondary {{
  background: #2a4056;
  color: #ffffff;
}}

.badge {{
  display: inline-block;
  padding: 6px 10px;
  border-radius: 999px;
  background: #f1a442;
  color: #172331;
  font-weight: 700;
}}

.ok {{
  padding: 12px;
  border-radius: 12px;
  background: #224f36;
}}

.error {{
  padding: 12px;
  border-radius: 12px;
  background: #5a2630;
}}
</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>"""

    return HTMLResponse(document)


async def yookassa_request(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    headers = {
        "Accept": "application/json",
    }

    if idempotency_key:
        headers[
            "Idempotence-Key"
        ] = idempotency_key

    async with httpx.AsyncClient(
        auth=httpx.BasicAuth(
            YOOKASSA_SHOP_ID,
            YOOKASSA_SECRET_KEY,
        ),
        timeout=25,
    ) as client:
        response = await client.request(
            method,
            f"{YOOKASSA_API_URL}{path}",
            headers=headers,
            json=body,
        )

    try:
        data = response.json()
    except Exception:
        data = {
            "description": response.text[:500]
        }

    if response.status_code >= 400:
        description = (
            data.get("description")
            or data.get("code")
            or "unknown error"
        )

        raise HTTPException(
            502,
            (
                f"YooKassa API error "
                f"{response.status_code}: "
                f"{description}"
            ),
        )

    return data


async def synchronize_order(
    payment: dict,
) -> dict:
    payment_id = str(
        payment.get("id") or ""
    )

    metadata = payment.get(
        "metadata"
    ) or {}

    internal_order_id = metadata.get(
        "internal_order_id"
    )

    if not payment_id or not internal_order_id:
        raise HTTPException(
            400,
            "Payment metadata is incomplete",
        )

    try:
        order_id = uuid.UUID(
            str(internal_order_id)
        )
    except ValueError as exc:
        raise HTTPException(
            400,
            "Invalid internal order id",
        ) from exc

    assert pool is not None

    async with pool.acquire() as db:
        async with db.transaction():
            order = await db.fetchrow(
                """
                SELECT po.*, pp.code AS package_code
                FROM payment_orders po
                JOIN payment_packages pp ON pp.id = po.package_id
                WHERE po.id=$1
                FOR UPDATE OF po
                """,
                order_id,
            )

            if not order:
                raise HTTPException(
                    404,
                    "Order not found",
                )

            amount = payment.get(
                "amount"
            ) or {}

            actual_minor = int(
                round(
                    float(
                        amount.get(
                            "value",
                            "0",
                        )
                    )
                    * 100
                )
            )

            actual_currency = str(
                amount.get("currency") or ""
            )

            if (
                actual_minor
                != order["amount"]
                or actual_currency
                != order["currency"]
            ):
                raise HTTPException(
                    409,
                    "Payment amount mismatch",
                )

            if (
                str(
                    metadata.get(
                        "package_code"
                    )
                    or ""
                )
                != order["package_code"]
            ):
                raise HTTPException(
                    409,
                    "Payment package mismatch",
                )

            if (
                str(
                    metadata.get(
                        "telegram_user_id"
                    )
                    or ""
                )
                != str(order["user_id"])
            ):
                raise HTTPException(
                    409,
                    "Payment user mismatch",
                )

            status = str(
                payment.get("status")
                or "unknown"
            )

            confirmation_url = (
                payment.get(
                    "confirmation"
                )
                or {}
            ).get("confirmation_url")

            receipt_registration_raw = payment.get("receipt_registration")
            receipt_registration = (
                str(receipt_registration_raw)
                if receipt_registration_raw
                else None
            )

            await db.execute(
                """
                UPDATE payment_orders
                SET
                    external_payment_id=$2,
                    status=CAST($3 AS varchar),
                    metadata=COALESCE(metadata, '{}'::jsonb) || $4::jsonb,
                    receipt_registration=COALESCE(CAST($5 AS varchar), receipt_registration),
                    paid_at=CASE
                        WHEN CAST($3 AS text)='succeeded'
                        THEN COALESCE(
                            paid_at,
                            NOW()
                        )
                        ELSE paid_at
                    END,
                    cancelled_at=CASE
                        WHEN CAST($3 AS text)='canceled'
                        THEN COALESCE(
                            cancelled_at,
                            NOW()
                        )
                        ELSE cancelled_at
                    END,
                    updated_at=NOW()
                WHERE id=$1
                """,
                order_id,
                payment_id,
                status,
                json.dumps(
                    {
                        "raw_payment": payment,
                        "confirmation_url": confirmation_url,
                        "test": payment.get("test", False),
                    },
                    ensure_ascii=False,
                ),
                receipt_registration,
            )

            updated = await db.fetchrow(
                """
                SELECT *
                FROM payment_orders
                WHERE id=$1
                """,
                order_id,
            )

            return dict(updated)


@app.on_event("startup")
async def startup() -> None:
    global pool

    # Safety: live mode cannot use test mode
    if YOOKASSA_MODE == "live" and TEST_MODE:
        raise RuntimeError(
            "Live mode cannot use PAYMENT_TEST_MODE=true"
        )

    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=4,
    )

    async with pool.acquire() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS
            payment_customer_emails (
                user_id BIGINT PRIMARY KEY,
                email TEXT NOT NULL,
                updated_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW()
            );
            """
        )

    log.info(
        (
            "ПОЛЯНА Pay started; "
            "mode=%s "
            "test_mode=%s "
            "credit_enabled=%s "
            "live_ready=%s "
            "receipt_mode=%s"
        ),
        YOOKASSA_MODE,
        TEST_MODE,
        CREDIT_ENABLED,
        PAYMENT_LIVE_READY,
        PAYMENT_RECEIPT_MODE,
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    if pool:
        await pool.close()


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "ПОЛЯНА Pay",
        "mode": YOOKASSA_MODE,
        "test_mode": TEST_MODE,
        "credit_enabled": CREDIT_ENABLED,
        "live_ready": PAYMENT_LIVE_READY,
        "receipt_mode": PAYMENT_RECEIPT_MODE,
    }


@app.get("/")
async def home(
    request: Request,
):
    session = read_session(request)

    if not session:
        return render_page(
            "Оплата AI-баллов",
            f"""
            <section class="card">
              <span class="badge">
                Тестовый режим
              </span>

              <h1>🌿 ПОЛЯНА</h1>

              <p>
                Войдите через Telegram,
                чтобы открыть страницу
                тестовой оплаты.
              </p>

              <script
                async
                src="https://telegram.org/js/telegram-widget.js?22"
                data-telegram-login="{html.escape(BOT_USERNAME)}"
                data-size="large"
                data-auth-url="{BASE_URL}/auth/telegram"
              ></script>

              <p>
                <small>
                  Реальные деньги не принимаются.
                  AI-баллы пока не начисляются.
                </small>
              </p>
            </section>
            """,
        )

    assert pool is not None

    async with pool.acquire() as db:
        packages = await db.fetch(
            """
            SELECT
                code,
                title,
                description,
                base_points,
                promo_points,
                rub_amount_minor
            FROM payment_packages
            WHERE active_for_yookassa=TRUE
            ORDER BY
                sort_order,
                base_points
            """
        )

        recent_orders = await db.fetch(
            """
            SELECT
                pp.title AS package_title,
                po.amount,
                po.status,
                po.created_at
            FROM payment_orders po
            JOIN payment_packages pp ON pp.id = po.package_id
            WHERE po.user_id=$1
            ORDER BY po.created_at DESC
            LIMIT 5
            """,
            int(session["uid"]),
        )

        saved_email = await db.fetchval(
            "SELECT email FROM payment_customer_emails WHERE user_id=$1",
            int(session["uid"]),
        )

    # Email gate: no email → show email form instead of packages
    if not saved_email:
        return render_page(
            "Email для чека",
            f"""
            <section class="card">
              <span class="badge">
                Тестовый магазин
              </span>

              <h1>📧 Email для кассового чека</h1>

              <p>
                Чек об оплате будет отправлен
                на эту почту.
              </p>

              <form method="post" action="/api/save-email">
                <p>
                  <input
                    type="email"
                    name="email"
                    placeholder="user@example.com"
                    required
                    maxlength="254"
                    style="width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #385069;background:#101c29;color:#fff;font-size:16px"
                  >
                </p>
                <input
                  type="hidden"
                  name="csrf"
                  value="{html.escape(session["csrf"])}"
                >
                <button type="submit">
                  Сохранить и продолжить
                </button>
              </form>

              <p>
                <small>
                  Email сохраняется для следующих покупок.
                  Изменить можно в любой момент.
                  В рассылки не используем без отдельного согласия.
                </small>
              </p>
            </section>
            """,
        )

    packages_html = ""

    for package in packages:
        total_points = (
            int(package["base_points"])
            + int(package["promo_points"] or 0)
        )

        rubles = (
            int(package["rub_amount_minor"])
            / 100
        )

        packages_html += f"""
        <form
          class="package"
          method="post"
          action="/api/create-payment"
        >
          <h2>
            {html.escape(package["title"])}
          </h2>

          <p>
            {html.escape(
                package["description"] or ""
            )}
          </p>

          <p>
            <b>
              {total_points} AI-баллов —
              {rubles:.0f} ₽
            </b>
          </p>

          <input
            type="hidden"
            name="package_code"
            value="{html.escape(package["code"])}"
          >

          <input
            type="hidden"
            name="csrf"
            value="{html.escape(session["csrf"])}"
          >

          <button type="submit">
            Перейти к тестовой оплате
          </button>
        </form>
        """

    if not packages_html:
        packages_html = """
        <section class="card">
          <p class="error">
            Активные пакеты не найдены.
          </p>
        </section>
        """

    recent_html = ""

    if recent_orders:
        rows = []

        for order in recent_orders:
            rows.append(
                (
                    "<li>"
                    f"{html.escape(order['package_title'])}: "
                    f"{order['amount'] / 100:.2f} ₽ — "
                    f"{html.escape(order['status'])}"
                    "</li>"
                )
            )

        recent_html = (
            '<section class="card">'
            "<h2>Последние попытки</h2>"
            "<ul>"
            + "".join(rows)
            + "</ul>"
            "</section>"
        )

    return render_page(
        "Оплата AI-баллов",
        f"""
        <section class="card">
          <span class="badge">
            Тестовый магазин
          </span>

          <h1>AI-баллы ПОЛЯНЫ</h1>

          <p>
            Telegram ID:
            <b>{int(session["uid"])}</b>
          </p>

          <p>
            📧 Чеки:
            <b>{html.escape(saved_email)}</b>
            · <a class="button secondary" style="display:inline;width:auto;margin-top:8px" href="/profile/email">изменить</a>
          </p>

          <p>
            <small>
              ЮKassa проведёт тестовый платёж,
              но кошелёк ПОЛЯНЫ пока
              не пополнится.
            </small>
          </p>
        </section>

        {packages_html}
        {recent_html}
        """,
    )


@app.get("/auth/telegram")
async def telegram_auth(
    request: Request,
):
    params = dict(
        request.query_params
    )

    try:
        user = verify_telegram_login(
            params
        )

    except Exception as exc:
        log.warning(
            "Telegram login rejected: %s",
            exc,
        )

        return render_page(
            "Ошибка входа",
            f"""
            <section class="card">
              <p class="error">
                {html.escape(str(exc))}
              </p>
            </section>
            """,
        )

    response = RedirectResponse(
        "/",
        status_code=303,
    )

    response.set_cookie(
        "polyana_pay_session",
        create_session(
            user["id"],
            user.get("username"),
        ),
        max_age=SESSION_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )

    return response


@app.get("/profile/email")
async def profile_email(
    request: Request,
):
    session = read_session(request)

    if not session:
        return RedirectResponse(
            "/",
            status_code=303,
        )

    assert pool is not None

    async with pool.acquire() as db:
        saved_email = await db.fetchval(
            "SELECT email FROM payment_customer_emails WHERE user_id=$1",
            int(session["uid"]),
        )

    return render_page(
        "Изменить email",
        f"""
        <section class="card">
          <h1>📧 Email для кассового чека</h1>

          <p>
            Чек об оплате будет отправлен
            на эту почту.
          </p>

          <form method="post" action="/api/save-email">
            <p>
              <input
                type="email"
                name="email"
                value="{html.escape(saved_email or "")}"
                placeholder="user@example.com"
                required
                maxlength="254"
                style="width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #385069;background:#101c29;color:#fff;font-size:16px"
              >
            </p>
            <input
              type="hidden"
              name="csrf"
              value="{html.escape(session["csrf"])}"
            >
            <button type="submit">
              Сохранить
            </button>
          </form>

          <p>
            <small>
              В рассылки не используем без отдельного согласия.
            </small>
          </p>

          <a class="button secondary" href="/">Назад к пакетам</a>
        </section>
        """,
    )


@app.post("/api/save-email")
async def save_email(
    request: Request,
):
    session = read_session(request)

    if not session:
        raise HTTPException(
            401,
            "Telegram login required",
        )

    form = parse_qs(
        (
            await request.body()
        ).decode(
            "utf-8",
            errors="replace",
        )
    )

    raw_email = (
        form.get("email")
        or [""]
    )[0]

    csrf = (
        form.get("csrf")
        or [""]
    )[0]

    if not csrf or not hmac.compare_digest(
        csrf,
        str(session.get("csrf") or ""),
    ):
        raise HTTPException(
            403,
            "CSRF check failed",
        )

    import re

    email = raw_email.strip()

    if not email or len(email) > 254:
        raise HTTPException(
            400,
            "Email пустой или слишком длинный",
        )

    if not re.match(
        r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
        email,
    ):
        raise HTTPException(
            400,
            "Неверный формат email",
        )

    # Нормализация: локальную часть оставляем как ввёл пользователь,
    # домен приводим к нижнему регистру.
    local_part, domain = email.rsplit("@", 1)
    email = f"{local_part}@{domain.lower()}"

    assert pool is not None

    async with pool.acquire() as db:
        await db.execute(
            """
            INSERT INTO payment_customer_emails
                (user_id, email)
            VALUES ($1, $2)
            ON CONFLICT (user_id)
            DO UPDATE SET email=$2, updated_at=NOW()
            """,
            int(session["uid"]),
            email,
        )

    return RedirectResponse(
        "/",
        status_code=303,
    )


@app.post("/api/create-payment")
async def create_payment(
    request: Request,
):
    session = read_session(request)

    if not session:
        raise HTTPException(
            401,
            "Telegram login required",
        )

    form = parse_qs(
        (
            await request.body()
        ).decode(
            "utf-8",
            errors="replace",
        )
    )

    package_code = (
        form.get("package_code")
        or [""]
    )[0]

    csrf = (
        form.get("csrf")
        or [""]
    )[0]

    if not csrf or not hmac.compare_digest(
        csrf,
        str(session.get("csrf") or ""),
    ):
        raise HTTPException(
            403,
            "CSRF check failed",
        )

    # Live-ready gate
    if YOOKASSA_MODE == "live" and not PAYMENT_LIVE_READY:
        raise HTTPException(
            503,
            "Оплата временно недоступна",
        )

    assert pool is not None

    async with pool.acquire() as db:
        package = await db.fetchrow(
            """
            SELECT
                id,
                code,
                title,
                description,
                base_points,
                promo_points,
                rub_amount_minor
            FROM payment_packages
            WHERE
                code=$1
                AND active_for_yookassa=TRUE
            """,
            package_code,
        )

        if not package:
            raise HTTPException(
                404,
                "Payment package not found",
            )

        # Email обязателен для фискального чека ЮKassa (доставка только по email)
        email = await db.fetchval(
            "SELECT email FROM payment_customer_emails WHERE user_id=$1",
            int(session["uid"]),
        )
        if not email:
            raise HTTPException(
                400,
                "Укажите email для кассового чека на главной странице",
            )

        order_id = uuid.uuid4()
        idempotency_key = str(order_id)

        total_points = (
            int(package["base_points"])
            + int(package["promo_points"] or 0)
        )

        amount_minor = int(
            package["rub_amount_minor"]
        )

        environment = "test" if TEST_MODE else "live"

        await db.execute(
            """
            INSERT INTO payment_orders (
                id,
                user_id,
                package_id,
                provider,
                environment,
                base_points,
                promo_points,
                total_points,
                amount,
                currency,
                status,
                idempotency_key,
                referral_base_points,
                invoice_payload
            )
            VALUES (
                $1,$2,$3,'yookassa',$4,
                $5,$6,$7,$8,
                'RUB','creating',$9,0,$10
            )
            """,
            order_id,
            int(session["uid"]),
            package["id"],
            environment,
            int(package["base_points"]),
            int(package["promo_points"] or 0),
            total_points,
            amount_minor,
            idempotency_key,
            f"polyana:{order_id}",
        )

    payment_payload = {
        "amount": {
            "value": (
                f"{amount_minor / 100:.2f}"
            ),
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": (
                f"{YOOKASSA_RETURN_URL}"
                f"?order_id={order_id}"
            ),
        },
        "description": (
            f"ПОЛЯНА: "
            f"{total_points} AI-баллов"
        ),
        "metadata": {
            "internal_order_id": str(
                order_id
            ),
            "package_code": str(
                package["code"]
            ),
            "telegram_user_id": str(
                session["uid"]
            ),
        },
        "receipt": {
            "customer": {
                "email": email,
            },
            "items": [
                {
                    "description": (
                        f"Доступ к AI-функциям ПОЛЯНЫ — "
                        f"пакет {total_points} баллов"
                    ),
                    "quantity": 1.0,
                    "amount": {
                        "value": (
                            f"{amount_minor / 100:.2f}"
                        ),
                        "currency": "RUB",
                    },
                    "vat_code": 1,
                    "payment_subject": "service",
                    "payment_mode": PAYMENT_RECEIPT_MODE,
                    "measure": "piece",
                }
            ],
            "internet": True,
        },
    }

    try:
        payment = await yookassa_request(
            "POST",
            "/payments",
            body=payment_payload,
            idempotency_key=(
                idempotency_key
            ),
        )

        order = await synchronize_order(
            payment
        )

    except Exception:
        async with pool.acquire() as db:
            await db.execute(
                """
                UPDATE payment_orders
                SET
                    status='create_failed',
                    updated_at=NOW()
                WHERE id=$1
                """,
                order_id,
            )

        raise

    raw_meta = order.get("metadata") or {}
    order_metadata = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
    confirmation_url = order_metadata.get(
        "confirmation_url"
    )

    if not confirmation_url:
        raise HTTPException(
            502,
            (
                "YooKassa did not return "
                "confirmation_url"
            ),
        )

    return RedirectResponse(
        confirmation_url,
        status_code=303,
    )


@app.get("/payment/result")
async def payment_result(
    request: Request,
    order_id: str,
):
    session = read_session(request)

    if not session:
        return RedirectResponse(
            "/",
            status_code=303,
        )

    try:
        order_uuid = uuid.UUID(
            order_id
        )
    except ValueError:
        raise HTTPException(
            400,
            "Invalid order id",
        )

    assert pool is not None

    async with pool.acquire() as db:
        order = await db.fetchrow(
            """
            SELECT po.*, pp.title AS package_title
            FROM payment_orders po
            JOIN payment_packages pp ON pp.id = po.package_id
            WHERE
                po.id=$1
                AND po.user_id=$2
            """,
            order_uuid,
            int(session["uid"]),
        )

    if not order:
        raise HTTPException(
            404,
            "Order not found",
        )

    order_data = dict(order)

    if order_data.get(
        "external_payment_id"
    ):
        try:
            payment = await yookassa_request(
                "GET",
                (
                    "/payments/"
                    f"{order_data['external_payment_id']}"
                ),
            )

            order_data = (
                await synchronize_order(
                    payment
                )
            )

        except Exception as exc:
            log.warning(
                (
                    "Payment result refresh "
                    "failed: %s"
                ),
                exc,
            )

    status = order_data["status"]

    if status == "succeeded":
        status_message = """
        <p class="ok">
          <b>
            Тестовый платёж успешно завершён.
          </b>
          <br>
          ЮKassa подтвердила статус
          succeeded.
        </p>
        """

        if not CREDIT_ENABLED:
            status_message += """
            <p>
              AI-баллы намеренно не начислены:
              кошелёк пока работает
              в безопасном режиме.
            </p>
            """

    elif status == "canceled":
        status_message = """
        <p class="error">
          Платёж отменён.
        </p>
        """

    else:
        status_message = f"""
        <p>
          Текущий статус:
          <b>{html.escape(status)}</b>.
          Обновите страницу через несколько секунд.
        </p>
        """

    return render_page(
        "Результат оплаты",
        f"""
        <section class="card">
          <h1>Результат оплаты</h1>

          {status_message}

          <p>
            Пакет:
            <b>
              {html.escape(
                  order_data["package_title"]
              )}
            </b>
          </p>

          <p>
            Сумма:
            <b>
              {
                  order_data["amount"]
                  / 100
              :.2f} ₽
            </b>
          </p>

          <a
            class="button secondary"
            href="/"
          >
            Вернуться к пакетам
          </a>
        </section>
        """,
    )


@app.post(
    "/api/yookassa/webhook/{token}"
)
async def yookassa_webhook(
    token: str,
    request: Request,
):
    if not hmac.compare_digest(
        token,
        WEBHOOK_TOKEN,
    ):
        raise HTTPException(
            404,
            "Not found",
        )

    notification = await request.json()

    event = str(
        notification.get("event")
        or ""
    )

    payment_object = (
        notification.get("object")
        or {}
    )

    payment_id = str(
        payment_object.get("id")
        or ""
    )

    allowed_events = {
        "payment.succeeded",
        "payment.canceled",
        "payment.waiting_for_capture",
        "refund.succeeded",
    }

    if event not in allowed_events:
        return JSONResponse(
            {
                "ok": True,
                "ignored": True,
            }
        )

    if event == "refund.succeeded":
        # Handle refund separately — different object structure
        refund_object = payment_object
        refund_id = str(refund_object.get("id") or "")
        refund_payment_id = str(
            (refund_object.get("payment_id") or {}).get("id")
            if isinstance(refund_object.get("payment_id"), dict)
            else refund_object.get("payment_id") or ""
        )

        if not refund_id or not refund_payment_id:
            raise HTTPException(400, "Refund data incomplete")

        # Verify refund via API
        verified_refund = await yookassa_request(
            "GET", f"/refunds/{refund_id}"
        )

        refund_amount = verified_refund.get("amount") or {}
        refund_minor = int(round(float(refund_amount.get("value", "0")) * 100))
        refund_currency = str(refund_amount.get("currency") or "")

        async with pool.acquire() as db:
            async with db.transaction():
                # Find order by external_payment_id
                order = await db.fetchrow(
                    "SELECT * FROM payment_orders WHERE external_payment_id=$1 FOR UPDATE",
                    refund_payment_id,
                )
                if not order:
                    log.warning("Refund for unknown payment: %s", refund_payment_id)
                    return {"ok": True, "ignored": True}

                # Idempotent insert
                try:
                    await db.execute(
                        """INSERT INTO payment_refunds
                        (order_id, provider, external_refund_id, amount_minor, currency, status, processed_at, metadata)
                        VALUES ($1, 'yookassa', $2, $3, $4, 'succeeded', NOW(), $5)""",
                        order["id"], refund_id, refund_minor, refund_currency,
                        json.dumps(verified_refund, ensure_ascii=False),
                    )
                except asyncpg.UniqueViolationError:
                    log.info("Refund already processed: %s", refund_id)
                    return {"ok": True}

                # Proportional reversal
                total_amount = order["amount"] or 1
                base = order["base_points"] or 0
                promo = order["promo_points"] or 0

                if refund_minor >= total_amount:
                    # Full refund
                    reversed_base = base
                    reversed_promo = promo
                else:
                    ratio = refund_minor / total_amount
                    reversed_base = int(base * ratio)
                    reversed_promo = int(promo * ratio)

                # Reverse wallet points
                if reversed_base > 0 or reversed_promo > 0:
                    uid = order["user_id"]
                    wallet = await db.fetchrow(
                        "SELECT * FROM wallets WHERE user_id=$1 FOR UPDATE", uid
                    )
                    if wallet:
                        new_paid = max(0, (wallet["paid_points"] or 0) - reversed_base)
                        new_bonus = max(0, (wallet["bonus_points"] or 0) - reversed_promo)
                        await db.execute(
                            "UPDATE wallets SET paid_points=$2, bonus_points=$3, updated_at=NOW() WHERE user_id=$1",
                            uid, new_paid, new_bonus,
                        )

                # Update order
                new_refunded = (order["refunded_amount"] or 0) + refund_minor
                new_status = "refunded" if new_refunded >= total_amount else order["status"]
                await db.execute(
                    """UPDATE payment_orders SET
                    refunded_amount=$2, reversed_base_points=reversed_base_points+$3,
                    reversed_promo_points=reversed_promo_points+$4,
                    status=$5, updated_at=NOW()
                    WHERE id=$1""",
                    order["id"], new_refunded, reversed_base, reversed_promo, new_status,
                )

        log.info("Refund processed: refund=%s order=%s reversed_base=%s reversed_promo=%s",
                 refund_id, order["id"], reversed_base, reversed_promo)
        return {"ok": True}

    if not payment_id:
        raise HTTPException(
            400,
            "Payment id missing",
        )

    # Не доверяем данным webhook напрямую.
    # Повторно получаем объект платежа
    # через авторизованный API ЮKassa.
    verified_payment = (
        await yookassa_request(
            "GET",
            f"/payments/{payment_id}",
        )
    )

    order = await synchronize_order(
        verified_payment
    )

    # Credit wallet on payment.succeeded
    if order["status"] == "succeeded" and CREDIT_ENABLED:
        try:
            async with pool.acquire() as db:
                async with db.transaction():
                    # Lock order — idempotency gate
                    o = await db.fetchrow(
                        "SELECT * FROM payment_orders WHERE id=$1 FOR UPDATE",
                        order["id"])
                    if o["credited_at"] is not None:
                        log.info("Order %s already credited", order["id"])
                    else:
                        uid = o["user_id"]
                        base = o["base_points"]
                        promo = o.get("promo_points") or 0

                        # Ensure wallet
                        await db.execute(
                            "INSERT INTO wallets (user_id, paid_points) "
                            "VALUES ($1,0) ON CONFLICT DO NOTHING", uid)
                        w = await db.fetchrow(
                            "SELECT * FROM wallets WHERE user_id=$1 FOR UPDATE", uid)

                        paid_debt = w["paid_debt_points"] or 0
                        bonus_debt = w["bonus_debt_points"] or 0

                        base_debt = min(paid_debt, base)
                        base_paid = base - base_debt
                        promo_debt = min(bonus_debt, promo)
                        promo_bonus = promo - promo_debt

                        if base_debt > 0:
                            await db.execute(
                                "UPDATE wallets SET paid_debt_points=paid_debt_points-$2, "
                                "updated_at=NOW() WHERE user_id=$1", uid, base_debt)
                        if base_paid > 0:
                            await db.execute(
                                "UPDATE wallets SET paid_points=paid_points+$2, "
                                "updated_at=NOW() WHERE user_id=$1", uid, base_paid)
                        if promo_debt > 0:
                            await db.execute(
                                "UPDATE wallets SET bonus_debt_points=bonus_debt_points-$2, "
                                "updated_at=NOW() WHERE user_id=$1", uid, promo_debt)
                        if promo_bonus > 0:
                            await db.execute(
                                "UPDATE wallets SET bonus_points=bonus_points+$2, "
                                "updated_at=NOW() WHERE user_id=$1", uid, promo_bonus)

                        await db.execute(
                            "UPDATE payment_orders SET credited_at=NOW(), "
                            "credited_base_points=$2, credited_promo_points=$3, "
                            "updated_at=NOW() WHERE id=$1",
                            o["id"], base, promo)

                        log.info("Credited order %s: base=%s promo=%s user=%s",
                                 o["id"], base, promo, uid)
        except Exception as exc:
            log.error("Credit failed for order %s: %s", order["id"], exc)

    log.info(
        (
            "Webhook verified: "
            "event=%s payment=%s "
            "order=%s status=%s "
            "receipt_registration=%s"
        ),
        event,
        payment_id,
        order["id"],
        order["status"],
        order.get("receipt_registration"),
    )

    return {
        "ok": True,
    }
