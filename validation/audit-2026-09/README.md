# Аудит 02.09.2026: материалы

Сводный отчёт — `AUDIT-2026-09-02.md`. Бриф агентов — `BRIEF.md`. Отчёты тринадцати аудитов — `reports/*.md`
(A_baseline, B_timeliness, C_core, D1_positioning, D2_rates, D3_value, E_algopack, F1_economist, F2_pm,
G_decision, H_weekly, P_package, S_skeptic, S2_core_skeptic) и ключевые таблицы. Скрипты — `scripts/`
(pandas/numpy; запускались из рабочего каталога аудита с подкаталогом `data/`).

Данные аудита (≈86 МБ) в репозиторий не кладутся. Как воспроизвести: снять копию боевого стора
(`/var/lib/moex-radar/raw`), положить в `<workdir>/store/raw`, выполнить `scripts/export_panel.py`
(он собирает `data/panel_prod_daily.csv`, `panel_prod_monthly.csv`, `raw_long.csv`, `prices_wide.csv`
кодом самой панели и сверяет композит с `validation/data/walkforward_results.csv`), затем скрипты по id.
Исследовательские ряды (`cb_decisions.csv`, `auctions.csv`, `orfr_flows.csv`, `panel_long.csv` и др.) —
из рабочего каталога исследования `moex-drivers/validation/data/`.

`impl/IMPLEMENTATION_SPEC.md` — спецификация внедрения пакета; `impl/decision_fixture.json` — эталон
позиций/битов ступени P2, который обязан воспроизводить `pipeline/compute/decision.py`
(тест `tests/test_decision.py`).
