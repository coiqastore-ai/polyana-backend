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
                SELECT *
                FROM yookassa_test_orders
                WHERE id=$1
                FOR UPDATE
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
                != order["amount_minor"]
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

            if (
                TEST_MODE
                and payment.get("test") is not True
            ):
                raise HTTPException(
                    409,
                    "Expected a test payment",
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

            await db.execute(
                """
                UPDATE yookassa_test_orders
                SET
                    yookassa_payment_id=$2,
                    status=CAST($3 AS varchar),
                    is_test=$4,
                    raw_payment=$5::jsonb,
                    confirmation_url=COALESCE(
                        $6,
                        confirmation_url
                    ),
                    paid_at=CASE
                        WHEN CAST($3 AS text)='succeeded'
                        THEN COALESCE(
                            paid_at,
                            NOW()
                        )
                        ELSE paid_at
                    END,
                    canceled_at=CASE
                        WHEN CAST($3 AS text)='canceled'
                        THEN COALESCE(
                            canceled_at,
                            NOW()
                        )
                        ELSE canceled_at
                    END,
                    updated_at=NOW()
                WHERE id=$1
                """,
                order_id,
                payment_id,
                status,
                bool(payment.get("test")),
                json.dumps(
                    payment,
                    ensure_ascii=False,
                ),
                confirmation_url,
            )

            updated = await db.fetchrow(
                """
                SELECT *
                FROM yookassa_test_orders
                WHERE id=$1
                """,
                order_id,
            )

            return dict(updated)


@app.on_event("startup")
async def startup() -> None:
    global pool

    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=4,
    )

    async with pool.acquire() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS
            yookassa_test_orders (
                id UUID PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                package_code TEXT NOT NULL,
                package_title TEXT NOT NULL,
                base_points BIGINT NOT NULL,
                promo_points BIGINT
                    NOT NULL DEFAULT 0,
                total_points BIGINT NOT NULL,
                amount_minor BIGINT NOT NULL,
                currency VARCHAR(3)
                    NOT NULL DEFAULT 'RUB',
                status VARCHAR(32)
                    NOT NULL DEFAULT 'created',
                yookassa_payment_id
                    TEXT UNIQUE,
                idempotency_key
                    TEXT NOT NULL UNIQUE,
                confirmation_url TEXT,
                is_test BOOLEAN,
                raw_payment JSONB,
                created_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW(),
                paid_at TIMESTAMPTZ,
                canceled_at TIMESTAMPTZ
            );

            CREATE INDEX IF NOT EXISTS
            idx_yookassa_test_orders_user
            ON yookassa_test_orders(
                user_id,
                created_at DESC
            );
            """
        )

    log.info(
        (
            "ПОЛЯНА Pay started; "
            "test_mode=%s "
            "credit_enabled=%s"
        ),
        TEST_MODE,
        CREDIT_ENABLED,
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
        "test_mode": TEST_MODE,
        "credit_enabled": CREDIT_ENABLED,
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
                package_title,
                amount_minor,
                status,
                created_at
            FROM yookassa_test_orders
            WHERE user_id=$1
            ORDER BY created_at DESC
            LIMIT 5
            """,
            int(session["uid"]),
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
                    f"{order['amount_minor'] / 100:.2f} ₽ — "
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

    assert pool is not None

    async with pool.acquire() as db:
        package = await db.fetchrow(
            """
            SELECT
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

        order_id = uuid.uuid4()
        idempotency_key = str(order_id)

        total_points = (
            int(package["base_points"])
            + int(package["promo_points"] or 0)
        )

        amount_minor = int(
            package["rub_amount_minor"]
        )

        await db.execute(
            """
            INSERT INTO yookassa_test_orders (
                id,
                user_id,
                username,
                package_code,
                package_title,
                base_points,
                promo_points,
                total_points,
                amount_minor,
                currency,
                status,
                idempotency_key
            )
            VALUES (
                $1,$2,$3,$4,$5,
                $6,$7,$8,$9,
                'RUB','creating',$10
            )
            """,
            order_id,
            int(session["uid"]),
            session.get("username") or None,
            package["code"],
            package["title"],
            int(package["base_points"]),
            int(package["promo_points"] or 0),
            total_points,
            amount_minor,
            idempotency_key,
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
                UPDATE yookassa_test_orders
                SET
                    status='create_failed',
                    updated_at=NOW()
                WHERE id=$1
                """,
                order_id,
            )

        raise

    confirmation_url = order.get(
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
            SELECT *
            FROM yookassa_test_orders
            WHERE
                id=$1
                AND user_id=$2
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
        "yookassa_payment_id"
    ):
        try:
            payment = await yookassa_request(
                "GET",
                (
                    "/payments/"
                    f"{order_data['yookassa_payment_id']}"
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
                  order_data["amount_minor"]
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
    }

    if event not in allowed_events:
        return JSONResponse(
            {
                "ok": True,
                "ignored": True,
            }
        )

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

    log.info(
        (
            "Webhook verified: "
            "event=%s payment=%s "
            "order=%s status=%s"
        ),
        event,
        payment_id,
        order["id"],
        order["status"],
    )

    return {
        "ok": True,
    }
