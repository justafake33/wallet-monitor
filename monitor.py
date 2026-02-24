import requests
import pandas as pd
import time
import threading
from datetime import datetime
from flask import Flask, request, jsonify

# ╔══════════════════════════════════════════════════════════╗
# ║           MONITOR DE CARTEIRAS SOLANA — v5              ║
# ║                                                          ║
# ║  Arquitetura: Webhook (Helius avisa → sem polling)       ║
# ║  Consumo: ~1 crédito por transação detectada             ║
# ║  vs v4: 100 créditos por chamada a cada 30s              ║
# ╚══════════════════════════════════════════════════════════╝

# ── CONFIGURAÇÃO ──────────────────────────────────────────
HELIUS_API_KEY = "4f586430-90ef-4c8f-9800-b98bfe5f1151"

TELEGRAM_TOKEN = "8319320909:AAFnhGkFS1YxhthhE4RolutJScEjBCjIvrA"
TELEGRAM_CHAT  = "-5284184650"

CARTEIRAS = {
    "GijFWw4oNyh9ko3FaZforNsi3jk6wDovARpkKahPD4o5": "carteira_A",
    "ANfB2knFb7pC7jKadHnSP4xKZ31KJGNLhWRo89LWsFeW": "carteira_B",
    "43C9gHfJ7YgqKv5ft3hodFgumydv1nEiNHD1PuANufk5": "carteira_C",
}

WEBHOOK_URL   = "https://wallet-monitor-production-fef3.up.railway.app/webhook"
SALVAR_A_CADA = 10  # minutos

TOKENS_IGNORAR = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "8S4Hk9bMLTTCBzBrFGSRcPbHiWbVXKpmWHvEMPEELXXt",
    "11111111111111111111111111111111",
}

# ── ESTADO GLOBAL ─────────────────────────────────────────
estado = {
    nome: {
        "tokens_conhecidos": set(),
        "registros":         [],
        "pendentes":         {},
        "ultimo_save":       time.time(),
        "arquivo_csv":       f"monitoramento_{nome}.csv",
    }
    for nome in set(CARTEIRAS.values())
}

mints_globais = {}  # mint → {carteira: timestamp}
app = Flask(__name__)


# ── UTILS ─────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ── TELEGRAM ──────────────────────────────────────────────
def telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log(f"⚠️  Telegram erro: {e}")


# ── CATEGORIZAÇÃO ─────────────────────────────────────────
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
    if mc0 == 0:
        return "❓ SEM DADOS SUFICIENTES"
    pico      = max(mc1, mc2, mc3)
    var_pico  = (pico - mc0) / mc0 * 100 if mc0 > 0 else 0
    var_final = (mc3  - mc0) / mc0 * 100 if mc0 > 0 and mc3 > 0 else None
    if   var_pico > 200 and var_final and var_final >  100: return "🏆 VENCEDOR — Subiu forte e manteve"
    elif var_pico > 200 and var_final and var_final <    0: return "🎯 PUMP & DUMP — Subiu e colapsou"
    elif var_pico >  50 and var_final and var_final >   20: return "📈 BOM TRADE — Crescimento sólido"
    elif var_pico >  50 and var_final and var_final <  -20: return "⚠️  ARMADILHA — Pico rápido e queda"
    elif var_final and var_final >  20:                     return "📊 CRESCIMENTO ESTÁVEL"
    elif var_final and var_final > -20:                     return "➡️  LATERAL — Pouco movimento"
    elif var_final is not None:                             return "💀 MORREU — Queda consistente"
    else:                                                   return "❓ DADOS INCOMPLETOS"


# ── DEXSCREENER ───────────────────────────────────────────
def get_dados_token(mint):
    preco = mc = liq = volume = 0
    dex = nome = "?"
    txns_5min = 0
    idade_min = None
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=8,
        )
        pairs = r.json().get("pairs") or []
        if pairs:
            par       = sorted(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0, reverse=True)[0]
            preco     = float(par["priceUsd"]) if par.get("priceUsd") else None
            mc        = par.get("marketCap") or 0
            liq       = par.get("liquidity", {}).get("usd") or 0
            volume    = par.get("volume", {}).get("h24") or 0
            dex       = par.get("dexId", "?")
            nome      = par.get("baseToken", {}).get("name", "?")
            m5        = par.get("txns", {}).get("m5", {})
            txns_5min = m5.get("buys", 0) + m5.get("sells", 0)
            criado_ts = par.get("pairCreatedAt")
            if criado_ts:
                idade_min = round((time.time() - criado_ts / 1000) / 60, 1)
    except:
        pass
    return preco, mc, liq, volume, dex, nome, txns_5min, idade_min


# ── ALERTA MULTI-CARTEIRA ─────────────────────────────────
def checar_multi_carteira(mint, nome_token, carteira_atual, mc_t0, liq_t0, ratio_vol_mc, idade_min):
    agora = time.time()
    if mint not in mints_globais:
        mints_globais[mint] = {}
    mints_globais[mint][carteira_atual] = agora

    recentes = {
        c: ts for c, ts in mints_globais[mint].items()
        if c != carteira_atual and (agora - ts) / 60 <= 60
    }
    if not recentes:
        return

    linhas = [f"  • <b>{c}</b> comprou há {round((agora-ts)/60,1)} min" for c, ts in recentes.items()]
    telegram(
        f"🚨 <b>MULTI-CARTEIRA DETECTADA</b>\n\n"
        f"Token: <b>{nome_token}</b>\n"
        f"Mint: <code>{mint}</code>\n\n"
        f"<b>{carteira_atual}</b> comprou agora\n"
        + "\n".join(linhas) + "\n\n"
        f"MC: <b>${mc_t0:,.0f}</b>\n"
        f"Liquidez: <b>${liq_t0:,.0f}</b>\n"
        f"Ratio Vol/MC: <b>{ratio_vol_mc:.1f}x</b>\n"
        f"Idade: <b>{idade_min:.0f} min</b>\n\n"
        f"🔗 https://pump.fun/{mint}"
    )
    log(f"🚨 MULTI-CARTEIRA: {nome_token} — {carteira_atual} + {list(recentes.keys())}")


# ── CHECKPOINTS T1 / T2 / T3 ─────────────────────────────
def agendar_checkpoints(nome, mint):
    threading.Timer(5  * 60, checar_checkpoint, args=[nome, mint, "t1"]).start()
    threading.Timer(15 * 60, checar_checkpoint, args=[nome, mint, "t2"]).start()
    threading.Timer(45 * 60, checar_checkpoint, args=[nome, mint, "t3"]).start()


def checar_checkpoint(nome, mint, checkpoint):
    est = estado[nome]
    if mint not in est["pendentes"]:
        return

    reg   = est["registros"][est["pendentes"][mint]["idx"]]
    preco, mc, liq, volume, _, _, txns_5min, _ = get_dados_token(mint)
    ratio = round(volume / reg["mc_t0"], 2) if reg.get("mc_t0", 0) > 0 else None

    if checkpoint == "t1":
        reg.update({
            "p_t1":            preco,
            "mc_t1":           mc,
            "liq_t1":          liq,
            "volume_t1":       volume,
            "txns5m_t1":       txns_5min,
            "var_liq_t1":      round((liq - reg["liq_t0"]) / reg["liq_t0"] * 100, 2) if reg.get("liq_t0", 0) > 0 else None,
            "ratio_vol_mc_t1": ratio,
            "var_t1_%":        round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None,
            "veredito_t1":     veredito_parcial(reg["mc_t0"], mc, "5min"),
        })
        if mc > (reg.get("mc_pico") or 0):
            reg["mc_pico"] = mc
        log(f"  ⏱️  [{nome}] T1 {reg['nome'][:20]} | MC: ${mc:,.0f} | Liq: ${liq:,.0f} | "
            f"Vol/MC: {ratio if ratio else '—'}x | {reg['veredito_t1']}")

    elif checkpoint == "t2":
        reg.update({
            "p_t2":            preco,
            "mc_t2":           mc,
            "liq_t2":          liq,
            "volume_t2":       volume,
            "txns5m_t2":       txns_5min,
            "var_liq_t2":      round((liq - reg["liq_t1"]) / reg["liq_t1"] * 100, 2) if reg.get("liq_t1", 0) > 0 else None,
            "ratio_vol_mc_t2": ratio,
            "var_t2_%":        round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None,
            "veredito_t2":     veredito_parcial(reg["mc_t1"], mc, "15min"),
        })
        if mc > (reg.get("mc_pico") or 0):
            reg["mc_pico"] = mc
        log(f"  ⏱️  [{nome}] T2 {reg['nome'][:20]} | MC: ${mc:,.0f} | Liq: ${liq:,.0f} | "
            f"Vol/MC: {ratio if ratio else '—'}x | {reg['veredito_t2']}")

    elif checkpoint == "t3":
        reg.update({
            "p_t3":            preco,
            "mc_t3":           mc,
            "liq_t3":          liq,
            "volume_t3":       volume,
            "txns5m_t3":       txns_5min,
            "var_liq_t3":      round((liq - reg["liq_t2"]) / reg["liq_t2"] * 100, 2) if reg.get("liq_t2", 0) > 0 else None,
            "ratio_vol_mc_t3": ratio,
            "var_t3_%":        round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None,
            "veredito_t3":     veredito_parcial(reg["mc_t2"], mc, "45min"),
        })
        if mc > (reg.get("mc_pico") or 0):
            reg["mc_pico"] = mc
        reg["var_pico_%"]      = round((reg["mc_pico"] - reg["mc_t0"]) / reg["mc_t0"] * 100, 2) if reg.get("mc_t0") else None
        reg["categoria_final"] = categoria_final(reg)
        log(f"  ✅ [{nome}] FINAL {reg['nome'][:20]} | MC: ${mc:,.0f} | {reg['categoria_final']}")
        del est["pendentes"][mint]
        salvar(nome)


# ── PROCESSAR TX RECEBIDA VIA WEBHOOK ────────────────────
def processar_tx(tx, carteira_addr, nome):
    est = estado[nome]

    if tx.get("type") == "TRANSFER" and tx.get("source") == "SYSTEM_PROGRAM":
        return

    for conta in tx.get("accountData", []):
        for change in conta.get("tokenBalanceChanges", []):
            if change.get("userAccount") != carteira_addr:
                continue
            mint = change.get("mint", "")
            if not mint or mint in TOKENS_IGNORAR:
                continue
            raw = change.get("rawTokenAmount", {})
            try:
                amount = int(raw.get("tokenAmount", "0")) / (10 ** raw.get("decimals", 0))
            except:
                continue
            if amount <= 0 or mint in est["tokens_conhecidos"]:
                continue

            est["tokens_conhecidos"].add(mint)
            data = datetime.fromtimestamp(tx.get("timestamp", time.time())).strftime("%Y-%m-%d %H:%M:%S")

            preco_t0, mc_t0, liq_t0, volume_t0, dex, nome_token, txns_5min, idade_min = get_dados_token(mint)
            ratio_vol_mc_t0 = round(volume_t0 / mc_t0, 2) if mc_t0 > 0 else None

            idx = len(est["registros"])
            est["registros"].append({
                "data_compra": data, "carteira": nome,
                "token_mint": mint, "nome": nome_token,
                "dex": dex, "quantidade": round(amount, 4),
                "signature": tx.get("signature", ""),
                "p_t0": preco_t0, "mc_t0": mc_t0, "liq_t0": liq_t0,
                "volume_t0": volume_t0, "txns5m_t0": txns_5min,
                "idade_min": idade_min, "ratio_vol_mc_t0": ratio_vol_mc_t0,
                "p_t1": None, "mc_t1": None, "liq_t1": None,
                "volume_t1": None, "txns5m_t1": None,
                "var_liq_t1": None, "ratio_vol_mc_t1": None,
                "var_t1_%": None, "veredito_t1": None,
                "p_t2": None, "mc_t2": None, "liq_t2": None,
                "volume_t2": None, "txns5m_t2": None,
                "var_liq_t2": None, "ratio_vol_mc_t2": None,
                "var_t2_%": None, "veredito_t2": None,
                "p_t3": None, "mc_t3": None, "liq_t3": None,
                "volume_t3": None, "txns5m_t3": None,
                "var_liq_t3": None, "ratio_vol_mc_t3": None,
                "var_t3_%": None, "veredito_t3": None,
                "mc_pico": mc_t0, "var_pico_%": None,
                "categoria_final": "⏳ aguardando",
            })

            est["pendentes"][mint] = {"idx": idx}

            log(f"🆕 [{nome}] {nome_token} | DEX: {dex} | "
                f"MC: ${mc_t0:,.0f} | Liq: ${liq_t0:,.0f} | "
                f"Vol/MC: {ratio_vol_mc_t0 if ratio_vol_mc_t0 else '—'}x | "
                f"Idade: {f'{idade_min:.0f}' if idade_min else '—'}min")

            checar_multi_carteira(mint, nome_token, nome, mc_t0, liq_t0, ratio_vol_mc_t0 or 0, idade_min or 0)
            agendar_checkpoints(nome, mint)


# ── SALVAR CSV ────────────────────────────────────────────
def salvar(nome):
    est = estado[nome]
    if not est["registros"]:
        return
    pd.DataFrame(est["registros"]).to_csv(est["arquivo_csv"], index=False)
    log(f"💾 [{nome}] Salvo — {len(est['registros'])} registros")


# ── WEBHOOK ENDPOINTS ─────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        txs = request.get_json()
        if not txs:
            return jsonify({"ok": True})
        for tx in txs:
            for acc in tx.get("accountData", []):
                addr = acc.get("account", "")
                if addr in CARTEIRAS:
                    processar_tx(tx, addr, CARTEIRAS[addr])
                    break
        return jsonify({"ok": True})
    except Exception as e:
        log(f"⚠️  Webhook erro: {e}")
        return jsonify({"ok": False}), 500


@app.route("/", methods=["GET"])
def health():
    total = sum(len(estado[n]["registros"]) for n in estado)
    pend  = sum(len(estado[n]["pendentes"]) for n in estado)
    return jsonify({"status": "running", "registros": total, "pendentes": pend})


# ── REGISTRAR WEBHOOK NA HELIUS ───────────────────────────
def registrar_webhook():
    log("📡 Registrando webhook na Helius...")
    try:
        r = requests.post(
            f"https://api.helius.xyz/v0/webhooks?api-key={HELIUS_API_KEY}",
            json={
                "webhookURL":       WEBHOOK_URL,
                "transactionTypes": ["Any"],
                "accountAddresses": list(CARTEIRAS.keys()),
                "webhookType":      "enhanced",
            },
            timeout=15,
        )
        if r.status_code in [200, 201]:
            wid = r.json().get("webhookID", "?")
            log(f"✅ Webhook registrado! ID: {wid}")
        else:
            log(f"⚠️  Helius webhook: {r.status_code} — {r.text[:200]}")
    except Exception as e:
        log(f"⚠️  Erro ao registrar webhook: {e}")


# ── MAIN ──────────────────────────────────────────────────
if __name__ == "__main__":
    log("🚀 MONITOR v5 INICIADO — Webhook mode")
    for addr, nome in CARTEIRAS.items():
        log(f"   {nome}: {addr[:20]}...")

    registrar_webhook()

    telegram(
        "🚀 <b>Monitor v5 iniciado!</b>\n\n"
        "Modo: <b>Webhook</b> — consumo mínimo\n\n"
        "Monitorando 3 carteiras:\n"
        "• carteira_A\n• carteira_B\n• carteira_C\n\n"
        "Alerta ativo:\n"
        "• 🚨 Multi-carteira (2+ carteiras no mesmo token)"
    )

    app.run(host="0.0.0.0", port=8080)
