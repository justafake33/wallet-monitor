import requests
import pandas as pd
import time
from datetime import datetime

# ╔══════════════════════════════════════════════════════════╗
# ║           MONITOR DE CARTEIRAS SOLANA — v4              ║
# ║                                                          ║
# ║  Novidades v4:                                           ║
# ║  • Holders corrigido (getProgramAccounts via RPC)        ║
# ║  • Liquidez e variação de liquidez por intervalo         ║
# ║  • Wallets únicas em T1                                  ║
# ║  • Ratio Volume/MC em T0, T1, T2, T3                    ║
# ║  • Alerta Telegram apenas em multi-carteira              ║
# ╚══════════════════════════════════════════════════════════╝

# ── CONFIGURAÇÃO ──────────────────────────────────────────
HELIUS_API_KEY = "4f586430-90ef-4c8f-9800-b98bfe5f1151"

TELEGRAM_TOKEN = "8319320909:AAFnhGkFS1YxhthhE4RolutJScEjBCjIvrA"
TELEGRAM_CHAT  = "6959328592"

CARTEIRAS = {
    "carteira_A": "GijFWw4oNyh9ko3FaZforNsi3jk6wDovARpkKahPD4o5",
    "carteira_B": "ANfB2knFb7pC7jKadHnSP4xKZ31KJGNLhWRo89LWsFeW",
    "carteira_C": "43C9gHfJ7YgqKv5ft3hodFgumydv1nEiNHD1PuANufk5",
}

INTERVALO_VERIFICACAO = 30    # segundos entre cada ciclo
SALVAR_A_CADA         = 10    # minutos entre cada save do CSV
ALERTA_MULTI_JANELA   = 60    # minutos — janela para considerar compras simultâneas

# Tokens nativos/stablecoins para ignorar
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
        "tokens_conhecidos": set(),
        "registros":         [],
        "pendentes":         {},
        "ultimo_save":       time.time(),
        "arquivo_csv":       f"monitoramento_{nome}.csv",
    }
    for nome in CARTEIRAS
}

# mint → {carteira: timestamp} — rastreia compras entre carteiras
mints_globais = {}


# ── UTILS ─────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ── TELEGRAM ──────────────────────────────────────────────
def telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT,
            "text":       msg,
            "parse_mode": "HTML",
        }, timeout=8)
    except Exception as e:
        log(f"⚠️  Telegram erro: {e}")


# ── CATEGORIZAÇÃO ─────────────────────────────────────────
def veredito_parcial(mc_anterior, mc_atual, tempo):
    """Classifica variação entre dois checkpoints."""
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
    """Determina categoria final com base na trajetória T0→T3."""
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


# ── HELIUS — TRANSAÇÕES ───────────────────────────────────
def get_ultimas_txs(carteira):
    try:
        r = requests.get(
            f"https://api.helius.xyz/v0/addresses/{carteira}/transactions",
            params={"api-key": HELIUS_API_KEY, "limit": 10},
            timeout=15,
        )
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        log(f"⚠️  Erro txs ({carteira[:8]}...): {e}")
        return []


# ── HELIUS RPC — HOLDERS ──────────────────────────────────
def get_holders(mint):
    """Conta holders reais via getProgramAccounts filtrado pelo mint."""
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
            json={
                "jsonrpc": "2.0",
                "id":      1,
                "method":  "getProgramAccounts",
                "params": [
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    {
                        "filters": [
                            {"dataSize": 165},
                            {"memcmp": {"offset": 0, "bytes": mint}},
                        ],
                        "encoding": "base64",
                    },
                ],
            },
            timeout=10,
        )
        if r.status_code == 200:
            return len(r.json().get("result", []))
    except:
        pass
    return 0


# ── HELIUS — WALLETS ÚNICAS ───────────────────────────────
def get_wallets_unicas(mint):
    """Conta carteiras únicas que interagiram com o token nas últimas 100 txs."""
    try:
        r = requests.get(
            f"https://api.helius.xyz/v0/addresses/{mint}/transactions",
            params={"api-key": HELIUS_API_KEY, "limit": 100},
            timeout=10,
        )
        if r.status_code == 200:
            wallets = set()
            for tx in r.json():
                for acc in tx.get("accountData", []):
                    for change in acc.get("tokenBalanceChanges", []):
                        w = change.get("userAccount")
                        if w:
                            wallets.add(w)
            return len(wallets)
    except:
        pass
    return 0


# ── DEXSCREENER — DADOS DO TOKEN ──────────────────────────
def get_dados_token(mint):
    """Retorna (preco, mc, liq, volume, dex, nome, txns_5min, idade_min, criado_ts)."""
    preco = mc = liq = volume = 0
    dex = nome = "?"
    txns_5min = 0
    idade_min = criado_ts = None
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
    return preco, mc, liq, volume, dex, nome, txns_5min, idade_min, criado_ts


# ── EXTRAÇÃO DE NOVAS COMPRAS ─────────────────────────────
def extrair_novas_compras(txs, carteira_addr, tokens_conhecidos):
    novas = []
    for tx in txs:
        if tx.get("type") == "TRANSFER" and tx.get("source") == "SYSTEM_PROGRAM":
            continue
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
                if amount > 0 and mint not in tokens_conhecidos:
                    novas.append({
                        "mint":       mint,
                        "timestamp":  tx.get("timestamp", 0),
                        "signature":  tx.get("signature", ""),
                        "quantidade": round(amount, 4),
                    })
                    tokens_conhecidos.add(mint)
    return novas


# ── ALERTA MULTI-CARTEIRA ─────────────────────────────────
def checar_multi_carteira(mint, nome_token, carteira_atual, mc_t0, liq_t0, ratio_vol_mc, idade_min):
    """Dispara alerta no Telegram se 2+ carteiras comprarem o mesmo token."""
    agora = time.time()

    if mint not in mints_globais:
        mints_globais[mint] = {}
    mints_globais[mint][carteira_atual] = agora

    # Filtra compras dentro da janela de tempo
    compras_recentes = {
        c: ts for c, ts in mints_globais[mint].items()
        if c != carteira_atual and (agora - ts) / 60 <= ALERTA_MULTI_JANELA
    }
    if not compras_recentes:
        return

    linhas = []
    for outra_cart, outra_ts in compras_recentes.items():
        diff = round((agora - outra_ts) / 60, 1)
        linhas.append(f"  • <b>{outra_cart}</b> comprou há {diff} min")

    msg = (
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
    telegram(msg)
    log(f"🚨 MULTI-CARTEIRA: {nome_token} — {carteira_atual} + {list(compras_recentes.keys())}")


# ── CHECAR PENDENTES (T1 / T2 / T3) ──────────────────────
def checar_pendentes(nome):
    est   = estado[nome]
    agora = time.time()

    for mint, info in list(est["pendentes"].items()):
        ts  = info["ts_compra"]
        reg = est["registros"][info["idx"]]

        # ── T1 — 5 minutos ────────────────────────────────
        if not info["t1_ok"] and agora >= ts + 5 * 60:
            preco, mc, liq, volume, _, _, txns_5min, _, _ = get_dados_token(mint)
            wallets = get_wallets_unicas(mint)

            reg.update({
                "p_t1":            preco,
                "mc_t1":           mc,
                "liq_t1":          liq,
                "volume_t1":       volume,
                "txns5m_t1":       txns_5min,
                "wallets_t1":      wallets,
                "var_liq_t1":      round((liq - reg["liq_t0"]) / reg["liq_t0"] * 100, 2) if reg.get("liq_t0", 0) > 0 else None,
                "ratio_vol_mc_t1": round(volume / reg["mc_t0"], 2) if reg.get("mc_t0", 0) > 0 else None,
                "var_t1_%":        round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None,
                "veredito_t1":     veredito_parcial(reg["mc_t0"], mc, "5min"),
            })
            if mc > (reg.get("mc_pico") or 0):
                reg["mc_pico"] = mc

            info["t1_ok"] = True
            log(f"  ⏱️  [{nome}] T1 {reg['nome'][:18]} | "
                f"MC: ${mc:,.0f} | Liq: ${liq:,.0f} | "
                f"Vol/MC: {reg['ratio_vol_mc_t1']}x | "
                f"Wallets: {wallets} | {reg['veredito_t1']}")

        # ── T2 — 15 minutos ───────────────────────────────
        if not info["t2_ok"] and agora >= ts + 15 * 60:
            preco, mc, liq, volume, _, _, txns_5min, _, _ = get_dados_token(mint)

            reg.update({
                "p_t2":            preco,
                "mc_t2":           mc,
                "liq_t2":          liq,
                "volume_t2":       volume,
                "txns5m_t2":       txns_5min,
                "var_liq_t2":      round((liq - reg["liq_t1"]) / reg["liq_t1"] * 100, 2) if reg.get("liq_t1", 0) > 0 else None,
                "ratio_vol_mc_t2": round(volume / reg["mc_t0"], 2) if reg.get("mc_t0", 0) > 0 else None,
                "var_t2_%":        round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None,
                "veredito_t2":     veredito_parcial(reg["mc_t1"], mc, "15min"),
            })
            if mc > (reg.get("mc_pico") or 0):
                reg["mc_pico"] = mc

            info["t2_ok"] = True
            log(f"  ⏱️  [{nome}] T2 {reg['nome'][:18]} | "
                f"MC: ${mc:,.0f} | Liq: ${liq:,.0f} | "
                f"Vol/MC: {reg['ratio_vol_mc_t2']}x | {reg['veredito_t2']}")

        # ── T3 — 45 minutos ───────────────────────────────
        if not info["t3_ok"] and agora >= ts + 45 * 60:
            preco, mc, liq, volume, _, _, txns_5min, _, _ = get_dados_token(mint)
            holders_t3 = get_holders(mint)

            reg.update({
                "p_t3":            preco,
                "mc_t3":           mc,
                "liq_t3":          liq,
                "volume_t3":       volume,
                "txns5m_t3":       txns_5min,
                "holders_t3":      holders_t3,
                "var_liq_t3":      round((liq - reg["liq_t2"]) / reg["liq_t2"] * 100, 2) if reg.get("liq_t2", 0) > 0 else None,
                "ratio_vol_mc_t3": round(volume / reg["mc_t0"], 2) if reg.get("mc_t0", 0) > 0 else None,
                "var_t3_%":        round((preco - reg["p_t0"]) / reg["p_t0"] * 100, 2) if preco and reg.get("p_t0") else None,
                "veredito_t3":     veredito_parcial(reg["mc_t2"], mc, "45min"),
            })
            if mc > (reg.get("mc_pico") or 0):
                reg["mc_pico"] = mc
            reg["var_pico_%"]      = round((reg["mc_pico"] - reg["mc_t0"]) / reg["mc_t0"] * 100, 2) if reg.get("mc_t0") else None
            reg["categoria_final"] = categoria_final(reg)

            info["t3_ok"] = True
            log(f"  ✅ [{nome}] FINAL {reg['nome'][:18]} | "
                f"MC: ${mc:,.0f} | Holders: {holders_t3} | {reg['categoria_final']}")

            del est["pendentes"][mint]


# ── SALVAR CSV ────────────────────────────────────────────
def salvar(nome):
    est = estado[nome]
    if not est["registros"]:
        return
    pd.DataFrame(est["registros"]).to_csv(est["arquivo_csv"], index=False)
    log(f"💾 [{nome}] Salvo — {len(est['registros'])} registros")


# ── PROCESSAR CARTEIRA ────────────────────────────────────
def processar_carteira(nome, carteira_addr):
    est   = estado[nome]
    txs   = get_ultimas_txs(carteira_addr)
    novas = extrair_novas_compras(txs, carteira_addr, est["tokens_conhecidos"])

    for compra in novas:
        mint = compra["mint"]
        data = datetime.fromtimestamp(compra["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")

        preco_t0, mc_t0, liq_t0, volume_t0, dex, nome_token, txns_5min, idade_min, _ = get_dados_token(mint)
        holders_t0      = get_holders(mint)
        ratio_vol_mc_t0 = round(volume_t0 / mc_t0, 2) if mc_t0 > 0 else None

        idx = len(est["registros"])
        est["registros"].append({
            # ── Identificação
            "data_compra":     data,
            "carteira":        nome,
            "token_mint":      mint,
            "nome":            nome_token,
            "dex":             dex,
            "quantidade":      compra["quantidade"],
            "signature":       compra["signature"],
            # ── T0
            "p_t0":            preco_t0,
            "mc_t0":           mc_t0,
            "liq_t0":          liq_t0,
            "volume_t0":       volume_t0,
            "txns5m_t0":       txns_5min,
            "holders_t0":      holders_t0,
            "idade_min":       idade_min,
            "ratio_vol_mc_t0": ratio_vol_mc_t0,
            # ── T1
            "p_t1": None, "mc_t1": None, "liq_t1": None,
            "volume_t1": None, "txns5m_t1": None, "wallets_t1": None,
            "var_liq_t1": None, "ratio_vol_mc_t1": None,
            "var_t1_%": None, "veredito_t1": None,
            # ── T2
            "p_t2": None, "mc_t2": None, "liq_t2": None,
            "volume_t2": None, "txns5m_t2": None,
            "var_liq_t2": None, "ratio_vol_mc_t2": None,
            "var_t2_%": None, "veredito_t2": None,
            # ── T3
            "p_t3": None, "mc_t3": None, "liq_t3": None,
            "volume_t3": None, "txns5m_t3": None, "holders_t3": None,
            "var_liq_t3": None, "ratio_vol_mc_t3": None,
            "var_t3_%": None, "veredito_t3": None,
            # ── Conclusão
            "mc_pico":         mc_t0,
            "var_pico_%":      None,
            "categoria_final": "⏳ aguardando",
        })

        est["pendentes"][mint] = {
            "ts_compra": time.time(),
            "idx":       idx,
            "t1_ok":     False,
            "t2_ok":     False,
            "t3_ok":     False,
        }

        log(f"🆕 [{nome}] {nome_token} | DEX: {dex} | "
            f"MC: ${mc_t0:,.0f} | Liq: ${liq_t0:,.0f} | "
            f"Vol/MC: {ratio_vol_mc_t0}x | "
            f"Holders: {holders_t0} | Idade: {idade_min}min")

        # Alerta Telegram apenas em multi-carteira
        checar_multi_carteira(mint, nome_token, nome, mc_t0, liq_t0, ratio_vol_mc_t0 or 0, idade_min or 0)

    checar_pendentes(nome)

    # Auto-save a cada SALVAR_A_CADA minutos
    if time.time() - est["ultimo_save"] >= SALVAR_A_CADA * 60:
        salvar(nome)
        est["ultimo_save"] = time.time()


# ── LOOP PRINCIPAL ────────────────────────────────────────
log("🚀 MONITOR v4 INICIADO — 3 carteiras + Telegram")
for nome, addr in CARTEIRAS.items():
    log(f"   {nome}: {addr[:20]}...")

telegram(
    "🚀 <b>Monitor v4 iniciado!</b>\n\n"
    "Monitorando 3 carteiras:\n"
    "• carteira_A\n• carteira_B\n• carteira_C\n\n"
    "Alerta ativo:\n"
    "• 🚨 Multi-carteira (2+ carteiras no mesmo token)"
)

ciclo = 0
while True:
    ciclo += 1
    try:
        for nome, addr in CARTEIRAS.items():
            processar_carteira(nome, addr)
            time.sleep(1)

        if ciclo % 20 == 0:
            total = sum(len(estado[n]["registros"]) for n in CARTEIRAS)
            pend  = sum(len(estado[n]["pendentes"]) for n in CARTEIRAS)
            log(f"💓 Ciclo {ciclo} | Registros: {total} | Pendentes: {pend}")

    except Exception as e:
        log(f"⚠️  Erro no ciclo {ciclo}: {e}")

    time.sleep(INTERVALO_VERIFICACAO)
