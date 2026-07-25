# ROUTER_AUDIT.md — Анализ handlers и routers

**Дата аудита**: 2026-07-25
**Код**: local HEAD `63dbd92` (master), 8082 строк main.py

---

## Bot Handlers (aiogram dp)

### /start — CommandStart
- **Строка**: 7377-7656
- **Декоратор**: `@dp.message(CommandStart())`
- **Функция**: `cmd_start`
- **Сценарии**:
  - Новый пользователь → WelcomeService.build_new_user_welcome()
  - По ref_ ссылке → WelcomeService.build_referral_welcome()
  - По event_ ссылке → показать экран приглашения
  - По save_recipe_ → сохранить рецепт
  - Возвращающийся → WelcomeService.build_returning_user_dashboard()
- **Статус**: Единственный обработчик `/start`

### /terms — Command("terms")
- **Строка**: 3226-3227
- **Декоратор**: `@dp.message(Command("terms"))`
- **Функция**: `cmd_terms`
- **Перенаправляет**: на `show_documents`

### /documents — (нет отдельной команды)
- **Обрабатывается через**: callback `show_documents`

### /privacy — (нет отдельной команды)
- **Обрабатывается через**: callback `show_documents`

### /balance — (нет отдельной команды)
- **Обрабатывается через**: Mini App API `/api/balance`

### /pay — (нет отдельной команды)
- **Обрабатывается через**: Mini App API `/api/payment/packages`

### /ref — Command("ref")
- **Строка**: существует через `cmd_ref`
- **Обрабатывает**: показ реферальной информации

---

## Callback Handlers

### Legal / Documents callbacks
- `show_terms` → `cb_show_terms` (строка 7716) → перенаправляет на `show_documents`
- `show_documents` → `cb_show_documents` → показывает экран документов
- `legal:view:{type}` → просмотр документа
- `legal:accept:{type}` → принятие документа
- `legal:back` → возврат к списку документов
- `legal:check_acceptance` → проверка принятия

### Onboarding callbacks
- `ob_start_tutorial` → начало туториала (строка 5937)
- `ob_start_skip_to_legal` → пропуск к юридическим экранам (строка 5955)
- `ob_next:{step}` → следующий шаг (строка 5828)
- `ob_prev:{step}` → предыдущий шаг (строка 5851)
- `ob_accept_legal` → принятие документов в onboarding (строка 5876)
- `ob_skip_legal` → пропуск документов (строка 5903)
- `onboarding_cancel` → отмена onboarding (строка 5925)

### Welcome sub-screen callbacks
- `ws_how_to_add` → как добавить рецепт
- `ws_example` → пример рецепта
- `ws_ai_functions` → AI-функции
- `ws_get_points` → получить баллы
- `ws_help` → как всё работает
- `ws_back` → возврат на приветственный экран

### Balance callbacks
- `balance` → показ баланса

### Payment callbacks
- `topup` → пополнение
- `topup_stars` → пополнение через Stars
- `topup_yookassa` → пополнение через ЮKassa

### Referral callbacks
- `show_ref` → показ реферальной программы
- `referral` → реферальная информация

---

## API Endpoints (FastAPI app)

### Public (no auth)
- `GET /health` — health check
- `GET /api/events/shared/{event_id}` — публичный просмотр события

### Auth required (get_current_user)
- `GET /api/events` — список событий
- `POST /api/events` — создание события
- `GET /api/events/{event_id}` — просмотр события
- `PATCH /api/events/{event_id}` — редактирование
- `DELETE /api/events/{event_id}` — удаление
- `POST /api/events/{event_id}/recipes` — добавление рецепта в событие
- `PATCH /api/events/{event_id}/recipes/{recipe_id}` — обновление множителя
- `DELETE /api/events/{event_id}/recipes/{recipe_id}` — удаление рецепта из события
- `GET /api/recipes` — личная библиотека
- `POST /api/recipes` — добавление рецепта
- `PATCH /api/recipes/{recipe_id}` — редактирование рецепта
- `DELETE /api/recipes/{recipe_id}` — удаление рецепта
- `POST /api/recipes/import-url` — импорт по URL
- `POST /api/recipes/import-text` — импорт текста
- `POST /api/recipes/{recipe_id}/prepare-share` — подготовка шаринга
- `POST /api/recipes/share/{token}/prepare-message` — подготовка сообщения
- `POST /api/recipes/share/{token}/join` — присоединение по шарингу
- `GET /api/files/photo/{file_id}` — прокси фото
- `GET /api/admin/migration-check` — проверка миграций (admin only)

### Legal API
- `GET /api/legal/documents` — список документов
- `GET /api/legal/documents/{doc_type}` — просмотр документа
- `GET /api/legal/status` — статус принятия документов
- `POST /api/legal/accept` — принятие документа

### Onboarding API
- `GET /api/onboarding/status` — статус onboarding
- `POST /api/onboarding/step` — продвижение шага
- `POST /api/onboarding/skip` — пропуск onboarding

### Balance / Payment API
- `GET /api/balance` — баланс
- `POST /api/balance/topup-stars` — пополнение Stars
- `POST /api/yookassa/webhook` — webhook ЮКассы

### Referral API
- `GET /api/referral` — реферальная информация

### Split Expenses API
- `POST /api/split/parse-receipt` — парсинг чека (если split_module доступен)
- `POST /api/split/create-event` — создание события
- `POST /api/split/add-participant` — добавление участника
- `POST /api/split/add-receipt` — добавление чека
- `POST /api/split/set-contribution` — установка взноса
- `POST /api/split/calculate` — расчёт

---

## Проверки

### Router не подключается дважды
- **ОК**: Все handlers определены в одном `main.py`, дублирования нет
- `@dp.message(CommandStart())` — единственная регистрация (строка 7777)
- `@dp.message(Command("terms"))` — единственная регистрация (строка 3226)

### Кнопка документов обрабатывается одним handler
- **ОК**: `show_documents` → `cb_show_documents` (единый entry point)
- `show_terms` → перенаправляет на `show_documents` (для backward compat)

### Старый `/start` больше не используется
- **ПРОВЕРИТЬ В PRODUCTION**: Локальный HEAD использует новый `cmd_start` с `WelcomeService`
- VPS использует старый текст "планировщик застолий"

### Callback получает answer()
- **ПРОВЕРИТЬ**: Все callback handlers должны вызывать `callback.answer()`
- Некоторые могут пропускать answer() при ошибке

### Webhook и polling не работают одновременно
- **ОК**: Бот использует polling (`dp.start_polling`), webhook не настроен
- VPS: `aiogram polling` в логах подтверждает polling

---

## Файловая структура handlers

```
main.py (8082 строки)
├── FastAPI app (строка 1149+)
│   ├── /health
│   ├── /api/events/*
│   ├── /api/recipes/*
│   ├── /api/legal/*
│   ├── /api/onboarding/*
│   ├── /api/balance/*
│   ├── /api/yookassa/webhook
│   ├── /api/referral
│   └── /api/split/* (conditionally imported)
├── aiogram dp (строка 3300+)
│   ├── cmd_start (CommandStart)
│   ├── cmd_terms (Command("terms"))
│   ├── cmd_ref (Command("ref"))
│   ├── cb_show_terms
│   ├── cb_show_documents
│   ├── onboarding callbacks (ob_*)
│   ├── welcome sub-screen callbacks (ws_*)
│   ├── balance callbacks
│   ├── payment callbacks
│   └── referral callbacks
├── UserService (строка 1065+)
├── LegalDocumentService (строка 1155+)
├── WelcomeService (строка 1512+)
├── WalletService (строка 2700+)
├── ReferralService (строка 2732+)
└── Inline handlers (строка 3300+)
```
