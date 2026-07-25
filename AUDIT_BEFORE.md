# AUDIT_BEFORE.md — Состояние проекта до исправлений

> **ВНИМАНИЕ**: Аудит реконструирован постфактум по git history, коммитам, diff и production state.
> Текущий HEAD: `63dbd92` (master). Production VPS: `bd517cd` (refactor/split-main).
> Состояние «до» = production VPS (`bd517cd`), т.к. именно это реально работает в production.

**Дата аудита**: 2026-07-25
**Локальный HEAD**: `63dbd92` (master)
**Production HEAD**: `bd517cd` (refactor/split-main)
**Ветка production**: `refactor/split-main`

---

## Текущее состояние production (VPS `bd517cd`)

### 1. Onboarding — backend
**Статус**: `NOT_DEPLOYED`

- VPS main.py (4315 строк) **не содержит** системы onboarding
- Нет таблиц `users` (с onboarding_status), `pending_onboarding_actions`
- Нет класса `UserService` с методами onboarding
- Нет `WelcomeService`
- Нет callback `onboarding:*`, `ob_start_tutorial`, `ob_start_skip_to_legal`
- Нет эндпоинтов `/api/onboarding/status`, `/api/onboarding/step`, `/api/onboarding/skip`
- **Доказательство**: `grep -n "onboarding" main.py` → 0 результатов на VPS

### 2. Onboarding — frontend
**Статус**: `NOT_DEPLOYED`

- На VPS **нет фронтенда** (`/root/polyana-frontend/` не существует)
- Нет `index.html` на VPS
- Nginx проксирует `polyana.coiqa.ru` → `127.0.0.1:8100` (backend), нет статического фронтенда
- **Доказательство**: `find /root -name "index.html"` → только gbrain

### 3. Сохранение onboarding status
**Статус**: `NOT_DEPLOYED`

- Нет таблицы `users` с полями `onboarding_status`, `onboarding_step`, `onboarding_version`, `onboarding_completed_at`
- Нет `UserService.update_onboarding_step()`, `UserService.complete_onboarding()`
- **Доказательство**: VPS main.py не содержит SQL с `onboarding_status`

### 4. Повторное открытие Mini App
**Статус**: `NOT_DEPLOYED`

- Фронтенд не деплоен → проверка невозможна
- Нет эндпоинта `/api/onboarding/status` на VPS (возвращает 404)
- **Доказательство**: `curl http://127.0.0.1:8100/api/onboarding/status` → `{"detail":"Not Found"}`

### 5. Новый `/start`
**Статус**: `NOT_DEPLOYED`

- VPS `cmd_start` (строка 4081) показывает **старый текст**: `ПОЛЯНА — планировщик застолий с друзьями`
- Локальный HEAD (`63dbd92`) показывает **новый текст**: `ПОЛЯНА — личная библиотека рецептов`
- Нет `WelcomeService`, нет `_welcome_keyboard()`, нет `_returning_user_keyboard()`
- **Доказательство**: grep "планировщик" → строка 4243 на VPS

### 6. Документы
**Статус**: `NOT_DEPLOYED`

- Нет таблицы `legal_documents`
- Нет таблицы `user_legal_acceptances`
- Нет класса `LegalDocumentService`
- Нет эндпоинтов `/api/legal/documents`, `/api/legal/status`, `/api/legal/accept`
- Есть только `/terms` (команда бота) со статичным текстом
- **Доказательство**: `curl http://127.0.0.1:8100/api/documents` → `{"detail":"Not Found"}`

### 7. Legal API
**Статус**: `NOT_STARTED`

- Нет API для документов
- Нет рендеринга шаблонов из `legal_docs.py`
- **Доказательство**: VPS не содержит `legal_docs.py`

### 8. Consent middleware
**Статус**: `NOT_STARTED`

- Нет middleware для проверки согласий
- Нет блокировки AI-функций без consent
- **Доказательство**: grep "consent" → 0 на VPS

### 9. WalletService
**Статус**: `NOT_DEPLOYED`

- VPS использует **старую** таблицу `user_balance` (поле `balance` в копейках)
- Нет таблицы `wallets` с `paid_points`, `bonus_points`, `reserved_points`
- Нет `WalletService.get_balance()`, `WalletService.debit()`
- **Доказательство**: grep "wallets" → 0 на VPS

### 10. Wallet ledger
**Статус**: `NOT_DEPLOYED`

- Нет таблицы `wallet_ledger`
- Старая таблица `payment_txns` существует (старый формат)
- **Доказательство**: grep "wallet_ledger" → 0 на VPS

### 11. Пакеты пополнения
**Статус**: `NOT_DEPLOYED`

- Нет таблицы `payment_packages`
- Нет эндпоинта `/api/payment/packages`
- **Доказательство**: `curl http://127.0.0.1:8100/api/payment/packages` → `{"detail":"Not Found"}`

### 12. Telegram Stars
**Статус**: `PARTIAL`

- Есть базовый обработчик Stars (строка 3813+)
- Есть `user_balance` таблица
- Нет идемпотентности (нет `telegram_payment_charge_id` проверки)
- Нет `wallet_grants`
- **Доказательство**: grep "Stars" → строка 3813 на VPS

### 13. ЮKassa
**Статус**: `PARTIAL`

- Есть webhook handler (строка 2834)
- Есть верификация через API
- Нет внешнего сайта для оплаты
- Нет `payment_packages`
- **Доказательство**: `curl http://127.0.0.1:8100/api/yookassa/webhook` → существует

### 14. Реферальные начисления
**Статус**: `PARTIAL`

- Есть таблица `referrals`, `referral_bonuses`
- Есть матурация бонусов
- Нет таблицы `referral_rewards` (новый формат)
- Нет `ReferralService` с новым API
- **Доказательство**: grep "referral" → строки 440-2807 на VPS

### 15. Frontend
**Статус**: `NOT_DEPLOYED`

- Фронтенд **не деплоен** на VPS
- Нет файла `index.html` на сервере
- Nginx не раздаёт статику
- **Доказательство**: `ls /root/polyana-frontend/` → No such file

### 16. Миграции
**Статус**: `NOT_DEPLOYED`

- Миграции выполняются inline в `main.py` (CREATE TABLE IF NOT EXISTS)
- VPS main.py содержит миграции A-L
- Локальный HEAD содержит миграции A-V
- **Доказательство**: VPS имеет 4315 строк, local — 8082 строки

### 17. Production deployment
**Статус**: `PARTIAL`

- systemd сервис `polyana.service` работает (с `Jul 24 13:36:48 UTC`)
- polyana-worker.service **не существует**
- WorkingDirectory: `/root/polyana-backend`
- ExecStart: `uvicorn main:app --host 127.0.0.1 --port 8100 --workers 1`
- Диск: 85% заполнен (32G/38G)
- Память: 1.6G/1.9G использовано
- **Доказательство**: `systemctl status polyana` → active (running)

---

## Сводная таблица

| Функция | Статус | Доказательство |
|---|---|---|
| Onboarding backend | NOT_DEPLOYED | grep "onboarding" → 0 на VPS |
| Onboarding frontend | NOT_DEPLOYED | Фронтенд не деплоен |
| Сохранение onboarding status | NOT_DEPLOYED | Нет таблицы users с onboarding полями |
| Повторное открытие Mini App | NOT_DEPLOYED | /api/onboarding/status → 404 |
| Новый `/start` | NOT_DEPLOYED | grep "планировщик" → строка 4243 |
| Документы | NOT_DEPLOYED | /api/documents → 404 |
| Legal API | NOT_STARTED | Нет legal_docs.py на VPS |
| Consent middleware | NOT_STARTED | grep "consent" → 0 |
| WalletService | NOT_DEPLOYED | Нет таблицы wallets |
| Wallet ledger | NOT_DEPLOYED | Нет таблицы wallet_ledger |
| Пакеты пополнения | NOT_DEPLOYED | /api/payment/packages → 404 |
| Telegram Stars | PARTIAL | Есть обработчик, нет идемпотентности |
| ЮKassa | PARTIAL | Есть webhook, нет внешнего сайта |
| Реферальные начисления | PARTIAL | Есть старый формат, нет нового |
| Frontend | NOT_DEPLOYED | index.html отсутствует на VPS |
| Миграции | NOT_DEPLOYED | VPS: A-L, local: A-V |
| Production deployment | PARTIAL | Работает, но устаревший код |

---

## Разница между VPS и local

- **VPS**: `bd517cd` (refactor/split-main) — 4315 строк main.py
- **Local**: `63dbd92` (master) — 8082 строк main.py
- **Разница**: +3767 строк, 17 файлов изменены, 662 insertions, 4184 deletions
- **Незакоммиченные изменения на VPS**: 17 файлов (удалены старые модули)
- **Ключевые отсутствия на VPS**: onboarding, documents, wallet system, new start screen, frontend
