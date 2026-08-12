"""Тайл «Частные инвесторы»: что именно показывает крупная цифра и как считается доля.

Ряд `moex_retail` собирался месяцами вслепую — показать его было негде, и поломку
парсера заметили только по баннеру «часть источников молчит». Тайл выводит его на
панель, и здесь заперты ровно те три места, где он способен соврать:

1. **счета против участников.** Открытых счетов 41,9 млн, сделки в месяц заключают
   3,0 млн. Крупной цифрой идёт доля в ОБОРОТЕ, а «41,9 млн» — только вместе с
   активными: иначе маркетинговый счётчик читается как число участников рынка;
2. **обыкновенные и привилегированные одного эмитента.** Биржа печатает «Сбербанка
   31,8%» и «Сбербанка (прив.) 7,3%» двумя строками подряд. Это одна ставка розницы:
   без сложения концентрация занижена почти на треть;
3. **порядок строк портфеля.** Список идёт как в релизе, а не по величине (ЛУКОЙЛ
   11,6% стоит после префов Сбербанка с 7,3%). «Крупнейший = первая строка» верно
   только пока Сбербанк случайно стоит первым.

Плюс тир: розница описывает состав рынка, а не предсказывает его (в 2024 крупнейший
нетто-продавец года, в 2026 выкупала падение) — тайл обязан оставаться `monitor`.
"""

import datetime as dt
import unittest

from tests import need

NOW = dt.datetime(2026, 8, 12, 10, 0, tzinfo=dt.timezone.utc)
PORTFOLIO = [
    {"name": "Сбербанка", "share_pct": 31.8},
    {"name": "Сбербанка (прив.)", "share_pct": 7.3},
    {"name": "ЛУКОЙЛа", "share_pct": 11.6},
    {"name": "Газпрома", "share_pct": 11.6},
    {"name": "ВТБ", "share_pct": 9.7},
]
PAYLOAD = {"inflow_equity_bln": 30.2, "inflow_bonds_bln": None, "inflow_funds_bln": 2.9,
           "clients_total_mln": 41.9, "clients_added_k": 62.0, "active_mln": 3.0,
           "share_equity_pct": 63.5, "portfolio": PORTFOLIO}
SERIES = {"id": "moex_retail", "points": {"2026-06-30": 3.0},
          "meta": {"source": "moex_iss_sitenews", "status": "ok", "asof": "2026-06",
                   "url": "https://www.moex.com/n101882", "fetched_at": "2026-08-12T09:20:27Z",
                   "payload": PAYLOAD}}


class FakeStore:
    def __init__(self, series):
        self.series = series

    def load_series(self, sid):
        return self.series.get(sid)


class TestRetailTile(unittest.TestCase):
    def setUp(self):
        self.mon = need(self, "pipeline.compute.monitors", "_t_retail")

    def tile(self, series=None, payload=None):
        obj = dict(SERIES)
        if payload is not None:
            obj["meta"] = dict(SERIES["meta"], payload=payload)
        if series is not None:
            obj["points"] = series
        return self.mon._t_retail(FakeStore({"moex_retail": obj}), NOW)

    def test_крупная_цифра_это_доля_в_обороте(self):
        tile = self.tile()
        self.assertEqual(tile["payload"]["share_equity_pct"], 63.5)
        # мутация: подставить сюда clients_total_mln -> «41,9» крупным на карточке
        # прочтётся как число участников рынка, хотя торгуют 3,0 млн из них.
        self.assertIn("оборот", tile["headline"])
        self.assertIn("3", tile["headline"])
        self.assertIn("41,9", tile["headline"].replace(".", ","))

    def test_активные_считаются_долей_от_счетов(self):
        # 3,0 / 41,9 = 7,2%. Биржа этой доли не публикует — она считается здесь.
        self.assertEqual(self.tile()["payload"]["active_share_pct"], 7.2)

    def test_префы_складываются_с_обыкновенными(self):
        payload = self.tile()["payload"]
        self.assertEqual(payload["top_name"], "Сбербанка")
        # мутация: взять только строку «Сбербанка» -> 31,8 вместо 39,1, концентрация
        # народного портфеля занижена почти на треть.
        self.assertEqual(payload["top_share_pct"], 39.1)
        self.assertNotIn("(прив.)", [row["name"] for row in payload["portfolio"]])

    def test_портфель_отсортирован_по_величине_а_не_по_релизу(self):
        # В релизе ЛУКОЙЛ (11,6%) идёт ПОСЛЕ префов Сбербанка (7,3%).
        shares = [row["share_pct"] for row in self.tile()["payload"]["portfolio"]]
        self.assertEqual(shares, sorted(shares, reverse=True))
        self.assertEqual(self.tile()["payload"]["portfolio"][1]["name"], "ЛУКОЙЛа")

    def test_дата_берётся_у_точки_ряда_а_не_у_месяца(self):
        # meta.asof = «2026-06»: подпись «данные: …» на витрине разбирает дату,
        # и обрезанный до месяца ISO ломает сравнение с горизонтом панели.
        self.assertEqual(self.tile()["asof"], "2026-06-30")
        self.assertEqual(self.tile()["payload"]["period"], "2026-06")

    def test_битый_релиз_не_роняет_тайл(self):
        for broken in ({}, {"portfolio": "текст", "share_equity_pct": None},
                       {"portfolio": [None, {"name": "", "share_pct": 5}]}):
            tile = self.tile(payload=broken)
            self.assertEqual(tile["id"], "retail")
            self.assertTrue(tile["headline"].strip())

    def test_нет_данных_вообще(self):
        tile = self.mon._t_retail(FakeStore({}), NOW)
        self.assertEqual(tile["status"], "missing")
        self.assertTrue(tile["note"].strip())

    def test_тир_остаётся_наблюдением(self):
        constants = need(self, "pipeline.lib.constants", "MONITOR_TIERS")
        # мутация: повысить до B/A -> описание состава рынка поедет на панель как
        # проверенный сигнал, хотя знак потока розницы менялся между 2024 и 2026.
        self.assertEqual(constants.MONITOR_TIERS["retail"], "monitor")
        self.assertEqual(self.tile()["tier"], "monitor")


if __name__ == "__main__":
    unittest.main()
