"""СЧА фондов денежного рынка: справочный счётчик МосБиржи не имеет права держать прогон.

Аудит 20.11.2026. С 13.11.2026 `www.moex.com/ru/moneyfunds` с прод-машины не отвечает:
три попытки по тридцать секунд на каждом суточном прогоне (журнал 19.11: «lqdt_aum ok
93 945 мс»). Счётчик нужен только строкой контекста в meta — в ряд он не идёт, — а
стоил полутора минут задержки публикации и вечернего сообщения при статусе «ok».

Фикстура tests/fixtures/investfunds_lqdt.html — блок «Динамика стоимости пая и СЧА»
карточки фонда LQDT, ответ 20.11.2026. В сеть не ходим: подменяется
`investfunds.get_text`.
"""

import unittest
from unittest import mock

from tests import fixture_text, need


class MoneyFundsCase(unittest.TestCase):
    def setUp(self):
        self.f = need(self, "pipeline.fetch.investfunds", "money_funds", "moex_total_bln",
                      "MOEX_TIMEOUT_S", "MOEX_RETRIES")
        self.page = fixture_text("investfunds_lqdt.html")
        self.calls = []

    def serve(self, moex):
        """moex — текст страницы счётчика либо исключение (источник молчит)."""
        def get_text(url, **kw):
            self.calls.append((url, kw))
            if "moex.com" in url:
                if isinstance(moex, Exception):
                    raise moex
                return moex
            return self.page
        patcher = mock.patch.object(self.f, "get_text", side_effect=get_text)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_сча_фонда_разбирается(self):
        # Снимок карточки: 18.11.2026, пай 2,14, СЧА 905 529 276 749,35 ₽ = 905,529 млрд.
        self.serve("<p>СЧА фондов денежного рынка превысила 1,8 трлн рублей</p>")
        _sid, points, meta = self.f.money_funds()
        self.assertEqual(points, {"2026-11-18": 905.529})
        self.assertEqual(meta["status"], "ok")
        self.assertEqual(meta["moex_total_bln"], 1800.0)
        self.assertIsNone(meta["moex_warning"])

    def test_счётчик_рынка_спрашивается_одной_короткой_попыткой(self):
        # мутация: вернуть таймаут и повторы по умолчанию -> молчащий moex.com снова
        # стоит суточному прогону полутора минут.
        self.serve(self.f.FetchError("не удалось скачать: timed out"))
        self.f.money_funds()
        kw = next(kw for url, kw in self.calls if "moex.com" in url)
        self.assertEqual(kw.get("retries"), 1)
        self.assertLessEqual(kw.get("timeout"), 10)
        self.assertEqual((self.f.MOEX_TIMEOUT_S, self.f.MOEX_RETRIES), (8, 1))

    def test_молчащий_счётчик_не_отказ_ряда_но_назван_в_meta(self):
        # СЧА фонда прочитана — ряд исправен; о потерянной строке контекста сказано.
        # мутация: молчать об отказе счётчика -> в журнале «ok», а почему на тайле
        # нет строки про весь рынок, не видно нигде.
        self.serve(self.f.FetchError("не удалось скачать: timed out"))
        _sid, points, meta = self.f.money_funds()
        self.assertEqual(points, {"2026-11-18": 905.529})
        self.assertEqual(meta["status"], "ok")
        self.assertIsNone(meta["moex_total_bln"])
        self.assertIn("счётчик рынка на moex.com не получен", meta["moex_warning"])
        self.assertIn("счётчик рынка на moex.com не получен", meta["note"])


if __name__ == "__main__":
    unittest.main()
