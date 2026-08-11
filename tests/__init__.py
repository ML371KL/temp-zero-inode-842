"""Общая обвязка тестов «MOEX Radar».

Три правила набора, за которые заплачено в соседних проектах:

1. НИКАКИХ «сегодня минус N». Любой тест, который отсчитывает даты от текущего дня,
   зелен ровно до того дня, когда перестаёт им быть, и краснеет не от поломки кода,
   а от календаря (в 838 набор был зелёным только по субботам — заметили через месяц).
   Поэтому все даты фиксированы, а ответы источников заморожены в tests/fixtures/.
   CI отдельным шагом ищет в tests/ вызовы текущего времени и валит сборку — ci.yml,
   шаг «В тестах нет „сегодня минус N“».
2. В СЕТЬ НЕ ХОДИМ. Фетчеры проверяются через подмену `pipeline.lib.http.get_bytes`:
   подменяется самый нижний слой, чтобы работали настоящие декодирование (windows-1251),
   разбор JSON и постраничный обход, а не только «парсер строки».
3. МОДУЛЬ МОЖЕТ ЕЩЁ НЕ СУЩЕСТВОВАТЬ. Файлы пишут параллельные агенты, поэтому тест
   отсутствующего модуля обязан ПРОПУСКАТЬСЯ с внятным сообщением, а не падать:
   красный набор из-за ненаписанного файла обесценивает красный цвет вообще.
"""

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# `unittest discover -t .` кладёт корень в sys.path сам, но набор запускают и из
# каталога tests, и из IDE — тогда `import pipeline...` не находится. Дешевле
# подстраховаться здесь, чем объяснять каждому запускающему про PYTHONPATH.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Логи пайплайна — на русском, а консоль Windows по умолчанию cp1252: любой print
# с кириллицей падает UnicodeEncodeError и уносит с собой тест, который проверял
# совсем другое (ловилось на test_store: диагностика битого JSON роняла проверку
# карантина). Прод — Linux/UTF-8, поэтому это защита среды разработки, а не логики.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # поток подменён/не текстовый — не беда
        pass


def need(test, dotted, *attrs):
    """Импортировать модуль пайплайна или пропустить тест с внятной причиной.

    Возвращает модуль. Если модуля нет (его пишет параллельный агент) либо в нём
    ещё нет нужных имён — self.skipTest, а не ImportError.
    """
    try:
        mod = importlib.import_module(dotted)
    except ImportError as exc:
        test.skipTest(f"нет модуля {dotted} ({exc}); тест включится после интеграции")
        raise  # недостижимо: skipTest бросает SkipTest
    missing = [a for a in attrs if not hasattr(mod, a)]
    if missing:
        test.skipTest(f"{dotted}: нет ещё {', '.join(missing)}; "
                      f"тест включится после интеграции")
    return mod


def need_any(test, dotted, *names):
    """Модуль + первый существующий атрибут из `names` (когда имя функции ещё не
    устоялось между агентами). -> (модуль, имя, объект)."""
    mod = need(test, dotted)
    for name in names:
        fn = getattr(mod, name, None)
        if callable(fn):
            return mod, name, fn
    test.skipTest(f"{dotted}: не нашёл ни одной из функций {', '.join(names)}; "
                  f"тест включится после интеграции")


def fixture_bytes(name):
    return (FIXTURES / name).read_bytes()


def fixture_text(name, encoding="utf-8"):
    return (FIXTURES / name).read_text(encoding=encoding)


def fixture_json(name):
    return json.loads(fixture_text(name))


def panel_small():
    """Замороженная панель на 300 торговых дней (правила построения — docs/TESTING.md)."""
    return fixture_json("panel_small.json")


def fake_http(mapping, default=None):
    """Замена `http.get_bytes`: (подстрока URL) -> bytes.

    Подменяем именно нижний слой, а не get_json/get_text: тогда в тесте работают
    настоящие gzip/декодирование/парсинг JSON, и ошибка кодировки не проскочит.
    """
    def _get_bytes(url, **_kw):
        for needle, data in mapping.items():
            if needle in url:
                return data if isinstance(data, bytes) else json.dumps(
                    data, ensure_ascii=False).encode("utf-8")
        if default is not None:
            return default if isinstance(default, bytes) else json.dumps(
                default, ensure_ascii=False).encode("utf-8")
        raise AssertionError(f"тест пошёл в сеть за незамоканным URL: {url}")
    return _get_bytes
