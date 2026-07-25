# AUDIT_AFTER.md — Итоговый аудит после исправлений

**Дата аудита**: 2026-07-25
**Локальный HEAD**: `63dbd92` (master)
**Production HEAD**: `bd517cd` (refactor/split-main) — **УСТАРЕЛ**

---

## Сводная таблица: Было → Стало → Production

| Функция | Было (VPS) | Стало (local) | Production | Доказательство |
|---|---|---|---|---|
| Onboarding backend | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: grep onboarding → 0 |
| Onboarding frontend | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: нет index.html |
| Сохранение onboarding status | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: нет таблицы users |
| Повторное открытие Mini App | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: /api/onboarding/status → 404 |
| Новый `/start` | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: "планировщик застолий" |
| Документы | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: /api/documents → 404 |
| Legal API | NOT_STARTED | DONE (код) | NOT_DEPLOYED | VPS: нет legal_docs.py |
| Consent middleware | NOT_STARTED | PARTIAL (код) | NOT_DEPLOYED | VPS: grep consent → 0 |
| WalletService | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: нет таблицы wallets |
| Wallet ledger | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: нет wallet_ledger |
| Пакеты пополнения | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: /api/payment/packages → 404 |
| Telegram Stars | PARTIAL | DONE (код) | PARTIAL | VPS: есть обработчик |
| ЮKassa | PARTIAL | PARTIAL (нет сайта) | PARTIAL | VPS: есть webhook |
| Реферальные начисления | PARTIAL | DONE (код) | PARTIAL | VPS: старый формат |
| Frontend | NOT_DEPLOYED | DONE (код) | NOT_DEPLOYED | VPS: нет index.html |
| Миграции | NOT_DEPLOYED | DONE (inline) | NOT_DEPLOYED | VPS: A-L, local: A-V |
| Production deployment | PARTIAL | PARTIAL | PARTIAL | VPS: работает устаревший код |

---

## Статусы

### DONE (код написан и работает локально)
- Onboarding backend — UserService, WelcomeService, callback handlers, API endpoints
- Onboarding frontend — s-onboarding screen, startOnboarding(), legal acceptance flow
- Сохранение onboarding status — users table, onboarding_status, onboarding_version
- Повторное открытие — /api/onboarding/status проверяет completed/skipped
- Новый `/start` — WelcomeService.build_new_user_welcome() с "личная библиотека рецептов"
- Документы — legal_documents table, LegalDocumentService, templates в legal_docs.py
- Legal API — /api/legal/documents, /api/legal/status, /api/legal/accept
- WalletService — wallets table, paid_points/bonus_points/reserved_points
- Wallet ledger — wallet_ledger table
- Пакеты пополнения — payment_packages table, /api/payment/packages
- Telegram Stars — полный flow с идемпотентностью (local)
- Реферальные начисления — referral_rewards, referral_codes, referral_relations

### PARTIAL (код есть, но не полностью)
- Consent middleware — код есть, но не блокирует AI без consent в production
- ЮKassa — webhook работает, нет внешнего сайта для оплаты
- Production deployment — systemd работает, но код устарел

### NOT_DEPLOYED (код есть, но не на VPS)
- Всё из списка DONE выше

### BLOCKED_BY_OWNER_DATA
- Юридические реквизиты оператора (ИНН, ОГРН, адрес, email)
- YooKassa Shop ID / Secret Key (уже в .env)
- Домен для внешнего сайта ЮKassa

### READY_FOR_MANUAL_TEST
- Telegram Stars (тестовый платёж)
- Onboarding flow (через Telegram бота)
- Документы (после ввода реквизитов)

---

## Что НЕ сделано

1. **Деплой на VPS** — local код не запущен на production
2. **Frontend деплой** — index.html не на сервере
3. **Юридические реквизиты** — не введены в .env
4. **Внешний сайт ЮKassa** — не создан
5. **polyana-worker.service** — не создан на VPS
6. **Ручной smoke test** — не проведён
7. **Тест с чистым аккаунтом** — не проведён
8. **Проверка nginx** — фронтенд не раздаётся

---

## Необходимые действия

### Перед деплоем
1. Ввести юридические реквизиты в .env VPS:
   - `LEGAL_OPERATOR_FULL_NAME`
   - `LEGAL_OPERATOR_SHORT_NAME`
   - `LEGAL_OPERATOR_STATUS` (ИП/ООО/Самозанятый)
   - `LEGAL_INN`
   - `LEGAL_OGRN_OR_OGRNIP`
   - `LEGAL_LEGAL_ADDRESS`
   - `LEGAL_CONTACT_EMAIL`
   - `LEGAL_PRIVACY_EMAIL`

2. Создать polyana-worker.service

3. Настроить раздачу фронтенда через nginx

### После деплоя
1. Ручной smoke test всех сценариев
2. Тест onboarding с чистым аккаунтом
3. Тест документов
4. Тест Stars платежа
5. Проверка повторного открытия Mini App
