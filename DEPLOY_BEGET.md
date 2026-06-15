# Деплой ПОЛЯНЫ на Beget VPS (уход с Railway)

Стек запуска: **Docker Compose** — `app` (FastAPI+бот) + `db` (Postgres) + `caddy`
(авто-HTTPS). Всё на одном VPS.

> ⚠️ Beget = РФ-IP. Telegram (`api.telegram.org`) с РФ режется ТСПУ — бот может
> периодически отваливаться. Принято как временное решение до постоянных клиентов,
> потом переезд на зарубежный VPS (тот же compose, просто другой сервер).

Что делаешь **ты** (нужен твой аккаунт/деньги/SSH): покупка VPS, домен, A-запись,
ввод секретов, перенос данных. Команды — ниже, копируй по порядку.

---

## 0. Предусловия
- **Beget VPS** (Ubuntu 22.04+), root по SSH.
- **Домен для API** (напр. `api.polyana.app` или поддомен от Beget), **A-запись → IP VPS**.
  Нужен, т.к. Telegram Mini App и браузер требуют HTTPS на API.
- Запиши IP VPS.

## 1. Установить Docker на VPS
```sh
ssh root@<IP_VPS>
curl -fsSL https://get.docker.com | sh
docker compose version   # проверка
```

## 2. Забрать код
```sh
git clone https://github.com/coiqastore-ai/polyana-backend.git
cd polyana-backend
```

## 3. Создать `.env` (рядом с docker-compose.yml)
```sh
nano .env
```
Вставь (значения — свои, БЕЗ кавычек):
```
API_DOMAIN=api.твойдомен
POSTGRES_PASSWORD=<придумай длинный пароль>
BOT_TOKEN=<токен бота>
OPENROUTER_API_KEY=<ключ OpenRouter>
FRONTEND_URL=https://coiqastore-ai.github.io/coiqastore-ai-polyana-frontend
INTERNAL_API_KEY=<любая длинная строка>
ADMIN_CHAT_ID=257938367
SUPPORT_HANDLE=@chigra89
```
(YOOKASSA_* не нужны — пополнение отключено.)
`DATABASE_URL` не пиши — его задаёт compose (внутренний Postgres).

## 4. Перенести данные с Railway (чтобы не потерять события/рецепты)
На своей машине (где есть доступ к Railway DATABASE_URL):
```sh
pg_dump "<RAILWAY_DATABASE_URL>" -Fc --no-owner --no-privileges -f polyana.dump
scp polyana.dump root@<IP_VPS>:~/polyana-backend/
```
На VPS — поднять только базу и залить дамп:
```sh
docker compose up -d db
sleep 8
docker compose exec -T db pg_restore -U polyana -d polyana --no-owner < polyana.dump || \
  cat polyana.dump | docker compose exec -T db pg_restore -U polyana -d polyana --no-owner
```
(Схему app создаст сам при старте через `CREATE TABLE IF NOT EXISTS` — дамп просто
добавит данные. Если базы на Railway не жалко / нет важных данных — шаг 4 пропусти,
app поднимет пустую схему.)

## 5. Запуск
```sh
docker compose up -d --build
docker compose logs -f app      # смотри старт, Ctrl+C для выхода
```
Caddy сам возьмёт TLS-сертификат на `API_DOMAIN` (нужно чтобы A-запись уже указывала на VPS).

## 6. Проверка
```sh
curl -s https://api.твойдомен/health
# ждём {"status":"ok","db_ready":true,...}
```

## 7. Переключить фронт на новый API
В `polyana-frontend/index.html` найди `const API =` и замени на:
```js
const API = 'https://api.твойдомен/api';
```
Закоммить + запушь — GitHub Pages обновится. (CORS уже разрешает github.io; домен
фронта не меняется → настройки Mini App в боте трогать не надо.)

## 8. Выключить Railway
Убедись что бот и Mini App работают на VPS (создай событие, открой покупки). Потом
в Railway — Settings → Delete service. Базу Railway удали последней (через 3–7 дней).

---

## Эксплуатация
- **Обновить код:** `git pull && docker compose up -d --build`
- **Логи:** `docker compose logs -f app`
- **Рестарт:** `docker compose restart app`
- **Бэкап БД (делай регулярно!):**
  `docker compose exec -T db pg_dump -U polyana polyana | gzip > backup_$(date +%F).sql.gz`
- Если бот молчит — почти всегда ТСПУ/Telegram-блок (РФ-IP), не код. Проверь
  `docker compose logs app` на таймауты к `api.telegram.org`.
