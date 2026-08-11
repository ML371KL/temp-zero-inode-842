"""Стор нормализованных рядов (docs/CONTRACT.md §1).

Раскладка: $STATE_DIR/raw/{series_id}.json + $STATE_DIR/_meta.json (dirty-множество
для выгрузки в R2). STATE_DIR читается на каждый вызов, а не на импорте: тесты
подменяют его через окружение, и «прочитали один раз при импорте» — классический
способ получить тесты, которые пишут в рабочий стор.

Запись атомарная (временный файл рядом + os.replace): прогон убивают по таймауту
регулярно, а полуфайл data-ряда потом молча читается как «источник пуст».

Про параллелизм: писатель один (VPS, см. контракт §5). _meta.json обновляется
read-modify-write без блокировок — двум писателям он не предназначен.
"""

import json
import os
import re
from pathlib import Path

try:  # пакет pipeline.lib
    from . import dates
except ImportError:  # sys.path указывает внутрь pipeline/
    import dates

DEFAULT_STATE_DIR = ".state"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def state_dir():
    return Path(os.environ.get("STATE_DIR") or DEFAULT_STATE_DIR)


def raw_dir():
    return state_dir() / "raw"


def series_path(series_id):
    _check_id(series_id)
    return raw_dir() / f"{series_id}.json"


def meta_path():
    return state_dir() / "_meta.json"


def _check_id(series_id):
    """id ряда идёт в имя файла и в ключ объекта R2 — мусор туда пускать нельзя."""
    if not isinstance(series_id, str) or not _ID_RE.match(series_id):
        raise ValueError(f"недопустимый series_id: {series_id!r}")
    return series_id


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # на Windows os.replace тоже атомарен и перезаписывает


def _read_json(path):
    """None, если файла нет. Битый файл уводим в .bad и говорим об этом вслух."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        bad = path.with_name(path.name + ".bad")
        try:
            os.replace(path, bad)
        except OSError:
            pass
        print(f"[store] битый {path.name} ({e}) -> {bad.name}, читаю как пустой")
        return None


# ------------------------------------------------------------------- ряды
def load_series(series_id):
    """Ряд целиком или None, если его ещё не собирали."""
    data = _read_json(series_path(series_id))
    if not isinstance(data, dict) or "points" not in data:
        return None
    return data


def save_series(series_id, series, force_dirty=True):
    """Записать ряд как есть и (по умолчанию) пометить его к выгрузке в R2."""
    _check_id(series_id)
    if not isinstance(series, dict) or not isinstance(series.get("points"), dict):
        raise ValueError(f"{series_id}: ожидался dict с ключом points")
    series.setdefault("id", series_id)
    series["points"] = _clean_points(series["points"], series_id)
    _write_json(series_path(series_id), series)
    if force_dirty:
        mark_dirty(series_id)


def upsert_points(series_id, points, meta_patch=None, unit=None, cadence=None):
    """Долить точки в ряд. Возвращает записанный ряд (контракт §1).

    Ретро-правки источника разрешены: значение по существующей дате перезаписывается
    (ISS пересчитывает обороты, ЦБ уточняет декады). Но dirty ставим только если
    что-то реально изменилось — иначе каждый прогон гонит в R2 45 одинаковых
    объектов и превращает лог выгрузки в белый шум.
    """
    _check_id(series_id)
    new_points = _clean_points(points or {}, series_id)
    # unit/cadence фетчер объявляет в meta: вызывающий (run.py) прокидывает meta
    # целиком и про единицы не знает — иначе ряд навсегда остаётся с unit=null.
    unit = unit or (meta_patch or {}).get("unit")
    cadence = cadence or (meta_patch or {}).get("cadence")
    series = load_series(series_id) or {"id": series_id, "unit": unit,
                                        "cadence": cadence, "points": {}, "meta": {}}
    if unit and not series.get("unit"):
        series["unit"] = unit
    if cadence and not series.get("cadence"):
        series["cadence"] = cadence

    old = series.get("points") or {}
    changed = any(k not in old or old[k] != v for k, v in new_points.items())
    merged = dict(old)
    merged.update(new_points)
    series["points"] = {k: merged[k] for k in sorted(merged)}

    meta = dict(series.get("meta") or {})
    old_status = meta.get("status")
    meta.update(meta_patch or {})
    meta.setdefault("status", "ok")
    meta["fetched_at"] = (meta_patch or {}).get("fetched_at") or dates.iso_utc()
    # asof пересчитываем КАЖДЫЙ раз: старое значение из meta пережило бы долив
    # свежих точек и ряд бы выглядел протухшим при живых данных.
    meta["asof"] = (meta_patch or {}).get("asof") or dates.last_date_in_points(series["points"])
    series["meta"] = meta

    _write_json(series_path(series_id), series)
    if changed or not old or meta.get("status") != old_status:
        mark_dirty(series_id)
    return series


def _clean_points(points, series_id):
    """Ключ — YYYY-MM-DD, значение — число или None (контракт §1). Строки не пускаем:
    один раз пропущенная строка '12,84' всплывает через месяц как NaN в z-скоре."""
    out = {}
    for key, value in points.items():
        k = str(key)
        if not _DATE_RE.match(k):
            raise ValueError(f"{series_id}: ключ точки не YYYY-MM-DD: {key!r}")
        if value is None:
            out[k] = None
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{series_id}: значение {k} не число: {value!r}")
        else:
            out[k] = float(value)
    return out


def last_date(series_id):
    """Последняя дата с не-None значением — точка отсчёта инкрементальной загрузки."""
    series = load_series(series_id)
    if not series:
        return None
    return dates.last_date_in_points(series.get("points"))


def list_series():
    d = raw_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def describe(series_id):
    """Сводка по ряду для блока sources в data.json (контракт §3)."""
    series = load_series(series_id)
    if not series:
        return {"id": series_id, "status": "missing", "asof": None,
                "fetched_at": None, "points": 0}
    meta = series.get("meta") or {}
    return {"id": series_id, "status": meta.get("status", "ok"),
            "asof": meta.get("asof"), "fetched_at": meta.get("fetched_at"),
            "source": meta.get("source"), "note": meta.get("note"),
            "points": len(series.get("points") or {})}


# ------------------------------------------------------------ dirty-множество
def _load_meta():
    data = _read_json(meta_path())
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("dirty"), dict):
        data["dirty"] = {}
    return data


def mark_dirty(series_ids):
    ids = [series_ids] if isinstance(series_ids, str) else list(series_ids)
    meta = _load_meta()
    stamp = dates.iso_utc()
    for sid in ids:
        meta["dirty"][_check_id(sid)] = stamp
    meta["updated_at"] = stamp
    _write_json(meta_path(), meta)


def list_dirty():
    """Что ещё не уехало в R2."""
    return sorted(_load_meta()["dirty"])


def mark_clean(series_ids=None):
    """Снять пометку после успешной выгрузки. None — снять со всех."""
    meta = _load_meta()
    if series_ids is None:
        meta["dirty"] = {}
    else:
        ids = [series_ids] if isinstance(series_ids, str) else list(series_ids)
        for sid in ids:
            meta["dirty"].pop(sid, None)
    meta["updated_at"] = dates.iso_utc()
    _write_json(meta_path(), meta)
