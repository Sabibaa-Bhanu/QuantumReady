/**
 * extension.ts — QuantumReady PQC Scanner VS Code Extension
 *
 * Features:
 *  ✅ Inline squiggly diagnostics on quantum-vulnerable lines
 *  ✅ Hover text: detected algorithm + risk level + NIST PQC fix recommendation
 *  ✅ Status bar item: "QR: N issues" (or "QR: Clean") for the active file
 *  ✅ Scans on file save + optional debounced-on-type scanning
 *  ✅ Backend URL, debounce delay, and scanOnType all configurable via settings
 *  ✅ Graceful degradation when backend is unreachable
 */

import * as vscode from 'vscode';
import * as https from 'https';
import * as http from 'http';
import * as url from 'url';

// ---------------------------------------------------------------------------
// Types matching the /api/scan-content response shape
// ---------------------------------------------------------------------------

interface Finding {
  line_number: number;       // 1-based
  line_content: string;
  vulnerability_type: string;
  risk: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | 'SAFE';
  penalty: number;
  description: string;
  fix: string;
}

interface ScanResponse {
  findings: Finding[];
  score: number;
  label: string;
  total_findings: number;
  skipped?: boolean;
  reason?: string;
  error?: string;
}

// ---------------------------------------------------------------------------
// Supported extensions (mirrors scanner.py SUPPORTED_EXTENSIONS)
// ---------------------------------------------------------------------------

const SUPPORTED_EXTENSIONS = new Set([
  '.py', '.java', '.js', '.ts', '.jsx', '.tsx',
  '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.cs',
  '.go', '.rs', '.rb', '.php', '.swift', '.kt',
  '.scala', '.groovy',
]);

// ---------------------------------------------------------------------------
// Module-level state
// ---------------------------------------------------------------------------

let diagnosticCollection: vscode.DiagnosticCollection;
let statusBarItem: vscode.StatusBarItem;
let outputChannel: vscode.OutputChannel;

/** Cache of { score, label, count } per document URI so the status bar can
 *  refresh instantly when switching tabs without re-scanning. */
const scanCache = new Map<string, { score: number; label: string; count: number }>();

/** Per-document debounce timer handles. */
const debounceTimers = new Map<string, ReturnType<typeof setTimeout>>();

// ---------------------------------------------------------------------------
// Extension lifecycle
// ---------------------------------------------------------------------------

export function activate(context: vscode.ExtensionContext): void {
  outputChannel = vscode.window.createOutputChannel('QuantumReady');
  outputChannel.appendLine('[QuantumReady] Extension activated.');

  // Diagnostic collection
  diagnosticCollection = vscode.languages.createDiagnosticCollection('quantumready');
  context.subscriptions.push(diagnosticCollection);

  // Status bar item — left side, relatively high priority so it stays visible
  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = 'quantumready.scanCurrentFile';
  statusBarItem.tooltip = 'Click to scan with QuantumReady';
  context.subscriptions.push(statusBarItem);

  // ── Commands ──────────────────────────────────────────────
  context.subscriptions.push(
    vscode.commands.registerCommand('quantumready.scanCurrentFile', () => {
      const doc = vscode.window.activeTextEditor?.document;
      if (doc) {
        scanDocument(doc, /* forceStatusMessage */ true);
      } else {
        vscode.window.showInformationMessage('QuantumReady: No active editor to scan.');
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('quantumready.clearDiagnostics', () => {
      diagnosticCollection.clear();
      scanCache.clear();
      updateStatusBar(null);
      outputChannel.appendLine('[QuantumReady] Diagnostics cleared.');
    })
  );

  // ── Event listeners ───────────────────────────────────────

  // Scan on save
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument((doc) => {
      if (isSupportedDocument(doc)) {
        cancelDebounce(doc.uri.toString());
        scanDocument(doc);
      }
    })
  );

  // Scan while typing (debounced)
  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument((event) => {
      const doc = event.document;
      if (!isSupportedDocument(doc)) { return; }
      const cfg = getConfig();
      if (!cfg.scanOnType) { return; }

      const key = doc.uri.toString();
      cancelDebounce(key);
      debounceTimers.set(key, setTimeout(() => {
        debounceTimers.delete(key);
        scanDocument(doc);
      }, cfg.debounceMs));
    })
  );

  // Refresh status bar when switching tabs
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor((editor) => {
      if (editor && isSupportedDocument(editor.document)) {
        const cached = scanCache.get(editor.document.uri.toString());
        updateStatusBar(cached ?? null);
      } else {
        updateStatusBar(null);
      }
    })
  );

  // Clean up cache + diagnostics when a document is closed
  context.subscriptions.push(
    vscode.workspace.onDidCloseTextDocument((doc) => {
      const key = doc.uri.toString();
      cancelDebounce(key);
      scanCache.delete(key);
      diagnosticCollection.delete(doc.uri);
    })
  );

  // ── Initial scan of the active document on activation ─────
  const activeDoc = vscode.window.activeTextEditor?.document;
  if (activeDoc && isSupportedDocument(activeDoc)) {
    scanDocument(activeDoc);
  }

  if (getConfig().showStatusBar) {
    statusBarItem.show();
  }
}

export function deactivate(): void {
  debounceTimers.forEach((timer) => clearTimeout(timer));
  debounceTimers.clear();
  outputChannel.appendLine('[QuantumReady] Extension deactivated.');
}

// ---------------------------------------------------------------------------
// Core scanning logic
// ---------------------------------------------------------------------------

async function scanDocument(
  doc: vscode.TextDocument,
  forceStatusMessage = false
): Promise<void> {
  const cfg = getConfig();
  const backendUrl = cfg.backendUrl.replace(/\/$/, '');
  const endpoint = `${backendUrl}/api/scan-content`;
  const key = doc.uri.toString();

  if (forceStatusMessage) {
    statusBarItem.text = '$(loading~spin) QR: Scanning…';
    statusBarItem.tooltip = 'QuantumReady is scanning the file…';
  }

  outputChannel.appendLine(`[QuantumReady] Scanning ${doc.fileName}`);

  let result: ScanResponse;
  try {
    result = await postJson(endpoint, {
      content: doc.getText(),
      filename: doc.fileName,
    });
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    outputChannel.appendLine(`[QuantumReady] ⚠ Network error: ${msg}`);
    // Only show a toast if the user explicitly triggered the scan
    if (forceStatusMessage) {
      vscode.window.showWarningMessage(
        `QuantumReady: Backend unreachable at ${backendUrl}. Is the server running?\n\n` +
        `(Check the QuantumReady output channel for details, or update ` +
        `"quantumready.backendUrl" in settings.)`
      );
    }
    updateStatusBar(null);
    return;
  }

  if (result.error) {
    outputChannel.appendLine(`[QuantumReady] ⚠ Server error: ${result.error}`);
    updateStatusBar(null);
    return;
  }

  if (result.skipped) {
    outputChannel.appendLine(`[QuantumReady] Skipped: ${result.reason ?? 'unsupported extension'}`);
    return;
  }

  // Apply diagnostics
  applyDiagnostics(doc, result.findings);

  // Update cache & status bar
  const cacheEntry = { score: result.score, label: result.label, count: result.total_findings };
  scanCache.set(key, cacheEntry);
  updateStatusBar(cacheEntry);

  outputChannel.appendLine(
    `[QuantumReady] ✓ ${doc.fileName} — Score: ${result.score}/100 (${result.label}), ` +
    `${result.total_findings} finding(s)`
  );
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

function applyDiagnostics(doc: vscode.TextDocument, findings: Finding[]): void {
  const diagnostics: vscode.Diagnostic[] = [];

  for (const finding of findings) {
    const lineIndex = finding.line_number - 1; // VS Code is 0-based
    if (lineIndex < 0 || lineIndex >= doc.lineCount) { continue; }

    const line = doc.lineAt(lineIndex);
    // Underline from the first non-whitespace character to end-of-line
    const startChar = line.firstNonWhitespaceCharacterIndex;
    const endChar = line.text.length;
    const range = new vscode.Range(lineIndex, startChar, lineIndex, endChar);

    const severity = mapSeverity(finding.risk);

    // Build a rich hover message using MarkdownString
    const md = new vscode.MarkdownString('', true);
    md.isTrusted = true;
    md.supportHtml = false;

    const riskEmoji = riskToEmoji(finding.risk);
    md.appendMarkdown(`### ${riskEmoji} QuantumReady — \`${finding.vulnerability_type}\`\n\n`);
    md.appendMarkdown(`**Risk:** ${finding.risk}  \n`);
    md.appendMarkdown(`**Why:** ${finding.description}  \n\n`);
    md.appendMarkdown(`**Recommended Fix:**  \n`);
    md.appendMarkdown(`> ${finding.fix}\n\n`);

    // Show the vulnerable code snippet for context
    if (finding.line_content.trim()) {
      md.appendMarkdown(`**Detected on line ${finding.line_number}:**\n`);
      md.appendCodeblock(finding.line_content.trim(), inferLanguage(doc.languageId));
    }

    const diag = new vscode.Diagnostic(range, buildDiagnosticMessage(finding), severity);
    diag.source = 'QuantumReady';
    diag.code = {
      value: finding.vulnerability_type,
      target: nistReferenceUri(finding.vulnerability_type),
    };
    diag.relatedInformation = [];  // could add links to NIST docs later

    // Attach the hover message — VS Code shows this in the Problems panel
    // and as hover tooltip on the squiggly
    (diag as vscode.Diagnostic & { message: string }).message = buildDiagnosticMessage(finding);

    diagnostics.push(diag);
  }

  diagnosticCollection.set(doc.uri, diagnostics);
}

function buildDiagnosticMessage(f: Finding): string {
  // Concise one-liner for the Problems panel; full detail appears on hover
  return `${f.vulnerability_type} detected — ${f.description} Fix: ${f.fix}`;
}

function mapSeverity(risk: Finding['risk']): vscode.DiagnosticSeverity {
  switch (risk) {
    case 'CRITICAL':
    case 'HIGH':
      return vscode.DiagnosticSeverity.Error;
    case 'MEDIUM':
      return vscode.DiagnosticSeverity.Warning;
    case 'LOW':
    case 'SAFE':
    default:
      return vscode.DiagnosticSeverity.Information;
  }
}

function riskToEmoji(risk: Finding['risk']): string {
  switch (risk) {
    case 'CRITICAL': return '🔴';
    case 'HIGH':     return '🟠';
    case 'MEDIUM':   return '🟡';
    case 'LOW':      return '🟢';
    default:         return 'ℹ️';
  }
}

/** Return a NIST document URI for the code field — opens in browser on click. */
function nistReferenceUri(vulnType: string): vscode.Uri {
  const refs: Record<string, string> = {
    RSA:             'https://csrc.nist.gov/pubs/fips/203/final',
    ECC:             'https://csrc.nist.gov/pubs/fips/204/final',
    DiffieHellman:   'https://csrc.nist.gov/pubs/fips/203/final',
    WeakRSAKeySize:  'https://csrc.nist.gov/pubs/fips/203/final',
    SHA1:            'https://csrc.nist.gov/pubs/fips/202/final',
    MD5:             'https://csrc.nist.gov/pubs/fips/202/final',
    WeakTLS:         'https://www.rfc-editor.org/rfc/rfc8996',
    KeyPairGenerator:'https://csrc.nist.gov/pubs/fips/203/final',
    AES:             'https://csrc.nist.gov/publications/detail/fips/197/final',
  };
  return vscode.Uri.parse(refs[vulnType] ?? 'https://csrc.nist.gov/projects/post-quantum-cryptography');
}

function inferLanguage(vscodeLanguageId: string): string {
  // Map VS Code language IDs to markdown code fence language identifiers
  const map: Record<string, string> = {
    python: 'python', java: 'java',
    javascript: 'javascript', typescript: 'typescript',
    javascriptreact: 'jsx', typescriptreact: 'tsx',
    go: 'go', rust: 'rust',
    c: 'c', cpp: 'cpp', csharp: 'csharp',
    ruby: 'ruby', php: 'php',
    swift: 'swift', kotlin: 'kotlin',
  };
  return map[vscodeLanguageId] ?? vscodeLanguageId;
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------

function updateStatusBar(
  cached: { score: number; label: string; count: number } | null
): void {
  if (!getConfig().showStatusBar) {
    statusBarItem.hide();
    return;
  }

  if (cached === null) {
    statusBarItem.text = '$(shield) QR';
    statusBarItem.tooltip = 'QuantumReady PQC Scanner — click to scan';
    statusBarItem.backgroundColor = undefined;
    statusBarItem.show();
    return;
  }

  const { score, label, count } = cached;
  const hasIssues = count > 0;

  statusBarItem.text = hasIssues
    ? `$(warning) QR: ${count} issue${count !== 1 ? 's' : ''}`
    : `$(check) QR: Clean`;

  statusBarItem.tooltip =
    `QuantumReady Score: ${score}/100 — ${label}\n` +
    `${count} cryptographic finding${count !== 1 ? 's' : ''} in this file.\n` +
    `Click to re-scan.`;

  statusBarItem.backgroundColor = hasIssues
    ? new vscode.ThemeColor('statusBarItem.errorBackground')
    : undefined;

  statusBarItem.show();
}

// ---------------------------------------------------------------------------
// Configuration helpers
// ---------------------------------------------------------------------------

interface Config {
  backendUrl: string;
  scanOnType: boolean;
  debounceMs: number;
  showStatusBar: boolean;
}

function getConfig(): Config {
  const cfg = vscode.workspace.getConfiguration('quantumready');
  return {
    backendUrl:    cfg.get<string>('backendUrl', 'http://localhost:5000'),
    scanOnType:    cfg.get<boolean>('scanOnType', true),
    debounceMs:    cfg.get<number>('debounceMs', 1500),
    showStatusBar: cfg.get<boolean>('showStatusBar', true),
  };
}

function isSupportedDocument(doc: vscode.TextDocument): boolean {
  if (doc.isUntitled || doc.uri.scheme !== 'file') { return false; }
  const ext = doc.fileName.includes('.')
    ? '.' + doc.fileName.split('.').pop()!.toLowerCase()
    : '';
  return SUPPORTED_EXTENSIONS.has(ext);
}

function cancelDebounce(key: string): void {
  const timer = debounceTimers.get(key);
  if (timer !== undefined) {
    clearTimeout(timer);
    debounceTimers.delete(key);
  }
}

// ---------------------------------------------------------------------------
// HTTP helper — no external dependencies (uses Node built-ins)
// ---------------------------------------------------------------------------

function postJson(endpoint: string, body: unknown): Promise<ScanResponse> {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const parsed = url.parse(endpoint);
    const isHttps = parsed.protocol === 'https:';
    const transport = isHttps ? https : http;

    const options: http.RequestOptions = {
      hostname: parsed.hostname ?? 'localhost',
      port: parsed.port ? parseInt(parsed.port, 10) : (isHttps ? 443 : 80),
      path: parsed.path ?? '/api/scan-content',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
        'Accept': 'application/json',
        'User-Agent': 'QuantumReady-VSCode/0.1.0',
      },
      // Generous timeout so large files don't cause spurious errors
      timeout: 30_000,
    };

    const req = transport.request(options, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (chunk: Buffer) => chunks.push(chunk));
      res.on('end', () => {
        try {
          const text = Buffer.concat(chunks).toString('utf-8');
          const json = JSON.parse(text) as ScanResponse;
          resolve(json);
        } catch {
          reject(new Error(`Failed to parse server response (status ${res.statusCode})`));
        }
      });
    });

    req.on('timeout', () => {
      req.destroy();
      reject(new Error(`Request to ${endpoint} timed out after 30 s`));
    });

    req.on('error', (err: NodeJS.ErrnoException) => {
      if (err.code === 'ECONNREFUSED') {
        reject(new Error(`Connection refused — is the QuantumReady backend running at ${endpoint}?`));
      } else {
        reject(err);
      }
    });

    req.write(payload);
    req.end();
  });
}
