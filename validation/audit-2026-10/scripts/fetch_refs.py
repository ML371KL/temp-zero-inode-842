"""Справочные ряды аудита, которых нет в сторе панели (кладёт в $AUDIT_WORK).

  usd_cbr_full.json — официальный курс USD ЦБ с 1997 года (XML_dynamic; до 1998 —
                      деноминация /1000). В проде ряд usd_cbr начинается с 2013.
  ruonia.json       — RUONIA с 2010 года: прокси доходности фонда денежного рынка.
  data.json         — живой снимок витрины: эталон для побитовой сверки обвязки.

Три запроса, ничего не пишет никуда, кроме $AUDIT_WORK.
"""

import datetime
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORK = Path(os.environ.get("AUDIT_WORK") or (ROOT / ".state" / "audit-2026-10"))
TODAY = datetime.date.today()


def get(url, encoding="utf-8"):
    req = urllib.request.Request(url, headers={"User-Agent": "moex-radar-audit"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode(encoding, errors="replace")


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    till = TODAY.strftime("%d/%m/%Y")
    xml = get("https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1=01/01/1997"
              f"&date_req2={till}&VAL_NM_RQ=R01235", "windows-1251")
    usd = {}
    for d, v in re.findall(r'<Record Date="([\d.]+)"[^>]*>.*?<VunitRate>([^<]+)</VunitRate>',
                           xml, re.S):
        day = datetime.datetime.strptime(d, "%d.%m.%Y").date()
        val = float(v.replace(",", "."))
        usd[day.isoformat()] = val / 1000 if day < datetime.date(1998, 1, 1) else val
    (WORK / "usd_cbr_full.json").write_text(json.dumps(usd), encoding="utf-8")
    print(f"usd_cbr_full.json: {len(usd)} записей {min(usd)}…{max(usd)}")

    html = get("https://www.cbr.ru/hd_base/ruonia/dynamics/?UniDbQuery.Posted=True"
               f"&UniDbQuery.From=11.01.2010&UniDbQuery.To={TODAY.strftime('%d.%m.%Y')}")
    ru = {datetime.datetime.strptime(d, "%d.%m.%Y").date().isoformat(): float(v.replace(",", "."))
          for d, v in re.findall(r'<tr>\s*<td>(\d\d\.\d\d\.\d{4})</td>\s*<td class="right">([\d,]+)</td>',
                                 html)}
    (WORK / "ruonia.json").write_text(json.dumps(ru), encoding="utf-8")
    print(f"ruonia.json: {len(ru)} записей {min(ru)}…{max(ru)}")

    live = get("https://tzi-842.pages.dev/data/data.json")
    json.loads(live)
    (WORK / "data.json").write_text(live, encoding="utf-8")
    print(f"data.json: {len(live)} байт")


if __name__ == "__main__":
    sys.exit(main())
