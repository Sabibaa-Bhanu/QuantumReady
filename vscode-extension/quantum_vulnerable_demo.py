"""
quantum_vulnerable_demo.py
A demo file with intentionally quantum-vulnerable cryptography.
Open this in the Extension Development Host to see QuantumReady diagnostics.
"""

# ── Quantum-vulnerable imports ─────────────────────────────────
from Crypto.PublicKey import RSA          # CRITICAL: RSA broken by Shor's Algorithm
from Crypto.Cipher import PKCS1_OAEP
from hashlib import md5, sha1             # HIGH: MD5 + SHA1 have known collisions
import ssl

# ── RSA key generation (CRITICAL) ─────────────────────────────
private_key = RSA.generate(2048)          # CRITICAL: WeakRSAKeySize — even 2048-bit breaks on QC
public_key = private_key.publickey()

# ── MD5 for "integrity" check (HIGH) ──────────────────────────
def get_file_hash(data: bytes) -> str:
    return md5(data).hexdigest()          # HIGH: MD5 collisions demonstrated

# ── SHA-1 password hashing (HIGH) ─────────────────────────────
def hash_password(password: str) -> str:
    return sha1(password.encode()).hexdigest()   # HIGH: SHA1 broken (SHAttered, 2017)

# ── Weak TLS (HIGH) ───────────────────────────────────────────
def create_legacy_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLSv1)     # HIGH: TLS 1.0 deprecated (RFC 8996)
    return ctx

# ── Diffie-Hellman key exchange (CRITICAL) ────────────────────
# Diffie-Hellman key exchange is broken by Shor's Algorithm
DH_PARAMS = {"Diffie-Hellman": True, "key_size": 2048}

# ── Safe: AES-256 (LOW — still safe) ──────────────────────────
from Crypto.Cipher import AES
def encrypt_data(key: bytes, data: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_GCM)   # LOW: AES-256 fine, AES-128 weakened by Grover's
    return cipher.encrypt(data)

if __name__ == "__main__":
    print("Demo: QuantumReady will flag lines 14, 15, 20, 24, 28, 33 above")
    print("Save this file in the Extension Development Host to see the squiggles!")
