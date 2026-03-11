"""
analise.py — Dashboard de Performance do Sistema de Score

Uso:
    python analise.py            # imprime no terminal
    python analise.py --telegram # envia resumo ao Telegram
    python analise.py --html     # salva analise.html
"""
import os
import sys
import argparse
import statistics
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime
from collections import defaultdict

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway"
)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "7819827538:AAH4MxxqJ_Lw9bHBBDf3LRlBcqiUe_SDSFY")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT", "-5284184650")

# ──────────────────────────────────────────────────────────
# COLETA DE DADOS
# ──────────────────────────────────────────────────────────
def buscar_registros():
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    id, carteira, tipo_carteira, token_mint, data_compra,
                    score_qualidade, var_pico, categoria_final,
                    mc_t0, liq_t0, volume_t0, txns5m_t0, buys_t0, sells_t0,
                    net_momentum_t0, idade_min, ratio_vol_mc_t0,
                    holders_count, top1_pct, top10_pct, dev_saiu, bc_progress,
                    is_multi, var_t1, var_t2, var_t3
                FROM registros
                WHERE tipo = 'COMPRA'
                  AND categoria_final IS NOT NULL
                  AND categoria_final NOT ILIKE '%aguardando%'
                  AND categoria_final NOT ILIKE '%sem dados%'
                  AND var_pico IS NOT NULL
                ORDER BY data_compra DESC
            """)
            return cur.fetchall()

# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────
def tier(score):
    if score is None:   return "❓ Sem score"
    if score >= 7:      return "🟢 ALTA CONFIANÇA"
    if score >= 4:      return "🟡 MODERADO"
    return              "🔴 BAIXA CONFIANÇA"

def pct(num, den):
    return f"{round(num / den * 100, 1)}%" if den else "—"

def med(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 1) if v else None

def p25(vals):
    v = sorted(x for x in vals if x is not None)
    if not v: return None
    return round(v[int(len(v) * 0.25)], 1)

def p75(vals):
    v = sorted(x for x in vals if x is not None)
    if not v: return None
    return round(v[int(len(v) * 0.75)], 1)

def media(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 1) if v else None

def barra(val, max_val=100, width=20):
    if val is None or max_val == 0: return "░" * width
    filled = int((val / max_val) * width)
    return "█" * min(filled, width) + "░" * (width - min(filled, width))

# ──────────────────────────────────────────────────────────
# ANÁLISES
# ──────────────────────────────────────────────────────────
def analise_por_tier(rows):
    grupos = defaultdict(list)
    for r in rows:
        grupos[tier(r["score_qualidade"])].append(r["var_pico"])

    linhas = []
    ordem = ["🟢 ALTA CONFIANÇA", "🟡 MODERADO", "🔴 BAIXA CONFIANÇA", "❓ Sem score"]
    for t in ordem:
        vals = grupos.get(t, [])
        if not vals:
            continue
        vencedores = sum(1 for v in vals if v > 50)
        mortes      = sum(1 for v in vals if v < -50)
        linhas.append({
            "tier":       t,
            "total":      len(vals),
            "win_rate":   pct(vencedores, len(vals)),
            "morte_rate": pct(mortes, len(vals)),
            "mediana":    med(vals),
            "p25":        p25(vals),
            "p75":        p75(vals),
            "media":      media(vals),
        })
    return linhas

def analise_por_carteira(rows):
    grupos = defaultdict(list)
    tipo_map = {}
    for r in rows:
        grupos[r["carteira"]].append(r["var_pico"])
        tipo_map[r["carteira"]] = r.get("tipo_carteira", "?")

    resultado = []
    for carteira, vals in sorted(grupos.items()):
        vencedores = sum(1 for v in vals if v > 50)
        resultado.append({
            "carteira":  carteira,
            "tipo":      tipo_map[carteira],
            "total":     len(vals),
            "win_rate":  pct(vencedores, len(vals)),
            "mediana":   med(vals),
            "media":     media(vals),
        })
    return resultado

def analise_multi_vs_single(rows):
    multi  = [r["var_pico"] for r in rows if r.get("is_multi")]
    single = [r["var_pico"] for r in rows if not r.get("is_multi")]

    def stats(vals, label):
        if not vals:
            return {"label": label, "total": 0, "win_rate": "—", "mediana": None, "media": None}
        venc = sum(1 for v in vals if v > 50)
        return {
            "label":    label,
            "total":    len(vals),
            "win_rate": pct(venc, len(vals)),
            "mediana":  med(vals),
            "media":    media(vals),
        }
    return [stats(multi, "Multi-carteira"), stats(single, "Single-carteira")]

def analise_categorias(rows):
    contagem = defaultdict(int)
    for r in rows:
        cat = r.get("categoria_final") or "?"
        # normaliza: remove variações de emoji
        contagem[cat] += 1
    return sorted(contagem.items(), key=lambda x: x[1], reverse=True)

def analise_score_vs_resultado(rows):
    """Para cada valor de score (0-10), mostra taxa de sucesso."""
    por_score = defaultdict(list)
    for r in rows:
        s = r.get("score_qualidade")
        if s is not None:
            por_score[s].append(r["var_pico"])
    resultado = []
    for score in sorted(por_score.keys()):
        vals = por_score[score]
        venc = sum(1 for v in vals if v > 50)
        resultado.append({
            "score":    score,
            "total":    len(vals),
            "win_rate": pct(venc, len(vals)),
            "mediana":  med(vals),
        })
    return resultado

FEATURES = [
    "score_qualidade", "mc_t0", "liq_t0", "volume_t0",
    "txns5m_t0", "buys_t0", "sells_t0", "net_momentum_t0",
    "idade_min", "ratio_vol_mc_t0", "holders_count",
    "top1_pct", "top10_pct", "bc_progress",
]

TARGETS = ["var_pico", "var_t1", "var_t2", "var_t3"]

def _pearson(xs, ys):
    n = len(xs)
    if n < 3: return None
    mx, my = sum(xs)/n, sum(ys)/n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx  = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy  = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0: return None
    return round(num / (dx * dy), 3)

def analise_correlacao_features(rows, target="var_pico"):
    """Correlação de Pearson entre cada feature e um target. Ordenado por |r|."""
    resultado = []
    for feat in FEATURES:
        pares = [(r[feat], r[target]) for r in rows
                 if r.get(feat) is not None and r.get(target) is not None]
        if len(pares) < 10:
            resultado.append({"feature": feat, "correlacao": None, "n": len(pares)})
            continue
        xs, ys = zip(*pares)
        resultado.append({"feature": feat, "correlacao": _pearson(list(xs), list(ys)), "n": len(pares)})
    return sorted(resultado, key=lambda x: abs(x["correlacao"] or 0), reverse=True)

def analise_correlacao_multi_targets(rows):
    """
    Tabela comparativa: para cada feature, mostra r contra var_pico, var_t1, var_t2, var_t3.
    Ordenado pelo maior |r| médio entre os targets com dados suficientes.
    """
    resultado = []
    for feat in FEATURES:
        entry = {"feature": feat, "targets": {}}
        rs = []
        for target in TARGETS:
            pares = [(r[feat], r[target]) for r in rows
                     if r.get(feat) is not None and r.get(target) is not None]
            n = len(pares)
            if n < 10:
                entry["targets"][target] = {"r": None, "n": n}
            else:
                xs, ys = zip(*pares)
                r_val = _pearson(list(xs), list(ys))
                entry["targets"][target] = {"r": r_val, "n": n}
                if r_val is not None:
                    rs.append(abs(r_val))
        entry["max_abs_r"] = max(rs) if rs else 0
        resultado.append(entry)
    return sorted(resultado, key=lambda x: x["max_abs_r"], reverse=True)

# ──────────────────────────────────────────────────────────
# FORMATAÇÃO TERMINAL
# ──────────────────────────────────────────────────────────
SEP = "─" * 60

def fmt_terminal(rows, ts):
    total = len(rows)
    linhas = []

    linhas.append(f"\n{'═'*60}")
    linhas.append(f"  DASHBOARD DE PERFORMANCE — {ts}")
    linhas.append(f"  Total de registros analisados: {total}")
    linhas.append(f"{'═'*60}")

    # 1. Por tier de score
    linhas.append(f"\n{'─'*60}")
    linhas.append("  PERFORMANCE POR TIER DE SCORE")
    linhas.append(f"{'─'*60}")
    linhas.append(f"  {'Tier':<22} {'N':>5}  {'Win>50%':>8}  {'Mortes<-50%':>11}  {'P25':>6}  {'Med':>6}  {'P75':>6}")
    linhas.append(f"  {'─'*22} {'─'*5}  {'─'*8}  {'─'*11}  {'─'*6}  {'─'*6}  {'─'*6}")
    for t in analise_por_tier(rows):
        linhas.append(
            f"  {t['tier']:<22} {t['total']:>5}  {t['win_rate']:>8}  {t['morte_rate']:>11}  "
            f"{str(t['p25'] or '—'):>6}  {str(t['mediana'] or '—'):>6}  {str(t['p75'] or '—'):>6}"
        )

    # 2. Score 0-10 vs win rate
    linhas.append(f"\n{'─'*60}")
    linhas.append("  SCORE 0-10 vs WIN RATE (var_pico > 50%)")
    linhas.append(f"{'─'*60}")
    for s in analise_score_vs_resultado(rows):
        barra_str = barra(float(s["win_rate"].rstrip("%")) if s["win_rate"] != "—" else 0, 100, 15)
        linhas.append(f"  Score {s['score']:>2}  {barra_str}  {s['win_rate']:>6}  (n={s['total']})")

    # 3. Por carteira
    linhas.append(f"\n{'─'*60}")
    linhas.append("  PERFORMANCE POR CARTEIRA")
    linhas.append(f"{'─'*60}")
    linhas.append(f"  {'Carteira':<12} {'Tipo':<6} {'N':>5}  {'Win>50%':>8}  {'Mediana':>8}  {'Média':>7}")
    linhas.append(f"  {'─'*12} {'─'*6} {'─'*5}  {'─'*8}  {'─'*8}  {'─'*7}")
    for c in analise_por_carteira(rows):
        linhas.append(
            f"  {c['carteira']:<12} {c['tipo']:<6} {c['total']:>5}  "
            f"{c['win_rate']:>8}  {str(c['mediana'] or '—'):>8}  {str(c['media'] or '—'):>7}"
        )

    # 4. Multi vs Single
    linhas.append(f"\n{'─'*60}")
    linhas.append("  MULTI-CARTEIRA vs SINGLE-CARTEIRA")
    linhas.append(f"{'─'*60}")
    linhas.append(f"  {'Tipo':<20} {'N':>5}  {'Win>50%':>8}  {'Mediana':>8}  {'Média':>7}")
    linhas.append(f"  {'─'*20} {'─'*5}  {'─'*8}  {'─'*8}  {'─'*7}")
    for m in analise_multi_vs_single(rows):
        linhas.append(
            f"  {m['label']:<20} {m['total']:>5}  "
            f"{m['win_rate']:>8}  {str(m['mediana'] or '—'):>8}  {str(m['media'] or '—'):>7}"
        )

    # 5. Categorias finais
    linhas.append(f"\n{'─'*60}")
    linhas.append("  DISTRIBUIÇÃO DE CATEGORIAS FINAIS")
    linhas.append(f"{'─'*60}")
    for cat, cnt in analise_categorias(rows):
        bar = barra(cnt, total, 15)
        linhas.append(f"  {bar}  {pct(cnt, total):>6}  {cnt:>4}×  {cat}")

    # 6. Correlação features (multi-target)
    linhas.append(f"\n{'─'*72}")
    linhas.append("  CORRELAÇÃO DAS FEATURES (r de Pearson por target)")
    linhas.append(f"{'─'*72}")
    linhas.append(f"  {'Feature':<22}  {'var_pico':>10}  {'var_t1':>10}  {'var_t2':>10}  {'var_t3':>10}")
    linhas.append(f"  {'─'*22}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*10}")

    def _fmt_r(entry, target):
        t = entry["targets"][target]
        if t["r"] is None:
            return f"{'—':>7}({t['n']:>3})"
        fa = abs(t["r"])
        star = "🔥" if fa >= 0.3 else ("⚡" if fa >= 0.15 else ("💧" if fa >= 0.05 else "  "))
        return f"{t['r']:>+.3f}({t['n']:>3}){star}"

    for f in analise_correlacao_multi_targets(rows):
        cols = "  ".join(_fmt_r(f, tgt) for tgt in TARGETS)
        linhas.append(f"  {f['feature']:<22}  {cols}")

    linhas.append(f"\n{'═'*60}\n")
    return "\n".join(linhas)

# ──────────────────────────────────────────────────────────
# FORMATAÇÃO TELEGRAM (resumo compacto)
# ──────────────────────────────────────────────────────────
def fmt_telegram(rows, ts):
    total = len(rows)
    tiers = analise_por_tier(rows)
    multi = analise_multi_vs_single(rows)

    msg = f"📊 <b>DASHBOARD DE PERFORMANCE</b>\n"
    msg += f"<i>{ts} — {total} registros</i>\n\n"

    msg += "<b>Score por Tier</b>\n"
    for t in tiers:
        msg += f"{t['tier']} | n={t['total']} | win={t['win_rate']} | med={t['mediana'] or '—'}%\n"

    msg += "\n<b>Multi vs Single</b>\n"
    for m in multi:
        msg += f"• {m['label']}: n={m['total']} | win={m['win_rate']} | med={m['mediana'] or '—'}%\n"

    multi_corr = analise_correlacao_multi_targets(rows)
    msg += "\n<b>Top features (melhor r em qualquer target)</b>\n"
    for f in multi_corr[:5]:
        parts = []
        for tgt in TARGETS:
            t = f["targets"][tgt]
            if t["r"] is not None:
                parts.append(f"{tgt}:{t['r']:+.3f}(n={t['n']})")
        if parts:
            msg += f"• {f['feature']}: {' | '.join(parts)}\n"

    return msg

def telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT,
        "text": msg,
        "parse_mode": "HTML"
    }, timeout=10)

# ──────────────────────────────────────────────────────────
# FORMATAÇÃO HTML
# ──────────────────────────────────────────────────────────
def fmt_html(rows, ts):
    total = len(rows)
    tiers = analise_por_tier(rows)
    por_carteira = analise_por_carteira(rows)
    multi = analise_multi_vs_single(rows)
    cats = analise_categorias(rows)
    scores = analise_score_vs_resultado(rows)

    def tabela(cabecalho, dados):
        cols = list(cabecalho)
        html = "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-family:monospace'>\n"
        html += "<tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>\n"
        for row in dados:
            html += "<tr>" + "".join(f"<td>{row.get(c, '—')}</td>" for c in cols) + "</tr>\n"
        html += "</table>\n"
        return html

    html = f"""<!DOCTYPE html>
<html lang='pt-BR'>
<head>
<meta charset='UTF-8'>
<title>Dashboard de Performance — {ts}</title>
<style>
  body {{ font-family: monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }}
  h1, h2 {{ color: #58a6ff; }}
  table {{ margin-bottom: 20px; }}
  th {{ background: #21262d; color: #58a6ff; }}
  td, th {{ border-color: #30363d; padding: 6px 12px; }}
  tr:nth-child(even) {{ background: #161b22; }}
</style>
</head>
<body>
<h1>Dashboard de Performance</h1>
<p>{ts} — {total} registros analisados</p>

<h2>Performance por Tier de Score</h2>
"""
    html += tabela(
        {"tier":"Tier","total":"N","win_rate":"Win >50%","morte_rate":"Mortes <-50%","p25":"P25%","mediana":"Mediana%","p75":"P75%"},
        tiers
    )

    html += "<h2>Score 0-10 vs Win Rate</h2>\n"
    html += tabela(
        {"score":"Score","total":"N","win_rate":"Win >50%","mediana":"Mediana%"},
        scores
    )

    html += "<h2>Performance por Carteira</h2>\n"
    html += tabela(
        {"carteira":"Carteira","tipo":"Tipo","total":"N","win_rate":"Win >50%","mediana":"Mediana%","media":"Média%"},
        por_carteira
    )

    html += "<h2>Multi-carteira vs Single</h2>\n"
    html += tabela(
        {"label":"Tipo","total":"N","win_rate":"Win >50%","mediana":"Mediana%","media":"Média%"},
        multi
    )

    html += "<h2>Categorias Finais</h2>\n"
    cat_rows = [{"categoria": c, "n": n, "pct": pct(n, total)} for c, n in cats]
    html += tabela({"categoria":"Categoria","n":"N","pct":"%"}, cat_rows)

    html += "<h2>Correlação das Features por Target</h2>\n"
    multi_corr = analise_correlacao_multi_targets(rows)

    def _fmt_cell(entry, target):
        t = entry["targets"][target]
        if t["r"] is None:
            return f"— (n={t['n']})"
        fa = abs(t["r"])
        star = " 🔥" if fa >= 0.3 else (" ⚡" if fa >= 0.15 else (" 💧" if fa >= 0.05 else ""))
        return f"{t['r']:+.3f} (n={t['n']}){star}"

    corr_rows = [
        {
            "feature":   f["feature"],
            "var_pico":  _fmt_cell(f, "var_pico"),
            "var_t1":    _fmt_cell(f, "var_t1"),
            "var_t2":    _fmt_cell(f, "var_t2"),
            "var_t3":    _fmt_cell(f, "var_t3"),
        }
        for f in multi_corr
    ]
    html += tabela({"feature":"Feature","var_pico":"var_pico","var_t1":"var_t1","var_t2":"var_t2","var_t3":"var_t3"}, corr_rows)

    html += "</body></html>"
    return html

# ──────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true", help="Envia resumo ao Telegram")
    parser.add_argument("--html",     action="store_true", help="Salva analise.html")
    args = parser.parse_args()

    print("Conectando ao banco de dados...")
    rows = buscar_registros()
    if not rows:
        print("Nenhum registro finalizado encontrado.")
        return

    ts = datetime.now().strftime("%d/%m/%Y %H:%M")

    # Sempre imprime no terminal
    print(fmt_terminal(rows, ts))

    if args.telegram:
        msg = fmt_telegram(rows, ts)
        telegram(msg)
        print("Resumo enviado ao Telegram.")

    if args.html:
        html = fmt_html(rows, ts)
        path = os.path.join(os.path.dirname(__file__), "analise.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"HTML salvo em: {path}")

if __name__ == "__main__":
    main()
