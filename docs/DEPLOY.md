# Развёртывание: что уже сделано и что осталось

Состояние на 2026-08-11.

## Сделано

| Что | Где | Проверка |
|---|---|---|
| Репозиторий | `github.com/ML371KL/temp-zero-inode-842` | `git log` |
| Бакет R2 | `moex-radar` (аккаунт `7e159ad9…`) | `npx wrangler r2 bucket list` |
| Проект Pages | `tzi-842` → https://tzi-842.pages.dev | открыть в браузере |
| Биндинг `DATA` → `moex-radar` | `deployment_configs` проекта (production и preview) | см. «Грабля 2» |
| Витрина в бакете | `data.json`, `history/daily.json` | `curl https://tzi-842.pages.dev/data/data.json` |
| Клон на VPS | `/srv/dash/repo-842` (пользователь `dash`) | `sudo -u dash git -C /srv/dash/repo-842 log -1` |
| Стор на VPS | `/var/lib/moex-radar` — 94 ряда, 220 тыс. точек (затравка исследования) | `sudo -u dash ls /var/lib/moex-radar/raw \| wc -l` |
| Таймеры systemd | `moex-radar-{intraday,daily,monthly}.timer` включены | `systemctl list-timers 'moex-radar*'` |
| Прогон на VPS | `--dry-run` за 18,9 с, все источники отвечают, ядро совпало с локальным (0,663) | см. ниже |

## Состояние на вечер 11.08.2026: панель работает сама

Ключ R2 выпущен, конвейер публикует с VPS (интрадей каждые 15 мин, полный прогон 19:05 МСК),
телеграм-канал проверен доставкой, секреты `CLOUDFLARE_*` в Actions заведены — публикация
сайта по пушу работает.

**Осталось одно (по желанию):** `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в секретах
репозитория. На VPS они есть, в GitHub — нет, поэтому сторож витрины (`watchdog.yml`)
сигналит пока только красным прогоном и письмом GitHub, а не сообщением в чат.

### Кто за чем следит

| Уровень | Что ловит | Где живёт |
|---|---|---|
| `dash-watch` (VPS, 5 мин) | конвейер сломан, машина жива | на VPS, умирает вместе с ней |
| `watchdog.yml` (GitHub, 2 ч) | **машина не отвечает** | вне VPS |
| `fallback.yml` | подмена писателя | только по кнопке (расписание снято) |

Порог сторожа — 26 часов: интрадей-такт живёт только в торговое время, и порог «три такта»
кричал бы каждую ночь.

## История: что было сделано руками при развёртывании

**Нужен ключ R2 для записи из VPS.** Программно его выпустить нельзя: OAuth-токен
wrangler не имеет права создавать API-токены (проверено — `9109 Unauthorized`), а
существующие ключи соседних панелей ограничены своими бакетами (проверено: `PUT` в
`moex-radar` ключом 838 отдаёт `403`, в свой `dash-838` — `200`).

1. Cloudflare → **R2** → *Manage R2 API Tokens* → **Create API token**:
   - имя `moex-radar-vps`;
   - права **Object Read & Write**;
   - область — только бакет `moex-radar`.
2. Скопировать **Access Key ID** и **Secret Access Key**.
3. На VPS вписать их в `/usr/local/etc/moex-radar/env` (файл уже создан, права 640 root:dash):
   ```
   R2_ACCOUNT_ID=7e159ad9043e61c10f79aec6411ee48b
   R2_BUCKET=moex-radar
   R2_ACCESS_KEY_ID=<из шага 2>
   R2_SECRET_ACCESS_KEY=<из шага 2>
   ```
4. Первый настоящий прогон и проверка:
   ```bash
   sudo runuser -u dash -- /usr/local/sbin/moex-radar daily
   curl -sI https://tzi-842.pages.dev/data/data.json | grep -i last-modified
   ```

Дальше панель обновляется сама: интрадей каждые 15 минут (только котировки), полный
пересчёт в 19:05 МСК, месячные источники — по своим окнам публикации.

### Необязательное, тоже руками

- **Телеграм-алерты**: `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в том же env-файле.
  Без них прогон работает молча (события копятся в `data.json` и видны на панели).
- **`site.yml` в Actions** (публикация сайта при изменении кода): секреты
  `CLOUDFLARE_API_TOKEN` и `CLOUDFLARE_ACCOUNT_ID` в настройках репозитория. Пока их
  нет, сайт публикуется руками: `npx wrangler pages deploy web --project-name tzi-842`.
- **`fallback.yml`** (запасной писатель, если VPS замолчит): те же `R2_*`, что и на VPS,
  в секретах репозитория. Лиз не даст двум писателям столкнуться.
- **Сторож на cron-job.org**: HEAD на `https://tzi-842.pages.dev/data/data.json` раз в
  30 минут; если `Last-Modified` старше двух часов в торговое время — дёргать
  `workflow_dispatch` у `fallback.yml`. ⚠️ PAT для Actions общий с 838 и истекает
  ~октябрь 2026 — обновлять один раз на все панели.

## Грабли развёртывания (оплачены здесь, не повторять)

**1. `wrangler r2 object put` по умолчанию пишет в ЛОКАЛЬНУЮ симуляцию.**
Объект «загружался» успешно, `wrangler r2 object get` его показывал, а бакет по API
оставался пустым и функция честно отвечала 503. Нужен флаг `--remote`:
```bash
npx wrangler r2 object put moex-radar/data.json --file .state/out/data.json --remote
```

**2. Биндинг R2 из `wrangler.toml` в Pages не применяется сам.**
Секция `[[r2_buckets]]` в конфиге не доехала до развёртывания (в API у деплоя
`bindings: {}`). Биндинг живёт в настройках проекта и ставится один раз:
```bash
curl -X PATCH "https://api.cloudflare.com/client/v4/accounts/$ACC/pages/projects/tzi-842" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data '{"deployment_configs":{"production":{"r2_buckets":{"DATA":{"name":"moex-radar"}}},
           "preview":{"r2_buckets":{"DATA":{"name":"moex-radar"}}}}}'
```
После этого нужен новый деплой — старые развёртывания неизменяемы.

**3. Сразу после деплоя край Cloudflare минуту-две отдаёт 522.** Это транзиент, а не
поломка функции (то же наблюдалось в соседних панелях).

**4. `python` без `-u` делает прогон немым.** В systemd journald при блочной
буферизации не видно ничего до самого конца — при зависшем фетчере это выглядит как
мёртвый сервис. В обёртке стоит `python3 -u`.

**5. Кириллица в логах на Windows.** Консоль cp1252 роняет `print` с русским текстом
(`UnicodeEncodeError`), причём падает не логика, а диагностика. Точки входа
(`run.py`, `devserver.py`, `tests/__init__.py`) переключают потоки на UTF-8.

## Локальная работа

```bash
python ops/seed_store.py --validation ../moex-drivers/validation/data  # разовая затравка
STATE_DIR=.state python -u pipeline/run.py --mode daily --dry-run --no-alerts
STATE_DIR=.state python ops/devserver.py --port 8842                   # панель на localhost
python -m unittest discover tests
```
