import sys; sys.path.insert(0, "scripts")
import numpy as np, pandas as pd
import B_timeliness_lib as L
df = L.load()
B = L.bits(df)
d = df.loc["2004-01-06":"2026-09-01"]
for mine, prod in [("trend_ma200","prod_trend"),("vol_w21_p80","prod_vol"),("bond_dd4","prod_bond")]:
    a = B[mine].reindex(d.index); b = B[prod].reindex(d.index)
    m = a.notna() & b.notna()
    print(mine, "vs", prod, "совпадение:", round(float((a[m]==b[m]).mean()),4), "n=", int(m.sum()))
tox = L.gate_from_bits(B["trend_ma200"], B["vol_w21_p80"], B["bond_dd4"]).reindex(d.index)
print("toxic recomputed vs prod:", round(float((tox==B["prod_toxic"].reindex(d.index)).mean()),4))
print("panel ma200 vs my rolling:", float((df.ma200 - df.imoex.rolling(200).mean()).abs().max()))
print("panel rv21 vs mine:", float((df.realized_vol_21 - L.rv(df.imoex,21)).abs().max()))
print("panel rgbi_dd vs mine:", float((df.rgbi_dd - np.log(df.rgbi/df.rgbi.rolling(252,min_periods=120).max())).abs().dropna().max()))
print("comp_sign counts from 2004:", d.comp_sign.value_counts().to_dict())
print("r_long NaN from 2004:", int(d.r_long.isna().sum()), " mm_rate NaN:", int(d.mm_rate.isna().sum()), " r_flat NaN:", int(d.r_flat.isna().sum()))
eps = L.episodes(df.imoex, 0.15)
pd.set_option("display.width", 200)
print(eps[["peak","trough","next_peak","dd","rise","confirmed","days_fall","days_rise"]].to_string())
eps25 = L.episodes(df.imoex, 0.25)
print(eps25[["peak","trough","next_peak","dd","rise","confirmed","days_fall","days_rise"]].to_string())
