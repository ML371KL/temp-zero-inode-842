"""Точечное доверие УЦ: бандл читается, применяется к своим хостам и только к ним.

Зачем эти проверки. Ряд ИПЦ на проде молча жил на зеркале: rosstat.gov.ru выдан УЦ
Минцифры, промежуточный сертификат в рукопожатии не отдаёт, и запрос падал с
CERTIFICATE_VERIFY_FAILED. Ошибка была невидимой (фолбэк отрабатывал штатно), поэтому
здесь проверяется не «функция что-то вернула», а три вещи, каждая из которых уже
ломалась или могла сломаться тихо:
  1. PEM в репозитории реально разбирается openssl (склеенный без переводов строк
     файл выглядит нормальным глазами и падает `[X509] PEM lib` в проде);
  2. чужой корень действует ТОЛЬКО для своих хостов — иначе это подмена системного
     доверия для всего конвейера;
  3. системные корни не потеряны — контекст обязан остаться пригодным для обычных
     сайтов, иначе один хост чинится ценой всех остальных.
"""

import os
import ssl
import unittest

from tests import need

# Отпечатки из шапки самого бандла. Подмена файла (или тихая «оптимизация» вида
# «положим сюда ещё один корень») обязана валить набор.
#
# Промежуточных ДВА, и это не запас: файл с gu-st.ru отдаёт выпуск 2022 года, а лист
# rosstat.gov.ru подписан выпуском 2024-го. С одним лишь gu-st-бандлом прод падал
# ровно так же, как без него, — «unable to get local issuer certificate», и обнаружить
# это удалось только живым запросом с прод-машины.
ROOT_SHA256 = "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31"
SUB_2022_SHA256 = "BBBDE2103E790B999EC62BD03CF625A5A2E7C316E10AFE6A490EEDEAD8B3FD9B"
SUB_2024_SHA256 = "2155785036C900DBB5F1BB2A1569C80C55595BD6BF94867A29BBDDBC7D88A3F2"


class CaBundleCase(unittest.TestCase):
    def setUp(self):
        self.http = need(self, "pipeline.lib.http", "ssl_context", "HOST_CA_BUNDLE",
                         "CA_DIR")
        self.http._ctx_cache.clear()
        self.addCleanup(self.http._ctx_cache.clear)

    def bundle_path(self):
        return os.path.join(self.http.CA_DIR, self.http.HOST_CA_BUNDLE["rosstat.gov.ru"])

    def test_бандл_лежит_в_репозитории_и_разбирается(self):
        path = self.bundle_path()
        self.assertTrue(os.path.exists(path), f"нет файла {path}")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=path)   # падает, если PEM склеен неправильно
        certs = ctx.get_ca_certs(binary_form=True)
        self.assertEqual(len(certs), 3, "в бандле корень и ОБА промежуточных (2022 и 2024)")

    def test_отпечатки_совпадают_с_объявленными(self):
        import hashlib
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=self.bundle_path())
        got = {hashlib.sha256(der).hexdigest().upper()
               for der in ctx.get_ca_certs(binary_form=True)}
        self.assertIn(ROOT_SHA256, got, "корневой сертификат Минцифры подменён")
        self.assertIn(SUB_2022_SHA256, got, "промежуточный 2022 подменён")
        self.assertIn(SUB_2024_SHA256, got,
                      "промежуточный 2024 подменён — именно им подписан лист Росстата")

    def test_оба_промежуточных_подписаны_корнем(self):
        # Промежуточный, не выводящийся на корень из этого же файла, — мусор в бандле:
        # цепочка не соберётся, а ошибка будет выглядеть как «сертификат не тот».
        import ssl as _ssl
        ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=self.bundle_path())
        certs = ctx.get_ca_certs()
        roots = [c for c in certs if "Russian Trusted Root CA" in str(c["subject"])]
        subs = [c for c in certs if "Russian Trusted Sub CA" in str(c["subject"])]
        self.assertEqual(len(roots), 1)
        self.assertEqual(len(subs), 2)
        for sub in subs:
            self.assertIn("Russian Trusted Root CA", str(sub["issuer"]))

    def test_контекст_даётся_только_своим_хостам(self):
        self.assertIsNotNone(self.http.ssl_context("rosstat.gov.ru"))
        for host in ("iss.moex.com", "www.cbr.ru", "minfin.gov.ru", "t.me",
                     "rosstat.gov.ru.evil.example"):
            self.assertIsNone(self.http.ssl_context(host),
                              f"чужой корень утёк на {host}")

    def test_системные_корни_не_потеряны(self):
        ctx = self.http.ssl_context("rosstat.gov.ru")
        subjects = " ".join(str(c.get("subject")) for c in ctx.get_ca_certs())
        self.assertIn("Russian Trusted Root CA", subjects,
                      "российский корень не подгрузился")
        # Системных корней на любой живой машине заведомо больше двух: если их нет,
        # значит контекст создан как «доверяем ТОЛЬКО этому файлу».
        self.assertGreater(len(ctx.get_ca_certs()), 2,
                           "системные корни затёрты — сломаются все прочие источники")

    def test_отсутствие_бандла_не_роняет_http(self):
        self.http._ctx_cache.clear()
        self.http.HOST_CA_BUNDLE["example.invalid"] = "нет-такого-файла.pem"
        self.addCleanup(self.http.HOST_CA_BUNDLE.pop, "example.invalid", None)
        quiet = []
        prev_log, self.http.LOG = self.http.LOG, quiet.append
        self.addCleanup(setattr, self.http, "LOG", prev_log)
        self.assertIsNone(self.http.ssl_context("example.invalid"))
        self.assertTrue(quiet, "пропажа бандла обязана попасть в журнал")

    def test_контекст_кэшируется(self):
        first = self.http.ssl_context("rosstat.gov.ru")
        self.assertIs(first, self.http.ssl_context("www.rosstat.gov.ru"),
                      "два хоста с одним файлом должны делить контекст")


if __name__ == "__main__":
    unittest.main()
