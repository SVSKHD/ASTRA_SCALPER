from __future__ import annotations

# =============================================================================
# ML GATE — LightGBM trade decision model (Phase 2)
#
# WHEN TO USE:
#   Only activate after collecting 150+ labeled signals in signal_log.csv.
#   With <150 samples the model will overfit and hurt performance.
#
# TRAINING:
#   python ml_gate.py --train
#   → reads data/signal_log.csv
#   → trains LightGBM classifier
#   → saves model to data/ml_gate.pkl
#   → prints cross-validation accuracy + feature importance
#
# PREDICTION (in runner.py):
#   from ml_gate import MLGate
#   gate = MLGate()                        # loads model once at startup
#   if gate.should_trade(features_dict):   # returns True/False
#       _handle_signal(signal, state)
#
# FEATURES USED (subset of signal_log.csv):
#   breakout_bar_body_ratio, breakout_bar_range_pips,
#   atr14, atr14_vs_20d_avg, price_vs_20d_mean,
#   hour_utc, day_of_week,
#   prev_day_outcome_enc, consecutive_losses, h1_trend_align, spread_pips
#
# THRESHOLD:
#   Only trade if P(TP) > CONFIDENCE_THRESHOLD (default 0.55)
#   → Trades less but wins more → higher WR, same R:R
#
# PHASE GUIDE:
#   Phase 1 (now)       : Rule-based filters (signal_filter.py)
#   Phase 2 (150 samples): LightGBM gate (this file)
#   Phase 3 (300+ samples): Calibrated probability + position sizing
# =============================================================================

import os
import csv
import logging
import pickle
from typing import Optional

log = logging.getLogger("ml_gate")

_MODEL_PATH  = os.path.join("data", "ml_gate.pkl")
_CSV_PATH    = os.path.join("data", "signal_log.csv")
_MIN_SAMPLES = 150    # minimum labeled trades before training is valid
_CONFIDENCE  = 0.55   # P(TP) threshold to place trade

FEATURE_COLS = [
    "breakout_bar_body_ratio",
    "breakout_bar_range_pips",
    "atr14",
    "atr14_vs_20d_avg",
    "price_vs_20d_mean",
    "hour_utc",
    "day_of_week",
    "consecutive_losses",
    "h1_trend_align",
    "spread_pips",
    # prev_day_outcome encoded: TP=1, SL=-1, NO_SIGNAL=0, UNKNOWN=0
    "prev_day_outcome_enc",
]


class MLGate:
    """
    Loads the trained LightGBM model and gates trade signals.
    Falls back to ALLOW if model is not yet trained.
    """
    def __init__(self, model_path: str = _MODEL_PATH, confidence: float = _CONFIDENCE):
        self.confidence = confidence
        self.model = None
        self._load(model_path)

    def _load(self, path: str):
        if not os.path.exists(path):
            log.info(f"[MLGate] No model at {path} — running in PASS-THROUGH mode")
            return
        try:
            with open(path, "rb") as f:
                self.model = pickle.load(f)
            log.info(f"[MLGate] Model loaded from {path}")
        except Exception as e:
            log.error(f"[MLGate] Failed to load model: {e}")

    def is_ready(self) -> bool:
        return self.model is not None

    def should_trade(self, features: dict) -> tuple[bool, float]:
        """
        Returns (should_trade: bool, confidence: float).
        If model not loaded → always returns (True, 1.0) = pass-through.

        features: dict with keys matching FEATURE_COLS.
        """
        if self.model is None:
            return True, 1.0

        try:
            row = _encode_features(features)
            import numpy as np
            X = np.array(row, dtype=float).reshape(1, -1)
            prob = self.model.predict_proba(X)[0][1]   # P(TP)
            decision = prob >= self.confidence
            log.info(f"[MLGate] P(TP)={prob:.3f} threshold={self.confidence} → {'TRADE' if decision else 'SKIP'}")
            return decision, round(float(prob), 4)
        except Exception as e:
            log.error(f"[MLGate] Prediction failed: {e} — defaulting to TRADE")
            return True, 1.0


def _encode_features(f: dict) -> list:
    """Convert feature dict to ordered list for model input."""
    outcome_map = {"TP": 1, "SL": -1, "NO_SIGNAL": 0, "UNKNOWN": 0, "": 0}
    prev = outcome_map.get(f.get("prev_day_outcome", "UNKNOWN"), 0)
    return [
        float(f.get("breakout_bar_body_ratio", 0.5)),
        float(f.get("breakout_bar_range_pips", 10.0)),
        float(f.get("atr14", 5.0)),
        float(f.get("atr14_vs_20d_avg", 1.0)),
        float(f.get("price_vs_20d_mean", 0.0)),
        float(f.get("hour_utc", 2)),
        float(f.get("day_of_week", 1)),
        float(f.get("consecutive_losses", 0)),
        float(f.get("h1_trend_align", 1)),
        float(f.get("spread_pips", 0.3)),
        float(prev),
    ]


def _load_training_data() -> tuple[list, list]:
    """Load and parse signal_log.csv. Returns (X, y)."""
    if not os.path.exists(_CSV_PATH):
        raise FileNotFoundError(f"No signal log at {_CSV_PATH}")

    X, y = [], []
    skipped = 0
    with open(_CSV_PATH, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Only use trades with a real outcome
            if row["outcome"] not in ("TP", "SL"):
                skipped += 1
                continue
            # Skip filtered/skipped signals
            if row.get("filter_applied"):
                skipped += 1
                continue
            try:
                features = {
                    "breakout_bar_body_ratio": float(row["breakout_bar_body_ratio"] or 0.5),
                    "breakout_bar_range_pips": float(row["breakout_bar_range_pips"] or 10),
                    "atr14":                  float(row["atr14"] or 5),
                    "atr14_vs_20d_avg":       float(row["atr14_vs_20d_avg"] or 1),
                    "price_vs_20d_mean":      float(row["price_vs_20d_mean"] or 0),
                    "hour_utc":               int(row["time_utc"][:2]),
                    "day_of_week":            0,   # not stored — derive from date
                    "consecutive_losses":     int(row["consecutive_losses"] or 0),
                    "h1_trend_align":         int(row["h1_trend_align"] or 1),
                    "spread_pips":            float(row["spread_pips"] or 0.3),
                    "prev_day_outcome":       row["prev_day_outcome"],
                }
                from datetime import datetime
                dt = datetime.strptime(row["date"], "%Y-%m-%d")
                features["day_of_week"] = dt.weekday()
                X.append(_encode_features(features))
                y.append(1 if row["outcome"] == "TP" else 0)
            except Exception as e:
                skipped += 1
                log.warning(f"[MLGate] Skipped row: {e}")

    print(f"  Loaded   : {len(X)} labeled trades")
    print(f"  Skipped  : {skipped} (pending/filtered/invalid)")
    print(f"  TP rate  : {sum(y)/len(y)*100:.1f}%" if y else "  No data")
    return X, y


def train(min_samples: int = _MIN_SAMPLES, confidence: float = _CONFIDENCE):
    """
    Train LightGBM model on signal_log.csv.
    Run: python ml_gate.py --train
    """
    try:
        import lightgbm as lgb
        import numpy as np
        from sklearn.model_selection import StratifiedKFold, cross_val_score
        from sklearn.calibration import CalibratedClassifierCV
    except ImportError:
        print("Install: pip install lightgbm scikit-learn --break-system-packages")
        return

    print("\n" + "="*50)
    print("  ML GATE — TRAINING")
    print("="*50)

    X, y = _load_training_data()

    if len(X) < min_samples:
        print(f"\n⚠️  Only {len(X)} samples. Need {min_samples} before training.")
        print(f"   Keep running the bot. Come back when signal_log.csv has {min_samples}+ TP/SL rows.")
        return

    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y, dtype=int)

    # ── Train LightGBM ────────────────────────────────────────────────────
    base_model = lgb.LGBMClassifier(
        n_estimators     = 200,
        max_depth        = 4,
        learning_rate    = 0.05,
        num_leaves       = 15,
        min_child_samples= 20,    # prevents overfitting on small datasets
        subsample        = 0.8,
        colsample_bytree = 0.8,
        class_weight     = "balanced",
        random_state     = 42,
        verbose          = -1,
    )

    # Probability calibration — critical for accurate P(TP) thresholding
    model = CalibratedClassifierCV(base_model, method="isotonic", cv=3)

    # ── Cross-validation ──────────────────────────────────────────────────
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_arr, y_arr, cv=cv, scoring="roc_auc")
    print(f"\n  CV AUC   : {cv_scores.mean():.3f} ± {cv_scores.std():.3f}")
    print(f"  (>0.60 = useful signal, >0.70 = strong, <0.55 = noise)")

    if cv_scores.mean() < 0.54:
        print("\n⚠️  AUC too low — model has no real predictive power yet.")
        print("   Continue collecting data. Don't deploy this model.")
        return

    # ── Full fit ──────────────────────────────────────────────────────────
    model.fit(X_arr, y_arr)

    # ── Feature importance ────────────────────────────────────────────────
    print(f"\n  Feature importance (LightGBM base):")
    try:
        importances = base_model.estimator_.feature_importances_
    except Exception:
        importances = base_model.feature_importances_ if hasattr(base_model, 'feature_importances_') else []

    if len(importances) == len(FEATURE_COLS):
        pairs = sorted(zip(FEATURE_COLS, importances), key=lambda x: -x[1])
        for feat, imp in pairs:
            bar = "█" * int(imp / max(importances) * 20)
            print(f"    {feat:<30} {bar} {imp:.0f}")

    # ── Simulate P(TP) threshold ─────────────────────────────────────────
    probs = model.predict_proba(X_arr)[:, 1]
    for thresh in [0.50, 0.55, 0.60, 0.65]:
        mask = probs >= thresh
        if mask.sum() > 0:
            filtered_wr = y_arr[mask].mean() * 100
            filtered_n  = mask.sum()
            skip_rate   = (1 - mask.mean()) * 100
            print(f"\n  Threshold P(TP)>{thresh:.2f}: "
                  f"WR={filtered_wr:.1f}% "
                  f"trades={filtered_n} "
                  f"skipped={skip_rate:.0f}%")

    # ── Save ──────────────────────────────────────────────────────────────
    os.makedirs("data", exist_ok=True)
    with open(_MODEL_PATH, "wb") as f:
        pickle.dump(model, f)
    print(f"\n  Saved → {_MODEL_PATH}")
    print(f"  Deploy: set enable_ml_gate=True in config.py")


if __name__ == "__main__":
    import sys
    if "--train" in sys.argv:
        train()
    else:
        gate = MLGate()
        if gate.is_ready():
            test_features = {
                "breakout_bar_body_ratio": 0.68,
                "breakout_bar_range_pips": 8.5,
                "atr14": 4.2,
                "atr14_vs_20d_avg": 1.1,
                "price_vs_20d_mean": 0.3,
                "hour_utc": 2,
                "day_of_week": 1,
                "consecutive_losses": 0,
                "h1_trend_align": 1,
                "spread_pips": 0.3,
                "prev_day_outcome": "TP",
            }
            decision, prob = gate.should_trade(test_features)
            print(f"Decision: {'TRADE' if decision else 'SKIP'} | P(TP)={prob:.3f}")
        else:
            print("No model trained yet. Run: python ml_gate.py --train")
            print("Status:")
            if os.path.exists(_CSV_PATH):
                with open(_CSV_PATH) as f:
                    n = sum(1 for row in csv.DictReader(f) if row["outcome"] in ("TP","SL"))
                print(f"  signal_log.csv: {n} labeled trades (need {_MIN_SAMPLES})")
            else:
                print(f"  signal_log.csv: not found yet (starts after first trade)")