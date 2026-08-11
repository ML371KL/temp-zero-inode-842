#!/usr/bin/env bash
# Установка конвейера «MOEX Radar» на VPS. Идемпотентна: повторный запуск — это
# штатный способ обновления, а не опасная операция.
#
# Что скрипт НЕ делает намеренно:
#   * не перезаписывает /usr/local/etc/moex-radar/env — там секреты, и установщик,
#     затирающий их «шаблоном по умолчанию», однажды выключит запись в бакет молча;
#   * не трогает /usr/local/sbin/dash-alert и dash-notify — это файлы соседнего
#     проекта (репозиторий 839-data), общие для всех панелей машины;
#   * не создаёт бакет R2 и токены — это делается руками один раз в дашборде.
#
# Запуск:  sudo bash ops/install-vps.sh
# Обновление после правки ops/:  git pull && sudo bash ops/install-vps.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ML371KL/temp-zero-inode-842.git}"
REPO_DIR="${REPO_DIR:-/srv/dash/repo-842}"
RUN_USER="${RUN_USER:-dash}"
ETC_DIR=/usr/local/etc/moex-radar
STATE_DIR="${STATE_DIR:-/var/lib/moex-radar}"
UNITS=(moex-radar-daily moex-radar-intraday moex-radar-monthly)

say() { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "нужен root: sudo bash ops/install-vps.sh" >&2; exit 1; }

# Каталог этого скрипта — источник файлов при первой установке (репозиторий ещё не
# склонирован, и брать юниты неоткуда, кроме как из рабочей копии, в которой скрипт
# запустили).
src_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Зависимости — до первых изменений в системе: установщик, упавший на середине из-за
# отсутствующего flock, оставит систему в полусобранном виде.
missing=()
for cmd in git curl flock systemctl runuser install; do
  command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if (( ${#missing[@]} )); then
  echo "нет команд: ${missing[*]} (apt install git curl util-linux)" >&2
  exit 1
fi

# ------------------------------------------------------------------ пользователь
say "пользователь $RUN_USER"
if id -u "$RUN_USER" >/dev/null 2>&1; then
  note "уже есть (uid $(id -u "$RUN_USER"))"
else
  # Системный, без входа в систему и без домашнего каталога в /home: этот пользователь
  # существует ради таймеров, а не ради человека.
  useradd --system --home-dir /srv/dash --shell /usr/sbin/nologin "$RUN_USER"
  note "создан"
fi

# /srv/dash общий с панелями 837 и 838. Создаём, только если его нет: `install -d` на
# существующем каталоге переставил бы владельца и права соседям, а они там работают.
if [[ -d /srv/dash ]]; then
  note "/srv/dash уже есть — права не трогаю (каталог общий с 837/838)"
else
  install -d -m 755 -o "$RUN_USER" -g "$RUN_USER" /srv/dash
fi
# StateDirectory= в юнитах создаст этот каталог и сам, но обвязку запускают и руками —
# тогда создавать его будет некому. Этот каталог только наш, права выставляем всегда.
install -d -m 750 -o "$RUN_USER" -g "$RUN_USER" "$STATE_DIR"

# ------------------------------------------------------------------ интерпретатор
say "python"
python_bin="${PYTHON_BIN:-/usr/bin/python3}"
if ! "$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "$python_bin это $("$python_bin" -V 2>&1), нужен 3.12+ (в Ubuntu 24.04 он системный)" >&2
  exit 1
fi
note "$("$python_bin" -V 2>&1) — годится"

# ---------------------------------------------------------------------- репозиторий
say "репозиторий $REPO_DIR"
if [[ -d "$REPO_DIR/.git" ]]; then
  # reset --hard, а не merge: рабочая копия на сервере — не место для локальных правок,
  # порядок изменения кода один — правка в git, потом сюда.
  if runuser -u "$RUN_USER" -- git -C "$REPO_DIR" fetch --quiet origin main \
     && runuser -u "$RUN_USER" -- git -C "$REPO_DIR" reset --quiet --hard origin/main; then
    note "обновлён до $(runuser -u "$RUN_USER" -- git -C "$REPO_DIR" log --oneline -1)"
  else
    note "ВНИМАНИЕ: origin недоступен, оставляю рабочее дерево как есть"
  fi
elif [[ -e "$REPO_DIR" ]]; then
  echo "$REPO_DIR существует, но это не git-репозиторий — разберитесь руками" >&2
  exit 1
else
  parent="$(dirname "$REPO_DIR")"
  # Родительский каталог тоже создаём только при отсутствии — по той же причине, что и
  # /srv/dash: он может быть чужим.
  [[ -d "$parent" ]] || install -d -m 755 -o "$RUN_USER" -g "$RUN_USER" "$parent"
  runuser -u "$RUN_USER" -- git clone --quiet "$REPO_URL" "$REPO_DIR"
  note "склонирован: $(runuser -u "$RUN_USER" -- git -C "$REPO_DIR" log --oneline -1)"
fi

# Файлы ставим из свежего чекаута, если он есть; при самой первой установке — из той
# копии, откуда запустили скрипт.
ops_dir="$REPO_DIR/ops"
[[ -f "$ops_dir/moex-radar.sh" ]] || ops_dir="$src_dir"
note "источник файлов: $ops_dir"

# ------------------------------------------------------------------------- обвязка
say "обвязка /usr/local/sbin/moex-radar"
# Нормализация переводов строк — не паранойя: правка шелл-скрипта редактором на Windows
# приезжает с CRLF, `\` в конце строки экранирует `\r`, продолжение строки рвётся, и
# скрипт ломается тихо. На этой машине такое уже случалось с соседним проектом.
tmp="$(mktemp)"
sed 's/\r$//' "$ops_dir/moex-radar.sh" > "$tmp"
bash -n "$tmp" || { echo "обвязка не разбирается — установка прервана" >&2; rm -f "$tmp"; exit 1; }
install -m 750 -o root -g "$RUN_USER" "$tmp" /usr/local/sbin/moex-radar
rm -f "$tmp"
note "установлена (750 root:$RUN_USER)"

# ----------------------------------------------------------------------- окружение
say "окружение $ETC_DIR/env"
install -d -m 750 -o root -g "$RUN_USER" "$ETC_DIR"
if [[ -f "$ETC_DIR/env" ]]; then
  note "уже есть — НЕ трогаю (там секреты)"
  # Права выправляем всегда: файл с ключами R2, прочитанный кем угодно, — это чужая
  # запись в бакет.
  chown root:"$RUN_USER" "$ETC_DIR/env"
  chmod 640 "$ETC_DIR/env"
else
  sed 's/\r$//' "$ops_dir/env.example" > "$ETC_DIR/env"
  chown root:"$RUN_USER" "$ETC_DIR/env"
  chmod 640 "$ETC_DIR/env"
  note "создан из шаблона — ЗАПОЛНИТЕ ключи R2 перед первым прогоном"
fi

env_ready=1
if ! grep -qE '^R2_ACCESS_KEY_ID=.+' "$ETC_DIR/env" \
   || ! grep -qE '^R2_SECRET_ACCESS_KEY=.+' "$ETC_DIR/env" \
   || ! grep -qE '^R2_ACCOUNT_ID=.+' "$ETC_DIR/env"; then
  env_ready=0
  note "ключи R2 не заполнены"
fi

# --------------------------------------------------------------------- мостик тревог
say "мостик тревог"
if [[ -x /usr/local/sbin/dash-alert ]]; then
  note "dash-alert на месте — падения приедут в телеграм"
else
  note "ВНИМАНИЕ: /usr/local/sbin/dash-alert не найден. Юниты это переживут ('-' в"
  note "ExecStopPost), но о падении прогона никто не узнает. Ставится из репозитория"
  note "839-data (ops/dash-alert), вместе с dash-notify."
fi

# ------------------------------------------------------------------------- юниты
say "юниты systemd"
changed=0
for unit in "${UNITS[@]}"; do
  for kind in service timer; do
    src="$ops_dir/$unit.$kind"
    dst="/etc/systemd/system/$unit.$kind"
    # Явная проверка: без неё пропавший юнит превращается в ошибку sed на следующей
    # строке, и в выводе будет «No such file or directory» без имени файла.
    [[ -f "$src" ]] || { echo "нет файла $src — чекаут неполный" >&2; exit 1; }
    tmp="$(mktemp)"
    sed 's/\r$//' "$src" > "$tmp"
    if [[ -f "$dst" ]] && cmp -s "$tmp" "$dst"; then
      note "$unit.$kind без изменений"
    else
      install -m 644 -o root -g root "$tmp" "$dst"
      note "$unit.$kind установлен"
      changed=1
    fi
    rm -f "$tmp"
  done
done

if [[ $changed -eq 1 ]]; then
  systemctl daemon-reload
  note "daemon-reload"
fi

# enable --now идемпотентен: повторный вызов не создаёт вторую ссылку и не сбивает
# расписание уже запущенного таймера.
systemctl enable --now "${UNITS[@]/%/.timer}" >/dev/null
note "таймеры включены"

# Юниты проверяем ПОСЛЕ установки: verify читает то, что лежит в /etc, вместе со всеми
# зависимостями. Не блокирует — на некоторых сборках он ругается на чужие юниты в
# графе, — но в журнале установки эти строки стоят дорого.
if command -v systemd-analyze >/dev/null 2>&1; then
  systemd-analyze verify "${UNITS[@]/%/.timer}" 2>&1 | sed 's/^/   verify: /' || true
fi

# ------------------------------------------------------------------- первый прогон
say "самопроверка"
if [[ $env_ready -eq 1 ]]; then
  # `--mode selftest` конвейера проверяет ровно то, что ломается при установке:
  # видит ли он ключи R2, настроен ли телеграм, какая роль писателя и куда положен
  # каталог состояния. Не блокирует — но её вывод в журнале установки стоит дорого.
  runuser -u "$RUN_USER" -- /usr/local/sbin/moex-radar selftest \
    || note "ВНИМАНИЕ: самопроверка нашла проблемы (см. вывод выше)"
else
  note "пропущена: окружение ещё не заполнено"
fi

say "первичный прогон"
if [[ "${SKIP_BOOTSTRAP:-0}" == "1" ]]; then
  note "пропущен по SKIP_BOOTSTRAP=1"
elif [[ $env_ready -eq 0 ]]; then
  note "пропущен: сначала заполните $ETC_DIR/env, потом"
  note "  sudo runuser -u $RUN_USER -- /usr/local/sbin/moex-radar bootstrap"
elif [[ -d "$STATE_DIR/raw" ]] && [[ -n "$(ls -A "$STATE_DIR/raw" 2>/dev/null)" ]]; then
  # Идемпотентность: сырьё уже есть, значит машина уже работала, и час на повторное
  # наполнение истории тратить незачем.
  note "сырьё в $STATE_DIR/raw уже есть — bootstrap не нужен, запускаю обычный суточный"
  systemctl start moex-radar-daily.service || note "ВНИМАНИЕ: суточный прогон упал, смотрите journalctl -u moex-radar-daily"
else
  note "наполняю историю (это долго, до часа; вывод идёт прямо сюда, не в journald)"
  runuser -u "$RUN_USER" -- /usr/local/sbin/moex-radar bootstrap \
    || note "ВНИМАНИЕ: bootstrap упал — смотрите вывод выше"
fi

say "состояние"
systemctl list-timers --all 'moex-radar-*' --no-pager || true
printf '\nГотово. Журнал: journalctl -u moex-radar-daily -n 50\n'
