"""Browser file transfer endpoints; capabilities travel in headers, never paths."""
import hashlib
from fastapi import APIRouter, Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from app.transfers import TransferStore, MAX_SQL_BYTES

CLIENT = r'''
const token = location.hash.slice(1);
history.replaceState(null, '', location.pathname);
const status = document.getElementById('status');
const mode = document.body.dataset.mode;
if (!token) {
  status.textContent = 'Missing transfer token. Request a new link from ChatGPT.';
} else if (mode === 'download') {
  (async () => {
    const response = await fetch('/files/content', {headers: {'X-Transfer-Token': token, 'ngrok-skip-browser-warning': 'true'}, cache: 'no-store', credentials: 'omit'});
    if (!response.ok) {status.textContent = 'Download unavailable or expired. Request a new export.'; return;}
    const mediaType = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
    const filename = response.headers.get('Content-Disposition')?.match(/filename="([a-z][a-z0-9-]{0,62}\.sql)"/)?.[1];
    const expectedHash = response.headers.get('X-Content-SHA256') || '';
    if (mediaType !== 'application/sql' || !filename || !/^[a-f0-9]{64}$/.test(expectedHash)) {
      status.textContent = 'Download blocked: the tunnel returned HTML or an unexpected response, not a SQL backup. Accept the ngrok visitor warning in a normal browser tab, then request a fresh export.'; return;
    }
    const blob = await response.blob();
    if (blob.size > 2097152 || !(await blob.slice(0, 96).text()).startsWith('-- Standard MCP full project SQL v1\n')) {
      status.textContent = 'Download blocked: this response is not a supported project SQL backup.'; return;
    }
    const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
    const actualHash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
    if (actualHash !== expectedHash) {
      status.textContent = 'Download blocked: backup checksum verification failed. Request a fresh export.'; return;
    }
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob); link.download = filename;
    link.textContent = 'Download ' + filename; document.body.appendChild(link);
    link.click(); status.textContent = 'Your SQL download is ready. The link expires in 15 minutes.';
  })().catch(() => {status.textContent = 'Download failed; request a new export.';});
} else {
  document.getElementById('upload').hidden = false;
  document.getElementById('upload').onsubmit = async event => {
    event.preventDefault();
    const file = document.getElementById('file').files[0];
    if (!file || !file.name.endsWith('.sql') || file.size > 2097152 || file.size === 0) {
      status.textContent = 'Select a nonempty .sql file of at most 2 MiB.'; return;
    }
    try {
      const response = await fetch('/files/upload-content', {method: 'POST', headers: {'X-Transfer-Token': token, 'Content-Type': 'application/sql', 'ngrok-skip-browser-warning': 'true'}, body: file, credentials: 'omit'});
      if (!(response.headers.get('Content-Type') || '').toLowerCase().includes('application/json')) {
        status.textContent = 'Upload blocked: the tunnel returned an unexpected page. Accept the ngrok visitor warning, then request a fresh upload link.'; return;
      }
      const result = await response.json();
      status.textContent = response.ok ? 'Uploaded. No project changes were made. Return to ChatGPT to preview import ' + result.import_id + '.' : result.detail;
      if (response.ok) document.getElementById('upload').hidden = true;
    } catch (_) {status.textContent = 'Upload failed; request a new import link.';}
  };
}
'''
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"}


def file_router(settings):
    router = APIRouter()
    store = TransferStore(settings.data_dir)

    @router.get('/files/client.js')
    def client_js():
        return Response(CLIENT, media_type='application/javascript', headers=HEADERS)

    def page(mode):
        return HTMLResponse(f'''<!doctype html><html><head><meta charset="utf-8"><title>Project SQL {mode}</title><script src="/files/client.js" defer></script></head><body data-mode="{mode}"><h1>Project SQL {mode}</h1><p id="status">This private transfer link expires in 15 minutes. Do not share it.</p><form id="upload" hidden><input id="file" type="file" accept=".sql" required><button type="submit">Upload SQL for preview</button></form></body></html>''', headers=HEADERS)

    @router.get('/files/download')
    def download_page():
        return page('download')

    @router.get('/files/upload')
    def upload_page():
        return page('upload')

    @router.get('/files/content')
    def download(request: Request):
        token = request.headers.get('x-transfer-token', '')
        if not token:
            return JSONResponse({'detail': 'Transfer not found or expired'}, 404, headers=HEADERS)
        try:
            record = store.lookup(token=token, kind='export')
        except ValueError:
            return JSONResponse({'detail': 'Transfer not found or expired'}, 404, headers=HEADERS)
        return Response(bytes(record['payload']), media_type='application/sql', headers={**HEADERS, 'Content-Disposition': f'attachment; filename="{record["project"]}.sql"', 'X-Content-SHA256': hashlib.sha256(bytes(record['payload'])).hexdigest()})

    @router.post('/files/upload-content')
    async def upload(request: Request):
        origin = request.headers.get('origin')
        if origin and origin != settings.public_url:
            return JSONResponse({'detail': 'Origin not allowed'}, 403, headers=HEADERS)
        token = request.headers.get('x-transfer-token', '')
        try:
            if not token:
                raise ValueError('Missing transfer token')
            record = store.lookup(token=token, kind='import')
        except ValueError:
            return JSONResponse({'detail': 'Transfer not found or expired'}, 404, headers=HEADERS)
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_SQL_BYTES:
                return JSONResponse({'detail': 'SQL file exceeds 2 MiB'}, 413, headers=HEADERS)
        try:
            store.upload(token, bytes(payload))
        except ValueError as exc:
            return JSONResponse({'detail': str(exc)}, 400, headers=HEADERS)
        return JSONResponse({'status': 'uploaded', 'import_id': record['id'], 'message': 'File staged only; project unchanged'}, headers=HEADERS)

    return router
