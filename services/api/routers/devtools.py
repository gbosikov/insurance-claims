"""
Dev Tools Router — только для development окружения.

GET  /devtools        → HTML-форма для ручной подачи заявки
POST /devtools/upload → принять файлы напрямую, сохранить в storage, запустить pipeline
"""

from __future__ import annotations

import uuid
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import DEFAULT_TENANT_ID, get_tenant_id
from core.config import get_settings
from core.database import get_db
from core.models.claim import Claim, ClaimDocument, ClaimStatus, DocType
from core.storage import get_storage_client
from services.worker.celery_app import celery_app

router = APIRouter()
log = structlog.get_logger()
settings = get_settings()

_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ICPS Test Tool</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: 'Courier New', monospace; max-width: 860px; margin: 40px auto;
         padding: 0 20px; background: #0d1117; color: #c9d1d9; font-size: 14px; }
  h1 { color: #58a6ff; margin-bottom: 4px; }
  h3 { color: #e6edf3; margin-top: 0; }
  p.sub { color: #8b949e; margin-top: 0; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 20px; margin: 16px 0; }
  label { display: block; color: #8b949e; margin-bottom: 4px; font-size: 12px; }
  input[type=text], input[type=password] {
    background: #21262d; border: 1px solid #30363d; color: #c9d1d9;
    padding: 8px 12px; border-radius: 6px; font-family: monospace; font-size: 13px;
    width: 100%; max-width: 400px; outline: none;
  }
  input[type=text]:focus, input[type=password]:focus { border-color: #58a6ff; }
  input[type=file] { color: #c9d1d9; }
  select {
    background: #21262d; border: 1px solid #30363d; color: #c9d1d9;
    padding: 6px 10px; border-radius: 6px; font-family: monospace; font-size: 13px;
    outline: none; cursor: pointer;
  }
  button {
    background: #238636; color: #fff; border: none; padding: 9px 18px;
    border-radius: 6px; cursor: pointer; font-family: monospace; font-size: 13px;
    transition: background .15s;
  }
  button:hover { background: #2ea043; }
  button.sec { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; }
  button.sec:hover { background: #30363d; }
  button.danger { background: #da3633; }
  button.danger:hover { background: #f85149; }
  .field { margin-bottom: 14px; }
  .file-row { display: flex; align-items: center; gap: 10px; margin: 6px 0;
              padding: 8px 10px; background: #0d1117; border: 1px solid #21262d;
              border-radius: 6px; }
  .file-name { flex: 1; font-size: 12px; color: #8b949e; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }
  .error { color: #f85149; font-size: 13px; margin-top: 8px; }
  .info { color: #8b949e; font-size: 12px; }
  /* Status badges */
  .badge { display: inline-block; padding: 3px 10px; border-radius: 12px;
           font-size: 12px; font-weight: bold; letter-spacing: 0.5px; }
  .badge.RECEIVED, .badge.PREPROCESSING, .badge.OCR_PROCESSING,
  .badge.EXTRACTING, .badge.RAG_SEARCH, .badge.DECISION_PENDING,
  .badge.IDENTITY_CHECK, .badge.SUBMITTING_TO_CORE { background: #9e6a03; color: #fff; }
  .badge.AUTO_APPROVED { background: #1a7f37; color: #fff; }
  .badge.MANUAL_REVIEW { background: #1f6feb; color: #fff; }
  .badge.FRAUD_FLAG { background: #da3633; color: #fff; }
  .badge.REJECTED { background: #da3633; color: #fff; }
  .badge.DOCS_REQUESTED { background: #9e6a03; color: #fff; }
  .badge.PAID { background: #1a7f37; color: #fff; }
  /* Pipeline steps */
  .steps { margin: 14px 0; }
  .step { padding: 5px 0; font-size: 13px; display: flex; align-items: center; gap: 8px; }
  .step-icon { width: 18px; text-align: center; }
  .step.done .step-icon { color: #3fb950; }
  .step.active .step-icon { color: #e3b341; }
  .step.waiting .step-icon { color: #484f58; }
  .step.done .step-label { color: #e6edf3; }
  .step.active .step-label { color: #e3b341; }
  .step.waiting .step-label { color: #484f58; }
  .step-meta { font-size: 11px; color: #8b949e; margin-left: auto; }
  /* Result card */
  .result-block { margin-top: 14px; padding-top: 14px; border-top: 1px solid #21262d; }
  .result-row { display: flex; justify-content: space-between; padding: 4px 0; }
  .result-label { color: #8b949e; }
  .result-val { color: #e6edf3; font-weight: bold; }
  /* Audit table */
  .audit-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
  .audit-table th { color: #8b949e; text-align: left; padding: 4px 8px;
                    border-bottom: 1px solid #21262d; }
  .audit-table td { padding: 4px 8px; border-bottom: 1px solid #0d1117; vertical-align: top; }
  .audit-table tr:hover td { background: #21262d; }
  .tag { display: inline-block; background: #21262d; padding: 1px 6px;
         border-radius: 4px; font-size: 11px; color: #8b949e; margin: 1px; }
  pre.conf { background: #0d1117; padding: 8px; border-radius: 4px;
             font-size: 11px; overflow: auto; max-height: 120px; margin: 0; }
  #spinner { display: none; width: 16px; height: 16px; border: 2px solid #30363d;
             border-top-color: #58a6ff; border-radius: 50%; animation: spin .8s linear infinite;
             vertical-align: middle; margin-left: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  hr { border: none; border-top: 1px solid #21262d; margin: 16px 0; }
</style>
</head>
<body>
<h1>⚕ ICPS Test Tool</h1>
<p class="sub">Инструмент ручного тестирования pipeline обработки заявок. Только dev-режим.</p>

<!-- ── Форма подачи заявки ── -->
<div class="card" id="form-section">
  <h3>Новая заявка</h3>
  <div class="field">
    <label>Номер медкарточки / полиса *</label>
    <input type="text" id="policy_number" placeholder="UNI 700003/1 или DMC-2024-001234">
  </div>
  <div class="field">
    <label>Client reference (опционально)</label>
    <input type="text" id="client_reference" placeholder="TEST-001">
  </div>
  <div class="field">
    <label>X-API-Key (опционально — без ключа используется дефолтный тенант)</label>
    <input type="password" id="api_key" placeholder="sk-...">
  </div>
  <hr>
  <div class="field">
    <label>Документы</label>
    <div id="files-list"></div>
    <button class="sec" onclick="addFileRow()" style="margin-top:8px">+ Добавить файл</button>
  </div>
  <div class="error" id="form-error"></div>
  <div style="margin-top:16px">
    <button onclick="submitClaim()" id="submit-btn">Подать заявку</button>
    <span id="spinner"></span>
  </div>
</div>

<!-- ── Результат обработки ── -->
<div class="card" id="result-section" style="display:none">
  <div style="display:flex; align-items:center; gap:12px; margin-bottom:14px">
    <div>
      <span id="status-badge" class="badge"></span>
      <span id="spinner2" style="display:none; width:14px; height:14px; border:2px solid #30363d;
            border-top-color:#58a6ff; border-radius:50%; animation:spin .8s linear infinite;
            vertical-align:middle; margin-left:8px;"></span>
    </div>
    <div style="margin-left:auto">
      <button class="sec" onclick="newClaim()">+ Новая заявка</button>
    </div>
  </div>
  <div class="info" id="claim-id-line"></div>

  <!-- Pipeline steps -->
  <div class="steps" id="pipeline-steps"></div>

  <!-- Final decision -->
  <div id="final-decision"></div>

  <!-- Audit log -->
  <div id="audit-section" style="margin-top:16px; display:none">
    <div style="display:flex; align-items:center; justify-content:space-between">
      <h3 style="margin:0; font-size:14px; color:#8b949e">Аудит-лог</h3>
      <button class="sec" style="font-size:11px; padding:4px 10px"
              onclick="toggleAudit()">скрыть</button>
    </div>
    <table class="audit-table" id="audit-table">
      <thead><tr><th>Шаг</th><th>Время</th><th>ms</th><th>Уверенность</th></tr></thead>
      <tbody id="audit-body"></tbody>
    </table>
  </div>
</div>

<script>
// ── State ──
let fileCount = 0;
let currentClaimId = null;
let currentApiKey = null;
let pollTimer = null;
let auditVisible = false;

// ── Pipeline definition ──
const PIPELINE = [
  { step: 'download',      label: 'Скачивание документов' },
  { step: 'preprocessing', label: 'Контроль качества' },
  { step: 'ocr',           label: 'Распознавание (OCR)' },
  { step: 'extraction',    label: 'Извлечение данных (AI)' },
  { step: 'decision',      label: 'Решение по договору (AI)' },
  { step: 'routing',       label: 'Маршрутизация и отправка' },
];

const STATUS_ORDER = [
  'RECEIVED','PREPROCESSING','OCR_PROCESSING','EXTRACTING',
  'IDENTITY_CHECK','RAG_SEARCH','DECISION_PENDING','SUBMITTING_TO_CORE',
  'AUTO_APPROVED','MANUAL_REVIEW','FRAUD_FLAG','REJECTED','DOCS_REQUESTED','PAID'
];

const TERMINAL = new Set(['AUTO_APPROVED','MANUAL_REVIEW','FRAUD_FLAG','REJECTED','DOCS_REQUESTED','PAID']);
const STEP_TO_IDX = { download:0, preprocessing:1, ocr:2, extraction:3, decision:4, routing:5 };

// ── Form: add file row ──
function addFileRow() {
  fileCount++;
  const id = fileCount;
  const row = document.createElement('div');
  row.className = 'file-row';
  row.id = `fr-${id}`;
  row.innerHTML = `
    <input type="file" id="f-${id}" accept=".pdf,.jpg,.jpeg,.png"
           onchange="updateFileName(${id})" style="display:none">
    <label for="f-${id}" class="sec"
           style="padding:5px 12px; border:1px solid #30363d; border-radius:6px;
                  cursor:pointer; font-size:12px; white-space:nowrap">Выбрать</label>
    <span class="file-name" id="fn-${id}">файл не выбран</span>
    <select id="dt-${id}">
      <option value="form_100">Форма 100</option>
      <option value="id_document">ID / Паспорт</option>
      <option value="receipt">Чек / Счёт</option>
    </select>
    <button class="sec" style="padding:4px 8px; font-size:12px"
            onclick="document.getElementById('fr-${id}').remove()">✕</button>
  `;
  document.getElementById('files-list').appendChild(row);
}

function updateFileName(id) {
  const f = document.getElementById(`f-${id}`);
  const span = document.getElementById(`fn-${id}`);
  span.textContent = f.files[0] ? f.files[0].name : 'файл не выбран';
  // auto-detect doc type by filename
  const name = (f.files[0]?.name || '').toLowerCase();
  const dt = document.getElementById(`dt-${id}`);
  if (/form.?100|ф100|форм/.test(name)) dt.value = 'form_100';
  else if (/id|passport|паспорт|personal/.test(name)) dt.value = 'id_document';
  else if (/receipt|чек|invoice|bill|kvit/.test(name)) dt.value = 'receipt';
}

// ── Submit ──
async function submitClaim() {
  const err = document.getElementById('form-error');
  err.textContent = '';
  const policy = document.getElementById('policy_number').value.trim();
  const ref    = document.getElementById('client_reference').value.trim();
  const apiKey = document.getElementById('api_key').value.trim();

  if (!policy) { err.textContent = '⚠ Укажите номер медкарточки'; return; }

  const formData = new FormData();
  formData.append('policy_number', policy);
  if (ref) formData.append('client_reference', ref);

  let hasFiles = false;
  for (let i = 1; i <= fileCount; i++) {
    const fi = document.getElementById(`f-${i}`);
    const di = document.getElementById(`dt-${i}`);
    if (fi && fi.files[0]) {
      formData.append('files', fi.files[0]);
      formData.append('doc_types', di.value);
      hasFiles = true;
    }
  }
  if (!hasFiles) { err.textContent = '⚠ Добавьте хотя бы один файл'; return; }

  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  document.getElementById('spinner').style.display = 'inline-block';

  const headers = {};
  if (apiKey) headers['X-API-Key'] = apiKey;

  try {
    const res = await fetch('/devtools/upload', { method: 'POST', headers, body: formData });
    const data = await res.json();
    if (!res.ok) { err.textContent = '✕ ' + (data.detail || 'Ошибка сервера'); return; }

    currentClaimId = data.claim_id;
    currentApiKey  = apiKey || null;
    showResultView(data.claim_id);
    startPolling();
  } catch(e) {
    err.textContent = '✕ Ошибка соединения: ' + e.message;
  } finally {
    btn.disabled = false;
    document.getElementById('spinner').style.display = 'none';
  }
}

// ── Result view ──
function showResultView(claimId) {
  document.getElementById('form-section').style.display = 'none';
  document.getElementById('result-section').style.display = 'block';
  document.getElementById('claim-id-line').textContent = 'claim_id: ' + claimId;
  updateStatus('RECEIVED', []);
}

function newClaim() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  currentClaimId = null;
  document.getElementById('result-section').style.display = 'none';
  document.getElementById('form-section').style.display = 'block';
  document.getElementById('form-error').textContent = '';
  document.getElementById('final-decision').innerHTML = '';
  document.getElementById('audit-section').style.display = 'none';
  document.getElementById('audit-body').innerHTML = '';
  auditVisible = false;
}

// ── Polling ──
function startPolling() {
  pollOnce();
  pollTimer = setInterval(pollOnce, 3000);
}

async function pollOnce() {
  if (!currentClaimId) return;
  const headers = {};
  if (currentApiKey) headers['X-API-Key'] = currentApiKey;

  try {
    const [statusRes, auditRes] = await Promise.all([
      fetch(`/v1/claims/${currentClaimId}`, { headers }),
      fetch(`/v1/claims/${currentClaimId}/audit`, { headers }),
    ]);
    if (!statusRes.ok) return;
    const claim = await statusRes.json();
    const audit = auditRes.ok ? await auditRes.json() : { entries: [] };

    updateStatus(claim.status, audit.entries || []);
    updateFinal(claim);
    updateAudit(audit.entries || []);

    if (TERMINAL.has(claim.status)) {
      clearInterval(pollTimer); pollTimer = null;
      document.getElementById('spinner2').style.display = 'none';
    }
  } catch(e) {}
}

// ── UI helpers ──
function updateStatus(status, auditEntries) {
  // Badge
  const badge = document.getElementById('status-badge');
  badge.textContent = status;
  badge.className = 'badge ' + status;
  const spin2 = document.getElementById('spinner2');
  spin2.style.display = TERMINAL.has(status) ? 'none' : 'inline-block';

  // Pipeline steps: determine done steps from audit
  const doneSteps = new Set(auditEntries.map(e => e.step));
  // Find active step by status
  const statusToStep = {
    PREPROCESSING: 'preprocessing', OCR_PROCESSING: 'ocr',
    EXTRACTING: 'extraction', IDENTITY_CHECK: 'extraction',
    RAG_SEARCH: 'decision', DECISION_PENDING: 'decision',
    SUBMITTING_TO_CORE: 'routing',
  };
  const activeStep = statusToStep[status] || null;

  let html = '';
  for (const p of PIPELINE) {
    let cls = 'waiting', icon = '○';
    const ms = auditEntries.find(e => e.step === p.step);
    if (doneSteps.has(p.step)) {
      cls = 'done'; icon = '✓';
    } else if (p.step === activeStep) {
      cls = 'active'; icon = '◌';
    } else if (TERMINAL.has(status)) {
      cls = 'waiting'; icon = '—';
    }
    const meta = ms ? `${ms.duration_ms != null ? ms.duration_ms + ' ms' : ''}` : '';
    html += `<div class="step ${cls}">
      <span class="step-icon">${icon}</span>
      <span class="step-label">${p.label}</span>
      <span class="step-meta">${meta}</span>
    </div>`;
  }
  document.getElementById('pipeline-steps').innerHTML = html;
}

function updateFinal(claim) {
  if (!TERMINAL.has(claim.status)) return;
  const fmt = (v, suffix='') => v != null ? `<span class="result-val">${v}${suffix}</span>` : '<span style="color:#484f58">—</span>';
  const pct = claim.overall_confidence != null ? (claim.overall_confidence * 100).toFixed(1) + '%' : null;

  let html = `<div class="result-block">`;
  html += `<div class="result-row"><span class="result-label">Статус решения</span>${fmt(claim.decision_type || claim.status)}</div>`;
  if (claim.final_payout != null)
    html += `<div class="result-row"><span class="result-label">Итоговая выплата</span>${fmt(claim.final_payout, ' GEL')}</div>`;
  if (claim.total_approved != null && claim.total_approved !== claim.final_payout)
    html += `<div class="result-row"><span class="result-label">Одобрено</span>${fmt(claim.total_approved, ' GEL')}</div>`;
  if (claim.total_claimed != null)
    html += `<div class="result-row"><span class="result-label">Запрошено</span>${fmt(claim.total_claimed, ' GEL')}</div>`;
  if (pct)
    html += `<div class="result-row"><span class="result-label">Уверенность AI</span>${fmt(pct)}</div>`;
  if (claim.routing_reason)
    html += `<div class="result-row"><span class="result-label">Причина</span><span class="result-val" style="font-size:12px">${claim.routing_reason}</span></div>`;
  html += '</div>';
  document.getElementById('final-decision').innerHTML = html;
  document.getElementById('audit-section').style.display = 'block';
}

function updateAudit(entries) {
  if (!entries.length) return;
  let rows = '';
  for (const e of entries) {
    const conf = e.confidence ? `<pre class="conf">${JSON.stringify(e.confidence, null, 2)}</pre>` : '—';
    const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString('ru', {hour12:false}) : '';
    rows += `<tr>
      <td><span class="tag">${e.step}</span></td>
      <td style="color:#8b949e">${ts}</td>
      <td style="color:#8b949e">${e.duration_ms != null ? e.duration_ms : '—'}</td>
      <td>${conf}</td>
    </tr>`;
  }
  document.getElementById('audit-body').innerHTML = rows;
  document.getElementById('audit-section').style.display = 'block';
}

function toggleAudit() {
  const tbl = document.getElementById('audit-table');
  auditVisible = !auditVisible;
  tbl.style.display = auditVisible ? 'none' : 'table';
}

// Init: start with two file rows
addFileRow();
</script>
</body>
</html>"""


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def devtools_page() -> HTMLResponse:
    """HTML-страница тестирования (только development)."""
    if settings.environment == "production":
        raise HTTPException(status_code=404)
    return HTMLResponse(_HTML)


@router.post("/upload")
async def devtools_upload(
    policy_number: str = Form(...),
    client_reference: str = Form(""),
    files: list[UploadFile] = File(...),
    doc_types: list[str] = Form(...),
    db: AsyncSession = Depends(get_db),
    tenant_id: UUID = Depends(get_tenant_id),
) -> dict:
    """
    Принять файлы напрямую, сохранить в storage, запустить pipeline.

    Эквивалент POST /v1/claims, но без pre-signed URL: файлы передаются
    как multipart/form-data и сохраняются в storage до запуска worker-а.
    Downloader в tasks.py пропускает документы, у которых уже есть storage_path.
    Только development-окружение.
    """
    if settings.environment == "production":
        raise HTTPException(status_code=404)

    if len(files) != len(doc_types):
        raise HTTPException(
            status_code=422,
            detail=f"Количество файлов ({len(files)}) не совпадает с количеством типов ({len(doc_types)})",
        )

    valid_types = {dt.value for dt in DocType}
    for dt in doc_types:
        if dt not in valid_types:
            raise HTTPException(status_code=422, detail=f"Неизвестный тип документа: {dt!r}")

    # Создать запись заявки
    claim = Claim(
        tenant_id=tenant_id,
        policy_number=policy_number.strip(),
        client_reference=client_reference.strip() or None,
        status=ClaimStatus.RECEIVED,
    )
    db.add(claim)
    await db.flush()  # получить claim.id до добавления документов

    storage = get_storage_client()
    log.info("devtools_upload", claim_id=str(claim.id), files=len(files))

    # Сохранить каждый файл в storage и создать ClaimDocument
    for upload_file, dt_str in zip(files, doc_types):
        data = await upload_file.read()
        filename = upload_file.filename or f"document_{uuid.uuid4().hex[:6]}.bin"
        content_type = upload_file.content_type or "application/octet-stream"

        storage_path = storage.generate_path(
            tenant_id=str(tenant_id),
            claim_id=str(claim.id),
            filename=filename,
        )
        await storage.upload(data, storage_path, content_type=content_type)

        doc = ClaimDocument(
            claim_id=claim.id,
            tenant_id=tenant_id,
            doc_type=DocType(dt_str),
            doc_type_source="filename_hint",
            doc_type_confirmed=False,
            storage_path=storage_path,
            source_url=None,  # уже в storage → downloader пропустит шаг 0
        )
        db.add(doc)

    await db.commit()

    # Запустить pipeline
    celery_app.send_task(
        "process_claim",
        args=[str(claim.id), str(tenant_id)],
    )

    return {"claim_id": str(claim.id), "status": "RECEIVED"}
