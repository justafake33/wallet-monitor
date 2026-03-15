"""
scout_calibrar_filtros.py
Analisa os trades do banco para calibrar os filtros do scout.py.
Foco: o que separa VENCEDORES de PERDEDORES no momento da entrada (T0).
"""
import os
import psycopg2
import psycopg2.extras
import statistics
from collections import defaultdict

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway"
)

def buscar_dados():
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    carteira, tipo_carteira, var_pico, categoria_final,
                    mc_t0, liq_t0, volume_t0, buys_t0, sells_t0,
                    idade_min, bc_progress, holders_count,
                    net_momentum_t0, ratio_vol_mc_t0, top10_pct,
                    is_multi, score_qualidade,
                    EXTRACT(HOUR FROM data_compra) AS hora_entrada,
                    EXTRACT(DOW FROM data_compra) AS dia_semana
                FROM registros
                WHERE tipo = 'COMPRA'
                  AND categoria_final IS NOT NULL
                  AND categoria_final NOT ILIKE '%aguardando%'
                  AND categoria_final NOT ILIKE '%sem dados%'
                  AND var_pico IS NOT NULL
                ORDER BY data_compra DESC
            """)
            return cur.fetchall()

def med(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 1) if len(v) >= 3 else None

def media(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 1) if v else None

def pct(n, d):
    return f"{round(n/d*100,1)}%" if d else "—"

def analisar_faixa(rows, campo, faixas):
    """Para cada faixa, calcula winrate e mediana de var_pico."""
    print(f"\n{'='*55}")
    print(f"  {campo.upper()}")
    print(f"{'='*55}")
    print(f"  {'Faixa':<22} {'N':>5} {'Winrate':>9} {'Mediana':>9} {'Média':>9}")
    print(f"  {'-'*52}")
    for label, fn in faixas:
        subset = [r for r in rows if r[campo] is not None and fn(r[campo])]
        if not subset:
            continue
        wins = sum(1 for r in subset if r["var_pico"] > 50)
        varps = [r["var_pico"] for r in subset]
        print(f"  {label:<22} {len(subset):>5} {pct(wins,len(subset)):>9} {str(med(varps)):>9} {str(media(varps)):>9}")

def analisar_categorico(rows, campo, valores):
    print(f"\n{'='*55}")
    print(f"  {campo.upper()}")
    print(f"{'='*55}")
    print(f"  {'Valor':<22} {'N':>5} {'Winrate':>9} {'Mediana':>9}")
    print(f"  {'-'*45}")
    for label, fn in valores:
        subset = [r for r in rows if fn(r.get(campo))]
        if not subset:
            continue
        wins = sum(1 for r in subset if r["var_pico"] > 50)
        varps = [r["var_pico"] for r in subset]
        print(f"  {label:<22} {len(subset):>5} {pct(wins,len(subset)):>9} {str(med(varps)):>9}")

def main():
    print("Conectando ao banco...")
    rows = buscar_dados()
    total = len(rows)
    vencedores = [r for r in rows if r["var_pico"] > 50]
    perdedores  = [r for r in rows if r["var_pico"] < -50]

    print(f"\n{'#'*55}")
    print(f"  CALIBRAÇÃO DE FILTROS — BASE: {total} trades")
    print(f"  Vencedores (>50%): {len(vencedores)} | Perdedores (<-50%): {len(perdedores)}")
    print(f"  Winrate geral: {pct(len(vencedores), total)}")
    print(f"  AVISO: base pequena — tendências, não verdades absolutas")
    print(f"{'#'*55}")

    # ── 1. Market Cap T0 ──────────────────────────────────
    analisar_faixa(rows, "mc_t0", [
        ("<$5k",        lambda x: x < 5_000),
        ("$5k–$15k",    lambda x: 5_000 <= x < 15_000),
        ("$15k–$30k",   lambda x: 15_000 <= x < 30_000),
        ("$30k–$60k",   lambda x: 30_000 <= x < 60_000),
        ("$60k–$150k",  lambda x: 60_000 <= x < 150_000),
        ("$150k–$500k", lambda x: 150_000 <= x < 500_000),
        (">$500k",      lambda x: x >= 500_000),
    ])

    # ── 2. Liquidez T0 ────────────────────────────────────
    analisar_faixa(rows, "liq_t0", [
        ("<$2k",        lambda x: x < 2_000),
        ("$2k–$5k",     lambda x: 2_000 <= x < 5_000),
        ("$5k–$10k",    lambda x: 5_000 <= x < 10_000),
        ("$10k–$30k",   lambda x: 10_000 <= x < 30_000),
        (">$30k",       lambda x: x >= 30_000),
    ])

    # ── 3. Volume T0 ──────────────────────────────────────
    analisar_faixa(rows, "volume_t0", [
        ("<$10k",       lambda x: x < 10_000),
        ("$10k–$30k",   lambda x: 10_000 <= x < 30_000),
        ("$30k–$100k",  lambda x: 30_000 <= x < 100_000),
        ("$100k–$300k", lambda x: 100_000 <= x < 300_000),
        (">$300k",      lambda x: x >= 300_000),
    ])

    # ── 4. Idade do token ─────────────────────────────────
    analisar_faixa(rows, "idade_min", [
        ("<10 min",     lambda x: x < 10),
        ("10–25 min",   lambda x: 10 <= x < 25),
        ("25–60 min",   lambda x: 25 <= x < 60),
        ("60–120 min",  lambda x: 60 <= x < 120),
        (">120 min",    lambda x: x >= 120),
    ])

    # ── 5. BC Progress ────────────────────────────────────
    analisar_faixa(rows, "bc_progress", [
        ("<20%",        lambda x: x < 20),
        ("20–40%",      lambda x: 20 <= x < 40),
        ("40–60%",      lambda x: 40 <= x < 60),
        ("60–80%",      lambda x: 60 <= x < 80),
        (">80%",        lambda x: x >= 80),
    ])

    # ── 6. Holders ────────────────────────────────────────
    analisar_faixa(rows, "holders_count", [
        ("<50",         lambda x: x < 50),
        ("50–100",      lambda x: 50 <= x < 100),
        ("100–200",     lambda x: 100 <= x < 200),
        ("200–500",     lambda x: 200 <= x < 500),
        (">500",        lambda x: x >= 500),
    ])

    # ── 7. Hora da entrada ────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  HORA DA ENTRADA (UTC)")
    print(f"{'='*55}")
    print(f"  {'Hora':<22} {'N':>5} {'Winrate':>9} {'Mediana':>9}")
    print(f"  {'-'*45}")
    por_hora = defaultdict(list)
    for r in rows:
        if r["hora_entrada"] is not None:
            por_hora[int(r["hora_entrada"])].append(r["var_pico"])
    for h in sorted(por_hora):
        vals = por_hora[h]
        wins = sum(1 for v in vals if v > 50)
        print(f"  {str(h)+'h':<22} {len(vals):>5} {pct(wins,len(vals)):>9} {str(med(vals)):>9}")

    # ── 8. Por carteira ───────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  PERFORMANCE POR CARTEIRA")
    print(f"{'='*55}")
    print(f"  {'Carteira':<20} {'Tipo':<8} {'N':>5} {'Winrate':>9} {'Mediana':>9}")
    print(f"  {'-'*52}")
    por_cart = defaultdict(list)
    tipo_map = {}
    for r in rows:
        por_cart[r["carteira"]].append(r["var_pico"])
        tipo_map[r["carteira"]] = r.get("tipo_carteira", "?")
    for c, vals in sorted(por_cart.items()):
        wins = sum(1 for v in vals if v > 50)
        print(f"  {c:<20} {tipo_map[c]:<8} {len(vals):>5} {pct(wins,len(vals)):>9} {str(med(vals)):>9}")

    # ── 9. Multi vs Single ───────────────────────────────
    analisar_categorico(rows, "is_multi", [
        ("Multi-carteira", lambda x: x),
        ("Single-carteira", lambda x: not x),
    ])

    # ── 10. Ratio vol/mc ──────────────────────────────────
    analisar_faixa(rows, "ratio_vol_mc_t0", [
        ("<0.5",        lambda x: x < 0.5),
        ("0.5–1.0",     lambda x: 0.5 <= x < 1.0),
        ("1.0–2.0",     lambda x: 1.0 <= x < 2.0),
        ("2.0–5.0",     lambda x: 2.0 <= x < 5.0),
        (">5.0",        lambda x: x >= 5.0),
    ])

    print(f"\n{'#'*55}")
    print("  FIM — use esses dados para calibrar os filtros do scout.py")
    print(f"{'#'*55}\n")

if __name__ == "__main__":
    main()
