"""
Análise de liquidez (liq_t0) como preditor de var_pico
Rode: python3 analise_liq.py
"""
import os
import numpy as np
from scipy import stats
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute("""
    SELECT liq_t0, mc_t0, var_pico
    FROM tokens_monitorados
    WHERE liq_t0 IS NOT NULL AND liq_t0 > 0
      AND var_pico IS NOT NULL
      AND mc_t0 IS NOT NULL AND mc_t0 > 0
""")
rows = cur.fetchall()
conn.close()

liq  = np.array([r[0] for r in rows], dtype=float)
mc   = np.array([r[1] for r in rows], dtype=float)
vp   = np.array([r[2] for r in rows], dtype=float)
win  = (vp > 50).astype(int)

print(f"Total registros: {len(rows)}")
print()

# Correlações
r_log, p_log = stats.pearsonr(np.log1p(liq), vp)
print(f"Correlação log(liq_t0) × var_pico : r={r_log:.4f}  p={p_log:.4f}")

ratio = liq / mc
r_ratio, p_ratio = stats.pearsonr(np.log1p(ratio), vp)
print(f"Correlação log(liq/mc)  × var_pico : r={r_ratio:.4f}  p={p_ratio:.4f}")
print()

# Faixas absolutas de liq_t0
bins_abs = [(0,2000),(2000,5000),(5000,10000),(10000,20000),(20000,50000),(50000,999999)]
print(f"{'Faixa liq_t0':<24} {'n':>5} {'win%':>6} {'med_pico':>9} {'mean_pico':>10}")
print("-"*57)
for lo, hi in bins_abs:
    mask = (liq >= lo) & (liq < hi)
    n = int(mask.sum())
    if n < 5:
        continue
    w   = win[mask].mean() * 100
    med = np.median(vp[mask])
    mean = vp[mask].mean()
    print(f"${lo:>6,} – ${hi:>7,}     {n:>5}  {w:>5.1f}%  {med:>8.1f}%  {mean:>8.1f}%")

print()

# Faixas de ratio liq/mc
bins_ratio = [(0,0.1),(0.1,0.2),(0.2,0.35),(0.35,0.5),(0.5,0.7),(0.7,1.0),(1.0,999)]
print(f"{'Faixa liq/mc':<20} {'n':>5} {'win%':>6} {'med_pico':>9}")
print("-"*43)
for lo, hi in bins_ratio:
    mask = (ratio >= lo) & (ratio < hi)
    n = int(mask.sum())
    if n < 5:
        continue
    w   = win[mask].mean() * 100
    med = np.median(vp[mask])
    print(f"{lo:.2f} – {hi:.2f}           {n:>5}  {w:>5.1f}%  {med:>8.1f}%")

print()
print("Interpretação:")
print("  r > |0.10| com p < 0.05 → liq tem sinal relevante para o score")
print("  r > |0.15|              → considerar como feature no ML")
