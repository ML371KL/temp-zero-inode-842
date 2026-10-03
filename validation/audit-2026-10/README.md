# Аудит 03.10.2026: материалы

Отчёт — `AUDIT-2026-10-03.md`. Скрипты — `scripts/`: каждая проверка считает на функциях
самой панели (`pipeline/compute/*`) и отличается от прода только тем, что объявляет явно.

## Воспроизведение

```bash
# 1. стор из первоисточников штатным кодом (ничего не публикует; ~40 мин, из них ~35 — КБД)
export STATE_DIR=$PWD/.state
python ops/seed_store.py
python pipeline/run.py --mode bootstrap --dry-run --no-alerts \
  --only imoex,imoex_value,mcftr,rgbi,rtsi,rvi,mcxsm,rusfar3m,usd_cbr,key_rate,deposit_decade,brent,brent_moex,rucbhycp_yield,urals_tax,zcyc
# если Минфин/Консультант недоступен, налоговая Urals за последний месяц берётся из
# тайла «Рублёвая бочка» живой витрины (03.10.2026: 2026-08-31 = 67,11 $)

# 2. справочные ряды вне стора: курс ЦБ с 1997, RUONIA, живой data.json
python validation/audit-2026-10/scripts/fetch_refs.py      # кладёт в $AUDIT_WORK (.state/audit-2026-10)

# 3. проверки (запускать из каталога scripts/)
cd validation/audit-2026-10/scripts
python check_backtest.py     # сверка с продом (побитово) + экономика, налоги, здоровье — §1, §2.5, §3.3–3.4
python check_usd_splice.py   # курс USD в ноге ядра — §2.1
python check_excess_tr.py    # «над деньгами» без дивидендов — §2.2
python check_repricing.py    # «репрайсинг» и шаг ключевой ставки — §2.4
python check_layer2.py       # конфликт карточек второго ряда — §3.1
```

Первая строка `check_backtest.py` обязана напечатать «побитово: True»: только при этом
условии остальные числа относятся к продовому движку, а не к его пересказу. Живой
`data.json` меняется каждый прогон — сверка верна на дату его снятия, и для другой даты
стор надо собирать в тот же день.
