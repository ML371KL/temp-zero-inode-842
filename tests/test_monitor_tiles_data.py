"""Data-paths всех 16 тайлов monitors.py на синтетическом сторе С ДАННЫМИ.

ПОЧЕМУ этот файл существует: аудит показал, что 12–14 тайлов из 16 исполнялись
тестами только на ПУСТОМ сторе — ветка «нет данных» покрыта, а заголовки, знаки и
единицы живого пути не проверял никто. Ровно этот класс дефектов четырежды за две
недели доезжал до прода:

* «7800-й перцентиль» — процент, умноженный на 100 второй раз;
* дивдоходность бумаги печаталась на месте миллиардов выплат;
* вес×доходность (гэп индекса) путался с доходностью отдельной бумаги;
* «лонги» (контракты) печатались на месте ЧИСЛА ЛИЦ-держателей.

Правила проверок:
1. числа в headline — те же, что в payload (никаких ×100/÷100, потерянных знаков);
2. ожидаемые значения посчитаны РУКАМИ из тех же входов (литерал с арифметикой в
   комментарии или независимая формула) — самоссылочный «ожидаемое = вызов той же
   функции» запрещён;
3. status/asof осмысленны: не «error» на исправных данных, asof не в будущем.

Часы ЗАМОРОЖЕНЫ: каждый тайл принимает `now` параметром, поэтому настоящие часы
не нужны вовсе (правило №1 набора — никаких «сегодня минус N»). Свежесть данных
задаётся фиксированной меткой fetched_at за полчаса до NOW: моложе любого SLA,
значит статус honestly «ok», а не случайно «stale» по календарю машины.
Сеть не нужна: тайлы читают только стор.

Числа фикстур выбраны «кривыми» (43.2, 15.6, 87.5…), чтобы перестановка полей и
масштабирование меняли результат: на 1.0 и 100 мутации невидимы.
"""

import math
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from tests import need

UTC = timezone.utc
NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)  # понедельник, 15:00 МСК
TODAY = "2026-08-17"                               # дата NOW по Москве
FETCHED = "2026-08-17T11:30:00Z"                   # «опросили полчаса назад» < любого SLA
NBSP = "\xa0"  # разделитель разрядов в заголовках (_n): неразрывный пробел, не обычный


def days(end, n):
    """n календарных дат подряд, последняя = end. Все даты фиксированы литералом."""
    last = date.fromisoformat(end)
    return [(last - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


class TileCase(unittest.TestCase):
    """Обвязка: временный STATE_DIR + сидеры рядов, общие для юнит- и интеграционного слоя."""

    def setUp(self):
        self.monitors = need(self, "pipeline.compute.monitors",
                             "BUILDERS", "build_monitors")
        self.store = need(self, "pipeline.lib.store", "save_series", "load_series")
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev = os.environ.get("STATE_DIR")
        os.environ["STATE_DIR"] = self.tmp.name
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self.prev is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self.prev

    def put(self, sid, points, meta=None):
        m = {"status": "ok", "fetched_at": FETCHED}
        m.update(meta or {})
        self.store.save_series(sid, {"id": sid, "points": dict(points), "meta": m})

    def tile(self, tid, now=NOW):
        # Снятые с витрины тайлы (cpi_weekly, аудит 02.09.2026) живут функциями
        # _t_<id> без строки в BUILDERS: их data-path тоже проверяется, чтобы
        # возврат тайла стоил одной строки, а не починки.
        fn = dict(self.monitors.BUILDERS).get(tid) or getattr(self.monitors, f"_t_{tid}")
        return fn(self.store, now)

    def vitals(self, t, tid, today=TODAY):
        """Требование №3: на исправных данных статус «ok», asof есть и не в будущем."""
        self.assertEqual(t["id"], tid)
        self.assertEqual(t["status"], "ok",
                         f"{tid}: исправные данные, а статус {t['status']!r} "
                         f"(headline: {t.get('headline')!r})")
        self.assertNotIn("error", t["payload"])
        self.assertIsNotNone(t["asof"], f"{tid}: asof пуст на живых данных")
        self.assertLessEqual(str(t["asof"])[:10], today,
                             f"{tid}: asof {t['asof']!r} из будущего — витрина знает "
                             f"больше, чем произошло")

    # ------------------------------------------------------------------ сидеры
    # Каждый сидер кладёт в стор ровно те ряды, что читает один тайл; интеграционный
    # тест вызывает их все на одном сторе, поэтому пересекающиеся ряды (imoex/mcftr)
    # вынесены в seed_market и согласованы между потребителями.

    ORFR_MONTHS = ["2026-01-31", "2026-02-28", "2026-03-31",
                   "2026-04-30", "2026-05-31", "2026-06-30"]

    def seed_orfr(self):
        self.put("orfr_flows_fiz",
                 dict(zip(self.ORFR_MONTHS, [5.0, 8.0, 12.0, 3.0, 7.0, 21.7])),
                 {"unit": "млрд ₽"})
        self.put("orfr_flows_nfo_du",
                 dict(zip(self.ORFR_MONTHS, [-10.0, -20.0, -30.0, -5.0, -15.0, -42.5])))

    def seed_lqdt(self):
        self.put("lqdt_aum", dict(zip(
            days("2026-08-15", 6),
            [1300.0, 1280.0, 1260.0, 1240.0, 1200.0, 1234.4])))

    def seed_market(self):
        """imoex ровный 3000.0 на 260 дней; mcftr 1000.0 везде, последняя точка 1080.0.

        Ровный индекс делает дивдоходность чистой функцией одного скачка MCFTR, а
        отношение mcxsm/imoex — чистой функцией mcxsm: ожидаемые числа считаются
        в одну строку арифметики.
        """
        span = days("2026-08-15", 260)
        self.put("imoex", {d: 3000.0 for d in span})
        self.put("mcftr", {d: (1080.0 if d == span[-1] else 1000.0) for d in span})

    def seed_deposit(self):
        self.put("deposit_decade", {"2026-07-31": 15.9, "2026-08-10": 15.6})

    def seed_dividends(self):
        self.put("dividends", {}, {
            "asof": "2026-08-16",  # день ЧТЕНИЯ календаря, отсечки лежат в будущем
            "items": [
                # прошедшая отсечка — не должна попасть ни в headline, ни в суммы
                {"ticker": "GAZP", "ex_date": "2026-07-15", "yield_pct": 5.1,
                 "amount_bn": 200.0, "index_drag_pct": 0.8, "weight_pct": 9.0},
                {"ticker": "SBER", "ex_date": "2026-09-10", "yield_pct": 9.3,
                 "amount_bn": 752.0, "index_drag_pct": 1.302, "weight_pct": 14.0},
                {"ticker": "LKOH", "ex_date": "2026-10-05", "yield_pct": 6.2,
                 "amount_bn": 380.0, "index_drag_pct": 0.62, "weight_pct": 10.0},
                # за горизонтом 90 дней — в списке upcoming, но вне сумм окна
                {"ticker": "TATN", "ex_date": "2026-12-20", "yield_pct": 4.0,
                 "amount_bn": 100.0, "index_drag_pct": 0.2, "weight_pct": 4.0},
            ]})

    def seed_cb(self):
        self.put("key_rate", {"2026-06-10": 17.0, "2026-07-27": 16.0})
        # консенсусы лежат по датам ЗАСЕДАНИЙ; строка прошедшего (24.07) оставлена
        # нарочно — ровно на ней ловится мутация «фолбэк на последнюю точку»
        self.put("cb_consensus", {"2026-07-24": 18.0, "2026-09-11": 15.0})
        self.put("rusfar3m", {"2026-08-15": 15.4})

    def seed_cpi(self):
        weeks = ["2026-06-23", "2026-06-30", "2026-07-07", "2026-07-14",
                 "2026-07-21", "2026-07-28", "2026-08-04", "2026-08-11"]
        self.put("cpi_weekly", dict(zip(
            weeks, [0.20, 0.18, 0.12, 0.07, 0.10, 0.05, 0.15, 0.10])))

    def seed_expectations(self):
        """Год ОФЗ 13.65 тридцать дней подряд, последние три — 13.75; ключ 14.0 всё
        время; RUSFAR 13.9; полгода 13.5. Спред сегодня −0.25, 21 точку назад −0.35."""
        span = days("2026-08-14", 30)
        self.put("zcyc_y1", {d: (13.75 if d in span[-3:] else 13.65) for d in span})
        self.put("zcyc_y0_5", {span[-1]: 13.5})
        self.put("key_rate", {"2026-07-01": 14.0})
        self.put("rusfar3m", {"2026-08-14": 13.9})

    def seed_ofz(self):
        self.put("ofz_auctions", {"2026-08-06": 61.0, "2026-08-13": 43.2},
                 {"last": {"date": "2026-08-13", "issue": "ОФЗ-26248",
                           "placed_bn": 43.2, "demand_bn": 87.5, "premium_bp": 5}})

    def seed_polymarket(self, meta=None):
        base = {"question": "Russia x Ukraine ceasefire agreement by December 31, 2026?",
                # endDate у биржи — момент РАСЧЁТА (полночь по Нью-Йорку плюс
                # буфер), поэтому он на сутки позже даты из вопроса. Заголовок
                # обязан называть дату вопроса, горизонт — считаться по расчёту.
                "end_date": "2027-01-01T04:59:00Z", "resolves_on": "2026-12-31",
                "horizon_days": 133, "volume": 2150868.76}
        base.update(meta or {})
        self.put("polymarket_ceasefire", dict(zip(
            days("2026-08-16", 10),
            [0.28, 0.28, 0.29, 0.30, 0.31, 0.31, 0.32, 0.33, 0.34, 0.35])), base)

    def seed_futoi(self):
        span = days("2026-08-15", 45)
        self.put("futoi_mx_fiz_pos",
                 {d: (30000.0 if d == span[-1] else 20000.0) for d in span})
        self.put("futoi_mx_fiz_long", {d: 60000.0 for d in span})
        self.put("futoi_mx_fiz_short", {d: -40000.0 for d in span})
        self.put("futoi_mx_fiz_long_num", {span[-1]: 12500.0})
        self.put("futoi_mx_fiz_short_num", {span[-1]: 8300.0})

    def seed_rvi(self):
        span = days("2026-08-14", 25)
        vals = [float(20 + i) for i in range(24)] + [32.5]  # 20..43, последняя 32.5
        self.put("rvi", dict(zip(span, vals)))

    def seed_rub_barrel(self):
        self.put("urals_tax", {"2026-05-31": 58.0, "2026-06-30": 60.0})
        self.put("usd_cbr", {"2026-06-10": 79.0, "2026-06-20": 81.0,
                             "2026-08-14": 84.0, "2026-08-15": 85.0})
        self.put("brent_moex", {"2026-08-15": 65.0})

    def seed_breadth(self):
        span = days("2026-08-14", 25)
        self.put("breadth", {d: (0.42 if d == span[-1] else 0.30) for d in span})

    def seed_mcxsm(self):
        self.seed_market()
        span = days("2026-08-14", 70)
        self.put("mcxsm", {d: (264.0 if d == span[-1] else 240.0) for d in span})

    def seed_hy(self):
        span = days("2026-08-14", 30)
        self.put("rucbhycp_yield",
                 {d: (24.5 if d == span[-1] else 22.2) for d in span})
        self.put("rucbcpns_yield", {span[-1]: 17.0})
        self.put("zcyc_y2", {d: 14.2 for d in span})

    def seed_retail(self):
        self.put("moex_retail", {"2026-07-31": 3.0}, {
            "asof": "2026-07-31", "url": "https://www.moex.com/n0",
            "payload": {
                "clients_total_mln": 41.9, "share_equity_pct": 67.0,
                "inflow_equity_bln": 12.4, "clients_added_k": 350.0,
                "inflow_bonds_bln": 100.2, "inflow_funds_bln": 45.6,
                "portfolio": [
                    {"name": "Сбербанк", "share_pct": 31.8},
                    {"name": "Сбербанк (прив.)", "share_pct": 7.3},
                    {"name": "Газпром", "share_pct": 14.1},
                    {"name": "ЛУКОЙЛ", "share_pct": 12.0},
                ]}})


class TestТайлыНаЖивыхДанных(TileCase):
    """По тайлу на тест: headline против payload против арифметики руками."""

    def test_orfr_числа_заголовка_совпадают_с_payload(self):
        """Стек потоков: ДУ и физлица в заголовке — те же млрд, что в payload.

        Мутация «×100» дала бы -4250.0, потерянный знак — +42.5: обе ловятся точным
        текстом. Исчерпание продавца: суммы кварталов посчитаны руками из фикстуры.
        """
        self.seed_orfr()
        t = self.tile("orfr")
        self.vitals(t, "orfr")
        self.assertEqual(t["asof"], "2026-06-30")
        p = t["payload"]
        self.assertEqual(p["last"]["nfo_du"], -42.5)
        self.assertEqual(p["last"]["fiz"], 21.7)
        se = p["seller_exhaustion"]
        self.assertEqual(se["sum_3m_nfo_du"], -62.5)  # −5 − 15 − 42.5
        self.assertEqual(se["prev_3m"], -60.0)        # −10 − 20 − 30
        self.assertEqual(se["delta"], -2.5)           # −62.5 − (−60)
        self.assertIn("июнь 2026", t["headline"])
        self.assertIn("ДУ -42.5 млрд", t["headline"])
        self.assertIn("физлица +21.7 млрд", t["headline"])
        self.assertEqual(p["months"][-1], "2026-06-30")

    def test_lqdt_сча_и_дневной_поток(self):
        """СЧА в млрд и поток за день: ловит и ×100, и перепутанные aum/peak.

        dd руками: (1234.4/1300 − 1)·100 = −5.046 → −5.0; порог −10 не пробит.
        """
        self.seed_lqdt()
        t = self.tile("lqdt")
        self.vitals(t, "lqdt")
        self.assertEqual(t["asof"], "2026-08-15")
        p = t["payload"]
        self.assertEqual(p["aum"], 1234.4)
        self.assertEqual(p["chg_1d"], 34.4)     # 1234.4 − 1200: ИЗМЕНЕНИЕ СЧА,
        # а не приток — СЧА растёт и сама, на доходность пая
        self.assertEqual(p["chg_5d"], -65.6)   # 1234.4 − 1300
        # Пик и просадка от него — вывод ОБ ИСТОРИИ, и на коротком ряде их нет:
        # у живого lqdt_aum было семь точек, а тайл печатал «просадка 0,0%» и
        # «большой ротации не случалось» — вердикт об истории из недели наблюдений.
        self.assertIsNone(p["peak"], "пик посчитан по огрызку ряда")
        self.assertIsNone(p["dd_from_peak_pct"])
        self.assertFalse(p["rotation_started"])
        self.assertIn("истории мало", t["headline"])
        # Заголовок называет ФОНД: ряд — СЧА одного LQDT, а не всего рынка
        # денежных фондов (тот больше 1,8 трлн).
        self.assertIn(f"СЧА LQDT 1{NBSP}234 млрд", t["headline"])
        self.assertIn("изменение за день +34.4 млрд", t["headline"])
        self.assertNotIn("поток", t["headline"].lower())

    def test_deposit_spread_дивдоходность_и_спред(self):
        """Дивдоходность из MCFTR/IMOEX и спред к вкладам, оба в процентах.

        Руками: индекс ровный, MCFTR за окно ×1.08 → dy = ln(1.08)·100 = 7.696 → 7.7;
        спред = 7.696 − 15.6 = −7.90 п.п. Потерянный минус спреда объявил бы
        «событие впервые в истории» — заголовок проверяется на «не в пользу акций».
        """
        self.seed_market()
        self.seed_deposit()
        t = self.tile("deposit_spread")
        self.vitals(t, "deposit_spread")
        self.assertEqual(t["asof"], "2026-08-10")
        p = t["payload"]
        exp_dy = round(math.log(1080.0 / 1000.0) * 100.0, 2)  # 7.7
        self.assertEqual(exp_dy, 7.7)
        self.assertEqual(p["dy_trail_pct"], exp_dy)
        self.assertEqual(p["deposit_pct"], 15.6)
        self.assertEqual(p["spread_pp"], -7.9)
        self.assertEqual(p["deposit_chg_pp"], -0.3)  # 15.6 − 15.9
        self.assertFalse(p["positive_now"])
        # «Впервые в истории» теперь проверяется ПО РЯДУ, а не предполагается:
        # раньше поле обещало «когда-либо», а считало сегодняшний день.
        self.assertEqual(p["positive_days_before"], 0)
        self.assertIn("Вклады 15.6%", t["headline"])
        self.assertIn("дивидендов 7.7%", t["headline"])
        # Спред — число, а не вердикт (аудит 02.09.2026): «в пользу/не в пользу
        # акций» снято, вместо него — единственная фаза, где спред работает.
        self.assertIn("спред -7.9 п.п.; как сигнал работает только в фазе смягчения",
                      t["headline"])
        self.assertNotIn("в пользу", t["headline"])

    def test_dividends_окно_90_дней_гэп_и_реинвест(self):
        """Суммы выплат, гэп индекса и реинвест — из окна 90 дней, не всего календаря.

        Руками: в окне до 2026-11-15 только SBER и LKOH → 752 + 380 = 1132 млрд,
        реинвест 1132·0.5 = 566, гэп 1.302 + 0.62 = 1.922%. Прошедший GAZP (200 млрд)
        и далёкий TATN (100 млрд) НЕ в суммах: мутация «весь календарь» дала бы
        1232/2.122, «включая прошлое» — 1432/2.922. Гэп индекса — вес×доходность,
        а не доходность бумаги: 1.92%, не 9.3%.
        """
        self.seed_dividends()
        t = self.tile("dividends")
        self.vitals(t, "dividends")
        self.assertEqual(t["asof"], "2026-08-16")  # день чтения, не будущая отсечка
        p = t["payload"]
        self.assertEqual(p["sum_90d_bn"], 1132.0)
        self.assertEqual(p["reinvest_est_bn"], 566.0)
        self.assertEqual(p["index_drag_90d_pct"], 1.922)
        self.assertEqual(p["horizon_to"], "2026-11-15")
        self.assertEqual([r["ticker"] for r in p["upcoming"]],
                         ["SBER", "LKOH", "TATN"])
        self.assertIn("SBER 10.09", t["headline"])
        self.assertIn("(9.3%)", t["headline"])
        self.assertIn("≈1.92%", t["headline"])
        self.assertIn(f"выплат 1{NBSP}132 млрд", t["headline"])
        self.assertIn("реинвест ≈566 млрд", t["headline"])

    def test_cb_meeting_ключ_консенсус_и_дни(self):
        """Ключ и консенсус в процентах, дельта в б.п., дни до заседания — руками.

        Руками: 11.09 − 17.08 = 25 дн.; дельта (15 − 16)·100 = −100 б.п. (мутация
        ×100/÷100 дала бы −1 или −10000); спред RUSFAR 15.4 − 16.0 = −0.6 → «рынок
        закладывает снижение». Прошедшее заседание 24.07 старше недели, и его
        консенсус 18% НЕ должен просочиться ни в какое поле.
        """
        self.seed_cb()
        t = self.tile("cb_meeting")
        self.vitals(t, "cb_meeting")
        self.assertEqual(t["asof"], "2026-08-15")  # дата ЧИСЕЛ, не дата заседания
        p = t["payload"]
        self.assertEqual(p["next_meeting"], "2026-09-11")
        self.assertEqual(p["days_left"], 25)
        self.assertEqual(p["key_rate"], 16.0)
        self.assertEqual(p["consensus"], 15.0)
        self.assertEqual(p["consensus_delta_bp"], -100)
        self.assertEqual(p["rusfar3m"], 15.4)
        self.assertEqual(p["spread_pp"], -0.6)
        self.assertIsNone(p["last_meeting"])    # 24.07 несвежее — сюрприз не считается
        self.assertIsNone(p["last_consensus"])
        self.assertIn("До заседания 11.09 — 25 дн.", t["headline"])
        self.assertIn("Ключ 16.00%", t["headline"])
        self.assertIn("консенсус 15.00%", t["headline"])
        self.assertIn("снижение", p["priced_text"])
        self.assertIn("0.60", p["priced_text"])
        self.assertNotIn("cpi_weekly_4w", p)   # ИПЦ не сидирован — строки нет, не «н/д»

    def test_cb_meeting_ставка_на_день_заседания_и_возраст(self):
        """Поля для события о СОХРАНЕНИИ ставки (alerts._cb).

        Решение попадает в ряд ЦБ на следующий рабочий день, поэтому «сохранил» от
        «ещё не дошло» отличается только сравнением с тем, что было В ДЕНЬ
        заседания. Здесь ставку СНИЗИЛИ: 14,25 на 24.07 и 14,00 с 27.07 — правило
        обязано увидеть разницу и промолчать.

        мутация: не считать rate_at_last_meeting -> поле None, и правило о
        сохранении не сработает никогда (или сработает на изменении).
        """
        self.put("key_rate", {"2026-07-23": 14.25, "2026-07-24": 14.25,
                              "2026-07-27": 14.0, "2026-07-28": 14.0})
        self.put("cb_consensus", {"2026-07-24": 14.25})
        self.put("rusfar3m", {"2026-07-28": 13.9})
        t = self.tile("cb_meeting", now=datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC))
        p = t["payload"]
        self.assertEqual(p["last_meeting"], "2026-07-24")
        self.assertEqual(p["rate_at_last_meeting"], 14.25, "взята не ставка дня заседания")
        self.assertEqual(p["days_since_last_meeting"], 4)   # 28 − 24 = 4, считаем руками
        self.assertEqual(p["key_rate"], 14.0)

    def test_cb_meeting_сохранение_ставки_видно_по_полям(self):
        # Ставка не менялась: та же 14,00 и в день заседания, и сейчас.
        self.put("key_rate", {"2026-07-23": 14.0, "2026-07-24": 14.0,
                              "2026-07-27": 14.0, "2026-07-28": 14.0})
        self.put("cb_consensus", {"2026-07-24": 14.0})
        self.put("rusfar3m", {"2026-07-28": 13.9})
        p = self.tile("cb_meeting", now=datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC))["payload"]
        self.assertEqual(p["rate_at_last_meeting"], p["key_rate"])
        self.assertEqual(p["last_consensus"], 14.0)

    def test_cb_meeting_старое_заседание_полей_не_даёт(self):
        # Через неделю last_meeting гаснет — вместе с ним и поля решения.
        self.seed_cb()
        p = self.tile("cb_meeting")["payload"]       # NOW = 17.08, заседание 24.07
        self.assertIsNone(p["last_meeting"])
        self.assertIsNone(p["rate_at_last_meeting"])
        self.assertIsNone(p["days_since_last_meeting"])

    def test_cb_meeting_строка_недельной_инфляции(self):
        """Недельный ИПЦ снят с витрины отдельным тайлом (аудит 02.09.2026) и живёт
        одной строкой здесь, где читается — перед заседанием. Руками: последние
        четыре недели +0.10, +0.05, +0.15, +0.10 → сумма +0.40, последняя 11.08."""
        self.seed_cb()
        self.seed_cpi()
        t = self.tile("cb_meeting")
        self.vitals(t, "cb_meeting")
        p = t["payload"]
        self.assertEqual(p["cpi_weekly_4w"],
                         "недельная инфляция за 4 нед.: +0.10%, +0.05%, +0.15%, +0.10% "
                         "(сумма +0.40%, последняя неделя 11.08)")
        self.assertEqual(p["cpi_weekly_prints"][-1], ["2026-08-11", 0.1])
        self.assertEqual(len(p["cpi_weekly_prints"]), 4)

    def test_expectations_четыре_спреда_и_заголовок(self):
        """Цена ожиданий: год ОФЗ − ключ, Δ21, RUSFAR − ключ, полгода − ключ — все в п.п.

        Руками: 13.75 − 14.0 = −0.25 (в цене 0.25 п.п. смягчения); 21 точку назад
        13.65 − 14.0 = −0.35 → Δ21 = +0.10; RUSFAR 13.9 − 14.0 = −0.10; полгода
        13.5 − 14.0 = −0.50. Порог репрайсинга +0.25 не пробит. Мутация «последняя
        ставка вместо действовавшей» здесь невидима (ставка одна), зато потерянный
        знак спреда напечатал бы «ужесточения».
        """
        self.seed_expectations()
        t = self.tile("expectations")
        self.vitals(t, "expectations")
        self.assertEqual(t["asof"], "2026-08-14")
        p = t["payload"]
        self.assertEqual(p["y1_pct"], 13.75)
        self.assertEqual(p["key_rate_pct"], 14.0)
        self.assertEqual(p["spread_y1_key_pp"], -0.25)
        self.assertEqual(p["chg_21d_pp"], 0.1)
        self.assertEqual(p["spread_rusfar_key_pp"], -0.1)
        self.assertEqual(p["spread_y05_key_pp"], -0.5)
        self.assertEqual(p["y05_pct"], 13.5)
        self.assertEqual(p["repricing_threshold_pp"], 0.25)
        self.assertFalse(p["repricing"])
        self.assertEqual(len(p["series"]), 30)
        self.assertEqual(p["series"][-1], ["2026-08-14", -0.25])
        self.assertEqual(t["headline"],
                         "Год ОФЗ 13.75% против ключа 14.00%: в цене 0.25 п.п. смягчения; "
                         "за 21 день спред +0.10 п.п.")
        self.assertIn("тень", t["note"])
        self.assertIn("направление не предсказывает", t["note"])

    def test_expectations_ставка_берётся_действовавшая_в_день_среза(self):
        """Ключ поднят 05.08 с 14 до 16: точки кривой до 05.08 сравниваются с 14, после —
        с 16. Мутация «последняя ставка на всё» дала бы Δ21 = 13.75 − 16 − (13.65 − 16)
        = +0.10 вместо честных −1.90 (шаг ЦБ и есть «репрайсинг» этих трёх недель)."""
        self.seed_expectations()
        self.put("key_rate", {"2026-07-01": 14.0, "2026-08-05": 16.0})
        t = self.tile("expectations")
        p = t["payload"]
        self.assertEqual(p["key_rate_pct"], 16.0)
        self.assertEqual(p["spread_y1_key_pp"], -2.25)
        self.assertEqual(p["chg_21d_pp"], -1.9)
        self.assertIn("в цене 2.25 п.п. смягчения", t["headline"])

    def test_expectations_снижение_ключа_не_репрайсинг(self):
        """Снижение ставки, которое рынок уже заложил, двигает СПРЕД, а не кривую.

        Ключ снижен 05.08 с 14 до 13 при неподвижном годе ОФЗ 13.65: спред за 21 день
        −0.35 → +0.65, то есть +1.00 — ровно шаг ЦБ. До 03.10.2026 бит стоял на
        спреде и называл это «репрайсингом ожиданий» (аудит 03.10.2026, §2.4).
        Мутация: вернуть бит на chg_21d_pp — repricing станет True.
        """
        self.seed_expectations()
        span = days("2026-08-14", 30)
        self.put("zcyc_y1", {d: 13.65 for d in span})
        self.put("key_rate", {"2026-07-01": 14.0, "2026-08-05": 13.0})
        t = self.tile("expectations")
        p = t["payload"]
        self.assertEqual(p["chg_21d_pp"], 1.0)
        self.assertEqual(p["y1_chg_21d_pp"], 0.0)
        self.assertEqual(p["key_chg_21d_pp"], -1.0)
        self.assertFalse(p["repricing"])
        self.assertIn("шаг ключа -1.00, год ОФЗ +0.00", t["headline"])
        self.assertNotIn("репрайсинг", t["headline"])

    def test_expectations_репрайсинг_подписывается(self):
        self.seed_expectations()
        span = days("2026-08-14", 30)
        self.put("zcyc_y1", {d: (14.2 if d in span[-3:] else 13.65) for d in span})
        t = self.tile("expectations")
        self.assertEqual(t["payload"]["chg_21d_pp"], 0.55)
        self.assertTrue(t["payload"]["repricing"])
        self.assertIn("репрайсинг ожиданий (тень)", t["headline"])
        self.assertIn("в цене 0.20 п.п. ужесточения", t["headline"])

    def test_expectations_без_ключа_нет_данных(self):
        span = days("2026-08-14", 5)
        self.put("zcyc_y1", {d: 13.65 for d in span})
        t = self.tile("expectations")
        self.assertEqual(t["status"], "missing")
        self.assertIn("Источники", t["note"])

    def test_cpi_weekly_процент_недели_и_saar(self):
        """Недельные +0.10% остаются +0.10%, годовая оценка — по честной формуле.

        Руками: среднее последних 4 недель (0.10+0.05+0.15+0.10)/4 = 0.10%;
        SAAR = ((1.001)^(365/7) − 1)·100 = 5.35 → 5.3%. Мутация ×100 напечатала бы
        «+10.00%» — инфляцию, которой нет.
        """
        self.seed_cpi()
        # Тайл снят с витрины (аудит 02.09.2026), функция осталась: data-path
        # по-прежнему проверяется, а в BUILDERS его быть не должно.
        self.assertNotIn("cpi_weekly", dict(self.monitors.BUILDERS))
        t = self.tile("cpi_weekly")
        self.vitals(t, "cpi_weekly")
        self.assertEqual(t["asof"], "2026-08-11")
        p = t["payload"]
        exp_annual = round(((1.0 + 0.10 / 100.0) ** (365.0 / 7.0) - 1.0) * 100.0, 1)
        self.assertEqual(exp_annual, 5.3)
        self.assertEqual(p["last_pct"], 0.1)
        self.assertEqual(p["annualized_last_pct"], exp_annual)
        self.assertEqual(p["annualized_4w_pct"], exp_annual)
        self.assertIn("Неделя 11.08: +0.10%", t["headline"])
        self.assertIn("5.3%", t["headline"])

    def test_ofz_объёмы_и_bid_to_cover(self):
        """Размещение/спрос в млрд и bid-to-cover как частное спроса к размещению.

        Руками: 87.5/43.2 = 2.025 → 2.03. Перестановка спроса и размещения дала бы
        0.49 — ловится точным числом. btc ≥ 1 и размещение > 0 → не провал.
        """
        self.seed_ofz()
        t = self.tile("ofz_auctions")
        self.vitals(t, "ofz_auctions")
        self.assertEqual(t["asof"], "2026-08-13")
        p = t["payload"]
        self.assertEqual(p["placed_bn"], 43.2)
        self.assertEqual(p["demand_bn"], 87.5)
        self.assertEqual(p["bid_to_cover"], 2.03)
        self.assertEqual(p["premium_bp"], 5)
        self.assertFalse(p["failed"])
        self.assertIn("Аукцион 13.08", t["headline"])
        # Тексты тайла с 20.11.2026 сразу в русской записи (запятая, «−»).
        self.assertIn("размещено 43,2 млрд", t["headline"])
        self.assertIn("при спросе 87,5 млрд", t["headline"])
        self.assertIn("bid-to-cover 2,03", t["headline"])
        self.assertNotIn("провален", t["headline"])

    def test_ofz_итог_минфина_назван_и_доразмещение_видно(self):
        """Аукцион 11.11.2026: Минфин 88,874 млрд, из них ДРПА 23,184; доска 62,70.

        мутация: показывать биржевое число без оговорки -> «размещено 62,7 млрд» при
        официальном итоге 88,9 (занижение на 29 %).
        """
        self.put("ofz_auctions", {"2026-11-11": 88.874},
                 {"last": {"date": "2026-11-11", "issue": "SU26235RMFS0, SU26250RMFS9",
                           "placed_bn": 88.874, "placed_board_bn": 62.7021,
                           "placed_official_bn": 88.874, "auction_bn": 65.69,
                           "drpa_bn": 23.184, "source": "minfin_tg", "failed": False,
                           "official_url": "https://t.me/minfin/9472", "demand_bn": None},
                  "method": "minfin_tg"})
        t = self.tile("ofz_auctions", now=datetime(2026, 11, 12, 9, 0, tzinfo=UTC))
        p = t["payload"]
        self.assertEqual(p["placed_bn"], 88.9)
        self.assertEqual(p["placed_source"], "minfin")
        self.assertEqual(p["placed_board_bn"], 62.7)
        self.assertEqual(p["drpa_bn"], 23.2)
        self.assertIn("Аукцион 11.11: размещено 88,9 млрд, из них доразмещение после "
                      "аукциона 23,2 млрд", t["headline"])
        self.assertIn("итог Минфина", t["note"])
        self.assertIn("биржевая доска показывает 62,7 млрд", t["note"])
        self.assertNotIn("без доразмещения", t["headline"])

    def test_ofz_биржевое_число_названо_биржевым(self):
        # Пост Минфина не найден или не разобран: число с доски, и тайл это говорит.
        # Запись старого формата (без поля source) — тоже биржевая.
        # мутация: молчать об источнике -> биржевое число читается как итог дня.
        self.put("ofz_auctions", {"2026-11-18": 70.6442},
                 {"last": {"date": "2026-11-18", "placed_bn": 70.6442, "failed": False,
                           "demand_bn": None}, "method": "moex_pact"})
        t = self.tile("ofz_auctions", now=datetime(2026, 11, 19, 9, 0, tzinfo=UTC))
        self.assertIn("размещено 70,6 млрд по биржевой доске, без доразмещения",
                      t["headline"])
        self.assertIn("итог Минфина за этот день не получен", t["note"])
        self.assertEqual(t["payload"]["placed_source"], "board")
        self.assertIsNone(t["payload"]["drpa_bn"])

    def test_ofz_нулевое_размещение_названо_словами(self):
        """Несостоявшийся аукцион — словами, а не «размещено 0.0 млрд» крупным кеглем."""
        self.put("ofz_auctions", {"2026-08-12": 0.0},
                 {"last": {"date": "2026-08-12", "placed_bn": 0.0}})
        t = self.tile("ofz_auctions")
        self.vitals(t, "ofz_auctions")
        self.assertTrue(t["payload"]["failed"])
        self.assertIn("не состоялся", t["headline"])
        self.assertIn("размещения не было", t["headline"])
        self.assertNotIn("0,0 млрд", t["headline"])

    def test_polymarket_доля_нормализуется_в_проценты_один_раз(self):
        """Ряд приходит долями (0.35): в заголовке 35%, а не 0% и не 3500%.

        Руками: 0.35·100 = 35%; изменение за 7 точек (0.35 − 0.29)·100 = +6.0 п.п.
        Ровно класс дефекта «7800-й перцентиль»: масштаб применяется один раз.
        """
        self.seed_polymarket()
        t = self.tile("polymarket")
        self.vitals(t, "polymarket")
        self.assertEqual(t["asof"], "2026-08-16")
        p = t["payload"]
        self.assertEqual(p["prob_pct"], 35.0)
        self.assertEqual(p["chg_7d_pp"], 6.0)
        self.assertIsNone(p["chg_30d_pp"])  # истории меньше 30 точек — честное «нет»
        self.assertIn("35%", t["headline"])
        # Горизонт — часть числа: «24% к декабрю 2026» и «2% к августу» — это один
        # рынок в один день, и без даты они читаются как противоречие.
        # Дата целиком: рынок разрешается конкретным днём, а месяц прописью
        # потребовал бы дательного падежа — «к декабрь 2026» уже уезжало в прод.
        self.assertIn("31.12.2026", t["headline"])
        self.assertNotIn("декабрь", t["headline"])
        self.assertEqual(p["horizon_days"], 133)
        self.assertIn("133 дн.", t["note"])

    def test_polymarket_без_срока_не_выдумывает_горизонт(self):
        # Ни даты расчёта, ни даты из вопроса: горизонт неизвестен, и выдумывать
        # его неоткуда. Если известна хоть одна — считаем по ней, это лучше «н/д».
        self.seed_polymarket({"end_date": None, "resolves_on": None, "horizon_days": None})
        t = self.tile("polymarket")
        self.assertIsNone(t["payload"]["horizon_days"])
        self.assertNotIn(" к ", t["headline"])
        self.assertNotIn("дн.:", t["note"])

    def test_polymarket_горизонт_считается_если_фетчер_его_не_положил(self):
        # Старые ряды в сторе лежат без horizon_days: считаем из даты РАСЧЁТА и
        # asof, а не показываем «нет данных» там, где срок известен.
        self.seed_polymarket({"horizon_days": None})
        t = self.tile("polymarket")
        self.assertEqual(t["payload"]["horizon_days"], 138)  # 16.08 -> 01.01

    def test_polymarket_заголовок_не_спорит_с_вопросом(self):
        # мутация: брать end_date вместо resolves_on -> «до 01.01.2027» рядом с
        # вопросом «by December 31, 2026» на одной плитке.
        self.seed_polymarket()
        t = self.tile("polymarket")
        self.assertIn("31.12.2026", t["headline"])
        self.assertNotIn("01.01.2027", t["headline"])
        self.assertIn("+6,0 п.п. за неделю", t["headline"])

    def test_futoi_нетто_z_и_держатели_не_путаются(self):
        """Нетто-позиция, z-скор и ЧИСЛО ЛИЦ — три разные величины в одном заголовке.

        Прод-дефект «лонги вместо числа лиц»: контракты (60 000) печатались на месте
        держателей (12 500). Руками: доля = нетто/брутто, брутто = 60000 − (−40000) =
        100000; история 44×0.2 и последняя 0.3 → z по выборочной сигме ≈ +6.56.
        z — БЕЗРАЗМЕРНЫЙ: мутация «z в проценты» дала бы 656.
        """
        self.seed_futoi()
        t = self.tile("futoi")
        self.vitals(t, "futoi")
        self.assertEqual(t["asof"], "2026-08-15")
        p = t["payload"]
        ratio = [0.2] * 44 + [0.3]
        m = sum(ratio) / len(ratio)
        sd = math.sqrt(sum((v - m) ** 2 for v in ratio) / (len(ratio) - 1))
        exp_z = round((0.3 - m) / sd, 2)
        self.assertEqual(exp_z, 6.56)
        self.assertEqual(p["net"], 30000.0)
        self.assertEqual(p["net_share"], 0.3)
        self.assertEqual(p["z120"], exp_z)
        self.assertEqual(p["long"], 60000.0)
        self.assertEqual(p["short"], -40000.0)
        self.assertEqual(p["holders_long"], 12500.0)
        self.assertEqual(p["holders_short"], 8300.0)
        self.assertIn(f"нетто-лонге 30{NBSP}000", t["headline"])
        self.assertIn("+6.56", t["headline"])
        self.assertIn("выше своей 120-дневной нормы", t["headline"])
        self.assertIn(f"держателей лонга 12{NBSP}500, шорта 8{NBSP}300", t["headline"])
        # Вердикт снят (аудит 02.09.2026): знак сигнала сменился в 2024–2026,
        # заголовок — только факты, нота говорит, что в решение он не входит.
        self.assertNotIn("контрариан", t["headline"])
        self.assertIn("в решение не входит", t["note"].lower())
        self.assertIn("2024–2026", t["note"])

    def test_rvi_уровень_и_перцентиль_без_второго_умножения(self):
        """Перцентиль средним рангом, руками: 25 значений, ниже 32.5 ровно 13 →
        (13 + (1+1)/2)/25·100 = 56. Дефект-прототип «7800-й перцентиль» здесь дал бы
        «5600-й» — проверяется и позитивно, и негативно.
        """
        self.seed_rvi()
        t = self.tile("rvi")
        self.vitals(t, "rvi")
        self.assertEqual(t["asof"], "2026-08-14")
        p = t["payload"]
        self.assertEqual(p["rvi"], 32.5)
        self.assertEqual(p["pct_3y"], 56.0)
        self.assertEqual(p["chg_5d"], -6.5)   # 32.5 − 39 (пять точек назад)
        self.assertEqual(p["peak_5d"], 43.0)
        self.assertFalse(p["peak_reversal"])  # пик 43 не выше 50
        self.assertIn("RVI 32.5", t["headline"])
        self.assertIn("(56-й перцентиль", t["headline"])
        self.assertNotIn(f"5{NBSP}600", t["headline"])  # «5600-й перцентиль» с ×100

    def test_rvi_разворот_с_пика_50(self):
        """Единственный живой остаток RVI: пик > 50 и откат глубже 10% помечаются."""
        span = days("2026-08-14", 22)
        vals = [30.0] * 20 + [60.0, 52.0]  # пик 60 > 50, 52 < 60·0.9
        self.put("rvi", dict(zip(span, vals)))
        t = self.tile("rvi")
        self.vitals(t, "rvi")
        self.assertTrue(t["payload"]["peak_reversal"])
        self.assertEqual(t["payload"]["peak_5d"], 60.0)
        self.assertIn("разворот с пика >50", t["headline"])

    def test_rub_barrel_среднемесячный_курс_и_гэп(self):
        """Бочка по СРЕДНЕМЕСЯЧНОМУ курсу месяца Urals, гэп к бюджетной цене руками.

        Руками: (79 + 81)/2 = 80 → 60·80 = 4800 ₽; гэп (4800/5440 − 1)·100 = −11.76 →
        −11.8%. Прод-дефект «последняя точка месяца вместо среднего» дал бы 4860,
        «последний курс вообще» — 5100: оба ловятся точным числом. Прокси руками:
        65·85·0.88 = 4862 ₽ (дисконт — фолбэк, ряда brent нет).
        """
        self.seed_rub_barrel()
        t = self.tile("rub_barrel")
        self.vitals(t, "rub_barrel")
        self.assertEqual(t["asof"], "2026-06-30")
        p = t["payload"]
        self.assertEqual(p["tax_barrel_rub"], 4800.0)
        self.assertEqual(p["urals_usd"], 60.0)
        self.assertEqual(p["usd"], 80.0)
        self.assertEqual(p["usd_basis"], "среднемесячный")
        self.assertEqual(p["gap_pct"], -11.8)
        self.assertEqual(p["budget_barrel_rub"], 5440.0)
        self.assertEqual(p["proxy_rub"], 4862.0)
        self.assertTrue(p["discount_is_fallback"])
        self.assertEqual(p["discount_k"], 0.88)
        self.assertIn(f"бочка 4{NBSP}800 ₽", t["headline"])
        self.assertIn("на 12% ниже", t["headline"])
        self.assertIn(f"5{NBSP}440 ₽", t["headline"])
        self.assertIn(f"интрадей-оценка 4{NBSP}862 ₽", t["headline"])
        # База названа БАЗОЙ ПРАВИЛА (отсечка × курс бюджета), а не «ориентиром
        # бюджета»: выше неё Минфин покупает валюту, бюджет получает саму базу.
        self.assertIn("базы бюджетного правила", t["headline"])
        self.assertEqual(p["budget_year"], 2026)
        self.assertEqual(p["budget_cutoff_usd"], 59.0)
        self.assertEqual(p["budget_fx"], 92.2)
        self.assertEqual(p["proxy_budget_barrel_rub"], 5440.0)

    def test_sep_node_дни_до_окна(self):
        """Календарный тайл: 17.08 → до 10.09 ровно 24 дня (руками: 14 + 10)."""
        t = self.tile("sep_node")
        self.vitals(t, "sep_node")
        self.assertEqual(t["asof"], TODAY)
        p = t["payload"]
        self.assertFalse(p["active"])
        self.assertEqual(p["days_to_start"], 24)
        self.assertIsNone(p["days_left"])
        self.assertEqual(p["window"], "10.09–05.10")
        self.assertIn("До окна бюджетного узла 24 дн.", t["headline"])

    def test_sep_node_окно_активно(self):
        """Внутри окна: 15.09 → до 05.10 осталось 20 дней, заголовок «активно»."""
        inside = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
        t = self.tile("sep_node", now=inside)
        self.vitals(t, "sep_node", today="2026-09-15")
        p = t["payload"]
        self.assertTrue(p["active"])
        self.assertEqual(p["days_left"], 20)
        self.assertIn("Окно узла активно до 05.10", t["headline"])

    def test_breadth_доля_бумаг_в_процентах(self):
        """Доля приходит долей (0.42): в заголовке 42% бумаг, не 0% и не 4200%.

        Руками: изменение за 21 точку (0.42 − 0.30)·100 = +12 п.п.; перцентиль
        (24 + 1)/25·100 = 100 — последняя точка выше всей короткой истории.
        """
        self.seed_breadth()
        t = self.tile("breadth")
        self.vitals(t, "breadth")
        self.assertEqual(t["asof"], "2026-08-14")
        p = t["payload"]
        self.assertEqual(p["pct_above_ma200"], 42.0)
        self.assertEqual(p["chg_21d_pp"], 12.0)
        self.assertEqual(p["pct_1y"], 100.0)
        self.assertIn("42% бумаг", t["headline"])
        self.assertIn("+12 п.п. за месяц", t["headline"])
        self.assertNotIn(f"4{NBSP}200", t["headline"])  # двойной масштаб ×100×100
        # Знак ширины зависит от эры — нота обязана назвать обе (аудит 02.09.2026).
        self.assertIn("подтверждение до 2022", t["note"])
        self.assertIn("контрариан в 2025–2026", t["note"])

    def test_mcxsm_относительная_сила_со_знаком(self):
        """Отношение малых капп к индексу: рост на 10% за 63 дня — «лучше», не «хуже».

        Руками: отношение 264/3000 = 0.088 против 240/3000 = 0.08 → +10.0%.
        Потерянный знак перевернул бы вердикт — заголовок проверяется на «лучше».
        """
        self.seed_mcxsm()
        t = self.tile("mcxsm")
        self.vitals(t, "mcxsm")
        self.assertEqual(t["asof"], "2026-08-14")
        p = t["payload"]
        self.assertEqual(p["ratio"], 0.088)
        self.assertEqual(p["rs_21d_pct"], 10.0)
        self.assertEqual(p["rs_63d_pct"], 10.0)
        self.assertIsNone(p["rs_252d_pct"])  # общей истории меньше года
        self.assertIn("Малые каппы лучше индекса на 10.0% за 63 дня", t["headline"])

    def test_hy_spread_спред_и_перцентиль(self):
        """Спред ВДО к базе в п.п. и его перцентиль — из одних и тех же пар.

        Руками: 24.5 − 14.2 = 10.3 п.п.; IG 17.0 − 14.2 = 2.8; изменение за 21 точку
        10.3 − 8.0 = 2.3; перцентиль (29 + 1)/30·100 = 100. Перестановка «доходность
        на месте спреда» дала бы 24.5 п.п. — ловится точным числом.
        """
        self.seed_hy()
        t = self.tile("hy_spread")
        self.vitals(t, "hy_spread")
        self.assertEqual(t["asof"], "2026-08-14")
        p = t["payload"]
        self.assertEqual(p["hy_yield"], 24.5)
        self.assertEqual(p["base_label"], "ОФЗ 2Y")
        self.assertEqual(p["base_yield"], 14.2)
        self.assertEqual(p["spread_pp"], 10.3)
        self.assertEqual(p["ig_spread_pp"], 2.8)
        self.assertEqual(p["chg_21d_pp"], 2.3)
        self.assertEqual(p["pct_1y"], 100.0)
        # Тексты тайла с 20.11.2026 сразу в русской записи (запятая, «−»).
        self.assertIn("ВДО 24,5%", t["headline"])
        self.assertIn("спред к ОФЗ 2Y 10,3 п.п.", t["headline"])
        self.assertIn("(100-й перцентиль", t["headline"])
        self.assertFalse(p["window_gap"])

    def test_retail_доли_и_народный_портфель(self):
        """Активные счета против открытых и концентрация по ЭМИТЕНТУ, не по строке.

        Руками: активная доля 3.0/41.9·100 = 7.16 → 7.2%; Сбербанк = 31.8 + 7.3 =
        39.1% (обычка + префы — одна ставка на эмитента). Мутация «первая строка
        как есть» дала бы 31.8 и занизила концентрацию на треть.
        """
        self.seed_retail()
        t = self.tile("retail")
        self.vitals(t, "retail")
        self.assertEqual(t["asof"], "2026-07-31")
        p = t["payload"]
        self.assertEqual(p["share_equity_pct"], 67.0)
        self.assertEqual(p["active_mln"], 3.0)
        self.assertEqual(p["clients_total_mln"], 41.9)
        self.assertEqual(p["active_share_pct"], 7.2)
        self.assertEqual(p["top_name"], "Сбербанк")
        self.assertEqual(p["top_share_pct"], 39.1)
        self.assertIn("67% оборота акций", t["headline"])
        self.assertIn("активны 3.0 из 41.9 млн счетов (7%)", t["headline"])
        self.assertIn("доля Сбербанк 39%", t["headline"])



class TestБюджет2027(TileCase):
    """Правки по бюджетному пакету 24.09.2026: база правила по годам, приор узла
    по данным, ротация только с подтверждением потоком физлиц."""

    # Закрытия IMOEX последних торговых дней августа и сентября — из ISS, 24.09.2026.
    IMOEX_ENDS = {
        "2021-08-31": 3918.96, "2021-09-30": 4103.52,
        "2022-08-31": 2400.08, "2022-09-30": 1957.31,
        "2023-08-31": 3227.99, "2023-09-29": 3133.26,
        "2024-08-30": 2650.32, "2024-09-30": 2857.56,
        "2025-08-29": 2899.38, "2025-09-30": 2684.60,
        "2026-08-31": 2178.87, "2026-09-23": 2312.27,
    }

    def _ret(self, a, b):
        return math.log(self.IMOEX_ENDS[b] / self.IMOEX_ENDS[a]) * 100.0

    def test_база_правила_2026_и_2027(self):
        """5 440 = 59 × 92,2 (закон 2026); 4 370 = 50 × 87,4 (проект 2027) — руками."""
        b26 = self.monitors._budget_base(2026)
        b27 = self.monitors._budget_base(2027)
        self.assertEqual(b26["barrel_rub"], 5440.0)
        self.assertEqual(b27["barrel_rub"], 4370.0)
        self.assertEqual(b27["cutoff_usd"], 50.0, "база 2027 — по отсечке, а не по прогнозу 53 $")
        self.assertEqual(b27["status"], "проект")
        self.assertIsNone(self.monitors._budget_base(2031))

    def test_rub_barrel_база_года_месяца_и_текущего_года(self):
        """В январе цена ещё декабрьская — её база 2026 года; интрадей-оценка про
        сегодня — её база уже 2027 года. Руками: 60·80 = 4 800; (4800/5440 − 1)·100 =
        −11,8; прокси 65·85·0,88 = 4 862; (4862/4370 − 1)·100 = +11,3."""
        self.put("urals_tax", {"2026-11-30": 58.0, "2026-12-31": 60.0})
        self.put("usd_cbr", {"2026-12-10": 79.0, "2026-12-20": 81.0,
                             "2027-01-08": 84.0, "2027-01-09": 85.0})
        self.put("brent_moex", {"2027-01-09": 65.0})
        now = datetime(2027, 1, 10, 12, 0, 0, tzinfo=UTC)
        t = self.tile("rub_barrel", now=now)
        p = t["payload"]
        self.assertEqual(p["budget_year"], 2026)
        self.assertEqual(p["budget_barrel_rub"], 5440.0)
        self.assertEqual(p["gap_pct"], -11.8)
        self.assertEqual(p["proxy_budget_barrel_rub"], 4370.0)
        self.assertEqual(p["proxy_gap_pct"], 11.3)
        self.assertIn("базы бюджетного правила", t["headline"])
        self.assertIn("Следующая база: 50 $ × 87,4 ₽", t["note"])

    def test_rub_barrel_без_параметров_года_не_выдумывает_разрыв(self):
        self.put("urals_tax", {"2031-05-31": 60.0})
        self.put("usd_cbr", {"2031-05-10": 90.0})
        t = self.tile("rub_barrel", now=datetime(2031, 6, 10, 12, 0, 0, tzinfo=UTC))
        self.assertIsNone(t["payload"]["gap_pct"])
        self.assertIsNone(t["payload"]["budget_barrel_rub"])
        self.assertIn("параметров бюджета на 2031 год нет", t["headline"])

    def test_sep_node_приор_считается_по_данным(self):
        """Сентябри 2022–2025 воспроизводят числа валидации: в среднем −5,3% за
        2022–24 и −7,7% за 2025; незакрытый сентябрь-2026 в среднее не идёт."""
        self.put("imoex", self.IMOEX_ENDS)
        now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
        t = self.tile("sep_node", now=now)
        self.vitals(t, "sep_node", today="2026-09-24")
        p = t["payload"]
        r = {2022: self._ret("2022-08-31", "2022-09-30"),
             2023: self._ret("2023-08-31", "2023-09-29"),
             2024: self._ret("2024-08-30", "2024-09-30"),
             2025: self._ret("2025-08-29", "2025-09-30")}
        self.assertEqual(p["septembers"], [[y, round(v, 2)] for y, v in r.items()])
        self.assertAlmostEqual((r[2022] + r[2023] + r[2024]) / 3, -5.28, places=2)
        self.assertAlmostEqual(r[2025], -7.70, places=2)
        self.assertEqual(p["sept_n"], 4, "2021 год раньше порога — в приор не входит")
        self.assertEqual(p["sept_mean_pct"], round(sum(r.values()) / 4, 2))
        cur = p["sept_current"]
        self.assertEqual(cur["year"], 2026)
        self.assertFalse(cur["closed"])
        self.assertEqual(cur["ret_pct"], round(self._ret("2026-08-31", "2026-09-23"), 2))
        self.assertIn("Окно узла активно до 05.10", t["headline"])
        self.assertIn("сентябрь 2026 пока +5.9%", t["headline"])

    def test_sep_node_сентябрь_закрывается_первого_октября(self):
        self.put("imoex", dict(self.IMOEX_ENDS, **{"2026-09-30": 2300.0}))
        t = self.tile("sep_node", now=datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC))
        p = t["payload"]
        self.assertEqual(p["sept_n"], 5)
        self.assertIsNone(p["sept_current"])
        self.assertEqual(p["septembers"][-1][0], 2026)

    def _lqdt_rotation(self):
        span = days("2026-08-15", 80)
        vals = [1300.0] * 70 + [1250.0, 1220.0, 1200.0, 1180.0, 1170.0, 1160.0, 1150.0,
                                1140.0, 1135.0, 1130.0]
        self.put("lqdt_aum", dict(zip(span, vals)))

    def test_lqdt_ротацию_подтверждает_поток_физлиц(self):
        """dd руками: (1130/1300 − 1)·100 = −13,1 — порог −10 пробит. Приток физлиц
        в акции +21,7 млрд за июнь — «похоже на ротацию»."""
        self._lqdt_rotation()
        self.put("orfr_flows_fiz", {"2026-05-31": 7.0, "2026-06-30": 21.7})
        t = self.tile("lqdt")
        p = t["payload"]
        self.assertTrue(p["rotation_started"])
        self.assertEqual(p["retail_equity_flow_bln"], 21.7)
        self.assertIn("отток из фонда", t["headline"])
        self.assertIn("похоже на ротацию", t["headline"])
        self.assertIn("внутри ПИФ", t["note"])

    def test_lqdt_отток_без_покупок_акций_не_ротация(self):
        self._lqdt_rotation()
        self.put("orfr_flows_fiz", {"2026-06-30": -12.4})
        t = self.tile("lqdt")
        self.assertIn("деньги ушли не в акции", t["headline"])
        self.assertNotIn("похоже на ротацию", t["headline"])


class TestВсеТайлыРазом(TileCase):
    """Интеграция: полный синтетический стор → build_monitors → ни одного «error»."""

    def test_все_16_тайлов_ок_на_полном_сторе(self):
        """На сторе, где есть ВСЁ, каждый из 16 тайлов обязан быть ok с датой не из
        будущего и без «н/д» в заголовке: «н/д» на полном сторе значит, что тайл
        потерял своё же число — ровно так выглядели все четыре прод-дефекта.
        """
        # seed_expectations раньше seed_cb: оба кладут key_rate, и для тайла
        # заседания важнее его собственная ставка (16 %), для ожиданий — любая.
        for seed in (self.seed_expectations, self.seed_orfr, self.seed_lqdt,
                     self.seed_market, self.seed_deposit, self.seed_dividends,
                     self.seed_cb, self.seed_cpi, self.seed_ofz, self.seed_polymarket,
                     self.seed_futoi, self.seed_rvi, self.seed_rub_barrel,
                     self.seed_breadth, self.seed_mcxsm, self.seed_hy,
                     self.seed_retail):
            seed()
        tiles = self.monitors.build_monitors(self.store, NOW)
        ids = [t["id"] for t in tiles]
        self.assertEqual(ids, [tid for tid, _ in self.monitors.BUILDERS])
        self.assertEqual(len(tiles), 16)
        # Состав после аудита 02.09.2026: cpi_weekly снят, expectations добавлен.
        expected_ids = {"orfr", "lqdt", "deposit_spread", "dividends", "cb_meeting",
                        "expectations", "ofz_auctions", "polymarket", "futoi", "rvi",
                        "rub_barrel", "sep_node", "breadth", "mcxsm", "hy_spread",
                        "retail"}
        self.assertEqual(set(ids), expected_ids)
        # Порядок — порядок на витрине: то, что читается перед решением по ставке,
        # первым; описательные тайлы — последними.
        self.assertEqual(ids[:2], ["expectations", "cb_meeting"])
        self.assertEqual(ids[-2:], ["mcxsm", "retail"])
        self.assertEqual(ids[2:14], ["orfr", "futoi", "hy_spread", "rub_barrel",
                                     "deposit_spread", "dividends", "ofz_auctions", "rvi",
                                     "breadth", "polymarket", "sep_node", "lqdt"])
        for t in tiles:
            with self.subTest(tile=t["id"]):
                self.vitals(t, t["id"])
                self.assertNotEqual(t["headline"], "тайл не собрался")
                self.assertNotIn("н/д", t["headline"],
                                 f"{t['id']}: «н/д» в заголовке на полном сторе — "
                                 f"тайл потерял собственное число")



class TestHyEstimateIsLabelled(TileCase):
    """Тайл называет, что за число показано: медиана состава или число биржи.

    Аудит 20.11.2026. Решает ПЕРЕЧЕНЬ оценённых дней (meta.estimated_days), а не
    поле meta.method: оно описывает последний прогон и переживает его. Вечером
    19.11.2026 подпись «посчитано из состава, покрытие 91 %» стояла над биржевым
    числом 95,99 % — поля остались в meta от дневного прогона.
    """

    def build(self, meta_extra, hy=None):
        self.put("rucbhycp_yield", hy or {"2026-08-17": 27.0, "2026-08-18": 29.2},
                 meta=dict({"unit": "pct"}, **meta_extra))
        self.put("rucbcpns_yield", {"2026-08-17": 15.8, "2026-08-18": 15.9},
                 meta={"unit": "pct"})
        self.put("zcyc_y1", {"2026-08-17": 14.1, "2026-08-18": 14.2})
        self.put("zcyc_y2", {"2026-08-17": 14.7, "2026-08-18": 14.8})
        return self.tile("hy_spread")

    def test_медиана_названа_в_заголовке_и_подписи(self):
        tile = self.build({"method": "constituents", "estimate_asof": "2026-08-18",
                           "estimate_cover_pct": 99.8, "exchange_last": ["2026-08-17", 85.65],
                           "estimated_days": ["2026-08-17", "2026-08-18"]})
        # мутация: не называть метод -> читатель сверяет 29,2 % с 85,65 % на сайте
        # биржи и не понимает, почему числа разные.
        self.assertIn("ВДО 29,2% (медиана доходности бумаг индекса)", tile["headline"])
        self.assertIn("медиана доходности бумаг индекса", tile["note"])
        self.assertIn("котировки есть у 99,8% веса", tile["note"])
        self.assertEqual(tile["payload"]["hy_method"], "median")
        self.assertEqual(tile["payload"]["method"], "constituents")
        self.assertEqual(tile["payload"]["exchange_yield"], 85.65)

    def test_поля_прошлой_оценки_не_делают_биржевое_число_оценкой(self):
        """(в) Вечер 19.11.2026: в meta — поля дневной оценки, в ряду — число биржи.

        мутация: решать по meta.method -> подпись «медиана… котировки есть у 91 %»
        над биржевым числом.
        """
        tile = self.build({"method": "constituents", "estimate_asof": "2026-08-18",
                           "estimate_cover_pct": 91.0,
                           "estimate_dropped": {"count": 14, "weight_pct": 8.68},
                           "estimated_days": ["2026-08-17"]})
        self.assertNotIn("медиана", tile["headline"])
        self.assertNotIn("медиана", tile["note"])
        self.assertNotIn("91", tile["note"])
        self.assertEqual(tile["payload"]["hy_method"], "exchange")
        self.assertNotIn("method", tile["payload"])

    def test_покрытие_чужого_дня_не_показывается(self):
        # Оценка этого прогона не посчиталась: на тайле вчерашняя медиана, а её
        # покрытия meta уже не знает. Чужое число к ней не приклеиваем.
        tile = self.build({"method": "constituents", "estimate_asof": "2026-08-17",
                           "estimate_cover_pct": 99.8,
                           "estimated_days": ["2026-08-17", "2026-08-18"]})
        self.assertIn("медиана доходности бумаг индекса", tile["note"])
        self.assertNotIn("99,8", tile["note"])
        self.assertIsNone(tile["payload"]["estimate_cover_pct"])

    def test_про_смещение_оценки_вниз_тайл_больше_не_говорит(self):
        tile = self.build({"method": "constituents", "estimate_asof": "2026-08-18",
                           "estimate_cover_pct": 92.8,
                           "estimate_dropped": {"count": 21, "weight_pct": 7.04},
                           "estimated_days": ["2026-08-18"]})
        self.assertNotIn("смещена", tile["note"])
        self.assertNotIn("отброшено", tile["note"])

    def test_биржевое_число_ничем_не_помечается(self):
        tile = self.build({})
        self.assertNotIn("медиана", tile["note"])
        self.assertNotIn("method", tile["payload"])

    def test_спред_ig_берётся_за_свой_последний_день(self):
        # Медиана ВДО есть за 18.08, биржевая доходность IG — только за 17.08
        # (строку дня биржа отдаёт ночью). Спред IG — за 17.08, обе ноги оттуда:
        # 15,8 − 14,7 = 1,1. мутация: требовать тот же день, что у ВДО -> None
        # каждый вечер; мутация: IG 17.08 минус база 18.08 -> 1,0.
        self.put("rucbhycp_yield", {"2026-08-17": 27.0, "2026-08-18": 29.2},
                 meta={"estimated_days": ["2026-08-18"]})
        self.put("rucbcpns_yield", {"2026-08-10": 15.5, "2026-08-17": 15.8})
        self.put("zcyc_y2", {"2026-08-10": 14.5, "2026-08-17": 14.7, "2026-08-18": 14.8})
        p = self.tile("hy_spread")["payload"]
        self.assertEqual(p["ig_spread_pp"], 1.1)
        self.assertEqual(p["ig_spread_asof"], "2026-08-17")
        # IG недельной давности к сегодняшнему ВДО не приклеивается.
        self.put("rucbcpns_yield", {"2026-08-10": 15.5})
        self.assertIsNone(self.tile("hy_spread")["payload"]["ig_spread_pp"])

    def test_старая_пара_при_свежем_ряде_не_зелёная(self):
        # У свежих точек ВДО нет базы того же дня: на тайле пара месячной давности.
        # мутация: статус только по meta ряда -> зелёный тайл со старым числом.
        self.put("rucbhycp_yield", {"2026-07-10": 27.0, "2026-08-17": 29.2},
                 meta={"asof": "2026-08-17"})
        self.put("zcyc_y2", {"2026-07-10": 14.7})
        t = self.tile("hy_spread")
        self.assertEqual(t["asof"], "2026-07-10")
        self.assertEqual(t["status"], "stale")

    def test_после_пропуска_свежая_точка_берёт_базу_прошлого_дня(self):
        # Утро после чистки ряда: медиана за 17.08 есть, КБД за 17.08 ещё нет, а
        # прошлая общая дата — месяц назад. На тайле свежая точка против базы
        # 14.08 (три дня, выходные), и день базы назван: 29,2 − 14,6 = 14,6.
        # мутация: держаться последней общей даты -> «ВДО 27,0 %» месячной давности.
        self.put("rucbhycp_yield", {"2026-07-10": 27.0, "2026-08-17": 29.2},
                 meta={"asof": "2026-08-17", "estimated_days": ["2026-08-17"]})
        self.put("zcyc_y2", {"2026-07-10": 14.7, "2026-08-14": 14.6})
        t = self.tile("hy_spread")
        p = t["payload"]
        self.assertEqual(t["asof"], "2026-08-17")
        self.assertEqual((p["hy_yield"], p["base_yield"], p["spread_pp"]), (29.2, 14.6, 14.6))
        self.assertEqual(p["base_asof"], "2026-08-14")
        self.assertEqual(t["status"], "ok")
        self.assertIsNone(p["chg_21d_pp"])

    def test_отказ_оценки_виден_статусом_тайла(self):
        # Ряд стареет честно: фетчер объявил stale — тайл жёлтый, а не зелёный.
        tile = self.build({"status": "stale", "estimated_days": ["2026-08-18"],
                           "note": "оценка по составу не посчиталась"})
        self.assertEqual(tile["status"], "stale")


class TestHyWindowAndPriceIndex(TileCase):
    """Перцентиль и изменение за 21 день — только по сплошному окну; ход цены индекса."""

    def seed(self, hole=None, n=60):
        """n торговых дней подряд (календарь — ряд КБД); hole — срез дней без точки ВДО."""
        span = days("2026-08-14", n)
        self.put("zcyc_y2", {d: 14.0 for d in span})
        hy = {d: 30.0 + 0.1 * i for i, d in enumerate(span)}
        for d in (span[hole] if hole else []):
            hy.pop(d)
        self.put("rucbhycp_yield", hy, {"estimated_days": [span[-1]]})
        return span

    def test_сплошное_окно_даёт_оба_числа(self):
        # Спред растёт на 0,1 п.п. в день: за 21 торговый день +2,1 п.п. Последнее
        # значение — наибольшее из 60: средний ранг (59 + (1 + 1)/2)/60·100 = 100.
        self.seed()
        p = self.tile("hy_spread")["payload"]
        self.assertEqual(p["chg_21d_pp"], 2.1)
        self.assertEqual(p["pct_1y"], 100.0)
        self.assertFalse(p["window_gap"])

    def test_месячная_дыра_прячет_перцентиль_и_изменение(self):
        """После чистки ряда в окне пропуск в 20 торговых дней (21.10–19.11.2026).

        мутация: считать «21 точку назад» -> изменение за 41 торговый день выдаётся
        за изменение за 21; перцентиль сравнивает сегодня с историей без последнего
        месяца.
        """
        self.seed(hole=slice(-21, -1))
        t = self.tile("hy_spread")
        p = t["payload"]
        self.assertIsNone(p["chg_21d_pp"])
        self.assertIsNone(p["pct_1y"])
        self.assertTrue(p["window_gap"])
        self.assertNotIn("перцентиль", t["headline"])
        self.assertNotIn("н/д", t["headline"])
        # То, что считается честно, остаётся: доходность, база, спред.
        self.assertEqual(p["hy_yield"], 35.9)
        self.assertEqual(p["spread_pp"], 21.9)

    def test_один_пропущенный_день_окно_не_рвёт(self):
        # Оценка не посчиталась в один из дней — рядовое дело, перцентиль остаётся.
        self.seed(hole=slice(-10, -9))
        p = self.tile("hy_spread")["payload"]
        self.assertFalse(p["window_gap"])
        self.assertIsNotNone(p["pct_1y"])
        self.assertEqual(p["chg_21d_pp"], 2.1)

    def test_нет_точки_ровно_21_день_назад_изменения_нет(self):
        self.seed(hole=slice(-22, -21))
        p = self.tile("hy_spread")["payload"]
        self.assertIsNone(p["chg_21d_pp"])
        self.assertIsNotNone(p["pct_1y"])

    def test_ход_ценового_индекса_за_21_торговый_день(self):
        """Настоящие закрытия RUCBHYCP 19.10–18.11.2026 (22 торговых дня).

        Руками: 59,83 / 68,38 − 1 = −12,50 %. мутация: считать от первой точки ряда
        или «за 21 календарный день» -> другое число.
        """
        self.seed()
        closes = [68.38, 68.46, 68.67, 68.69, 68.9, 68.55, 68.06, 67.81, 66.74, 65.44,
                  64.68, 63.42, 62.43, 63.06, 63.45, 63.3, 63.11, 62.9, 62.27, 61.18,
                  60.15, 59.83]
        span = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
                "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
                "2026-11-02", "2026-11-03", "2026-11-05", "2026-11-06", "2026-11-09",
                "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16",
                "2026-11-17", "2026-11-18"]
        self.put("rucbhycp", {"2026-10-16": 68.13, **dict(zip(span, closes))})
        t = self.tile("hy_spread")
        p = t["payload"]
        self.assertEqual(p["price_chg_21d_pct"], -12.5)
        self.assertEqual(p["price_index"], 59.83)
        self.assertEqual(p["price_index_from"], 68.38)
        self.assertEqual(p["price_index_from_asof"], "2026-10-19")
        self.assertIn("Ценовой индекс ВДО за 21 торговый день: −12,5%", t["note"])
        self.assertIn("68,38 на 19.10 → 59,83 на 18.11", t["note"])
        self.assertIn("ценовой индекс за 21 торговый день −12,5%", t["headline"])

    def test_без_ряда_цены_строки_нет(self):
        self.seed()
        t = self.tile("hy_spread")
        self.assertNotIn("price_text", t["payload"])
        self.assertNotIn("Ценовой индекс", t["note"])


if __name__ == "__main__":
    unittest.main()
