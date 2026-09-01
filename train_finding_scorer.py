"""
train_finding_scorer.py — Programmatic PQC Finding-Level ML Risk Classifier

Features:
  1. Algorithm Type (RSA, ECC, DiffieHellman, DSA, MD5, SHA1, WeakTLS, KeyPairGenerator, AES, PQC)
  2. Detected Key Size (bits: 512, 1024, 2048, 4096, 256, 128, 0 for N/A)
  3. Usage Context (authentication, data_in_transit, data_at_rest, key_exchange, digital_signature, hashing, unknown)
  4. Codebase Frequency (number of occurrences in the project)

Target Priority:
  0 = Low Risk (Modern symmetric ciphers or post-quantum hybrids)
  1 = Medium Risk (Unhardened parameters, weak symmetric keys, review items)
  2 = High Risk (Collision-vulnerable hashes, deprecated TLS versions)
  3 = Critical Risk (Shor-vulnerable public key cryptography: RSA, ECC, DH, DSA)

Usage:
  python train_finding_scorer.py
"""

import os
import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score, StratifiedKFold
from sklearn.metrics import classification_report, accuracy_score

MODEL_FILE = "finding_risk_model.pkl"

# Enumerated mappings
ALGORITHMS = [
    "RSA", "ECC", "DiffieHellman", "DSA", "MD5", 
    "SHA1", "WeakTLS", "KeyPairGenerator", "AES", "PQC"
]

CONTEXTS = [
    "authentication", "data_in_transit", "data_at_rest", 
    "key_exchange", "digital_signature", "hashing", "unknown"
]

PRIORITY_LABELS = {
    0: "Low",
    1: "Medium",
    2: "High",
    3: "Critical"
}


def encode_features(algo: str, key_size: int, context: str, frequency: int) -> list:
    """Encode categorical and numerical features into a flat feature vector."""
    # 1. One-hot encoding for algorithm
    algo_vec = [1 if a == algo else 0 for a in ALGORITHMS]
    
    # 2. Normalized key size (0 to 4096)
    key_size_norm = float(key_size) / 4096.0
    
    # 3. One-hot encoding for context
    context_vec = [1 if c == context else 0 for c in CONTEXTS]
    
    # 4. Normalized frequency (capped at 20)
    freq_norm = min(float(frequency), 20.0) / 20.0
    
    return algo_vec + [key_size_norm] + context_vec + [freq_norm]


def get_feature_names() -> list:
    """Return human-readable names for all feature dimensions."""
    names = [f"algo_{a}" for a in ALGORITHMS]
    names.append("key_size_norm")
    names.extend([f"ctx_{c}" for c in CONTEXTS])
    names.append("frequency_norm")
    return names


def generate_synthetic_dataset() -> tuple[np.ndarray, np.ndarray, list]:
    """
    Programmatically generate a balanced, realistic dataset representing 
    post-quantum migration scenarios for auditable ML scoring.
    """
    X = []
    y = []
    descriptions = []

    # ── 1. CRITICAL RISK SCENARIOS (Shor's Algorithm Threat) ─────────────────
    # RSA key generation / signing in various contexts
    for ksz in [512, 1024, 2048, 4096]:
        for ctx in ["authentication", "data_in_transit", "digital_signature", "key_exchange"]:
            for freq in [1, 3, 5, 10]:
                X.append(encode_features("RSA", ksz, ctx, freq))
                y.append(3)
                descriptions.append(f"RSA {ksz}-bit in {ctx} (freq={freq})")

    # ECC key generation / ECDSA / ECDH
    for ksz in [256, 384, 521]:
        for ctx in ["authentication", "digital_signature", "key_exchange"]:
            for freq in [1, 2, 6, 12]:
                X.append(encode_features("ECC", ksz, ctx, freq))
                y.append(3)
                descriptions.append(f"ECC {ksz}-bit in {ctx} (freq={freq})")

    # Diffie-Hellman Key Exchange
    for ksz in [1024, 2048]:
        for freq in [1, 4, 8]:
            X.append(encode_features("DiffieHellman", ksz, "key_exchange", freq))
            y.append(3)
            descriptions.append(f"Diffie-Hellman in key_exchange (freq={freq})")

    # DSA
    for ksz in [1024, 2048]:
        for freq in [1, 3]:
            X.append(encode_features("DSA", ksz, "digital_signature", freq))
            y.append(3)
            descriptions.append(f"DSA signature in {ctx} (freq={freq})")

    # ── 2. HIGH RISK SCENARIOS (Classical Collisions & Deprecated Protocols) ──
    # MD5 hashing
    for ctx in ["authentication", "hashing", "data_at_rest", "unknown"]:
        for freq in [1, 2, 5, 15]:
            X.append(encode_features("MD5", 128, ctx, freq))
            y.append(2)
            descriptions.append(f"MD5 hash in {ctx} (freq={freq})")

    # SHA1 hashing
    for ctx in ["authentication", "digital_signature", "hashing", "unknown"]:
        for freq in [1, 3, 7, 14]:
            X.append(encode_features("SHA1", 160, ctx, freq))
            y.append(2)
            descriptions.append(f"SHA1 in {ctx} (freq={freq})")

    # Weak TLS Protocols (TLS 1.0 / SSLv3)
    for freq in [1, 2, 4, 8]:
        X.append(encode_features("WeakTLS", 0, "data_in_transit", freq))
        y.append(2)
        descriptions.append(f"Weak TLS in data_in_transit (freq={freq})")

    # ── 3. MEDIUM RISK SCENARIOS (Unhardened Parameters & Weak Symmetrics) ───
    # KeyPairGenerator without explicit PQC algorithm spec
    for ctx in ["authentication", "key_exchange", "unknown"]:
        for freq in [1, 2, 5]:
            X.append(encode_features("KeyPairGenerator", 0, ctx, freq))
            y.append(1)
            descriptions.append(f"Generic KeyPairGenerator in {ctx} (freq={freq})")

    # Legacy DES or weak key sizes in non-critical contexts
    for ctx in ["data_at_rest", "unknown"]:
        for freq in [1, 2, 4]:
            X.append(encode_features("AES", 128, ctx, freq))
            y.append(1)
            descriptions.append(f"AES 128-bit in {ctx} (freq={freq})")

    # ── 4. LOW RISK SCENARIOS (Quantum Safe & Hardened Cryptography) ─────────
    # Modern AES-256-GCM
    for ctx in ["data_at_rest", "data_in_transit", "unknown"]:
        for freq in [1, 4, 10]:
            X.append(encode_features("AES", 256, ctx, freq))
            y.append(0)
            descriptions.append(f"AES 256-bit in {ctx} (freq={freq})")

    # Post-quantum primitives (ML-KEM / Kyber, ML-DSA / Dilithium)
    for ksz in [768, 1024, 65, 87]:
        for ctx in ["key_exchange", "digital_signature", "authentication"]:
            for freq in [1, 3, 8]:
                X.append(encode_features("PQC", ksz, ctx, freq))
                y.append(0)
                descriptions.append(f"PQC lattice primitive in {ctx} (freq={freq})")

    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int32), descriptions


def train_model() -> dict:
    """Train finding-level Random Forest classifier and save model artifact."""
    X, y, descriptions = generate_synthetic_dataset()
    feature_names = get_feature_names()

    # Train Random Forest classifier
    clf = RandomForestClassifier(
        n_estimators=60,
        max_depth=6,
        random_state=42,
        class_weight="balanced"
    )

    # Stratified 5-Fold Cross Validation
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores = cross_val_score(clf, X, y, cv=cv)
    cv_mean = float(np.mean(scores))
    cv_std = float(np.std(scores))

    # Fit final model
    clf.fit(X, y)
    preds = clf.predict(X)
    acc = float(accuracy_score(y, preds))
    report_dict = classification_report(y, preds, target_names=["Low", "Medium", "High", "Critical"], output_dict=True)

    # Feature importances
    importances = {
        name: round(float(imp), 4)
        for name, imp in sorted(zip(feature_names, clf.feature_importances_), key=lambda x: x[1], reverse=True)
    }

    # Model metadata packet
    model_artifact = {
        "model": clf,
        "feature_names": feature_names,
        "algorithms": ALGORITHMS,
        "contexts": CONTEXTS,
        "priority_labels": PRIORITY_LABELS,
        "training_samples": len(X),
        "accuracy": acc,
        "cv_mean_accuracy": cv_mean,
        "cv_std": cv_std,
        "classification_report": report_dict,
        "feature_importances": importances,
        "model_type": "RandomForestClassifier",
        "description": "NIST Post-Quantum Vulnerability Finding-Level Risk Classifier"
    }

    joblib.dump(model_artifact, MODEL_FILE)
    return model_artifact


if __name__ == "__main__":
    artifact = train_model()
    print("=" * 60)
    print(" QuantumReady Finding-Level ML Classifier Trained Successfully")
    print(f" Samples: {artifact['training_samples']} | CV Accuracy: {artifact['cv_mean_accuracy']*100:.2f}% (±{artifact['cv_std']*100:.2f}%)")
    print(f" Model saved to: {MODEL_FILE}")
    print("=" * 60)
    print("\nTop 8 Feature Importances:")
    for k, v in list(artifact["feature_importances"].items())[:8]:
        bar = "#" * int(v * 40)
        print(f"  {k:20s}: {v:6.4f} {bar}")
