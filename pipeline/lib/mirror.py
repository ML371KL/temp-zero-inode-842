"""Восстановление стора из зеркала raw/ в R2.

Зеркало — единственная восстановимая копия рядов, которых нет в git: publish
пишет raw/{sid}.json и манифест raw/_index.json на каждом прогоне. Отсюда стор
поднимается за минуты там, где холодный сбор с источников занимает часы: у
квартальной реколибровки на пустом раннере и у фолбэк-писателя в аварии VPS.
Фолбэк без этого был страховкой, которая не срабатывает: полный сбор zcyc — ~2900
позапросных дней ISS, futoi — ~2000 с, и job GitHub умирал по таймауту ДО
публикации на каждой попытке (аудит 18.08.2026).

Здесь же — единая проверка полноты: ряд ядра (role=core) или обязательный
(required) обязан восстановиться, иначе панель молча посчитает композит из двух
ног вместо трёх — другое число, возможно другой знак, ложный алерт «разворот
ядра». Прежний фильтр смотрел только на required и пропускал ногу urals_tax
(role=core, required=False — месячный ряд не роняет прогон при разовом отказе,
но модель без него другая).
"""

import json

from . import r2, registry

__all__ = ["restore_from_r2", "missing_critical", "RestoreError"]


class RestoreError(RuntimeError):
    """Восстановление невозможно или заведомо неполно для счёта модели."""


def restore_from_r2(target):
    """Скачать зеркало raw/ в пустой стор. -> (сколько рядов, чего не хватило)."""
    if not r2.configured():
        raise RestoreError("стор пуст, а R2 не сконфигурирован — нечем восстанавливать")
    index = r2.get("raw/_index.json")
    if not index:
        raise RestoreError(
            "в бакете нет raw/_index.json — манифест пишет publish._mirror_index, "
            "он появится после первого прогона конвейера новой версии.")
    ids = json.loads(index.decode("utf-8")).get("series") or []
    target.mkdir(parents=True, exist_ok=True)
    missing = []
    for sid in ids:
        body = r2.get(f"raw/{sid}.json")
        if body is None:
            missing.append(sid)
            continue
        (target / f"{sid}.json").write_bytes(body)
    return len(ids) - len(missing), missing


def missing_critical(target):
    """Ряды, без которых модель считать нельзя, а их в сторе нет.

    Проверяется НАЛИЧИЕ ФАЙЛА, а не список missing из restore_from_r2: ряд,
    выпавший из самого манифеста, в missing не попадает вовсе — а отсутствует
    так же.
    """
    need = sorted(sid for sid, spec in registry.SERIES.items()
                  if spec.get("required") or spec.get("role") == "core")
    return [sid for sid in need if not (target / f"{sid}.json").exists()]
