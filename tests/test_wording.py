"""Как панель разговаривает наружу.

ПОЧЕМУ ЭТОТ ФАЙЛ. Сообщения в телеграме читает человек без панели перед глазами, а
до 14.08.2026 туда уезжала внутренняя кухня дословно: «Смена ячейки: bear|stress|ok
→ bear|stress|stress», «Ядро развернулось», «Облигационный флаг ВКЛЮЧЁН», «hit 0.64»,
«dd<−10%», «статус dead», «Лиз писателя потерян». Владелец прямо сказал, что читать
это невозможно.

Отдельная горькая деталь: промпт комментатора (`lib/commentary.py`) ЗАПРЕЩАЕТ модели
слова «композит, ядро, ячейку, слои, мониторы» — то есть правило «не говорить
жаргоном» в проекте было, но применялось только к ИИ-комментарию, а сам факт над ним
писался жаргоном.

Здесь закреплён шлюз наружу, устроенный так же, как в 837/838: словарь внешних имён,
русские числа, единая сборка сообщения. Главная проверка — `test_жаргон_не_протекает`:
она же есть у 837 (`notify-test.mjs`), и она же ловит возврат старых формулировок.
"""

import re
import unittest

from tests import need


class WordingCase(unittest.TestCase):
    def setUp(self):
        self.w = need(self, "pipeline.lib.wording", "num", "pct", "ru_day", "ru_month",
                      "plural", "cell_words", "cell_plain", "regime_name", "points",
                      "sentence", "render_market", "render_ops", "plain_text", "esc")


class TestNumbers(WordingCase):
    def test_разделитель_запятая_минус_типографский(self):
        self.assertEqual(self.w.num(-0.66, 2, plus=True), "−0,66")
        self.assertEqual(self.w.num(0.66, 2, plus=True), "+0,66")
        self.assertEqual(self.w.pct(14.0, 2), "14,00%")

    def test_тысячи_разделяются_неразрывным_пробелом(self):
        # Неразрывный намеренно: обычный пробел позволяет телеграму перенести
        # строку внутри числа, и «415 649» превращается в «415» и «649».
        self.assertEqual(self.w.num(415649, 0), "415 649")

    def test_отсутствующее_число_это_слово_а_не_ноль(self):
        for junk in (None, "12", True):
            self.assertEqual(self.w.num(junk), "н/д")

    def test_дата_для_человека_а_не_для_машины(self):
        self.assertEqual(self.w.ru_day("2026-07-15"), "15.07.2026")
        self.assertEqual(self.w.ru_month("2026-07-31"), "июль 2026")

    def test_склонение_считается_а_не_угадывается(self):
        self.assertEqual(self.w.plural(1, "месяц", "месяца", "месяцев"), "месяц")
        self.assertEqual(self.w.plural(3, "месяц", "месяца", "месяцев"), "месяца")
        self.assertEqual(self.w.plural(11, "месяц", "месяца", "месяцев"), "месяцев")

    def test_дробные_пункты_в_родительном_единственном(self):
        # «на 8,1 процентных пунктов» — счёт по целой части; по-русски «пункта».
        self.assertEqual(self.w.points(8.1), "8,1 процентного пункта")
        self.assertEqual(self.w.points(3.0), "3,0 процентных пункта")

    def test_чужая_строка_нормализуется(self):
        self.assertEqual(self.w.ru_decimals("физлица купили на 12.3 млрд"),
                         "физлица купили на 12,3 млрд")
        self.assertIn("−5", self.w.ru_decimals("шаг -5 б.п."))

    def test_длительность_без_нулевых_минут(self):
        self.assertEqual(self.w.hours_minutes(15), "15 мин")
        self.assertEqual(self.w.hours_minutes(1440), "24 ч")
        self.assertEqual(self.w.hours_minutes(95), "1 ч 35 мин")

    def test_момент_по_москве(self):
        from datetime import datetime, timezone
        ref = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(self.w.msk_when("2026-10-21T15:30:16Z", ref), "18:30 МСК")
        # 21:30 UTC 20.10 — уже 21.10 по Москве: тот же день, что и ref.
        self.assertEqual(self.w.msk_when("2026-10-20T21:30:00Z", ref), "00:30 МСК")
        self.assertEqual(self.w.msk_when("2026-10-19T20:00:00Z", ref), "19.10, 23:00 МСК")
        self.assertEqual(self.w.msk_when("2025-12-31T12:00:00Z", ref), "31.12.2025, 15:00 МСК")
        self.assertEqual(self.w.msk_when("мусор", ref), "н/д")


class TestNames(WordingCase):
    def test_код_ячейки_превращается_в_слова(self):
        # мутация: слать код как есть -> «bear|stress|stress» в телеграме.
        got = self.w.cell_words("bear|stress|stress")
        self.assertEqual(got, "падающий рынок · нервная торговля · ОФЗ под давлением")
        self.assertNotIn("|", got)

    def test_у_каждого_режима_есть_внешнее_имя(self):
        """Правило пополнения: новый режим — новая строка в REGIME_NAMES.

        Иначе наружу уедет подпись из таблицы модели, как уехало слово «ячейка».
        """
        constants = need(self, "pipeline.lib.constants", "CELL_STATS")
        for stats in constants.CELL_STATS.values():
            label = stats["label"]
            with self.subTest(label=label):
                self.assertIn(label.lower(), self.w.REGIME_NAMES,
                              f"режим «{label}» без внешнего имени")
                self.assertNotIn("ячейк", self.w.regime_name(label))

    def test_статистика_режима_без_обозначений_таблицы(self):
        got = self.w.cell_plain({"median_fwd1m_pct": 0.64, "worst_pct": -30.0,
                                 "hit": 0.54, "n": 25, "n_closed": 24})
        for token in ("n=", "hit", "mean", "%/мес"):
            self.assertNotIn(token, got, f"обозначение таблицы «{token}» уехало наружу")
        self.assertIn("типичный месяц +0,6%", got)
        # 24, а не 25: медиана, доля плюсовых и худший месяц посчитаны по закрытым
        # месяцам, и называть рядом с ними другую выборку — та же подмена, из-за
        # которой долю плюсовых однажды переписали по незакрытому месяцу.
        self.assertIn("24 месяца истории", got)
        self.assertIn("закрывались 54%", got)

    def test_без_n_closed_выборка_прежняя(self):
        got = self.w.cell_plain({"median_fwd1m_pct": 1.22, "worst_pct": -16.7,
                                 "hit": 0.59, "n": 110})
        self.assertIn("110 месяцев истории", got)

    def test_строка_становится_предложением(self):
        self.assertEqual(self.w.sentence("просадка RGBI −1,2% от максимума"),
                         "Просадка RGBI −1,2% от максимума.")
        self.assertEqual(self.w.sentence(""), "")


class TestRender(WordingCase):
    def event(self, **kw):
        base = {"kind": "cb_decision", "title": "Банк России снизил ключевую ставку",
                "before": "15,00%", "after": "14,00%",
                "detail": "Шаг −100 б.п.", "ts": "2026-08-12T09:00:00Z"}
        base.update(kw)
        return base

    def test_рыночное_сообщение_собрано_как_у_соседей(self):
        got = self.w.render_market(self.event(comment="Ставка идёт к нейтральной."))
        lines = got.split("\n")
        # Пометка панели — первым словом заголовка, значок — по направлению.
        self.assertEqual(lines[0], "📉 <b>Рынок РФ · Банк России снизил ключевую ставку</b>")
        self.assertEqual(lines[1], "15,00% → <b>14,00%</b>")
        comment = next(i for i, line in enumerate(lines) if line.startswith("💬 "))
        self.assertEqual(lines[comment - 1], "", "разбор модели отбивается пустой строкой")
        # Подвал — последней строкой после пустой: ссылка и время события по Москве.
        self.assertEqual(lines[-2], "")
        self.assertEqual(lines[-1], '<a href="https://tzi-842.pages.dev">Открыть панель</a>'
                                    " · 12.08, 12:00 МСК")

    def test_без_комментария_пустой_строки_нет(self):
        got = self.w.render_market(self.event())
        self.assertNotIn("💬", got)
        self.assertFalse(got.endswith("\n"))
        self.assertEqual(got.count("\n\n"), 1, "пустая строка — только перед подвалом")

    def test_данные_экранируются_а_разметка_нет(self):
        got = self.w.render_market(self.event(title="ЦБ <b>поднял</b> ставку"))
        self.assertIn("&lt;b&gt;поднял&lt;/b&gt;", got)
        self.assertIn("<b>Рынок РФ · ЦБ &lt;b&gt;", got, "своя разметка не должна экранироваться")

    def test_санитарное_в_формате_общего_мостика(self):
        """Тот же вид, что шлёт /usr/local/sbin/dash-notify для 837, 838 и 839."""
        got = self.w.render_ops({"kind": "core_missing", "title": "панель потеряла оценку",
                                 "fact": "Вчера было +0,66, сегодня «нет данных».",
                                 "meaning": "Похоже на неполный набор рядов.",
                                 "where": "Смотреть: карточку «Здоровье модели»."})
        lines = got.split("\n")
        self.assertEqual(lines[0], "🔴 <b>Рынок РФ · Панель потеряла оценку</b>")
        self.assertEqual(len(lines), 6, "факт, следствие, «куда смотреть», пустая, подвал")
        self.assertTrue(lines[3].startswith("Смотреть:"),
                        "сообщение о поломке без адреса поломки заставляет искать заново")
        self.assertTrue(lines[-1].startswith('<a href="https://tzi-842.pages.dev">'))

    def test_порог_здоровья_жёлтый_а_не_красный(self):
        # health_review — достигнутый порог плановой ревалидации, а не поломка:
        # красный значок читался бы как «панель сломалась» (аудит 02.09.2026).
        got = self.w.render_ops({"kind": "health_review", "title": "достигнут порог",
                                 "fact": "12 месяцев подряд.", "where": "Смотреть: §7."})
        self.assertTrue(got.startswith("🟡 "))
        self.assertNotIn("health_dead", self.w.OPS_KIND)

    def test_у_смены_позиции_свой_вид(self):
        # Смена позиции — главное событие панели: 🔔 по словарю стандарта §4.
        self.assertEqual(self.w.KIND["position_change"]["emoji"], "🔔")

    def test_длинное_сообщение_режется_под_предел_телеграма(self):
        got = self.w.render_market(self.event(detail="я" * 6000))
        self.assertLessEqual(len(got), self.w.TG_LIMIT)
        self.assertIn("…", got)
        # Режется тело, подвал со ссылкой остаётся целым.
        self.assertTrue(got.endswith("12:00 МСК"), got[-80:])

    def test_плоский_текст_для_журнала_без_разметки(self):
        got = self.w.plain_text(self.event())
        for token in ("<b>", "→ <b>", "📊"):
            self.assertNotIn(token, got)
        self.assertIn("15,00% → 14,00%", got)


class TestJargonGate(unittest.TestCase):
    """Сторож: внутренние слова не имеют права уехать читателю.

    Такая же проверка стоит у 837 (`notify-test.mjs`): «внутренняя терминология
    снова протекла наружу». Список — ровно те слова, которые уезжали до правки.
    """

    JARGON = re.compile(
        r"\bячейк|\bядро\b|\bядра\b|композит|\bлиз\b|hit\s|\bn=|dd<|"
        r"\bdead\b|\bstale\b|\bwarn\b|bull\||bear\||\|stress|\|calm|\|ok\b|"
        # слой решения (02.09.2026): устройство ворот наружу не выпускаем
        r"гистерезис|\bбит\b|\bбита\b|\bбиты\b|\bбитов\b|\breview\b",
        re.I)

    def setUp(self):
        from tests import test_alerts as ta
        self.case = ta.TestTexts("test_every_event_carries_a_number")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.alerts = need(self, "pipeline.alerts", "render", "is_ops")

    def test_жаргон_не_протекает(self):
        for ev in self.case.all_kinds():
            with self.subTest(kind=ev["kind"]):
                found = self.JARGON.search(self.alerts.render(ev))
                self.assertIsNone(found,
                                  f"{ev['kind']}: внутреннее слово «{found.group(0) if found else ''}» "
                                  f"уехало читателю — {ev['text'][:120]}")

    def test_у_каждого_события_есть_заголовок(self):
        for ev in self.case.all_kinds():
            with self.subTest(kind=ev["kind"]):
                self.assertTrue(ev.get("title"), "событие без заголовка")
                self.assertLess(len(ev["title"]), 70,
                                "заголовок длиннее строки телефона")

    def test_санитарные_называют_куда_смотреть(self):
        for ev in self.case.all_kinds():
            if not self.alerts.is_ops(ev):
                continue
            with self.subTest(kind=ev["kind"]):
                self.assertIn("Смотреть:", self.alerts.render(ev))


if __name__ == "__main__":
    unittest.main()
