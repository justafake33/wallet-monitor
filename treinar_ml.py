"""
Treina o modelo XGBoost e salva os .pkl na pasta do projeto.
Uso: python treinar_ml.py
"""
import os, pickle, json
import pandas as pd
import numpy as np
import psycopg2
import warnings
warnings.filterwarnings('ignore')

from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.metrics import roc_auc_score, precision_recall_curve
from sklearn.calibration import CalibratedClassifierCV
from xgboost import XGBClassifier, XGBRegressor

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:OgNvgWkjcpuFxZPHBaASjCKnLNsXKlpI@switchyard.proxy.rlwy.net:47120/railway"
)

MODEL_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 1. Carrega dados ──────────────────────────────────────────────────────────
print("Conectando ao banco Railway...")
with psycopg2.connect(DATABASE_URL) as conn:
    df = pd.read_sql("""
        SELECT id, token_mint, data_compra, score_qualidade,
               mc_t0, liq_t0, volume_t0, txns5m_t0, buys_t0, sells_t0, net_momentum_t0,
               idade_min, ratio_vol_mc_t0, holders_count, top1_pct, top10_pct,
               dev_saiu, bc_progress, is_multi, var_t1, var_t2, var_t3, var_pico,
               categoria_final, dex
        FROM registros
        WHERE tipo = 'COMPRA'
          AND categoria_final IS NOT NULL
          AND categoria_final NOT ILIKE '%%aguardando%%'
          AND categoria_final NOT ILIKE '%%sem dados%%'
          AND var_pico IS NOT NULL
        ORDER BY data_compra DESC
    """, conn, parse_dates=['data_compra'])

print(f"Registros carregados: {len(df)}")
print(f"Período: {df['data_compra'].min().date()} → {df['data_compra'].max().date()}")
print(f"Vencedores (var_pico > 50%): {(df['var_pico']>50).sum()} ({(df['var_pico']>50).mean()*100:.1f}%)")

# ── 2. Feature engineering ────────────────────────────────────────────────────
df2 = df.copy()
total_txns = df2['buys_t0'].fillna(0) + df2['sells_t0'].fillna(0)
df2['ratio_bs']  = np.where(total_txns > 0, df2['buys_t0'].fillna(0) / total_txns, np.nan)
df2['is_pumpfun'] = (df2['dex'] == 'pumpfun').astype(int)
df2['log_mc']    = np.log1p(df2['mc_t0'].clip(lower=0))
df2['log_liq']   = np.log1p(df2['liq_t0'].clip(lower=0))
df2['log_vol']   = np.log1p(df2['volume_t0'].clip(lower=0))
df2['is_multi']  = df2['is_multi'].fillna(False).astype(int)
df2['vencedor']  = (df2['var_pico'] > 50).astype(int)
df2['var_pico_c'] = df2['var_pico'].clip(-100, 1000)

FEATURES = [
    'bc_progress', 'ratio_bs', 'log_mc', 'log_liq', 'log_vol',
    'idade_min', 'ratio_vol_mc_t0', 'net_momentum_t0',
    'holders_count', 'top10_pct', 'is_multi', 'is_pumpfun', 'score_qualidade',
]

df_ml = df2.dropna(subset=['log_mc', 'idade_min', 'var_pico']).copy()
for col in FEATURES:
    if df_ml[col].isna().any():
        mediana = df_ml[col].median()
        df_ml[col] = df_ml[col].fillna(mediana)
        print(f"  {col}: NULLs preenchidos com mediana ({mediana:.2f})")

X    = df_ml[FEATURES]
y_cls = df_ml['vencedor']
y_reg = df_ml['var_pico_c']

print(f"\nDataset final: {len(df_ml)} registros | {y_cls.sum()} vencedores ({y_cls.mean()*100:.1f}%)")

# ── 3. XGBClassifier ──────────────────────────────────────────────────────────
print("\n[1/4] Treinando XGBClassifier (cross-validation)...")
scale_pos_weight = (y_cls == 0).sum() / (y_cls == 1).sum()
xgb_cls = XGBClassifier(
    n_estimators=300, max_depth=4, learning_rate=0.05,
    subsample=0.8, colsample_bytree=0.8,
    scale_pos_weight=scale_pos_weight,
    random_state=42, eval_metric='auc', verbosity=0
)
cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
auc_scores = cross_val_score(xgb_cls, X, y_cls, cv=cv, scoring='roc_auc')
ap_scores  = cross_val_score(xgb_cls, X, y_cls, cv=cv, scoring='average_precision')
print(f"  ROC-AUC  (5-fold): {auc_scores.mean():.3f} ± {auc_scores.std():.3f}")
print(f"  Avg Prec (5-fold): {ap_scores.mean():.3f} ± {ap_scores.std():.3f}")
xgb_cls.fit(X, y_cls)

# ── 4. Calibração ─────────────────────────────────────────────────────────────
print("\n[2/4] Calibrando probabilidades...")
xgb_calibrado = CalibratedClassifierCV(xgb_cls, method='isotonic', cv=5)
xgb_calibrado.fit(X, y_cls)
probas = xgb_calibrado.predict_proba(X)[:, 1]
print(f"  AUC no treino (calibrado): {roc_auc_score(y_cls, probas):.3f}")

# Threshold ótimo
prec, rec, thresh = precision_recall_curve(y_cls, probas)
f1 = 2 * prec * rec / (prec + rec + 1e-9)
idx_best = f1.argmax()
best_thresh = float(thresh[idx_best]) if idx_best < len(thresh) else float(thresh[-1])
print(f"  Threshold ótimo (max F1): {best_thresh:.3f}  "
      f"| Precision={prec[idx_best]:.3f} | Recall={rec[idx_best]:.3f} | F1={f1[idx_best]:.3f}")

# ── 5. XGBRegressor ───────────────────────────────────────────────────────────
print("\n[3/4] Treinando XGBRegressor...")
xgb_reg = XGBRegressor(
    n_estimators=300, max_depth=4, learning_rate=0.05,
    subsample=0.8, colsample_bytree=0.8, random_state=42, verbosity=0
)
cv_reg = cross_val_score(xgb_reg, X, y_reg, cv=5, scoring='neg_mean_absolute_error')
r2_cv  = cross_val_score(xgb_reg, X, y_reg, cv=5, scoring='r2')
print(f"  MAE (5-fold): {-cv_reg.mean():.1f}% ± {cv_reg.std():.1f}%")
print(f"  R²  (5-fold): {r2_cv.mean():.3f} ± {r2_cv.std():.3f}")
xgb_reg.fit(X, y_reg)

# ── 6. Feature importance ─────────────────────────────────────────────────────
imp = sorted(zip(FEATURES, xgb_cls.feature_importances_), key=lambda x: -x[1])
print("\nImportância das features:")
for feat, v in imp:
    bar = '█' * int(v * 60)
    print(f"  {feat:<25} {v:.4f}  {bar}")

# ── 7. Salva .pkl ─────────────────────────────────────────────────────────────
print("\n[4/4] Salvando modelos...")
with open(os.path.join(MODEL_DIR, 'modelo_binario.pkl'), 'wb') as f:
    pickle.dump(xgb_calibrado, f)
with open(os.path.join(MODEL_DIR, 'modelo_regressao.pkl'), 'wb') as f:
    pickle.dump(xgb_reg, f)
with open(os.path.join(MODEL_DIR, 'feature_cols.pkl'), 'wb') as f:
    pickle.dump(FEATURES, f)

meta = {
    'features': FEATURES,
    'n_train': len(df_ml),
    'base_winrate': float(y_cls.mean()),
    'threshold_otimo': best_thresh,
    'auc_cv': float(auc_scores.mean()),
    'mae_cv': float(-cv_reg.mean()),
    'data_treino': str(df['data_compra'].max().date()),
    'versao': 'v1.0'
}
with open(os.path.join(MODEL_DIR, 'modelo_meta.pkl'), 'wb') as f:
    pickle.dump(meta, f)
with open(os.path.join(MODEL_DIR, 'modelo_meta.json'), 'w') as f:
    json.dump(meta, f, indent=2)

print(f"""
✅ Concluído!
   modelo_binario.pkl   — XGBClassifier calibrado (AUC={meta['auc_cv']:.3f})
   modelo_regressao.pkl — XGBRegressor (MAE={meta['mae_cv']:.1f}%)
   feature_cols.pkl     — {len(FEATURES)} features
   modelo_meta.json     — metadados

Base win rate : {meta['base_winrate']*100:.1f}%
Threshold ótimo : ml_proba >= {best_thresh:.2f}

Próximo passo: fazer git add *.pkl e push para o Render pegar os modelos.
""")
