"""Контраст токенов панели по WCAG — в наборе, а не «на глаз».

Зачем: валидатор палитры проверяет марки друг против друга и против поверхности,
но НЕ проверяет чернила по поверхности. Из-за этой дыры светлая тема месяцами
жила с вторичной прозой на 3,26–3,50 при норме 4,5, с жёлтой точкой статуса на
1,79 при норме 3,0 и с нулевой осью графиков на 1,75 — и ни один прогон об этом
не сказал. Здесь фиксируются ровно те пары, которые несут смысл: если кто-то
осветлит --ink-3 «чтобы было воздушнее», набор упадёт.

Осознанные исключения (проверяются отдельно, чтобы их нельзя было потерять молча):
  • --s3 на светлой поверхности 2,74 — документированный тон справочной палитры,
    для него действует правило рельефа (подпись у каждой линии + режим таблицы);
  • --mid 1,23/1,48 — середина диверджент-шкалы, её нельзя темнить, иначе она
    перестанет читаться как «ничего». Различимость даёт волосяной контур
    --ink-3/--axis у нейтрального сегмента, свотча легенды и рамки ленты.
"""

import re
import unittest
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "web" / "styles.css"

TEXT_MIN = 4.5      # обычный текст (крупным на панели ни один цветной не считается)
GRAPHIC_MIN = 3.0   # нетекстовые марки и линии, WCAG 1.4.11


def _tokens():
    """Светлые и тёмные токены. Тёмный блок НАСЛЕДУЕТ светлый: в CSS так и есть —
    что не переобъявлено в :root[data-theme=dark], приходит из :root."""
    text = CSS.read_text(encoding="utf-8")

    def block(marker):
        i = text.index(marker)
        j = text.index("\n}", i)
        return {k: v.strip() for k, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", text[i:j])}

    light = block(":root {")
    dark = dict(light)
    dark.update(block(':root[data-theme="dark"] {'))
    media = block('@media (prefers-color-scheme: dark)')
    return light, dark, media


def _rgb(v):
    m = re.match(r"^#([0-9a-fA-F]{6})$", v.strip())
    if not m:
        raise ValueError(f"ожидался #rrggbb, пришло {v!r}")
    h = m.group(1)
    return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _ratio(a, b):
    la = sum(w * _lin(c) for w, c in zip((0.2126, 0.7152, 0.0722), _rgb(a)))
    lb = sum(w * _lin(c) for w, c in zip((0.2126, 0.7152, 0.0722), _rgb(b)))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# (что это на экране, чем красим, по чему, порог)
PAIRS = [
    ("вторичная проза и подписи осей", "--ink-3", "--surface", TEXT_MIN),
    ("вторичная проза в чипах и бейджах", "--ink-3", "--surface-2", TEXT_MIN),
    ("подписи секций и подвал", "--ink-3", "--plane", TEXT_MIN),
    ("основной вторичный текст", "--ink-2", "--surface", TEXT_MIN),
    ("вклад/число со знаком «плюс»", "--pos-ink", "--surface", TEXT_MIN),
    ("вклад/число со знаком «минус»", "--neg-ink", "--surface", TEXT_MIN),
    ("тон «плюс» в чипе", "--pos-ink", "--surface-2", TEXT_MIN),
    ("тон «минус» в чипе", "--neg-ink", "--surface-2", TEXT_MIN),
    ("бейдж тира B и ссылки", "--s1", "--surface-2", TEXT_MIN),
    ("ссылка в подвале", "--s1", "--plane", TEXT_MIN),
    ("строка «данные устарели»", "--serious-ink", "--plane", TEXT_MIN),
    ("точка статуса «устарело»", "--warn-ink", "--surface", GRAPHIC_MIN),
    ("точка статуса «ок»", "--good", "--surface", GRAPHIC_MIN),
    ("точка статуса «ошибка»", "--crit", "--surface", GRAPHIC_MIN),
    ("точка статуса «нет данных»", "--ink-3", "--surface", GRAPHIC_MIN),
    ("нулевая ось и рамка ленты", "--axis", "--surface", GRAPHIC_MIN),
    ("линия «плюс»", "--pos", "--surface", GRAPHIC_MIN),
    ("линия «минус»", "--neg", "--surface", GRAPHIC_MIN),
    ("слот 1", "--s1", "--surface", GRAPHIC_MIN),
    ("слот 2", "--s2", "--surface", GRAPHIC_MIN),
    ("контур нейтрального свотча", "--ink-3", "--surface", GRAPHIC_MIN),
]


class TestTokenContrast(unittest.TestCase):
    def setUp(self):
        self.light, self.dark, self.media = _tokens()

    def test_pairs_both_themes(self):
        bad = []
        for theme, toks in (("светлая", self.light), ("тёмная", self.dark)):
            for what, fg, bg, need in PAIRS:
                r = _ratio(toks[fg], toks[bg])
                if r < need - 0.005:
                    bad.append(f"{theme}: {what} — {fg} на {bg} = {r:.2f} при норме {need}")
        self.assertEqual([], bad, "\n" + "\n".join(bad))

    def test_declared_exceptions_still_exceptions(self):
        """Исключения не должны тихо расползаться: если тон изменится так, что
        станет проходить, — это хорошо, но пусть автор придёт и удалит запись."""
        self.assertLess(_ratio(self.light["--s3"], self.light["--surface"]), GRAPHIC_MIN)
        self.assertLess(_ratio(self.light["--mid"], self.light["--surface"]), GRAPHIC_MIN)
        # Компенсация: контур, которым нейтраль и слот отделяются от поверхности.
        self.assertGreaterEqual(_ratio(self.light["--ink-3"], self.light["--surface"]), GRAPHIC_MIN)
        self.assertGreaterEqual(_ratio(self.dark["--ink-3"], self.dark["--surface"]), GRAPHIC_MIN)

    def test_dark_declared_twice_identically(self):
        """Тёмная тема объявлена дважды (медиазапрос и data-theme). Разъехавшиеся
        блоки — классический способ получить панель, которая в системной тёмной
        выглядит иначе, чем в переключённой вручную."""
        self.assertTrue(self.media, "медиазапрос тёмной темы не разобрался")
        toggle = {k: v for k, v in self.dark.items() if k in self.media}
        self.assertEqual(self.media, toggle)
        # и наоборот: в переключателе не должно быть токенов, которых нет в медиа
        light, _, _ = _tokens()
        toggle_only = {k for k in self.dark if k not in light or self.dark[k] != light[k]}
        self.assertEqual(set(self.media), toggle_only)

    def test_no_opacity_muting_on_tiles(self):
        """opacity на карточке — это не приглушение, а нечитаемость: .72 опускала
        заметку опровергнутого тайла до 2,34."""
        text = CSS.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\.tile--dead\s*\{[^}]*opacity")


class TestTypeScale(unittest.TestCase):
    """Шкала кеглей и начертаний: объявленных значений должно быть столько же,
    сколько система реально рисует. Девять начертаний в диапазоне 400–700
    system-ui рисует тремя, а девять кеглей в коридоре 10,5–15px не различает
    никто."""

    def test_weights(self):
        text = CSS.read_text(encoding="utf-8")
        weights = {int(w) for w in re.findall(r"font-weight:\s*(\d+)", text)}
        self.assertTrue(weights <= {400, 600, 700}, f"лишние начертания: {sorted(weights)}")

    def test_sizes(self):
        text = CSS.read_text(encoding="utf-8")
        sizes = {float(s) for s in re.findall(r"font-size:\s*([\d.]+)px", text)}
        ladder = {11, 13, 15, 21, 26, 38, 46}
        self.assertTrue(sizes <= ladder, f"кегли вне шкалы {sorted(ladder)}: {sorted(sizes - ladder)}")


if __name__ == "__main__":
    unittest.main()
