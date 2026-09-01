# QuantumReady — PQC Scanner (VS Code Extension)

Real-time post-quantum cryptography vulnerability detection right inside your editor.  
Detects **RSA, ECC, SHA-1, MD5, Diffie-Hellman, weak TLS**, and more — and maps every finding to the NIST-standardised post-quantum replacement (FIPS 203/204/205).

---

## What you get

| Feature | Details |
|---|---|
| **Squiggly underlines** | Quantum-vulnerable lines get inline diagnostics (red = CRITICAL/HIGH, yellow = MEDIUM, blue = LOW) |
| **Hover tooltip** | Shows the detected algorithm, why it's vulnerable, and the exact NIST PQC replacement |
| **Problems panel** | All findings appear in VS Code's Problems tab with a clickable NIST reference link |
| **Status bar** | `⚠ QR: 2 issues` (or `✓ QR: Clean`) — always visible in the bottom bar |
| **On-save scan** | Automatically scans every time you save a supported file |
| **Debounced typing scan** | Optional live scan while you type (configurable delay, default 1.5 s) |

---

## Prerequisites

1. **Node.js ≥ 18** and **npm** installed (`node --version`)
2. **VS Code ≥ 1.85** installed
3. The **QuantumReady backend** running locally (see below)

---

## Step 1 — Start the QuantumReady backend

From the **repo root** (`Quantum-main/`):

```powershell
# Windows
.\.venv\Scripts\python.exe app.py
```

The backend should print:
```
  QuantumReady Web Application Server
  * Local:   http://127.0.0.1:5000
```

Verify it's up:
```powershell
curl http://127.0.0.1:5000/api/health
# → {"status":"ok","version":"2.0", ...}
```

---

## Step 2 — Install extension dependencies

```powershell
cd vscode-extension
npm install
```

---

## Step 3 — Compile the TypeScript

```powershell
npm run compile
```

You should see no errors and an `out/` directory created with `extension.js`.

To **watch for changes** during development:
```powershell
npm run watch
```

---

## Step 4 — Launch in Extension Development Host

1. Open the `vscode-extension/` folder **in VS Code**:
   ```powershell
   code .
   ```
2. Press **`F5`** (or go to **Run → Start Debugging**).  
   A new **Extension Development Host** window opens with the extension active.

3. In the Extension Development Host window, open any supported source file — for example a Python file containing:
   ```python
   from Crypto.PublicKey import RSA
   key = RSA.generate(2048)
   ```
4. **Save the file** (or wait ~1.5 s if `scanOnType` is on). You should see:
   - A **red squiggly underline** on the `RSA.generate(2048)` line
   - Hovering shows: *"RSA detected — RSA is broken by Shor's Algorithm. Fix: Replace with CRYSTALS-Kyber512 (NIST FIPS 203)"*
   - The status bar shows `⚠ QR: 1 issue`

---

## Configuration

Open **Settings** (`Ctrl+,`) and search for `QuantumReady`, or add these directly to your `settings.json`:

```jsonc
{
  // Base URL of the QuantumReady Flask backend (no trailing slash)
  "quantumready.backendUrl": "http://localhost:5000",

  // Scan while typing (debounced). Set false to only scan on save.
  "quantumready.scanOnType": true,

  // Debounce delay in ms when scanOnType is true (min 250, max 10000)
  "quantumready.debounceMs": 1500,

  // Show/hide the status bar item
  "quantumready.showStatusBar": true
}
```

> **Tip:** If your backend is on a different port (e.g. `5001`) or on a remote host, just update `quantumready.backendUrl` — no code changes needed.

---

## Supported Languages

`.py` · `.java` · `.js` · `.ts` · `.jsx` · `.tsx` · `.go` · `.rs` · `.c` · `.cpp` · `.cs` · `.rb` · `.php` · `.swift` · `.kt` · `.scala` · `.groovy`

---

## Commands

| Command | Description |
|---|---|
| `QuantumReady: Scan Current File` | Manually trigger a scan (also accessible via the shield icon in the editor title bar) |
| `QuantumReady: Clear Diagnostics` | Remove all squiggles and reset the status bar |

---

## Detected Vulnerabilities

| Pattern | Risk | NIST Fix |
|---|---|---|
| RSA (any key size) | 🔴 CRITICAL | ML-KEM / CRYSTALS-Kyber (FIPS 203) |
| ECC / elliptic curve | 🔴 CRITICAL | ML-DSA / CRYSTALS-Dilithium (FIPS 204) |
| Diffie-Hellman | 🔴 CRITICAL | ML-KEM / CRYSTALS-Kyber (FIPS 203) |
| Weak RSA key size (≤2048) | 🔴 CRITICAL | ML-KEM (FIPS 203) |
| SHA-1 | 🟠 HIGH | SHA-3 / SHA3-256 (FIPS 202) |
| MD5 | 🟠 HIGH | SHA3-256 or Argon2 |
| Weak TLS (1.0/1.1/SSL) | 🟠 HIGH | TLS 1.3 minimum (RFC 8996) |
| KeyPairGenerator (Java) | 🟡 MEDIUM | Verify it's not using RSA/EC |
| AES (128-bit) | 🟢 LOW | Use AES-256 for post-quantum safety |

---

## Troubleshooting

**Squiggles not appearing?**
- Check the **QuantumReady Output Channel** (`View → Output → QuantumReady`) for error messages.
- Make sure the backend is running: `curl http://localhost:5000/api/health`
- Confirm the file extension is in the supported list above.

**"Backend unreachable" warning?**
- The backend URL defaults to `http://localhost:5000`. If your server is on a different port, update `quantumready.backendUrl` in settings.

**TypeScript compilation errors?**
- Run `npm install` first to install `@types/vscode` and `typescript`.
- Then `npm run compile`.
