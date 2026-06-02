# Миграция БД на Selectel (242-ФЗ)

**Схема:** БД (персданные) → Selectel Managed PostgreSQL (РФ). Бот/API остаётся
на Railway (за рубежом — Telegram режут с РФ-хостинга). Коннект Railway → Selectel
по TLS.

> Делать когда появятся первые РФ-клиенты. Разовая операция, ~1–2 часа, без
> переписывания кода (миграции накатятся сами при старте).

---

## 0. Перед стартом — узнать версию текущей БД
pg_dump/pg_restore должны быть **той же или новее** мажорной версии, что и БД.
```sh
psql "$RAILWAY_DATABASE_URL" -c "show server_version;"
```
Запомнить мажор (напр. 16). На Selectel создавать кластер **той же мажорной версии**.

## 1. Selectel: создать кластер
1. Панель Selectel → **Managed Databases → PostgreSQL** → создать кластер.
   - версия = как в Railway (см. шаг 0)
   - регион РФ
   - включить **SSL/TLS**
2. Создать БД (напр. `polyana`) и пользователя.
3. В **сетевом доступе/файрволле** разрешить подключение извне (Railway за рубежом
   подключается к БД). Безопасно: ограничить по IP Railway если статичный, иначе
   `0.0.0.0/0` **только с обязательным SSL** (sslmode=require) + сложный пароль.
4. Скопировать строку подключения: `host`, `port`, `user`, `password`, `dbname`.

## 2. Дамп с Railway
```sh
pg_dump "$RAILWAY_DATABASE_URL" -Fc --no-owner --no-privileges -f polyana.dump
```

## 3. Восстановление в Selectel
```sh
pg_restore --no-owner --no-privileges --clean --if-exists \
  -d "postgresql://USER:PASS@HOST:PORT/polyana?sslmode=require" polyana.dump
```
Схема простая (SERIAL/JSONB/TIMESTAMPTZ, без спец-расширений) — restore должен
пройти чисто.

## 4. Переключить приложение
- В **Railway → Variables** заменить `DATABASE_URL` на строку Selectel
  (с `?sslmode=require`). Railway передеплоит.

⚠️ **Возможная правка кода (SSL):** `asyncpg.create_pool(DATABASE_URL, ...)` может
не подхватить `sslmode=require` из DSN на части версий. Если в логах `db_error`
про SSL — добавить ssl-контекст **с проверкой CA** (Selectel даёт root CA в
панели; НЕ отключать verify — иначе MITM):
```python
import ssl as _ssl
# CA-файл Selectel положить в репо, напр. assets/selectel-ca.pem
_ssl_ctx = _ssl.create_default_context(cafile="assets/selectel-ca.pem")
# check_hostname/verify_mode оставить дефолтными (hostname=True, CERT_REQUIRED)
pool = await asyncpg.create_pool(DATABASE_URL, ssl=_ssl_ctx, min_size=2, max_size=10, command_timeout=30)
```
Если хостнейм в строке не совпадает с сертификатом — лучше поправить host в DSN,
а не выключать `check_hostname`.

## 5. Проверка
```sh
curl -s https://polyana-backend-production.up.railway.app/health
# ждём {"status":"ok","db_ready":true,"db_error":null}
```
- Открыть Mini App, проверить: события грузятся, баланс на месте, покупки ок.

## 6. После
- Railway-базу не удалять 3–7 дней (бэкап/откат). Потом decommission.
- Webhook ЮКассы и menu-button URL **менять НЕ надо** (домен Railway тот же).

## Что НЕ меняется
- Бот, API, домен Railway, ключи ЮКассы/OpenRouter, вебхук.
- Меняется ровно одна переменная: `DATABASE_URL`.
