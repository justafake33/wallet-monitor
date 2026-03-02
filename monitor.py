import requests
import pandas as pd
import time
import threading
import os
from datetime import datetime
from flask import Flask, request, jsonify

# ╔══════════════════════════════════════════════════════════╗
# ║           MONITOR DE CARTEIRAS SOLANA — v6.1            ║
# ║                                                          ║
# ║  Novidades v6.0:                                         ║
# ║  • Parser agnóstico — captura Axiom, Photon, Trojan      ║
# ║  • Monitoramento de VENDA (CSV, sem notificação indiv.)  ║
# ║  • Validação de mint address no multi-carteira           ║
# ║  • Score de qualidade no alerta (0-10)                   ║
# ║  • Alerta de saída quando T1 ≥ 50%                       ║
# ║                                                          ║
# ║  Novidades v6.1:                                         ║
# ║  • Holders na bonding curve via pump.fun API             ║
# ║  • Holders pós-migração via Helius (automático)          ║
# ║  • Bonding curve progress % no alerta                    ║
# ║  • Dev saiu? detectado em ambas as fases                 ║
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
    "So11111111111111111111111111111111111111112",    # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", # USDT
    "8S4Hk9bMLTTCBzBrFGSRcPbHiWbVXKpmWHvEMPEELXXt",
    "11111111111111111111111111111111",
}

# ── ESTADO GLOBAL ─────────────────────────────────────────
estado = {
    nome: {
        "tokens_conhecidos": set(),   # compras já processadas
        "registros":         [],
        "pendentes":         {},
        "ultimo_save":       time.time(),
        "arquivo_csv":       f"monitoramento_{nome}.csv",
    }
    for nome in set(CARTEIRAS.values())
}

mints_globais     = {}    # mint → {carteira: timestamp}  — multi-carteira
signatures_vistas = set() # deduplicação
app = Flask(__name__)


# ══════════════════════════════════════════════════════════
# UTILS
# ══════════════════════════════════════════════════════════
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ══════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════
def telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code != 200:
            log(f"⚠️  Telegram status {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log(f"⚠️  Telegram erro: {e}")


# ══════════════════════════════════════════════════════════
# SCORE DE QUALIDADE
# ══════════════════════════════════════════════════════════
def calcular_score(mc_t0, liq_t0, txns, ratio_vol_mc, idade_min, dex):
    """
    Retorna (score 0-10, emoji, descricao)
    Baseado na análise dos 10 tokens multi-carteira reais.
    """
    score = 0

    # Vol/MC — indicador mais importante
    if ratio_vol_mc and ratio_vol_mc >= 3:    score += 3
    elif ratio_vol_mc and ratio_vol_mc >= 1.5: score += 2
    elif ratio_vol_mc and ratio_vol_mc >= 1:   score += 1

    # Txns — moderadas = sinal real, altas = suspeito
    if txns and 100 <= txns <= 450:           score += 2
    elif txns and txns < 100:                 score += 1
    elif txns and txns > 500:                 score -= 2  # padrão artificial

    # Liquidez $0 = ainda na bonding curve
    if liq_t0 == 0:                           score += 2

    # Idade do token
    if idade_min and idade_min <= 15:         score += 2
    elif idade_min and idade_min <= 30:       score += 1

    # DEX
    if dex == "pumpfun":                      score += 1

    # Vol/MC muito baixo = sem tração
    if ratio_vol_mc and ratio_vol_mc < 0.8:   score -= 2

    score = max(0, min(10, score))

    if score >= 7:
        return score, "🟢", "ALTA CONFIANÇA"
    elif score >= 4:
        return score, "🟡", "MODERADO"
    else:
        return score, "🔴", "BAIXA CONFIANÇA"


# ══════════════════════════════════════════════════════════
# CATEGORIZAÇÃO
# ══════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════
# DADOS DO TOKEN — DEXSCREENER + HELIUS FALLBACK
# ══════════════════════════════════════════════════════════
def get_dados_token(mint):
    preco = mc = liq = volume = 0
    dex = nome = "?"
    txns_5min = 0
    idade_min = None
    fonte = "dexscreener"

    # 1️⃣ DexScreener
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
            if mc > 0:
                return preco, mc, liq, volume, dex, nome, txns_5min, idade_min, fonte
    except:
        pass

    # 2️⃣ Fallback: token ainda na pump.fun bonding curve
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
            result     = r.json().get("result", {}).get("value", {})
            supply     = float(result.get("uiAmount", 0))
            sol_price  = get_sol_price()
            tokens_sold = max(0, 1_000_000_000 - supply)
            virtual_sol = 30 + (tokens_sold / 1_000_000_000) * 800
            preco_sol   = virtual_sol / (793_000_000 - tokens_sold) if tokens_sold < 793_000_000 else 0
            preco       = preco_sol * sol_price if sol_price else None
            mc          = round(preco * 1_000_000_000, 0) if preco else 0
            liq         = round(virtual_sol * sol_price, 0) if sol_price else 0
    except:
        pass

    return preco, mc, liq, volume, dex, nome, txns_5min, idade_min, fonte


def get_sol_price():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=5,
        )
        return r.json().get("solana", {}).get("usd", 0)
    except:
        return 0


# ══════════════════════════════════════════════════════════
# DADOS DE HOLDERS — v6.1
# Bonding curve  → pump.fun API (dados reais imediatos)
# Pós-migração   → Helius getTokenLargestAccounts
# ══════════════════════════════════════════════════════════
def get_holder_data(mint, liq_t0=0, dev_wallet=None):
    """
    Retorna:
      - holders_count : total de holders
      - top1_pct      : % do maior holder
      - top10_pct     : % dos 10 maiores holders
      - dev_saiu      : True / False / None
      - bc_progress   : % da bonding curve preenchida (só bonding curve)
    """
    holders_count = None
    top1_pct      = None
    top10_pct     = None
    dev_saiu      = None
    bc_progress   = None

    # ── Bonding curve: usa pump.fun API (liq = $0)
    if liq_t0 == 0:
        try:
            r = requests.get(
                f"https://frontend-api.pump.fun/coins/{mint}",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                data          = r.json()
                holders_count = data.get("holder_count")
                bc_progress   = data.get("bonding_curve_progress")  # 0-100%
                dev_wallet_bc = data.get("creator")

                # Dev saiu? — verifica via reply
                if dev_wallet_bc:
                    try:
                        r2 = requests.get(
                            f"https://frontend-api.pump.fun/coins/{mint}/holders",
                            timeout=8,
                            headers={"User-Agent": "Mozilla/5.0"},
                        )
                        if r2.status_code == 200:
                            holders_list = r2.json()
                            top_wallets  = [h.get("owner", "") for h in holders_list[:20]]
                            dev_saiu     = dev_wallet_bc not in top_wallets

                            # Top holder %
                            total_supply = 1_000_000_000  # pump.fun sempre 1B
                            if holders_list:
                                top1_bal  = holders_list[0].get("balance", 0) if holders_list else 0
                                top10_bal = sum(h.get("balance", 0) for h in holders_list[:10])
                                top1_pct  = round(top1_bal  / total_supply * 100, 1)
                                top10_pct = round(top10_bal / total_supply * 100, 1)
                    except:
                        pass
        except Exception as e:
            log(f"⚠️  pump.fun holder_data erro: {e}")

        return holders_count, top1_pct, top10_pct, dev_saiu, bc_progress

    # ── Pós-migração: usa Helius getTokenLargestAccounts
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]},
            timeout=8,
        )
        total_supply = 0
        if r.status_code == 200:
            total_supply = float(r.json().get("result", {}).get("value", {}).get("uiAmount", 0))

        if total_supply > 0:
            r2 = requests.post(
                f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]},
                timeout=8,
            )
            if r2.status_code == 200:
                accounts = r2.json().get("result", {}).get("value", [])
                if accounts:
                    holders_count = len(accounts)
                    top1_amount   = float(accounts[0].get("uiAmount", 0))
                    top10_amount  = sum(float(a.get("uiAmount", 0)) for a in accounts[:10])
                    top1_pct      = round(top1_amount  / total_supply * 100, 1)
                    top10_pct     = round(top10_amount / total_supply * 100, 1)
                    if dev_wallet:
                        holder_addresses = [a.get("address", "") for a in accounts]
                        dev_saiu = dev_wallet not in holder_addresses
    except Exception as e:
        log(f"⚠️  helius holder_data erro: {e}")

    return holders_count, top1_pct, top10_pct, dev_saiu, bc_progress


# ══════════════════════════════════════════════════════════
# PARSER AGNÓSTICO DE TRANSAÇÃO — NOVO v6.0
# ══════════════════════════════════════════════════════════
def extrair_mudancas_token(tx, carteira_addr):
    """
    Extrai mudanças de token de uma transação de forma agnóstica.
    Suporta: pumpfun nativo, Axiom, Photon, Trojan, Jupiter e outros agregadores.

    Retorna lista de dicts:
      [{mint, amount (positivo=compra, negativo=venda)}]
    """
    mudancas = {}

    # ── Método 1: tokenBalanceChanges (modo enhanced do Helius)
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
            if amount == 0:
                continue
            # Acumula (pode ter múltiplas mudanças para o mesmo mint)
            mudancas[mint] = mudancas.get(mint, 0) + amount

    # ── Método 2: nativeTransfers + tokenTransfers (para Axiom/Photon)
    # Alguns agregadores usam tokenTransfers ao invés de tokenBalanceChanges
    for transfer in tx.get("tokenTransfers", []):
        mint    = transfer.get("mint", "")
        to_acc  = transfer.get("toUserAccount", "")
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
            # Recebeu tokens = COMPRA
            mudancas[mint] = mudancas.get(mint, 0) + amount
        elif from_acc == carteira_addr:
            # Enviou tokens = VENDA
            mudancas[mint] = mudancas.get(mint, 0) - amount

    return [{"mint": m, "amount": a} for m, a in mudancas.items()]


# ══════════════════════════════════════════════════════════
# ALERTA MULTI-CARTEIRA
# ══════════════════════════════════════════════════════════
def checar_multi_carteira(mint, nome_token, carteira_atual, mc_t0, liq_t0,
                           ratio_vol_mc, idade_min, score, score_emoji, score_desc,
                           holders_count=None, top1_pct=None, top10_pct=None,
                           dev_saiu=None, bc_progress=None):
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

    # Timing
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

    linhas = [
        f"  • <b>{c}</b> comprou há {round((agora-ts)/60,1)} min"
        for c, ts in recentes.items()
    ]

    # Linha de holders
    holder_linha = ""
    if holders_count:
        holder_linha += f"\n👥 Holders: <b>{holders_count}</b>"
    if top1_pct is not None:
        holder_linha += f" | Top holder: <b>{top1_pct}%</b>"
    if top10_pct is not None:
        holder_linha += f" | Top 10: <b>{top10_pct}%</b>"
    if dev_saiu is True:
        holder_linha += "\n✅ Dev saiu"
    elif dev_saiu is False:
        holder_linha += "\n⚠️ Dev ainda segura"
    if bc_progress is not None:
        holder_linha += f"\n📈 Bonding curve: <b>{bc_progress:.0f}%</b>"

    telegram(
        f"{urgencia}\n\n"
        f"Token: <b>{nome_token}</b>\n"
        f"Mint: <code>{mint}</code>\n\n"
        f"<b>{carteira_atual}</b> comprou agora\n"
        + "\n".join(linhas) + "\n\n"
        f"⏱ Timing: <b>{timing_str}</b>\n\n"
        f"💰 MC: <b>${mc_t0:,.0f}</b>\n"
        f"💧 Liquidez: <b>${liq_t0:,.0f}</b>\n"
        f"📊 Vol/MC: <b>{ratio_vol_mc:.1f}x</b>\n"
        f"🕐 Idade: <b>{idade_min:.0f} min</b>"
        f"{holder_linha}\n\n"
        f"Score: {score_emoji} <b>{score}/10 — {score_desc}</b>\n\n"
        f"🔗 https://pump.fun/{mint}"
    )
    log(f"🚨 MULTI-CARTEIRA: {nome_token} — {carteira_atual} + {list(recentes.keys())} | timing={timing_str}")


# ══════════════════════════════════════════════════════════
# ALERTA DE VENDA — NOVO v6.0
# ══════════════════════════════════════════════════════════
def processar_venda(carteira_addr, nome, mint, amount_vendido, tx):
    est = estado[nome]

    # Busca registro de compra correspondente
    reg = None
    for r in est["registros"]:
        if r.get("token_mint") == mint:
            reg = r
            break

    preco_atual, mc_atual, liq_atual, _, _, nome_token, _, _, _ = get_dados_token(mint)
    nome_token = reg["nome"] if reg else nome_token

    # Calcular PnL se tiver compra registrada
    variacao = None
    if reg and reg.get("p_t0") and preco_atual:
        variacao = round((preco_atual - reg["p_t0"]) / reg["p_t0"] * 100, 2)

    # Apenas log — sem notificação Telegram (venda individual polui o canal)
    log(f"🔴 [{nome}] VENDA: {nome_token} | MC: ${mc_atual:,.0f} | "
        f"variação: {f'{variacao:+.1f}%' if variacao is not None else '—'}")

    # Registrar venda no CSV para análise posterior
    data = datetime.fromtimestamp(tx.get("timestamp", time.time())).strftime("%Y-%m-%d %H:%M:%S")
    est["registros"].append({
        "data_compra":     data,
        "carteira":        nome,
        "token_mint":      mint,
        "nome":            nome_token,
        "dex":             "venda",
        "fonte_dados":     "venda",
        "quantidade":      round(abs(amount_vendido), 4),
        "signature":       tx.get("signature", ""),
        "tipo":            "VENDA",
        "mc_t0":           mc_atual,
        "var_desde_compra": variacao,
        "categoria_final": "🔴 VENDA",
    })


# ══════════════════════════════════════════════════════════
# CHECKPOINTS T1 / T2 / T3
# ══════════════════════════════════════════════════════════
def agendar_checkpoints(nome, mint):
    threading.Timer(5  * 60, checar_checkpoint, args=[nome, mint, "t1"]).start()
    threading.Timer(15 * 60, checar_checkpoint, args=[nome, mint, "t2"]).start()
    threading.Timer(45 * 60, checar_checkpoint, args=[nome, mint, "t3"]).start()


def checar_checkpoint(nome, mint, checkpoint):
    est = estado[nome]
    if mint not in est["pendentes"]:
        return

    reg   = est["registros"][est["pendentes"][mint]["idx"]]
    preco, mc, liq, volume, _, _, txns_5min, _, _ = get_dados_token(mint)
    ratio = round(volume / reg["mc_t0"], 2) if reg.get("mc_t0", 0) > 0 else None

    if checkpoint == "t1":
        var_t1 = round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None
        reg.update({
            "p_t1":            preco,
            "mc_t1":           mc,
            "liq_t1":          liq,
            "volume_t1":       volume,
            "txns5m_t1":       txns_5min,
            "var_liq_t1":      round((liq - reg["liq_t0"]) / reg["liq_t0"] * 100, 2) if reg.get("liq_t0", 0) > 0 else None,
            "ratio_vol_mc_t1": ratio,
            "var_t1_%":        var_t1,
            "veredito_t1":     veredito_parcial(reg["mc_t0"], mc, "5min"),
        })
        if mc > (reg.get("mc_pico") or 0):
            reg["mc_pico"] = mc

        log(f"  ⏱️  [{nome}] T1 {reg['nome'][:20]} | MC: ${mc:,.0f} | {reg['veredito_t1']}")

        # ── ALERTA DE SAÍDA — novo v6.0
        if var_t1 and var_t1 >= 100:
            telegram(
                f"🚨 <b>ALERTA DE SAÍDA — T1 EXPLOSIVO</b>\n\n"
                f"Token: <b>{reg['nome']}</b>\n"
                f"Carteira: <b>{reg['carteira']}</b>\n\n"
                f"📈 T1: <b>+{var_t1:.0f}%</b> em 5 minutos\n"
                f"💰 MC atual: <b>${mc:,.0f}</b>\n\n"
                f"⚠️ <i>Análise histórica: tokens com T1 ≥100% caíram na maioria dos casos após o pico. Considere sair agora ou parcialmente.</i>\n\n"
                f"🔗 https://pump.fun/{reg['token_mint']}"
            )
        elif var_t1 and var_t1 >= 50:
            telegram(
                f"⚠️ <b>ALERTA DE SAÍDA — T1 FORTE</b>\n\n"
                f"Token: <b>{reg['nome']}</b>\n"
                f"Carteira: <b>{reg['carteira']}</b>\n\n"
                f"📈 T1: <b>+{var_t1:.0f}%</b> em 5 minutos\n"
                f"💰 MC atual: <b>${mc:,.0f}</b>\n\n"
                f"💡 <i>Considere realizar parte da posição.</i>\n\n"
                f"🔗 https://pump.fun/{reg['token_mint']}"
            )

    elif checkpoint == "t2":
        reg.update({
            "p_t2":            preco,
            "mc_t2":           mc,
            "liq_t2":          liq,
            "volume_t2":       volume,
            "txns5m_t2":       txns_5min,
            "var_liq_t2":      round((liq - reg.get("liq_t1", liq)) / reg.get("liq_t1", liq) * 100, 2) if reg.get("liq_t1", 0) > 0 else None,
            "ratio_vol_mc_t2": ratio,
            "var_t2_%":        round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None,
            "veredito_t2":     veredito_parcial(reg.get("mc_t1"), mc, "15min"),
        })
        if mc > (reg.get("mc_pico") or 0):
            reg["mc_pico"] = mc
        log(f"  ⏱️  [{nome}] T2 {reg['nome'][:20]} | MC: ${mc:,.0f} | {reg['veredito_t2']}")

    elif checkpoint == "t3":
        reg.update({
            "p_t3":            preco,
            "mc_t3":           mc,
            "liq_t3":          liq,
            "volume_t3":       volume,
            "txns5m_t3":       txns_5min,
            "var_liq_t3":      round((liq - reg.get("liq_t2", liq)) / reg.get("liq_t2", liq) * 100, 2) if reg.get("liq_t2", 0) > 0 else None,
            "ratio_vol_mc_t3": ratio,
            "var_t3_%":        round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None,
            "veredito_t3":     veredito_parcial(reg.get("mc_t2"), mc, "45min"),
        })
        if mc > (reg.get("mc_pico") or 0):
            reg["mc_pico"] = mc
        reg["var_pico_%"]      = round((reg["mc_pico"] - reg["mc_t0"]) / reg["mc_t0"] * 100, 2) if reg.get("mc_t0") else None
        reg["categoria_final"] = categoria_final(reg)
        log(f"  ✅ [{nome}] FINAL {reg['nome'][:20]} | MC: ${mc:,.0f} | {reg['categoria_final']}")
        del est["pendentes"][mint]
        salvar(nome)


# ══════════════════════════════════════════════════════════
# PROCESSAR TX — NOVO v6.0 (agnóstico)
# ══════════════════════════════════════════════════════════
def processar_tx(tx, carteira_addr, nome):
    est = estado[nome]

    # Ignorar transferências simples de SOL
    if tx.get("type") == "TRANSFER" and tx.get("source") == "SYSTEM_PROGRAM":
        return

    # Extrair mudanças de token de forma agnóstica
    mudancas = extrair_mudancas_token(tx, carteira_addr)

    for mudanca in mudancas:
        mint   = mudanca["mint"]
        amount = mudanca["amount"]

        if amount == 0:
            continue

        # ── VENDA detectada
        if amount < 0:
            processar_venda(carteira_addr, nome, mint, amount, tx)
            continue

        # ── COMPRA detectada
        if mint in est["tokens_conhecidos"]:
            continue  # já processou essa compra

        est["tokens_conhecidos"].add(mint)
        data = datetime.fromtimestamp(tx.get("timestamp", time.time())).strftime("%Y-%m-%d %H:%M:%S")

        # Dados do token
        preco_t0, mc_t0, liq_t0, volume_t0, dex, nome_token, txns_5min, idade_min, fonte = get_dados_token(mint)
        ratio_vol_mc_t0 = round(volume_t0 / mc_t0, 2) if mc_t0 > 0 else None
        token_antigo    = "sim" if (idade_min and idade_min > 1440) else "não"

        # Score de qualidade
        score, score_emoji, score_desc = calcular_score(
            mc_t0, liq_t0, txns_5min, ratio_vol_mc_t0, idade_min, dex
        )

        # Holders data
        holders_count = top1_pct = top10_pct = dev_saiu = bc_progress = None
        try:
            holders_count, top1_pct, top10_pct, dev_saiu, bc_progress = get_holder_data(mint, liq_t0=liq_t0)
        except Exception as e:
            log(f"⚠️  holders erro [{nome_token}]: {e}")

        # Log bonding curve progress se disponível
        flag_antigo = f" ⚠️ TOKEN ANTIGO ({idade_min/1440:.0f} dias)" if token_antigo == "sim" else ""
        holders_str = f"Holders: {holders_count}" if holders_count else "Holders: —"
        bc_str      = f" | BC: {bc_progress:.0f}%" if bc_progress is not None else ""
        dev_str     = " | Dev: ✅saiu" if dev_saiu else (" | Dev: ⚠️segura" if dev_saiu is False else "")

        log(
            f"🆕 [{nome}] {nome_token} | DEX: {dex} | "
            f"MC: ${mc_t0:,.0f} | Liq: ${liq_t0:,.0f} | "
            f"Txns: {txns_5min} | Vol/MC: {ratio_vol_mc_t0 if ratio_vol_mc_t0 else '—'}x | "
            f"Idade: {f'{idade_min:.0f}' if idade_min else '—'}min | "
            f"{holders_str}{bc_str}{dev_str} | Score: {score}/10{flag_antigo}"
        )

        idx = len(est["registros"])
        est["registros"].append({
            # Identificação
            "data_compra":     data,
            "carteira":        nome,
            "token_mint":      mint,
            "nome":            nome_token,
            "dex":             dex,
            "fonte_dados":     fonte,
            "quantidade":      round(amount, 4),
            "signature":       tx.get("signature", ""),
            "tipo":            "COMPRA",
            # T0
            "p_t0":            preco_t0,
            "mc_t0":           mc_t0,
            "liq_t0":          liq_t0,
            "volume_t0":       volume_t0,
            "txns5m_t0":       txns_5min,
            "idade_min":       idade_min,
            "token_antigo":    token_antigo,
            "ratio_vol_mc_t0": ratio_vol_mc_t0,
            "score_qualidade": score,
            # Holders
            "holders_count":   holders_count,
            "top1_pct":        top1_pct,
            "top10_pct":       top10_pct,
            "dev_saiu":        dev_saiu,
            "bc_progress":     bc_progress,
            # T1
            "p_t1": None, "mc_t1": None, "liq_t1": None,
            "volume_t1": None, "txns5m_t1": None,
            "var_liq_t1": None, "ratio_vol_mc_t1": None,
            "var_t1_%": None, "veredito_t1": None,
            # T2
            "p_t2": None, "mc_t2": None, "liq_t2": None,
            "volume_t2": None, "txns5m_t2": None,
            "var_liq_t2": None, "ratio_vol_mc_t2": None,
            "var_t2_%": None, "veredito_t2": None,
            # T3
            "p_t3": None, "mc_t3": None, "liq_t3": None,
            "volume_t3": None, "txns5m_t3": None,
            "var_liq_t3": None, "ratio_vol_mc_t3": None,
            "var_t3_%": None, "veredito_t3": None,
            # Conclusão
            "mc_pico":         mc_t0,
            "var_pico_%":      None,
            "categoria_final": "⏳ aguardando",
        })

        est["pendentes"][mint] = {"idx": idx}

        # Alerta multi-carteira (com score + holders)
        checar_multi_carteira(
            mint, nome_token, nome,
            mc_t0, liq_t0, ratio_vol_mc_t0 or 0, idade_min or 0,
            score, score_emoji, score_desc,
            holders_count=holders_count,
            top1_pct=top1_pct,
            top10_pct=top10_pct,
            dev_saiu=dev_saiu,
            bc_progress=bc_progress,
        )
        agendar_checkpoints(nome, mint)


# ══════════════════════════════════════════════════════════
# SALVAR CSV
# ══════════════════════════════════════════════════════════
def salvar(nome):
    est = estado[nome]
    if not est["registros"]:
        return
    pd.DataFrame(est["registros"]).to_csv(est["arquivo_csv"], index=False)
    log(f"💾 [{nome}] Salvo — {len(est['registros'])} registros")


# ══════════════════════════════════════════════════════════
# WEBHOOK ENDPOINTS
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

            for acc in tx.get("accountData", []):
                addr = acc.get("account", "")
                if addr in CARTEIRAS:
                    processar_tx(tx, addr, CARTEIRAS[addr])
                    break

        return jsonify({"ok": True})
    except Exception as e:
        import traceback
        log(f"⚠️  Webhook erro: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False}), 500


@app.route("/", methods=["GET"])
def health():
    total = sum(len(estado[n]["registros"]) for n in estado)
    pend  = sum(len(estado[n]["pendentes"]) for n in estado)
    compras = sum(1 for n in estado for r in estado[n]["registros"] if r.get("tipo") == "COMPRA")
    vendas  = sum(1 for n in estado for r in estado[n]["registros"] if r.get("tipo") == "VENDA")
    return jsonify({
        "status":    "running v6.1",
        "registros": total,
        "compras":   compras,
        "vendas":    vendas,
        "pendentes": pend,
    })


# ══════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════
def startup():
    time.sleep(3)
    telegram(
        "🚀 <b>Monitor v6.1 iniciado!</b>\n\n"
        "Novidades v6.1:\n"
        "• 👥 Holders na bonding curve via pump.fun API\n"
        "• 📈 Bonding curve progress % no alerta\n"
        "• ✅ Dev saiu? detectado em tempo real\n\n"
        "Mantido de v6.0:\n"
        "• 🔌 Parser agnóstico (Axiom, Photon, Trojan)\n"
        "• 🔴 Vendas rastreadas no CSV\n"
        "• 🎯 Score de qualidade 0-10\n"
        "• ⚠️ Alerta de saída T1 ≥ 50%\n\n"
        "Monitorando 3 carteiras:\n"
        "• carteira_A\n• carteira_B\n• carteira_C"
    )
    log("✅ Monitor v6.1 — aguardando transações")


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    log("🚀 MONITOR v6.1 INICIADO — Webhook mode")
    for addr, nome in CARTEIRAS.items():
        log(f"   {nome}: {addr[:20]}...")

    threading.Thread(target=startup, daemon=True).start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
