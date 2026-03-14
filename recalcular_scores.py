"""
recalcular_scores.py — Recalcula score_qualidade de todos os registros com v8.0

Uso:
    python recalcular_scores.py          # dry-run (mostra diff sem salvar)
    python recalcular_scores.py --apply  # aplica no banco

Limitação: dev_classif não é salvo em registros, então será tratado como None (neutro).
"""
import os
import sys
import psycopg2
import psycopg2.extras
from collections import Counter

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway"
)

def calcular_score_v8(mc_t0, liq_t0, txns, ratio_vol_mc, idade_min, dex,
                      holders_count=None, top10_pct=None, buys=0, sells=0,
                      dev_classif=None, bc_progress=None, top1_pct=None, carteira=None):
    score = 0
    if dev_classif == "serial_rugger":
        return 0

    # 1. MARKET CAP
    if mc_t0:
        if 5000 <= mc_t0 < 15000:     score += 3
        elif 15000 <= mc_t0 < 30000:  score += 1
        elif mc_t0 < 5000:            score -= 1
        elif mc_t0 >= 120000:         score -= 2

    # 2. IDADE DO TOKEN
    if idade_min is not None:
        if 45 <= idade_min <= 60:     score += 3
        elif 25 <= idade_min < 45:    score += 2
        elif 10 <= idade_min < 25:    score += 1
        elif idade_min > 120:         score -= 1

    # 3. BC PROGRESS
    if bc_progress is not None:
        if 40 <= bc_progress <= 80:   score += 2
        elif bc_progress > 80:        score -= 1

    # 4. RATIO BUY/SELL
    total_txns = (buys or 0) + (sells or 0)
    if total_txns > 0:
        ratio_bs = buys / total_txns
        if ratio_bs >= 0.65:          score += 2
        elif ratio_bs >= 0.55:        score += 1
        elif ratio_bs < 0.40:         score -= 1

    # 5. TOP1_PCT
    if top1_pct is not None:
        if top1_pct > 50:             score -= 2

    # 6. CARTEIRA
    if carteira == "carteira_C":      score += 2
    elif carteira == "carteira_D":    score += 1

    # 7. NET MOMENTUM
    if total_txns > 0:
        net = (buys or 0) - (sells or 0)
        if net >= 20:   score += 1
        elif net < 0:   score -= 1

    # 8. HOLDERS
    if holders_count is not None:
        if holders_count >= 200:      score += 1
        elif holders_count < 80:      score -= 1

    # 9. DEV HISTORY
    if dev_classif == "confiavel":    score += 2
    elif dev_classif == "rugger":     score -= 3

    # 10. RATIO VOL/MC
    if ratio_vol_mc is not None:
        if ratio_vol_mc < 0.5:        score -= 1

    return max(0, min(10, score))

def tier(score):
    if score >= 7: return "🟢 ALTA"
    if score >= 4: return "🟡 MOD"
    return "🔴 BAIXA"

def main():
    apply = "--apply" in sys.argv

    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, score_qualidade,
                       mc_t0, liq_t0, txns5m_t0, ratio_vol_mc_t0, idade_min, dex,
                       holders_count, top10_pct, buys_t0, sells_t0,
                       bc_progress, top1_pct, carteira
                FROM registros
                WHERE tipo = 'COMPRA'
                ORDER BY id
            """)
            rows = cur.fetchall()

        print(f"Total de registros: {len(rows)}")

        mudancas = []
        distribuicao_nova = Counter()
        distribuicao_velha = Counter()

        for r in rows:
            score_novo = calcular_score_v8(
                mc_t0=r["mc_t0"],
                liq_t0=r["liq_t0"],
                txns=r["txns5m_t0"],
                ratio_vol_mc=r["ratio_vol_mc_t0"],
                idade_min=r["idade_min"],
                dex=r["dex"],
                holders_count=r["holders_count"],
                top10_pct=r["top10_pct"],
                buys=r["buys_t0"] or 0,
                sells=r["sells_t0"] or 0,
                dev_classif=None,
                bc_progress=r["bc_progress"],
                top1_pct=r["top1_pct"],
                carteira=r["carteira"],
            )
            score_velho = r["score_qualidade"]
            distribuicao_velha[tier(score_velho) if score_velho is not None else "❓"] += 1
            distribuicao_nova[tier(score_novo)] += 1

            if score_velho != score_novo:
                mudancas.append((r["id"], score_velho, score_novo))

        print(f"\nRegistros que mudam de score: {len(mudancas)} / {len(rows)}")

        print("\n--- Distribuição ANTES (v7) ---")
        for t, n in sorted(distribuicao_velha.items()):
            print(f"  {t}: {n}")

        print("\n--- Distribuição DEPOIS (v8) ---")
        for t, n in sorted(distribuicao_nova.items()):
            print(f"  {t}: {n}")

        if not apply:
            print("\n[DRY-RUN] Nenhuma alteração salva. Use --apply para aplicar.")
            # Mostra amostra das mudanças
            print("\nAmostra (primeiros 20 que mudaram):")
            for row_id, antes, depois in mudancas[:20]:
                print(f"  id={row_id}: {antes} → {depois} ({tier(antes) if antes else '?'} → {tier(depois)})")
            return

        # Aplica
        with conn.cursor() as cur:
            for row_id, _, score_novo in mudancas:
                cur.execute(
                    "UPDATE registros SET score_qualidade = %s WHERE id = %s",
                    (score_novo, row_id)
                )
        conn.commit()
        print(f"\n✅ {len(mudancas)} registros atualizados com score v8.0")

if __name__ == "__main__":
    main()
