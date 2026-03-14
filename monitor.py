import requests
import pandas as pd
import time
import threading
import os
import json
import pickle
import html as html_lib
import psycopg2
import psycopg2.extras
import numpy as np
from datetime import datetime
from flask import Flask, request, jsonify
# v6.3 + PostgreSQL — dados persistentes entre restarts

# ── ML Score ─────────────────────────────────────────────────────────────────
_ML_DIR = os.path.dirname(os.path.abspath(__file__))

def _carregar_modelos_ml():
    try:
        with open(os.path.join(_ML_DIR, 'modelo_binario.pkl'), 'rb') as f:
            cls = pickle.load(f)
        with open(os.path.join(_ML_DIR, 'feature_cols.pkl'), 'rb') as f:
            feats = pickle.load(f)
        log("[ML] Modelos carregados com sucesso")
        return cls, feats
    except Exception as e:
        print(f"[ML] Modelos não encontrados (treinar ml_score.ipynb primeiro): {e}")
        return None, None

_ML_MODEL  = None
_ML_FEATS  = None

def calcular_ml_proba(mc_t0, liq_t0, volume_t0, buys, sells, idade_min,
                      ratio_vol_mc, net_momentum, holders_count,
                      top10_pct, is_multi, dex, bc_progress, score_qualidade):
    """Retorna P(var_pico > 50%) entre 0.0 e 1.0. None se modelo indisponível."""
    if _ML_MODEL is None:
        return None
    try:
        total = (buys or 0) + (sells or 0)
        ratio_bs = (buys or 0) / total if total > 0 else np.nan
        row = {
            'bc_progress':     bc_progress if bc_progress is not None else np.nan,
            'ratio_bs':        ratio_bs,
            'log_mc':          np.log1p(max(mc_t0 or 0, 0)),
            'log_liq':         np.log1p(max(liq_t0 or 0, 0)),
            'log_vol':         np.log1p(max(volume_t0 or 0, 0)),
            'idade_min':       idade_min if idade_min is not None else np.nan,
            'ratio_vol_mc_t0': ratio_vol_mc if ratio_vol_mc is not None else np.nan,
            'net_momentum_t0': net_momentum or 0,
            'holders_count':   holders_count if holders_count is not None else np.nan,
            'top10_pct':       top10_pct if top10_pct is not None else np.nan,
            'is_multi':        int(bool(is_multi)),
            'is_pumpfun':      int(dex == 'pumpfun'),
            'score_qualidade': score_qualidade if score_qualidade is not None else np.nan,
        }
        X = pd.DataFrame([row])[_ML_FEATS]
        proba = _ML_MODEL.predict_proba(X)[0, 1]
        return round(float(proba), 3)
    except Exception as e:
        print(f"[ML] Erro ao calcular proba: {e}")
        return None
HELIUS_API_KEY = "4f586430-90ef-4c8f-9800-b98bfe5f1151"
TELEGRAM_TOKEN = "8319320909:AAFnhGkFS1YxhthhE4RolutJScEjBCjIvrA"
TELEGRAM_CHAT  = "-5284184650"
DASHBOARD_KEY  = "neide12"
DATABASE_URL   = os.environ.get("DATABASE_URL", "postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway")
CARTEIRAS = {
    "GijFWw4oNyh9ko3FaZforNsi3jk6wDovARpkKahPD4o5": "carteira_A",
    "ANfB2knFb7pC7jKadHnSP4xKZ31KJGNLhWRo89LWsFeW": "carteira_B",
    "43C9gHfJ7YgqKv5ft3hodFgumydv1nEiNHD1PuANufk5": "carteira_C",
    "EvGpkcSBfhp5K9SNP48wVtfNXdKYRBiK3kvMkB66kU3Q": "carteira_D",
}
TIPO_CARTEIRA = {
    "carteira_A": "bot",
    "carteira_B": "bot",
    "carteira_C": "humano",
    "carteira_D": "humano",
}
TOKENS_IGNORAR = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "8S4Hk9bMLTTCBzBrFGSRcPbHiWbVXKpmWHvEMPEELXXt",
    "11111111111111111111111111111111",
}
# Estado em memória (cache — o banco é a fonte de verdade)
estado = {
    nome: {
        "tokens_conhecidos": set(),
        "registros":         [],
        "pendentes":         {},
    }
    for nome in set(CARTEIRAS.values())
}
mints_globais     = {}
signatures_vistas = set()
app = Flask(__name__)
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response
# ══════════════════════════════════════════════════════════
# BANCO DE DADOS
# ══════════════════════════════════════════════════════════
def get_conn():
    return psycopg2.connect(DATABASE_URL)
def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Migração: adicionar colunas se não existirem
            for col_name, col_type in [
                ("holders_t1","INT"),("top1_t1","FLOAT"),("top10_t1","FLOAT"),("dev_saiu_t1","BOOLEAN"),
                ("holders_t2","INT"),("top1_t2","FLOAT"),("top10_t2","FLOAT"),("dev_saiu_t2","BOOLEAN"),
                ("holders_t3","INT"),("top1_t3","FLOAT"),("top10_t3","FLOAT"),("dev_saiu_t3","BOOLEAN"),
                ("ml_proba","FLOAT"),
            ]:
                try:
                    cur.execute(f"ALTER TABLE registros ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
                except Exception:
                    pass
            conn.commit()
            # Tabela de histórico de deployers
            cur.execute("""
                CREATE TABLE IF NOT EXISTS deployers (
                    dev_wallet      TEXT PRIMARY KEY,
                    tokens_total    INT DEFAULT 0,
                    tokens_rug      INT DEFAULT 0,
                    tokens_migrou   INT DEFAULT 0,
                    rug_rate        FLOAT DEFAULT 0,
                    classificacao   TEXT DEFAULT 'desconhecido',
                    ultima_update   TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS registros (
                    id              SERIAL PRIMARY KEY,
                    data_compra     TIMESTAMP,
                    carteira        TEXT,
                    tipo_carteira   TEXT,
                    token_mint      TEXT,
                    nome            TEXT,
                    dex             TEXT,
                    fonte_dados     TEXT,
                    quantidade      FLOAT,
                    signature       TEXT,
                    tipo            TEXT,
                    is_multi        BOOLEAN DEFAULT FALSE,
                    p_t0            FLOAT,
                    mc_t0           FLOAT,
                    liq_t0          FLOAT,
                    volume_t0       FLOAT,
                    txns5m_t0       INT,
                    buys_t0         INT,
                    sells_t0        INT,
                    net_momentum_t0 INT,
                    idade_min       FLOAT,
                    token_antigo    TEXT,
                    ratio_vol_mc_t0 FLOAT,
                    score_qualidade INT,
                    holders_count   INT,
                    top1_pct        FLOAT,
                    top10_pct       FLOAT,
                    dev_saiu        BOOLEAN,
                    bc_progress     FLOAT,
                    holders_t1      INT,
                    top1_t1         FLOAT,
                    top10_t1        FLOAT,
                    dev_saiu_t1     BOOLEAN,
                    holders_t2      INT,
                    top1_t2         FLOAT,
                    top10_t2        FLOAT,
                    dev_saiu_t2     BOOLEAN,
                    holders_t3      INT,
                    top1_t3         FLOAT,
                    top10_t3        FLOAT,
                    dev_saiu_t3     BOOLEAN,
                    p_t1 FLOAT, mc_t1 FLOAT, liq_t1 FLOAT, volume_t1 FLOAT,
                    txns5m_t1 INT, buys_t1 INT, sells_t1 INT,
                    ratio_vol_mc_t1 FLOAT, var_t1 FLOAT, veredito_t1 TEXT,
                    p_t2 FLOAT, mc_t2 FLOAT, liq_t2 FLOAT, volume_t2 FLOAT,
                    txns5m_t2 INT, buys_t2 INT, sells_t2 INT,
                    ratio_vol_mc_t2 FLOAT, var_t2 FLOAT, veredito_t2 TEXT,
                    p_t3 FLOAT, mc_t3 FLOAT, liq_t3 FLOAT, volume_t3 FLOAT,
                    txns5m_t3 INT, buys_t3 INT, sells_t3 INT,
                    ratio_vol_mc_t3 FLOAT, var_t3 FLOAT, veredito_t3 TEXT,
                    mc_pico         FLOAT,
                    var_pico        FLOAT,
                    categoria_final TEXT,
                    var_desde_compra FLOAT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_token_mint ON registros(token_mint)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_carteira ON registros(carteira)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_data ON registros(data_compra)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signatures (
                    sig TEXT PRIMARY KEY
                )
            """)
        conn.commit()
    log("✅ Banco de dados inicializado")
def db_insert(reg):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO registros (
                    data_compra, carteira, tipo_carteira, token_mint, nome, dex,
                    fonte_dados, quantidade, signature, tipo, is_multi,
                    p_t0, mc_t0, liq_t0, volume_t0, txns5m_t0, buys_t0, sells_t0,
                    net_momentum_t0, idade_min, token_antigo, ratio_vol_mc_t0,
                    score_qualidade, holders_count, top1_pct, top10_pct,
                    dev_saiu, bc_progress, mc_pico, categoria_final,
                    var_desde_compra, ml_proba
                ) VALUES (
                    %(data_compra)s, %(carteira)s, %(tipo_carteira)s, %(token_mint)s,
                    %(nome)s, %(dex)s, %(fonte_dados)s, %(quantidade)s, %(signature)s,
                    %(tipo)s, %(is_multi)s, %(p_t0)s, %(mc_t0)s, %(liq_t0)s,
                    %(volume_t0)s, %(txns5m_t0)s, %(buys_t0)s, %(sells_t0)s,
                    %(net_momentum_t0)s, %(idade_min)s, %(token_antigo)s,
                    %(ratio_vol_mc_t0)s, %(score_qualidade)s, %(holders_count)s,
                    %(top1_pct)s, %(top10_pct)s, %(dev_saiu)s, %(bc_progress)s,
                    %(mc_pico)s, %(categoria_final)s, %(var_desde_compra)s,
                    %(ml_proba)s
                ) RETURNING id
            """, reg)
            row_id = cur.fetchone()[0]
        conn.commit()
    return row_id
def db_update_checkpoint(row_id, checkpoint, preco, mc, liq, volume, txns, buys, sells, ratio, var, veredito, mc_pico):
    n = checkpoint  # t1, t2, t3
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE registros SET
                    p_{n}=%s, mc_{n}=%s, liq_{n}=%s, volume_{n}=%s,
                    txns5m_{n}=%s, buys_{n}=%s, sells_{n}=%s,
                    ratio_vol_mc_{n}=%s, var_{n}=%s, veredito_{n}=%s,
                    mc_pico=%s
                WHERE id=%s
            """, (preco, mc, liq, volume, txns, buys, sells, ratio, var, veredito, mc_pico, row_id))
        conn.commit()
def db_update_holders(db_id, checkpoint, holders, top1, top10, dev_saiu):
    """Salva dados de holders no checkpoint especificado (t1, t2, t3)"""
    try:
        with get_conn() as conn:  # CORRIGIDO: era get_db()
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE registros SET
                        holders_{checkpoint} = %s,
                        top1_{checkpoint}    = %s,
                        top10_{checkpoint}   = %s,
                        dev_saiu_{checkpoint}= %s
                    WHERE id = %s
                """, (holders, top1, top10, dev_saiu, db_id))
            conn.commit()
    except Exception as e:
        log(f"db_update_holders erro: {e}")
def db_update_final(row_id, mc_pico, var_pico, categoria):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE registros SET mc_pico=%s, var_pico=%s, categoria_final=%s
                WHERE id=%s
            """, (mc_pico, var_pico, categoria, row_id))
        conn.commit()
def db_update_multi(row_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE registros SET is_multi=TRUE WHERE id=%s", (row_id,))
        conn.commit()
def db_carregar_estado():
    """Carrega dados do banco para memória ao iniciar."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT sig FROM signatures")
                for row in cur.fetchall():
                    signatures_vistas.add(row["sig"])
                cur.execute("SELECT * FROM registros ORDER BY data_compra")
                rows = cur.fetchall()
        log(f"📂 Carregando {len(rows)} registros do banco...")
        for row in rows:
            reg = dict(row)
            # Normaliza nomes de colunas para compatibilidade com código existente
            reg["var_t1_%"] = reg.pop("var_t1", None)
            reg["var_t2_%"] = reg.pop("var_t2", None)
            reg["var_t3_%"] = reg.pop("var_t3", None)
            reg["var_pico_%"] = reg.pop("var_pico", None)
            if reg.get("data_compra"):
                reg["data_compra"] = reg["data_compra"].strftime("%Y-%m-%d %H:%M:%S")
            nome = reg.get("carteira")
            if nome and nome in estado:
                idx = len(estado[nome]["registros"])
                estado[nome]["registros"].append(reg)
                estado[nome]["tokens_conhecidos"].add(reg["token_mint"])
                # Tokens ainda pendentes (sem categoria final ou aguardando)
                if reg.get("categoria_final") == "⏳ aguardando" and reg.get("tipo") == "COMPRA":
                    mint = reg["token_mint"]
                    db_id = reg["id"]
                    # Calcular quanto tempo passou desde a compra
                    try:
                        dt_compra = datetime.strptime(reg["data_compra"], "%Y-%m-%d %H:%M:%S")
                        segundos_passados = (datetime.now() - dt_compra).total_seconds()
                    except:
                        segundos_passados = 9999
                    # Se passou mais de 2 horas, finalizar como sem dados
                    if segundos_passados > 7200:
                        log(f"⚠️  Token preso há {segundos_passados/3600:.1f}h — finalizando: {reg.get('nome','?')}")
                        cat = "❓ DADOS INCOMPLETOS — restart perdeu checkpoints"
                        try:
                            db_update_final(db_id, reg.get("mc_pico") or reg.get("mc_t0") or 0, None, cat)
                            reg["categoria_final"] = cat
                        except Exception as e:
                            log(f"⚠️  Erro ao finalizar token preso: {e}")
                        continue  # não adiciona aos pendentes
                    # Reagendar checkpoints restantes
                    estado[nome]["pendentes"][mint] = {"idx": idx, "db_id": db_id}
                    # Checkpoints principais
                    if reg.get("mc_t1") is None:
                        delay = max(0, 300 - segundos_passados)
                        threading.Timer(delay, checar_checkpoint, args=[nome, mint, "t1"]).start()
                    if reg.get("mc_t2") is None:
                        delay = max(0, 900 - segundos_passados)
                        threading.Timer(delay, checar_checkpoint, args=[nome, mint, "t2"]).start()
                    if reg.get("mc_t3") is None:
                        delay = max(0, 2700 - segundos_passados)
                        threading.Timer(delay, checar_checkpoint, args=[nome, mint, "t3"]).start()
                    # Snapshots intermediários de pico
                    if segundos_passados < 120:
                        threading.Timer(max(0, 120 - segundos_passados), atualizar_pico, args=[nome, mint, "2min"]).start()
                    if segundos_passados < 600:
                        threading.Timer(max(0, 600 - segundos_passados), atualizar_pico, args=[nome, mint, "10min"]).start()
                    if segundos_passados < 1500:
                        threading.Timer(max(0, 1500 - segundos_passados), atualizar_pico, args=[nome, mint, "25min"]).start()
        log(f"✅ Estado restaurado — {sum(len(estado[n]['registros']) for n in estado)} registros em memória")
    except Exception as e:
        log(f"⚠️  Erro ao carregar estado do banco: {e}")
def db_sig_add(sig):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO signatures(sig) VALUES(%s) ON CONFLICT DO NOTHING", (sig,))
            conn.commit()
    except:
        pass
def he(s):
    """Escapa caracteres HTML especiais em strings fornecidas por APIs externas."""
    return html_lib.escape(str(s)) if s else ""
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
def telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            log(f"Telegram erro {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log(f"Telegram erro: {e}")
def telegram_documento(caminho, caption=""):
    try:
        with open(caminho, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT, "caption": caption, "parse_mode": "HTML"},
                files={"document": f},
                timeout=30,
            )
    except Exception as e:
        log(f"Telegram doc erro: {e}")
def calcular_momentum(buys, sells):
    buys = buys or 0
    sells = sells or 0
    total = buys + sells
    if total == 0:
        return None, 0
    net = buys - sells
    blocos = round(buys / total * 8)
    barra = "🟢" * blocos + "⬜" * (8 - blocos)
    sinal = "+" if net >= 0 else ""
    return f"{barra} {sinal}{net} ({buys}B / {sells}S)", net
def classificar_momentum(net, total):
    if not total: return ""
    if net >= 10:  return "🔥 Comprando forte"
    if net >= 5:   return "📈 Pressão compradora"
    if net >= 0:   return "➡️  Equilibrado"
    if net >= -5:  return "📉 Pressão vendedora"
    return "🧊 Vendendo forte"
def calcular_score(mc_t0, liq_t0, txns, ratio_vol_mc, idade_min, dex,
                   holders_count=None, top10_pct=None, buys=0, sells=0,
                   dev_classif=None, hora_utc=None, is_multi=False,
                   bc_progress=None, top1_pct=None, carteira=None):
    # v8.0 — score recalibrado com base em 1004 registros reais (14/03/2026)
    # Mudanças vs v7:
    #   mc_t0: faixa ideal corrigida para 5k-15k (+3), 30-60k deixou de ser sweet spot
    #   idade_min: janela 45-60min separada (+3, win 83.3%)
    #   bc_progress: INVERTIDO — 40-80 é o melhor (não <30)
    #   ratio_bs: gradiente ajustado, ≥65% = +2
    #   top1_pct: adicionado — >50% penaliza (-2)
    #   carteira: bônus para carteira_C (+2) e carteira_D (+1)
    #   is_multi, is_pumpfun, hora_utc: removidos (importância ~0)
    #   dev_classif: confiavel mantido em +2
    score = 0
    if dev_classif == "serial_rugger":
        return 0, "💀", "SERIAL RUGGER — BLOQUEADO"

    # 1. MARKET CAP — faixa ideal corrigida (dados: 5k-15k = 45.9% win, melhor faixa)
    if mc_t0:
        if 5000 <= mc_t0 < 15000:    score += 3   # melhor faixa (win 45.9%)
        elif 15000 <= mc_t0 < 30000: score += 1   # acima da média (win 36.0%)
        elif mc_t0 < 5000:           score -= 1   # muito pequeno (win 30.7%)
        elif 30000 <= mc_t0 < 120000: score += 0  # neutro (win ~33%)
        elif mc_t0 >= 120000:        score -= 2   # grande demais (win 5.6%)

    # 2. IDADE DO TOKEN — janela dourada 45-60min descoberta nos dados
    if idade_min is not None:
        if 45 <= idade_min <= 60:    score += 3   # win 83.3%
        elif 25 <= idade_min < 45:   score += 2   # win 55.2%
        elif 10 <= idade_min < 25:   score += 1   # win 43.3%
        elif idade_min < 10:         score += 0   # muito novo, neutro (win 33.9%)
        elif idade_min > 120:        score -= 1   # token velho (win 35.7%)

    # 3. BC PROGRESS — relação NÃO é linear: 40-80 é o ideal
    if bc_progress is not None:
        if 40 <= bc_progress <= 80:  score += 2   # win ~55% (era penalizado no v7!)
        elif bc_progress > 80:       score -= 1   # perto do topo (win 31.8%)
        # <40: neutro (win 37.3%)

    # 4. RATIO BUY/SELL — gradiente positivo claro nos dados
    total_txns = (buys or 0) + (sells or 0)
    if total_txns > 0:
        ratio_bs = buys / total_txns
        if ratio_bs >= 0.65:         score += 2   # win 42.9%
        elif ratio_bs >= 0.55:       score += 1   # win 38.3%
        elif ratio_bs >= 0.40:       score += 0   # neutro (win 36.3%)
        else:                        score -= 1   # win 32.7%

    # 5. TOP1_PCT — concentração do dev: >50% é sinal negativo
    if top1_pct is not None:
        if top1_pct > 50:            score -= 2   # win 23.2% — concentração perigosa
        # 10-50%: neutro (~36-38% win)

    # 6. CARTEIRA — humanas têm performance muito superior
    if carteira == "carteira_C":     score += 2   # win 61.9%
    elif carteira == "carteira_D":   score += 1   # win 44.8%
    # carteira_A/B: neutro (win 34.0%)

    # 7. NET MOMENTUM
    if total_txns > 0:
        net = (buys or 0) - (sells or 0)
        if net >= 20:   score += 1
        elif net < 0:   score -= 1

    # 8. HOLDERS
    if holders_count is not None:
        if holders_count >= 200:     score += 1
        elif holders_count < 80:     score -= 1

    # 9. DEV HISTORY
    if dev_classif == "confiavel":   score += 2
    elif dev_classif == "rugger":    score -= 3

    # 10. RATIO VOL/MC — penalidade apenas para volume muito baixo
    if ratio_vol_mc is not None:
        if ratio_vol_mc < 0.5:       score -= 1

    # Removidos (importância ~0): is_pumpfun, is_multi, hora_utc

    score = max(0, min(10, score))
    if score >= 7:   return score, "🟢", "ALTA CONFIANÇA"
    elif score >= 4: return score, "🟡", "MODERADO"
    else:            return score, "🔴", "BAIXA CONFIANÇA"
def veredito_parcial(mc_anterior, mc_atual, tempo):
    if not mc_anterior or not mc_atual or mc_anterior == 0:
        return "❓ sem dados"
    var = (mc_atual - mc_anterior) / mc_anterior * 100
    if   var >  200: return f"🚀 +{var:.0f}% em {tempo} — EXPLOSIVO"
    elif var >   50: return f"📈 +{var:.0f}% em {tempo} — FORTE"
    elif var >   10: return f"📊 +{var:.0f}% em {tempo} — SUBINDO"
    elif var >  -10: return f"➡️  {var:.0f}% em {tempo} — ESTÁVEL"
    elif var >  -50: return f"📉 {var:.0f}% em {tempo} — FRAQUEJANDO"
    else:            return f"💀 {var:.0f}% em {tempo} — COLAPSANDO"
def categoria_final(reg):
    mc0 = reg.get("mc_t0") or 0
    mc1 = reg.get("mc_t1") or 0
    mc2 = reg.get("mc_t2") or 0
    mc3 = reg.get("mc_t3") or 0
    if mc0 == 0: return "❓ SEM DADOS"
    var_t1 = reg.get("var_t1_%")
    var_t2 = reg.get("var_t2_%")
    var_t3 = reg.get("var_t3_%")
    t2_morreu = mc2 == 0 or (var_t2 is not None and var_t2 < -70)
    t3_morreu = mc3 == 0 or (var_t3 is not None and var_t3 < -70)
    if var_t1 and var_t1 > 50 and t2_morreu:
        return "🎯 PUMP & DUMP — Morreu após T1"
    if var_t1 and var_t1 > 50 and mc3 > 0 and t3_morreu:
        return "🎯 PUMP & DUMP — Morreu após pico"
    pico = max(mc1, mc2, mc3)
    var_pico  = (pico - mc0) / mc0 * 100 if mc0 else 0
    var_final = (mc3  - mc0) / mc0 * 100 if mc0 and mc3 else None
    if   var_pico > 200 and var_final and var_final >  100: return "🏆 VENCEDOR — Subiu forte e manteve"
    elif var_pico > 200 and var_final and var_final <    0: return "🎯 PUMP & DUMP — Subiu e colapsou"
    elif var_pico >  50 and var_final and var_final >   20: return "📈 BOM TRADE — Crescimento sólido"
    elif var_pico >  50 and var_final and var_final <  -20: return "⚠️  ARMADILHA — Pico rápido e queda"
    elif var_final and var_final >  20:                     return "📊 CRESCIMENTO ESTÁVEL"
    elif var_final and var_final > -20:                     return "➡️  LATERAL — Pouco movimento"
    elif var_final is not None:                             return "💀 MORREU — Queda consistente"
    else:                                                   return "❓ DADOS INCOMPLETOS"
def get_dados_token(mint):
    preco = mc = liq = volume = 0
    dex = nome = "?"
    txns_5min = buys_5min = sells_5min = 0
    idade_min = None
    fonte = "dexscreener"
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=8)
        pairs = r.json().get("pairs") or []
        if pairs:
            par        = sorted(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0, reverse=True)[0]
            preco      = float(par["priceUsd"]) if par.get("priceUsd") else None
            mc         = par.get("marketCap") or 0
            liq        = par.get("liquidity", {}).get("usd") or 0
            volume     = par.get("volume", {}).get("h24") or 0
            dex        = par.get("dexId", "?")
            nome       = par.get("baseToken", {}).get("name", "?")
            m5         = par.get("txns", {}).get("m5", {})
            buys_5min  = m5.get("buys", 0)
            sells_5min = m5.get("sells", 0)
            txns_5min  = buys_5min + sells_5min
            criado_ts  = par.get("pairCreatedAt")
            if criado_ts:
                idade_min = round((time.time() - criado_ts / 1000) / 60, 1)
            if mc > 0:
                return preco, mc, liq, volume, dex, nome, txns_5min, idade_min, fonte, buys_5min, sells_5min
    except:
        pass
    fonte = "pumpfun"
    dex   = "pumpfun"
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
            json={"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": mint}},
            timeout=8,
        )
        if r.status_code == 200:
            asset     = r.json().get("result", {})
            nome      = asset.get("content", {}).get("metadata", {}).get("name", "?")
            criado_ts = asset.get("createdAt")
            if criado_ts:
                idade_min = round((time.time() - criado_ts) / 60, 1)
    except:
        pass
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]},
            timeout=8,
        )
        if r.status_code == 200:
            result      = r.json().get("result", {}).get("value", {})
            supply      = float(result.get("uiAmount", 0))
            sol_price   = get_sol_price()
            tokens_sold = max(0, 1_000_000_000 - supply)
            virtual_sol = 30 + (tokens_sold / 1_000_000_000) * 800
            preco_sol   = virtual_sol / (793_000_000 - tokens_sold) if tokens_sold < 793_000_000 else 0
            preco       = preco_sol * sol_price if sol_price else None
            mc          = round(preco * 1_000_000_000, 0) if preco else 0
            liq         = round(virtual_sol * sol_price, 0) if sol_price else 0
    except:
        pass
    return preco, mc, liq, volume, dex, nome, txns_5min, idade_min, fonte, buys_5min, sells_5min
def get_sol_price():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=5,
        )
        return r.json().get("solana", {}).get("usd", 0)
    except:
        return 0
def get_dev_wallet(mint):
    HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    try:
        r = requests.post(HELIUS_URL,
            json={"jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress",
                  "params":[mint, {"limit": 5, "commitment": "confirmed"}]},
            timeout=8)
        if r.status_code != 200:
            return None
        sigs = r.json().get("result", [])
        if not sigs:
            return None
        sig_criacao = sigs[-1].get("signature")
        if not sig_criacao:
            return None
        r2 = requests.post(HELIUS_URL,
            json={"jsonrpc":"2.0","id":1,"method":"getTransaction",
                  "params":[sig_criacao, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]},
            timeout=8)
        if r2.status_code != 200:
            return None
        tx = r2.json().get("result", {})
        if not tx:
            return None
        account_keys = tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
        if account_keys:
            dev = account_keys[0].get("pubkey") if isinstance(account_keys[0], dict) else account_keys[0]
            return dev
    except Exception as e:
        log(f"get_dev_wallet erro: {e}")
    return None
def get_deployer_history(dev_wallet):
    if not dev_wallet:
        return None, None, None, "desconhecido"
    HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    try:
        # Verificar cache no banco primeiro
        with get_conn() as conn:  # CORRIGIDO: era get_db()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT tokens_total, tokens_rug, rug_rate, classificacao, ultima_update
                    FROM deployers WHERE dev_wallet = %s
                """, (dev_wallet,))
                row = cur.fetchone()
                if row:
                    from datetime import datetime, timezone
                    ultima = row[4]
                    if ultima and (datetime.now() - ultima.replace(tzinfo=None)).seconds < 21600:
                        return row[0], row[1], row[2], row[3]
        r = requests.post(HELIUS_URL,
            json={"jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress",
                  "params":[dev_wallet, {"limit": 100, "commitment": "confirmed"}]},
            timeout=10)
        if r.status_code != 200:
            return None, None, None, "desconhecido"
        sigs = r.json().get("result", [])
        if not sigs:
            return 0, 0, 0.0, "novo"
        tokens_lancados = []
        for sig_info in sigs[:50]:
            sig = sig_info.get("signature")
            if not sig:
                continue
            try:
                r2 = requests.post(HELIUS_URL,
                    json={"jsonrpc":"2.0","id":1,"method":"getTransaction",
                          "params":[sig, {"encoding":"jsonParsed",
                                         "maxSupportedTransactionVersion":0}]},
                    timeout=8)
                if r2.status_code != 200:
                    continue
                tx = r2.json().get("result", {})
                if not tx:
                    continue
                log_msgs = tx.get("meta", {}).get("logMessages", [])
                is_pumpfun_create = any(
                    "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke" in m
                    for m in log_msgs
                ) and any("create" in m.lower() for m in log_msgs)
                if not is_pumpfun_create:
                    continue
                post_balances = tx.get("meta", {}).get("postTokenBalances", [])
                for bal in post_balances:
                    mint_addr = bal.get("mint")
                    if mint_addr and mint_addr not in tokens_lancados:
                        tokens_lancados.append(mint_addr)
                        break
            except:
                continue
        if not tokens_lancados:
            return 0, 0, 0.0, "novo"
        tokens_rug = 0
        for mint_addr in tokens_lancados[:20]:
            try:
                r3 = requests.post(HELIUS_URL,
                    json={"jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress",
                          "params":[mint_addr, {"limit": 10}]},
                    timeout=8)
                if r3.status_code != 200:
                    continue
                sigs_token = r3.json().get("result", [])
                if len(sigs_token) < 2:
                    tokens_rug += 1
                    continue
                primeiro_ts = sigs_token[-1].get("blockTime", 0)
                ultimo_ts   = sigs_token[0].get("blockTime", 0)
                tempo_vida_min = (ultimo_ts - primeiro_ts) / 60 if primeiro_ts else 999
                if tempo_vida_min < 60 and len(sigs_token) < 5:
                    tokens_rug += 1
            except:
                continue
        total = len(tokens_lancados)
        rug_rate = round(tokens_rug / total, 2) if total > 0 else 0
        if total == 0:
            classif = "novo"
        elif rug_rate >= 0.80:
            classif = "serial_rugger"
        elif rug_rate >= 0.50:
            classif = "rugger"
        elif rug_rate >= 0.20:
            classif = "misto"
        else:
            classif = "confiavel"
        log(f"  [deployer] {dev_wallet[:8]}... | tokens={total} rugs={tokens_rug} rate={rug_rate:.0%} → {classif}")
        try:
            with get_conn() as conn:  # CORRIGIDO: era get_db()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO deployers (dev_wallet, tokens_total, tokens_rug, rug_rate, classificacao, ultima_update)
                        VALUES (%s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (dev_wallet) DO UPDATE SET
                            tokens_total = EXCLUDED.tokens_total,
                            tokens_rug   = EXCLUDED.tokens_rug,
                            rug_rate     = EXCLUDED.rug_rate,
                            classificacao= EXCLUDED.classificacao,
                            ultima_update= NOW()
                    """, (dev_wallet, total, tokens_rug, rug_rate, classif))
                conn.commit()
        except Exception as e:
            log(f"  deployer cache erro: {e}")
        return total, tokens_rug, rug_rate, classif
    except Exception as e:
        log(f"get_deployer_history erro: {e}")
        return None, None, None, "desconhecido"
def get_holder_data(mint, liq_t0=0, dev_wallet=None):
    holders_count = top1_pct = top10_pct = dev_saiu = bc_progress = None
    HELIUS_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    LP_KNOWN = {
        "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg",
        "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",
        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
        "HVh6wHNBAsG3pq1Bj5oCzRjoWKVogEDHwUHkRz3ekFgt",
        "4wTV81avi27QFu8BcXVFEQhaqHXRv7r4f3BCQBFR6SJ1",
    }
    try:
        r1 = requests.post(HELIUS_URL,
            json={"jsonrpc":"2.0","id":1,"method":"getTokenSupply","params":[mint]},
            timeout=8)
        total_supply = 0
        if r1.status_code == 200:
            total_supply = float(r1.json().get("result",{}).get("value",{}).get("uiAmount", 0))
        if total_supply > 0:
            r2 = requests.post(HELIUS_URL,
                json={"jsonrpc":"2.0","id":1,"method":"getTokenLargestAccounts","params":[mint]},
                timeout=8)
            if r2.status_code == 200:
                accounts = r2.json().get("result",{}).get("value",[])
                accs_validos = [a for a in accounts if a.get("address","") not in LP_KNOWN]
                if accs_validos:
                    top1_pct  = round(float(accs_validos[0].get("uiAmount",0)) / total_supply * 100, 1)
                    top10_pct = round(sum(float(a.get("uiAmount",0)) for a in accs_validos[:10]) / total_supply * 100, 1)
                if dev_wallet:
                    dev_saiu = dev_wallet not in [a.get("address","") for a in accs_validos]
        r3 = requests.post(HELIUS_URL,
            json={"jsonrpc":"2.0","id":1,"method":"getTokenAccounts",
                  "params":{"mint": mint, "limit": 1000, "page": 1}},
            timeout=10)
        if r3.status_code == 200:
            token_accounts = r3.json().get("result",{}).get("token_accounts",[])
            contas_validas = [
                a for a in token_accounts
                if a.get("amount", 0) > 0
                and a.get("owner","") not in LP_KNOWN
            ]
            count = len(contas_validas)
            holders_count = count if count < 1000 else 1000
            log(f"  holders={holders_count} top1={top1_pct} top10={top10_pct} dev_saiu={dev_saiu}")
        if liq_t0 and liq_t0 > 0:
            TARGET_LIQ_USD = 11050
            bc_progress = min(round(liq_t0 / TARGET_LIQ_USD * 100, 1), 99.0)
            if liq_t0 >= TARGET_LIQ_USD:
                bc_progress = 99.0
    except Exception as e:
        log(f"helius holder erro: {e}")
    return holders_count, top1_pct, top10_pct, dev_saiu, bc_progress
def extrair_mudancas_token(tx, carteira_addr):
    mudancas = {}
    for conta in tx.get("accountData", []):
        for change in conta.get("tokenBalanceChanges", []):
            if change.get("userAccount") != carteira_addr:
                continue
            mint = change.get("mint", "")
            if not mint or mint in TOKENS_IGNORAR:
                continue
            raw = change.get("rawTokenAmount", {})
            try:
                amount = int(raw.get("tokenAmount", "0")) / (10 ** int(raw.get("decimals", 0)))
            except:
                continue
            if amount != 0:
                mudancas[mint] = mudancas.get(mint, 0) + amount
    for transfer in tx.get("tokenTransfers", []):
        mint     = transfer.get("mint", "")
        to_acc   = transfer.get("toUserAccount", "")
        from_acc = transfer.get("fromUserAccount", "")
        if not mint or mint in TOKENS_IGNORAR:
            continue
        try:
            amount = float(transfer.get("tokenAmount", 0))
        except:
            continue
        if amount == 0:
            continue
        if to_acc == carteira_addr:
            mudancas[mint] = mudancas.get(mint, 0) + amount
        elif from_acc == carteira_addr:
            mudancas[mint] = mudancas.get(mint, 0) - amount
    return [{"mint": m, "amount": a} for m, a in mudancas.items()]
def checar_multi_carteira(mint, nome_token, carteira_atual, mc_t0, liq_t0,
                           ratio_vol_mc, idade_min, score, score_emoji, score_desc,
                           holders_count=None, top1_pct=None, top10_pct=None,
                           dev_saiu=None, bc_progress=None,
                           buys_5min=0, sells_5min=0, ml_proba=None):
    agora = time.time()
    if mint not in mints_globais:
        mints_globais[mint] = {}
    mints_globais[mint][carteira_atual] = agora
    recentes = {
        c: ts for c, ts in mints_globais[mint].items()
        if c != carteira_atual and (agora - ts) / 60 <= 60
    }
    if not recentes:
        return False
    timing_s = min(int(agora - ts) for ts in recentes.values())
    if timing_s < 120:
        timing_str = f"⚡ {timing_s}s"
        urgencia   = "🚨🚨 SINCRONIZADO"
    elif timing_s < 600:
        timing_str = f"~{timing_s//60}min"
        urgencia   = "🚨 MULTI-CARTEIRA"
    else:
        timing_str = f"{timing_s//60}min"
        urgencia   = "ℹ️ MULTI-CARTEIRA"
    todas   = list(recentes.keys()) + [carteira_atual]
    humanos = [c for c in todas if TIPO_CARTEIRA.get(c) == "humano"]
    mints_globais[mint]["__multi_info__"] = {
        "carteiras": todas,
        "timing_s": timing_s,
        "urgencia_nivel": 1 if timing_s < 120 else 2 if timing_s < 600 else 3,
        "tem_humano": len(humanos) > 0,
        "n_humanos": len(humanos),
        "humanos": humanos,
    }
    if humanos:
        urgencia = "⭐" * len(humanos) + " " + urgencia
    def label(c):
        i = "👤" if TIPO_CARTEIRA.get(c) == "humano" else "🤖"
        return f"{i} <b>{c}</b>"
    linhas = [f"  • {label(c)} comprou há {round((agora-ts)/60,1)} min" for c, ts in recentes.items()]
    holder_linha = ""
    if holders_count:         holder_linha += f"\n👥 Holders: <b>{holders_count}</b>"
    if top1_pct is not None:  holder_linha += f" | Top: <b>{top1_pct}%</b>"
    if top10_pct is not None: holder_linha += f" | Top10: <b>{top10_pct}%</b>"
    if dev_saiu is True:      holder_linha += "\n✅ Dev saiu"
    elif dev_saiu is False:   holder_linha += "\n⚠️ Dev ainda segura"
    if bc_progress is not None: holder_linha += f"\n📈 BC: <b>{bc_progress:.0f}%</b>"
    momentum_linha = ""
    barra, net = calcular_momentum(buys_5min, sells_5min)
    if barra:
        momentum_linha = f"\n🔄 {barra}\n    {classificar_momentum(net, buys_5min + sells_5min)}"
    icone = "👤" if TIPO_CARTEIRA.get(carteira_atual) == "humano" else "🤖"
    telegram(
        f"{urgencia}\n\n"
        f"Token: <b>{he(nome_token)}</b>\n"
        f"Mint: <code>{mint}</code>\n\n"
        f"{icone} <b>{carteira_atual}</b> comprou agora\n"
        + "\n".join(linhas) + "\n\n"
        f"⏱ Timing: <b>{timing_str}</b>\n\n"
        f"💰 MC: <b>${mc_t0:,.0f}</b>\n"
        f"💧 Liq: <b>${liq_t0:,.0f}</b>\n"
        f"📊 Vol/MC: <b>{ratio_vol_mc:.1f}x</b>\n"
        f"🕐 Idade: <b>{idade_min:.0f} min</b>\n"
        f"{holder_linha}"
        f"{momentum_linha}\n\n"
        f"Score: {score_emoji} <b>{score}/10 — {score_desc}</b>"
        + (f" | 🤖 ML: <b>{ml_proba*100:.0f}%</b>" if ml_proba is not None else "") + "\n\n"
        f"🔗 https://pump.fun/{mint}"
    )
    log(f"🚨 MULTI: {nome_token} | {carteira_atual} + {list(recentes.keys())} | {timing_str}")
    return True
def processar_venda(carteira_addr, nome, mint, amount_vendido, tx):
    est = estado[nome]
    reg = next((r for r in est["registros"] if r.get("token_mint") == mint), None)
    _, mc_atual, _, _, _, nome_token, _, _, _, _, _ = get_dados_token(mint)
    nome_token = reg["nome"] if reg else nome_token
    variacao = None
    if reg and reg.get("p_t0"):
        preco_atual, _, _, _, _, _, _, _, _, _, _ = get_dados_token(mint)
        if preco_atual:
            variacao = round((preco_atual - reg["p_t0"]) / reg["p_t0"] * 100, 2)
    log(f"🔴 [{nome}] VENDA: {nome_token} | MC: ${mc_atual:,.0f} | variação: {f'{variacao:+.1f}%' if variacao is not None else '—'}")
    data = datetime.fromtimestamp(tx.get("timestamp", time.time())).strftime("%Y-%m-%d %H:%M:%S")
    reg_venda = {
        "data_compra": data, "carteira": nome, "tipo_carteira": TIPO_CARTEIRA.get(nome, "?"),
        "token_mint": mint, "nome": nome_token, "dex": "venda", "fonte_dados": "venda",
        "quantidade": round(abs(amount_vendido), 4), "signature": tx.get("signature", ""),
        "tipo": "VENDA", "is_multi": False,
        "p_t0": None, "mc_t0": mc_atual, "liq_t0": None, "volume_t0": None,
        "txns5m_t0": None, "buys_t0": None, "sells_t0": None, "net_momentum_t0": None,
        "idade_min": None, "token_antigo": None, "ratio_vol_mc_t0": None,
        "score_qualidade": None, "holders_count": None, "top1_pct": None,
        "top10_pct": None, "dev_saiu": None, "bc_progress": None,
        "mc_pico": None, "categoria_final": "🔴 VENDA", "var_desde_compra": variacao,
        "ml_proba": None,
    }
    est["registros"].append(reg_venda)
    try:
        db_insert(reg_venda)
    except Exception as e:
        log(f"⚠️  DB insert venda erro: {e}")
def agendar_checkpoints(nome, mint):
    threading.Timer(5  * 60, checar_checkpoint, args=[nome, mint, "t1"]).start()
    threading.Timer(15 * 60, checar_checkpoint, args=[nome, mint, "t2"]).start()
    threading.Timer(45 * 60, checar_checkpoint, args=[nome, mint, "t3"]).start()
    threading.Timer(2  * 60, atualizar_pico, args=[nome, mint, "2min"]).start()
    threading.Timer(10 * 60, atualizar_pico, args=[nome, mint, "10min"]).start()
    threading.Timer(25 * 60, atualizar_pico, args=[nome, mint, "25min"]).start()
def atualizar_pico(nome, mint, label):
    est = estado[nome]
    if mint not in est["pendentes"]:
        return
    info = est["pendentes"][mint]
    reg  = est["registros"][info["idx"]]
    db_id = info.get("db_id")
    try:
        _, mc_atual, _, _, _, txns_atual, _, _, _, buys_atual, sells_atual = get_dados_token(mint)
        if not mc_atual or mc_atual == 0:
            return
        mc_pico_atual = reg.get("mc_pico") or 0
        if mc_atual > mc_pico_atual:
            reg["mc_pico"] = mc_atual
            log(f"  📈 [{nome}] Pico atualizado {label}: {reg['nome'][:20]} | MC: ${mc_atual:,.0f} (era ${mc_pico_atual:,.0f})")
            if db_id:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE registros SET mc_pico=%s WHERE id=%s", (mc_atual, db_id))
                    conn.commit()
        if label == "2min" and txns_atual is not None:
            txns_t0 = reg.get("txns5m_t0") or 0
            acel = round(txns_atual / txns_t0, 2) if txns_t0 > 0 else None
            reg["aceleracao_2min"] = acel
            reg["txns_2min"]       = txns_atual
            reg["buys_2min"]       = buys_atual
            reg["sells_2min"]      = sells_atual
            if acel:
                if acel >= 3:    emoji = "🚀"
                elif acel >= 1.5: emoji = "📈"
                elif acel < 0.8:  emoji = "📉"
                else:             emoji = "➡️"
                log(f"  ⚡ [{nome}] Acel 2min: {reg['nome'][:20]} | txns: {txns_t0}→{txns_atual} ({acel}x) {emoji}")
            score_atual = reg.get("score_qualidade") or 0
            bonus_acel = 0
            if acel is not None:
                if acel >= 3:    bonus_acel = 1
                elif acel < 0.8: bonus_acel = -1
            if bonus_acel != 0:
                novo_score = max(0, min(10, score_atual + bonus_acel))
                reg["score_qualidade"] = novo_score
                if novo_score >= 7:   reg["score_emoji"] = "🟢"
                elif novo_score >= 4: reg["score_emoji"] = "🟡"
                else:                 reg["score_emoji"] = "🔴"
                log(f"  🔄 [{nome}] Score ajustado por aceleração: {score_atual}→{novo_score} (acel={acel}x)")
                if db_id:
                    with get_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute("UPDATE registros SET score_qualidade=%s WHERE id=%s", (novo_score, db_id))
                        conn.commit()
    except Exception as e:
        log(f"⚠️  atualizar_pico erro [{label}]: {e}")
def checar_checkpoint(nome, mint, checkpoint, _retry=0):
    est = estado[nome]
    if mint not in est["pendentes"]:
        return
    info  = est["pendentes"][mint]
    reg   = est["registros"][info["idx"]]
    db_id = info.get("db_id")
    preco, mc, liq, volume, _, _, txns_5min, _, _, buys, sells = get_dados_token(mint)
    # Se T1 ainda não tem dados e temos retentativas disponíveis, reagendar
    if checkpoint == "t1" and (not mc or mc == 0) and _retry < 2:
        delay = 60 * (_retry + 1)  # 60s na 1ª, 120s na 2ª
        log(f"  ⏳ [{nome}] T1 mc=0 (retry {_retry+1}/2), reagendando em {delay}s: {reg['nome'][:20]}")
        threading.Timer(delay, checar_checkpoint, args=[nome, mint, "t1", _retry + 1]).start()
        return
    ratio = round(volume / reg["mc_t0"], 2) if reg.get("mc_t0", 0) > 0 else None
    if checkpoint == "t1":
        var_t1 = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None
        veredito = veredito_parcial(reg["mc_t0"], mc, "5min")
        mc_pico = max(mc, reg.get("mc_pico") or 0)
        h_t1 = top1_t1 = top10_t1 = dev_saiu_t1 = None
        if mc:
            dev_w = reg.get("dev_wallet")
            h_t1, top1_t1, top10_t1, dev_saiu_t1, _ = get_holder_data(mint, liq_t0=liq, dev_wallet=dev_w)
            log(f"  [T1 holders] {h_t1} top1={top1_t1} top10={top10_t1} dev_saiu={dev_saiu_t1}")
        reg.update({
            "p_t1": preco, "mc_t1": mc, "liq_t1": liq, "volume_t1": volume,
            "txns5m_t1": txns_5min, "buys_t1": buys, "sells_t1": sells,
            "ratio_vol_mc_t1": ratio, "var_t1_%": var_t1,
            "veredito_t1": veredito, "mc_pico": mc_pico,
            "holders_t1": h_t1, "top1_t1": top1_t1, "top10_t1": top10_t1, "dev_saiu_t1": dev_saiu_t1,
        })
        if db_id:
            db_update_checkpoint(db_id, "t1", preco, mc, liq, volume, txns_5min, buys, sells, ratio, var_t1, veredito, mc_pico)
            if h_t1 is not None:
                db_update_holders(db_id, "t1", h_t1, top1_t1, top10_t1, dev_saiu_t1)
        log(f"  ⏱️  [{nome}] T1 {reg['nome'][:20]} | MC: ${mc:,.0f} | {veredito}")
        if reg.get("is_multi") and var_t1:
            if var_t1 >= 100:
                telegram(f"🚨 <b>SAÍDA — T1 EXPLOSIVO</b>\n\nToken: <b>{he(reg['nome'])}</b>\n📈 T1: <b>+{var_t1:.0f}%</b> em 5min\n💰 MC: <b>${mc:,.0f}</b>\n\n⚠️ <i>Considere realizar lucro.</i>\n\n🔗 https://pump.fun/{reg['token_mint']}")
            elif var_t1 >= 50:
                telegram(f"⚠️ <b>SAÍDA — T1 FORTE</b>\n\nToken: <b>{he(reg['nome'])}</b>\n📈 T1: <b>+{var_t1:.0f}%</b> em 5min\n💰 MC: <b>${mc:,.0f}</b>\n\n💡 <i>Considere realizar parte.</i>\n\n🔗 https://pump.fun/{reg['token_mint']}")
    elif checkpoint == "t2":
        var_t2 = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None
        veredito = veredito_parcial(reg.get("mc_t1"), mc, "15min")
        mc_pico = max(mc, reg.get("mc_pico") or 0)
        h_t2 = top1_t2 = top10_t2 = dev_saiu_t2 = None
        if mc:
            dev_w = reg.get("dev_wallet")
            h_t2, top1_t2, top10_t2, dev_saiu_t2, _ = get_holder_data(mint, liq_t0=liq, dev_wallet=dev_w)
            log(f"  [T2 holders] {h_t2} top1={top1_t2} top10={top10_t2} dev_saiu={dev_saiu_t2}")
        reg.update({
            "p_t2": preco, "mc_t2": mc, "liq_t2": liq, "volume_t2": volume,
            "txns5m_t2": txns_5min, "buys_t2": buys, "sells_t2": sells,
            "ratio_vol_mc_t2": ratio, "var_t2_%": var_t2,
            "veredito_t2": veredito, "mc_pico": mc_pico,
            "holders_t2": h_t2, "top1_t2": top1_t2, "top10_t2": top10_t2, "dev_saiu_t2": dev_saiu_t2,
        })
        if db_id:
            db_update_checkpoint(db_id, "t2", preco, mc, liq, volume, txns_5min, buys, sells, ratio, var_t2, veredito, mc_pico)
            if h_t2 is not None:
                db_update_holders(db_id, "t2", h_t2, top1_t2, top10_t2, dev_saiu_t2)
        log(f"  ⏱️  [{nome}] T2 {reg['nome'][:20]} | MC: ${mc:,.0f} | {veredito}")
    elif checkpoint == "t3":
        var_t3 = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None
        veredito = veredito_parcial(reg.get("mc_t2"), mc, "45min")
        mc_pico = max(mc, reg.get("mc_pico") or 0)
        var_pico = round((mc_pico - reg["mc_t0"]) / reg["mc_t0"] * 100, 2) if reg.get("mc_t0") else None
        cat = categoria_final({**reg, "mc_t3": mc})
        h_t3 = top1_t3 = top10_t3 = dev_saiu_t3 = None
        if mc:
            dev_w = reg.get("dev_wallet")
            h_t3, top1_t3, top10_t3, dev_saiu_t3, _ = get_holder_data(mint, liq_t0=liq, dev_wallet=dev_w)
            log(f"  [T3 holders] {h_t3} top1={top1_t3} top10={top10_t3} dev_saiu={dev_saiu_t3}")
        reg.update({
            "p_t3": preco, "mc_t3": mc, "liq_t3": liq, "volume_t3": volume,
            "txns5m_t3": txns_5min, "buys_t3": buys, "sells_t3": sells,
            "ratio_vol_mc_t3": ratio, "var_t3_%": var_t3,
            "veredito_t3": veredito, "mc_pico": mc_pico,
            "var_pico_%": var_pico, "categoria_final": cat,
            "holders_t3": h_t3, "top1_t3": top1_t3, "top10_t3": top10_t3, "dev_saiu_t3": dev_saiu_t3,
        })
        if db_id:
            db_update_checkpoint(db_id, "t3", preco, mc, liq, volume, txns_5min, buys, sells, ratio, var_t3, veredito, mc_pico)
            db_update_final(db_id, mc_pico, var_pico, cat)
        log(f"  ✅ [{nome}] FINAL {reg['nome'][:20]} | MC: ${mc:,.0f} | {cat}")
        del est["pendentes"][mint]
def processar_tx(tx, carteira_addr, nome):
    est = estado[nome]
    if tx.get("type") == "TRANSFER" and tx.get("source") == "SYSTEM_PROGRAM":
        return
    for mudanca in extrair_mudancas_token(tx, carteira_addr):
        mint   = mudanca["mint"]
        amount = mudanca["amount"]
        if amount == 0:
            continue
        if amount < 0:
            processar_venda(carteira_addr, nome, mint, amount, tx)
            continue
        if mint in est["tokens_conhecidos"]:
            continue
        est["tokens_conhecidos"].add(mint)
        data = datetime.fromtimestamp(tx.get("timestamp", time.time())).strftime("%Y-%m-%d %H:%M:%S")
        preco_t0, mc_t0, liq_t0, volume_t0, dex, nome_token, txns_5min, idade_min, fonte, buys_5min, sells_5min = get_dados_token(mint)
        if not mc_t0 or mc_t0 == 0:
            log(f"  ⏳ MC=0 na primeira tentativa para {nome_token[:20]}, aguardando 30s...")
            time.sleep(30)
            preco_t0, mc_t0, liq_t0, volume_t0, dex, nome_token, txns_5min, idade_min, fonte, buys_5min, sells_5min = get_dados_token(mint)
        if not mc_t0 or mc_t0 == 0:
            log(f"  ⏳ MC=0 na segunda tentativa, aguardando 60s...")
            time.sleep(60)
            preco_t0, mc_t0, liq_t0, volume_t0, dex, nome_token, txns_5min, idade_min, fonte, buys_5min, sells_5min = get_dados_token(mint)
        if not mc_t0 or mc_t0 == 0:
            log(f"  ⏳ MC=0 na terceira tentativa, aguardando 2min...")
            time.sleep(120)
            preco_t0, mc_t0, liq_t0, volume_t0, dex, nome_token, txns_5min, idade_min, fonte, buys_5min, sells_5min = get_dados_token(mint)
        ratio_vol_mc_t0 = round(volume_t0 / mc_t0, 2) if mc_t0 > 0 else None
        token_antigo    = "sim" if (idade_min and idade_min > 1440) else "não"
        holders_count = top1_pct = top10_pct = dev_saiu = bc_progress = None
        try:
            dev_wallet = get_dev_wallet(mint)
            holders_count, top1_pct, top10_pct, dev_saiu, bc_progress = get_holder_data(mint, liq_t0=liq_t0, dev_wallet=dev_wallet)
            dev_tokens_total, dev_tokens_rug, dev_rug_rate, dev_classif = get_deployer_history(dev_wallet)
            for n in estado:
                for r in estado[n]["registros"]:
                    if r.get("token_mint") == mint and r.get("tipo") == "COMPRA":
                        r["dev_wallet"]       = dev_wallet
                        r["dev_classif"]      = dev_classif
                        r["dev_rug_rate"]     = dev_rug_rate
                        r["dev_tokens_total"] = dev_tokens_total
                        break
        except Exception as e:
            log(f"holders erro [{nome_token}]: {e}")
        dev_classif_score = None
        for n in estado:
            for r in estado[n]["registros"]:
                if r.get("token_mint") == mint and r.get("tipo") == "COMPRA":
                    dev_classif_score = r.get("dev_classif")
                    break
        is_multi = False
        score, score_emoji, score_desc = calcular_score(
            mc_t0, liq_t0, txns_5min, ratio_vol_mc_t0, idade_min, dex,
            holders_count=holders_count, top10_pct=top10_pct,
            buys=buys_5min, sells=sells_5min,
            dev_classif=dev_classif_score,
            bc_progress=bc_progress,
            top1_pct=top1_pct,
            carteira=nome
        )
        ml_proba = calcular_ml_proba(
            mc_t0=mc_t0, liq_t0=liq_t0, volume_t0=volume_t0,
            buys=buys_5min, sells=sells_5min, idade_min=idade_min,
            ratio_vol_mc=ratio_vol_mc_t0, net_momentum=(buys_5min or 0) - (sells_5min or 0),
            holders_count=holders_count, top10_pct=top10_pct,
            is_multi=is_multi, dex=dex, bc_progress=bc_progress,
            score_qualidade=score
        )
        flag_antigo = f" ⚠️ TOKEN ANTIGO ({idade_min/1440:.0f}d)" if token_antigo == "sim" else ""
        if not mc_t0 or mc_t0 == 0:
            log(f"⚠️  [{nome}] {nome_token} | MC=0 — token não indexado, ignorando checkpoints")
            agora_ts = time.time()
            if mint not in mints_globais:
                mints_globais[mint] = {}
            mints_globais[mint][nome] = agora_ts
            outras = {c: ts for c, ts in mints_globais[mint].items()
                      if c != nome and (agora_ts - ts) / 60 <= 60}
            if outras:
                outras_str = ", ".join(outras.keys())
                timing_s = min(int(agora_ts - ts) for ts in outras.values())
                telegram(
                    f"🚨 <b>ALERTA MULTI-CARTEIRA</b> (MC não disponível)\n\n"
                    f"Token: <b>{nome_token}</b>\n"
                    f"Carteiras: <b>{outras_str}</b> + <b>{nome}</b>\n"
                    f"⏱ Timing: <b>{timing_s}s</b>\n"
                    f"⚠️ MC não indexado ainda\n\n"
                    f"🔗 https://pump.fun/{mint}"
                )
            reg_sem_dados = {
                "data_compra": data, "carteira": nome, "tipo_carteira": TIPO_CARTEIRA.get(nome, "?"),
                "token_mint": mint, "nome": nome_token, "dex": dex, "fonte_dados": fonte,
                "quantidade": round(amount, 4), "signature": tx.get("signature", ""),
                "tipo": "COMPRA", "is_multi": False,
                "p_t0": None, "mc_t0": 0, "liq_t0": liq_t0, "volume_t0": volume_t0,
                "txns5m_t0": txns_5min, "buys_t0": buys_5min, "sells_t0": sells_5min,
                "net_momentum_t0": 0, "idade_min": idade_min, "token_antigo": token_antigo,
                "ratio_vol_mc_t0": None, "score_qualidade": 0,
                "holders_count": None, "top1_pct": None, "top10_pct": None,
                "dev_saiu": None, "bc_progress": None,
                "p_t1": None, "mc_t1": None, "liq_t1": None, "volume_t1": None,
                "txns5m_t1": None, "buys_t1": None, "sells_t1": None,
                "ratio_vol_mc_t1": None, "var_t1_%": None, "veredito_t1": None,
                "p_t2": None, "mc_t2": None, "liq_t2": None, "volume_t2": None,
                "txns5m_t2": None, "buys_t2": None, "sells_t2": None,
                "ratio_vol_mc_t2": None, "var_t2_%": None, "veredito_t2": None,
                "p_t3": None, "mc_t3": None, "liq_t3": None, "volume_t3": None,
                "txns5m_t3": None, "buys_t3": None, "sells_t3": None,
                "ratio_vol_mc_t3": None, "var_t3_%": None, "veredito_t3": None,
                "mc_pico": 0, "var_pico_%": None, "var_desde_compra": None,
                "categoria_final": "❓ SEM DADOS — MC não disponível",
                "ml_proba": None,
            }
            est["registros"].append(reg_sem_dados)
            try:
                db_insert(reg_sem_dados)
            except Exception as e:
                log(f"⚠️  DB insert sem dados erro: {e}")
            continue
        log(f"🆕 [{nome}] {nome_token} | {dex} | MC: ${mc_t0:,.0f} | Score: {score}/10{flag_antigo}")
        reg = {
            "data_compra": data, "carteira": nome, "tipo_carteira": TIPO_CARTEIRA.get(nome, "?"),
            "token_mint": mint, "nome": nome_token, "dex": dex, "fonte_dados": fonte,
            "quantidade": round(amount, 4), "signature": tx.get("signature", ""),
            "tipo": "COMPRA", "is_multi": False,
            "p_t0": preco_t0, "mc_t0": mc_t0, "liq_t0": liq_t0, "volume_t0": volume_t0,
            "txns5m_t0": txns_5min, "buys_t0": buys_5min, "sells_t0": sells_5min,
            "net_momentum_t0": (buys_5min or 0) - (sells_5min or 0),
            "idade_min": idade_min, "token_antigo": token_antigo,
            "ratio_vol_mc_t0": ratio_vol_mc_t0, "score_qualidade": score,
            "holders_count": holders_count, "top1_pct": top1_pct,
            "top10_pct": top10_pct, "dev_saiu": dev_saiu, "bc_progress": bc_progress,
            "p_t1": None, "mc_t1": None, "liq_t1": None, "volume_t1": None,
            "txns5m_t1": None, "buys_t1": None, "sells_t1": None,
            "ratio_vol_mc_t1": None, "var_t1_%": None, "veredito_t1": None,
            "p_t2": None, "mc_t2": None, "liq_t2": None, "volume_t2": None,
            "txns5m_t2": None, "buys_t2": None, "sells_t2": None,
            "ratio_vol_mc_t2": None, "var_t2_%": None, "veredito_t2": None,
            "p_t3": None, "mc_t3": None, "liq_t3": None, "volume_t3": None,
            "txns5m_t3": None, "buys_t3": None, "sells_t3": None,
            "ratio_vol_mc_t3": None, "var_t3_%": None, "veredito_t3": None,
            "mc_pico": mc_t0, "var_pico_%": None, "var_desde_compra": None,
            "categoria_final": "⏳ aguardando",
            "ml_proba": ml_proba,
        }
        idx = len(est["registros"])
        est["registros"].append(reg)
        db_id = None
        try:
            db_id = db_insert(reg)
        except Exception as e:
            log(f"⚠️  DB insert erro: {e}")
        est["pendentes"][mint] = {"idx": idx, "db_id": db_id}
        is_multi = checar_multi_carteira(
            mint, nome_token, nome, mc_t0, liq_t0,
            ratio_vol_mc_t0 or 0, idade_min or 0,
            score, score_emoji, score_desc,
            holders_count=holders_count, top1_pct=top1_pct,
            top10_pct=top10_pct, dev_saiu=dev_saiu, bc_progress=bc_progress,
            buys_5min=buys_5min, sells_5min=sells_5min, ml_proba=ml_proba,
        )
        est["registros"][idx]["is_multi"] = bool(is_multi)
        if is_multi and db_id:
            try:
                db_update_multi(db_id)
            except:
                pass
        agendar_checkpoints(nome, mint)
def verificar_calibracao():
    """Roda diariamente — analisa performance real por tier e sugere ajustes no score."""
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT score_qualidade, holders_count, top10_pct, dev_saiu,
                           holders_t1, holders_t3, var_pico, categoria_final,
                           mc_t0, liq_t0, ratio_vol_mc_t0, net_momentum_t0,
                           idade_min, bc_progress, buys_t0, sells_t0, txns5m_t0
                    FROM registros
                    WHERE tipo = 'COMPRA'
                      AND categoria_final IS NOT NULL
                      AND categoria_final NOT ILIKE '%aguardando%'
                      AND categoria_final NOT ILIKE '%sem dados%'
                      AND var_pico IS NOT NULL
                """)
                rows = cur.fetchall()
        total = len(rows)
        MIN_TOTAL = 30  # mínimo para análise (resultado confiável a partir de 200)

        if total < MIN_TOTAL:
            log(f"[calibração] 🔴 Faltam {MIN_TOTAL - total} tokens para análise (têm {total})")
            threading.Timer(24 * 60 * 60, verificar_calibracao).start()
            return

        aviso_amostra = f"⚠️ Amostra pequena ({total} tokens) — resultados preliminares" if total < 200 else ""

        # ── Performance por tier ────────────────────────────────
        def stats_tier(filtro):
            vals = [r["var_pico"] for r in rows if filtro(r) and r["var_pico"] is not None]
            if not vals: return None
            venc = sum(1 for v in vals if v > 50)
            mortes = sum(1 for v in vals if v < -50)
            mediana = sorted(vals)[len(vals)//2]
            return {"n": len(vals), "win_pct": round(venc/len(vals)*100,1),
                    "morte_pct": round(mortes/len(vals)*100,1), "mediana": round(mediana,1)}

        alta    = stats_tier(lambda r: (r.get("score_qualidade") or 0) >= 7)
        mod     = stats_tier(lambda r: 4 <= (r.get("score_qualidade") or 0) < 7)
        baixa   = stats_tier(lambda r: (r.get("score_qualidade") or 0) < 4)
        geral   = stats_tier(lambda r: True)

        # ── Detecta problemas nos cortes dos tiers ──────────────
        avisos_tier = []
        if alta and mod:
            diff = alta["win_pct"] - mod["win_pct"]
            if diff < 5:
                avisos_tier.append(f"⚠️ ALTA e MODERADO parecidos ({alta['win_pct']}% vs {mod['win_pct']}%) — corte em 7 alto demais")
            elif diff > 25:
                avisos_tier.append(f"✅ Separação ALTA vs MOD: +{diff:.1f}% win rate")
        if mod and baixa:
            diff = mod["win_pct"] - baixa["win_pct"]
            if diff < 5:
                avisos_tier.append(f"⚠️ MODERADO e BAIXA parecidos ({mod['win_pct']}% vs {baixa['win_pct']}%) — corte em 4 baixo demais")
        if alta and alta["win_pct"] < 40:
            avisos_tier.append(f"🚨 ALTA CONFIANÇA com win rate {alta['win_pct']}% — score não filtra bem")

        # ── Análise por critério (discriminação) ────────────────
        def win_pct(subset):
            if not subset: return None
            return round(sum(1 for r in subset if (r["var_pico"] or 0) > 50) / len(subset) * 100, 1)

        def disc(label, bons, ruins, bonus, penalidade):
            """Compara win rate bons vs ruins para um critério. Sugere ajuste se discriminação fraca."""
            wr_bom  = win_pct(bons)
            wr_ruim = win_pct(ruins)
            if wr_bom is None or wr_ruim is None or len(bons) < 5 or len(ruins) < 5:
                return None
            delta = wr_bom - wr_ruim
            if delta < 5 and bonus >= 2:
                nota = f"⚠️ {label}: +{bonus}pts mas Δwin={delta:+.1f}% → reduza para +{bonus - 1}"
            elif delta < 5 and penalidade <= -2:
                nota = f"⚠️ {label}: {penalidade}pts mas Δwin={delta:+.1f}% → reduza para {penalidade + 1}"
            elif delta < 0 and bonus > 0:
                nota = f"🚨 {label}: critério INVERTIDO — bons={wr_bom}% vs ruins={wr_ruim}% → remova bônus"
            elif delta < 0 and penalidade < 0:
                nota = f"🚨 {label}: penalidade INVERTIDA — bons={wr_bom}% vs ruins={wr_ruim}% → remova penalidade"
            elif delta >= 20:
                nota = f"✅ {label}: forte discriminador Δ={delta:+.1f}% — peso ok"
            else:
                nota = None  # discriminação ok, sem sugestão
            return {"label": label, "delta": delta, "wr_bom": wr_bom, "n_bom": len(bons),
                    "wr_ruim": wr_ruim, "n_ruim": len(ruins), "nota": nota}

        criterios = []

        # ratio buys/sells
        rows_bs = [r for r in rows if (r.get("buys_t0") or 0) + (r.get("sells_t0") or 0) > 0]
        if rows_bs:
            bons  = [r for r in rows_bs if r["buys_t0"] / (r["buys_t0"] + r["sells_t0"]) >= 0.70]
            ruins = [r for r in rows_bs if r["buys_t0"] / (r["buys_t0"] + r["sells_t0"]) < 0.40]
            d = disc("ratio_bs≥70% (+3) vs <40% (-2)", bons, ruins, 3, -2)
            if d: criterios.append(d)

        # mc_t0
        rows_mc = [r for r in rows if r.get("mc_t0")]
        if rows_mc:
            bons  = [r for r in rows_mc if 30000 <= r["mc_t0"] <= 60000]
            ruins = [r for r in rows_mc if r["mc_t0"] > 60000]
            d = disc("mc_t0 30k-60k (+2) vs >60k (-2)", bons, ruins, 2, -2)
            if d: criterios.append(d)

        # idade_min
        rows_id = [r for r in rows if r.get("idade_min") is not None]
        if rows_id:
            bons  = [r for r in rows_id if 25 <= r["idade_min"] <= 60]
            ruins = [r for r in rows_id if 10 < r["idade_min"] < 25]
            d = disc("idade 25-60min (+2) vs 10-25min (-2)", bons, ruins, 2, -2)
            if d: criterios.append(d)

        # holders_count
        rows_h = [r for r in rows if r.get("holders_count") is not None]
        if rows_h:
            bons  = [r for r in rows_h if r["holders_count"] >= 200]
            ruins = [r for r in rows_h if r["holders_count"] < 80]
            d = disc("holders≥200 (+1) vs <80 (-1)", bons, ruins, 1, -1)
            if d: criterios.append(d)

        # bc_progress
        rows_bc = [r for r in rows if r.get("bc_progress") is not None]
        if rows_bc:
            bons  = [r for r in rows_bc if r["bc_progress"] < 30]
            ruins = [r for r in rows_bc if r["bc_progress"] >= 60]
            d = disc("bc_progress<30% (+1) vs ≥60% (-1)", bons, ruins, 1, -1)
            if d: criterios.append(d)

        # ratio_vol_mc
        rows_rv = [r for r in rows if r.get("ratio_vol_mc_t0") is not None]
        if rows_rv:
            bons  = [r for r in rows_rv if 1.0 <= r["ratio_vol_mc_t0"] < 3.0]
            ruins = [r for r in rows_rv if r["ratio_vol_mc_t0"] < 0.8]
            d = disc("ratio_vol_mc 1-3x (+1) vs <0.8x (-1)", bons, ruins, 1, -1)
            if d: criterios.append(d)

        # txns5m_t0
        rows_tx = [r for r in rows if r.get("txns5m_t0") is not None]
        if rows_tx:
            bons  = [r for r in rows_tx if r["txns5m_t0"] >= 80]
            ruins = [r for r in rows_tx if r["txns5m_t0"] < 30]
            d = disc("txns5m≥80 (+1) vs <30 (0)", bons, ruins, 1, 0)
            if d: criterios.append(d)

        sugestoes = [c["nota"] for c in criterios if c.get("nota")]

        # ── Verifica completude das colunas novas ───────────────
        cols_novas = {
            "holders_count": "holders T0",
            "top10_pct":     "top10 T0",
            "holders_t1":    "holders T1",
            "holders_t3":    "holders T3",
            "dev_saiu":      "dev_saiu",
        }
        prontos = [nome for col, nome in cols_novas.items()
                   if sum(1 for r in rows if r.get(col) is not None) >= 30]

        # ── Monta e envia relatório ─────────────────────────────
        def fmt(s):
            if not s: return "—"
            return f"n={s['n']} | win={s['win_pct']}% | morte={s['morte_pct']}% | med={s['mediana']}%"

        linhas = [
            f"🧠 <b>CALIBRAÇÃO DIÁRIA — {datetime.now().strftime('%d/%m %H:%M')}</b>",
            f"📊 {total} tokens finalizados\n",
        ]
        if aviso_amostra:
            linhas.append(aviso_amostra + "\n")

        linhas += [
            f"<b>Performance por tier:</b>",
            f"🟢 ALTA    {fmt(alta)}",
            f"🟡 MOD     {fmt(mod)}",
            f"🔴 BAIXA   {fmt(baixa)}",
            f"⚪ GERAL   {fmt(geral)}",
        ]
        if avisos_tier:
            linhas.append(f"\n<b>Diagnóstico dos tiers:</b>")
            linhas += avisos_tier

        if criterios:
            linhas.append(f"\n<b>Discriminação por critério:</b>")
            for c in criterios:
                delta_str = f"{c['delta']:+.1f}%"
                linhas.append(
                    f"  • {c['label']}: bons={c['wr_bom']}%(n={c['n_bom']}) "
                    f"ruins={c['wr_ruim']}%(n={c['n_ruim']}) Δ={delta_str}"
                )

        if sugestoes:
            linhas.append(f"\n<b>Sugestões de ajuste:</b>")
            linhas += sugestoes

        if prontos:
            linhas.append(f"\n<b>Colunas com dados suficientes (≥30):</b>")
            linhas += [f"  ✅ {p}" for p in prontos]
        if total >= 800 and len(prontos) >= 3:
            linhas.append(f"\n🚀 <b>Dados prontos para ML!</b>")

        msg = "\n".join(linhas)
        telegram(msg)
        log(f"[calibração] ✅ Relatório enviado | {total} tokens | ALTA={alta['win_pct'] if alta else '—'}% win | {len(sugestoes)} sugestões")
    except Exception as e:
        log(f"[calibração] erro: {e}")
    threading.Timer(24 * 60 * 60, verificar_calibracao).start()
def enviar_csv_diario():
    log("📤 Enviando CSV diário...")
    todos = []
    for nome in set(CARTEIRAS.values()):
        todos.extend(estado[nome]["registros"])
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    if not todos:
        telegram("📊 <b>Relatório diário</b>\n\nNenhum registro ainda.")
        threading.Timer(24 * 60 * 60, enviar_csv_diario).start()
        return
    caminho = "monitoramento_consolidado.csv"
    pd.DataFrame(todos).to_csv(caminho, index=False)
    compras = sum(1 for r in todos if r.get("tipo") == "COMPRA")
    vendas  = sum(1 for r in todos if r.get("tipo") == "VENDA")
    multis  = sum(1 for r in todos if r.get("is_multi"))
    telegram_documento(caminho, caption=(
        f"📊 <b>Relatório consolidado</b>\nGerado em: {agora}\n\n"
        f"Compras: <b>{compras}</b> | Vendas: <b>{vendas}</b>\nMulti: <b>{multis}</b>"
    ))
    threading.Timer(24 * 60 * 60, enviar_csv_diario).start()
# ══════════════════════════════════════════════════════════
# ROTAS
# ══════════════════════════════════════════════════════════
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        txs = request.get_json()
        if not txs:
            return jsonify({"ok": True})
        for tx in txs:
            sig = tx.get("signature", "")
            if sig in signatures_vistas:
                continue
            if sig:
                signatures_vistas.add(sig)
                threading.Thread(target=db_sig_add, args=[sig], daemon=True).start()
            for acc in tx.get("accountData", []):
                addr = acc.get("account", "")
                if addr in CARTEIRAS:
                    processar_tx(tx, addr, CARTEIRAS[addr])
                    break
        return jsonify({"ok": True})
    except Exception as e:
        import traceback
        log(f"Webhook erro: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False}), 500
@app.route("/", methods=["GET"])
def health():
    total   = sum(len(estado[n]["registros"]) for n in estado)
    pend    = sum(len(estado[n]["pendentes"]) for n in estado)
    compras = sum(1 for n in estado for r in estado[n]["registros"] if r.get("tipo") == "COMPRA")
    vendas  = sum(1 for n in estado for r in estado[n]["registros"] if r.get("tipo") == "VENDA")
    multis  = sum(1 for n in estado for r in estado[n]["registros"] if r.get("is_multi"))
    return jsonify({
        "status": "running v6.3+db", "registros": total,
        "compras": compras, "vendas": vendas,
        "multis": multis, "pendentes": pend,
    })
@app.route("/dados", methods=["GET"])
def dados():
    if request.args.get("key") != DASHBOARD_KEY:
        return jsonify({"erro": "nao autorizado"}), 401
    todos = []
    for n in estado:
        todos.extend(estado[n]["registros"])
    todos_sorted = sorted(todos, key=lambda r: r.get("data_compra", ""), reverse=True)
    ativos = []
    for n in estado:
        for mint, info in estado[n]["pendentes"].items():
            ativos.append(dict(estado[n]["registros"][info["idx"]]))
    mints_multi = set(
        r["token_mint"] for r in todos_sorted
        if r.get("is_multi") and r.get("tipo") == "COMPRA"
    )
    multis_por_mint = {}
    for r in todos_sorted:
        if r.get("tipo") != "COMPRA":
            continue
        m = r["token_mint"]
        if m not in mints_multi:
            continue
        if m not in multis_por_mint:
            multis_por_mint[m] = []
        multis_por_mint[m].append(r)
    multis = []
    for mint_m, regs_m in list(multis_por_mint.items())[:50]:
        base = regs_m[0]
        info_m = mints_globais.get(mint_m, {}).get("__multi_info__", {})
        entradas = []
        for r in regs_m:
            entradas.append({
                "carteira": r["carteira"],
                "tipo_carteira": TIPO_CARTEIRA.get(r["carteira"], "?"),
                "mc_t0": r.get("mc_t0"),
                "data_compra": r.get("data_compra"),
                "var_t1_%": r.get("var_t1_%"),
                "var_t2_%": r.get("var_t2_%"),
                "var_t3_%": r.get("var_t3_%"),
                "score_qualidade": r.get("score_qualidade"),
                "ml_proba": r.get("ml_proba"),
            })
        multi_entry = dict(base)
        multi_entry["entradas"] = entradas
        multi_entry["n_carteiras"] = len(regs_m)
        multi_entry["tem_humano"] = any(TIPO_CARTEIRA.get(r["carteira"]) == "humano" for r in regs_m)
        multi_entry["timing_s"] = info_m.get("timing_s")
        multis.append(multi_entry)
    stats = {}
    for n in set(CARTEIRAS.values()):
        regs_n      = [r for r in todos if r.get("carteira") == n and r.get("tipo") == "COMPRA"]
        finalizados = [r for r in regs_n if r.get("categoria_final") and "aguardando" not in r.get("categoria_final", "")]
        vencedores  = [r for r in finalizados if r.get("var_pico_%") and r["var_pico_%"] > 20]
        winrate     = round(len(vencedores) / len(finalizados) * 100, 1) if finalizados else 0
        stats[n] = {
            "tipo": TIPO_CARTEIRA.get(n, "?"),
            "total": len(regs_n),
            "finalizados": len(finalizados),
            "vencedores": len(vencedores),
            "winrate": winrate,
        }
    return jsonify({
        "status":    "ok",
        "versao":    "v6.3+db",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "resumo": {
            "total_registros": len(todos),
            "compras": sum(1 for r in todos if r.get("tipo") == "COMPRA"),
            "vendas":  sum(1 for r in todos if r.get("tipo") == "VENDA"),
            "multis":  len(multis),
            "ativos":  len(ativos),
        },
        "stats_carteiras": stats,
        "tokens_ativos":   ativos,
        "alertas_multi":   multis,
        "historico":       [r for r in todos_sorted if r.get("categoria_final") and "aguardando" not in r.get("categoria_final", "")][:200],
    })
@app.route("/analise", methods=["GET"])
def rota_analise():
    if request.args.get("key") != DASHBOARD_KEY:
        return jsonify({"erro": "nao autorizado"}), 401
    try:
        import analise as mod_analise
        rows = mod_analise.buscar_registros()
        if not rows:
            return jsonify({"status": "sem dados", "msg": "Nenhum registro finalizado encontrado."})
        ts  = datetime.now().strftime("%d/%m/%Y %H:%M")
        msg = mod_analise.fmt_telegram(rows, ts)
        telegram(msg)
        return jsonify({"status": "ok", "registros": len(rows), "msg": "Análise enviada ao Telegram."})
    except Exception as e:
        return jsonify({"status": "erro", "msg": str(e)}), 500

# ══════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════
def startup():
    global _ML_MODEL, _ML_FEATS
    time.sleep(3)
    init_db()
    _ML_MODEL, _ML_FEATS = _carregar_modelos_ml()
    db_carregar_estado()
    total = sum(len(estado[n]["registros"]) for n in estado)
    telegram(
        f"🚀 <b>Monitor v6.3 + PostgreSQL iniciado!</b>\n\n"
        f"📂 {total} registros restaurados do banco\n\n"
        "🤖 carteira_A | 🤖 carteira_B\n"
        "👤 carteira_C | 👤 carteira_D"
    )
    log("✅ Monitor v6.3+db — aguardando transações")
    threading.Timer(24 * 60 * 60, enviar_csv_diario).start()
    threading.Timer(6 * 60 * 60, verificar_calibracao).start()
    log("🧠 Verificação de calibração agendada (6h)")
if __name__ == "__main__":
    log("🚀 MONITOR v6.3+DB INICIADO")
    for addr, nome in CARTEIRAS.items():
        log(f"   {nome}: {addr[:20]}...")
    threading.Thread(target=startup, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
