// server.mjs — dependency-free HTTP server for the Content Factory app
// Usage:  GEMINI_API_KEY=... node server/server.mjs   (port via PORT, default 8080)
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadGraph, findWorkflow, runWorkflow, describeWorkflow } from './engine.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(__dirname, '..', 'public');
const PORT = Number(process.env.PORT || 8080);
const graph = loadGraph();

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'content-type': 'application/json', 'access-control-allow-origin': '*' });
  res.end(body);
}

async function readBody(req) {
  let data = '';
  for await (const chunk of req) { data += chunk; if (data.length > 5e6) break; }
  try { return JSON.parse(data || '{}'); } catch { return {}; }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const p = url.pathname;

  // ---- API ----
  if (p === '/api/workflows') {
    return json(res, 200, { provider: graph.provider, models: graph.models, workflows: graph.workflows.map(describeWorkflow) });
  }
  if (p.startsWith('/api/workflow/')) {
    const wf = findWorkflow(graph, decodeURIComponent(p.slice('/api/workflow/'.length)));
    return wf ? json(res, 200, wf) : json(res, 404, { error: 'workflow not found' });
  }
  if (p === '/api/run' && req.method === 'POST') {
    const body = await readBody(req);
    const wf = findWorkflow(graph, body.workflow || '');
    if (!wf) return json(res, 404, { error: `unknown workflow "${body.workflow}"`, available: graph.workflows.map(w => w.id) });
    try {
      const out = await runWorkflow(wf, body.inputs || {}, {
        allPro: !!body.all_pro || url.searchParams.get('all_pro') === '1',
        apiKey: body.api_key || process.env.GEMINI_API_KEY || '',
      });
      return json(res, 200, out);
    } catch (err) {
      return json(res, 500, { error: String(err.message || err) });
    }
  }

  // ---- static UI ----
  let file = p === '/' ? '/index.html' : p;
  file = path.normalize(path.join(PUBLIC, file));
  if (!file.startsWith(PUBLIC)) { res.writeHead(403); return res.end(); }
  fs.readFile(file, (err, buf) => {
    if (err) { res.writeHead(404, { 'content-type': 'text/plain' }); return res.end('not found'); }
    res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(buf);
  });
});

server.listen(PORT, () => {
  const mode = process.env.GEMINI_API_KEY ? 'LIVE (Gemini API)' : 'SIMULATION (no GEMINI_API_KEY set)';
  console.log(`🏭 Content Factory running at http://localhost:${PORT}  —  mode: ${mode}`);
  console.log(`   workflows: ${graph.workflows.map(w => w.id).join(', ')}`);
});
