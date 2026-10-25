// engine.mjs — universal workflow runtime (shared by server + tests)
// Executes graph/workflows.compiled.json ("universal-workflow-graph/v1")
// with Google's Gemini API (Gemini 2.5 Pro / Flash, Nano Banana Pro).

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const GRAPH_PATH = path.join(__dirname, '..', 'graph', 'workflows.compiled.json');

export function loadGraph(p = GRAPH_PATH) {
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

export function findWorkflow(graph, id) {
  return graph.workflows.find(w => w.id === id);
}

// ---- template helpers ----------------------------------------------------

function nodeTitleOf(wf, nodeId) {
  const n = wf.nodes.find(x => x.id === nodeId);
  return n ? (n.title || n.id) : nodeId;
}

// Replace @{uuid-or-id} refs with upstream values.
export function renderTemplate(tpl, wf, outputs) {
  if (!tpl) return '';
  return String(tpl).replace(/@\{([^}]+)\}/g, (_, ref) => {
    const id = resolveNodeId(wf, ref);
    if (!(id in outputs)) return `[pending:${nodeTitleOf(wf, id)}]`;
    return stringify(outputs[id]);
  });
}

export function resolveNodeId(wf, ref) {
  const byId = wf.nodes.find(n => n.id === ref);
  if (byId) return byId.id;
  const byOrig = wf.nodes.find(n => n.originalId === ref);
  return byOrig ? byOrig.id : ref;
}

function stringify(v) {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return v.map(stringify).join('\n\n');
  if (typeof v === 'object') return [v.text || '', ...(v.dataUrls || [])].filter(Boolean).join('\n\n');
  return String(v);
}

// ---- routing --------------------------------------------------------------

function routeKey(route) {
  return String(route.label ?? route.targetId ?? '').trim().toLowerCase();
}

function pickRoute(routes, choice) {
  const c = String(choice ?? '').trim().toLowerCase();
  // exact label match first, then prefix/contains match
  return routes.find(r => routeKey(r) === c)
      || routes.find(r => c.startsWith(routeKey(r)))
      || routes.find(r => routeKey(r).startsWith(c))
      || null;
}

// ---- generation -----------------------------------------------------------

const BASE_URL = () => process.env.GEMINI_BASE_URL
  || 'https://generativelanguage.googleapis.com/v1beta';

async function callGemini({ model, prompt, imageRefs, apiKey }) {
  const role = (modelRole(model));
  const parts = [{ text: prompt }];
  for (const b64 of (imageRefs || [])) {
    parts.push({ inlineData: { mimeType: 'image/png', data: b64 } });
  }
  const cfg = {
    contents: [{ role: 'user', parts }],
    generationConfig: role === 'image'
      ? { responseModalities: ['TEXT', 'IMAGE'] }
      : { temperature: 0.8 },
  };
  const url = `${BASE_URL()}/models/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(apiKey)}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(cfg),
  });
  if (!res.ok) throw new Error(`Gemini ${model} -> HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const j = await res.json();
  const outParts = j?.candidates?.[0]?.content?.parts ?? [];
  const texts = outParts.filter(p => p.text).map(p => p.text);
  const images = outParts.filter(p => p.inlineData).map(p => `data:${p.inlineData.mimeType || 'image/png'};base64,${p.inlineData.data}`);
  if (!texts.length && !images.length) throw new Error(`Gemini ${model}: empty response`);
  return role === 'image'
    ? { kind: 'image', text: texts.join('\n'), dataUrls: images }
    : { kind: 'text', text: texts.join('\n'), dataUrls: [] };
}

function modelRole(model) {
  return ({ 'gemini-2.5-pro': 'text', 'gemini-2.5-flash': 'text',
            'gemini-3-pro-image': 'image', 'gemini-2.5-flash-image': 'image' })[model] || 'text';
}

function simulate(node, rendered) {
  if (node.capability === 'generate-image') {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="720" height="480"><rect width="100%" height="100%" fill="#111a2e"/><text x="24" y="40" fill="#7fd1ff" font-family="monospace" font-size="20">[SIM] ${escapeXml(node.title)}</text><text x="24" y="80" fill="#cfe4ff" font-family="monospace" font-size="13">${escapeXml(rendered.slice(0, 90))}</text></svg>`;
    return { kind: 'image', text: `[simulation] ${node.title}`, dataUrls: ['data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64')] };
  }
  return { kind: 'text', text: `[simulation] ${node.title}\n\nPrompt preview:\n\`\`\`\n${rendered.slice(0, 400)}\n\`\`\``, dataUrls: [] };
}

function escapeXml(s) { return String(s).replace(/[<>&"]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c])); }

// ---- main runner ------------------------------------------------------------
// inputs: { [nodeId or title]: value }   options: { allPro, proTextModel, proImageModel, apiKey }
export async function runWorkflow(wf, inputs = {}, options = {}) {
  const {
    allPro = false,
    proTextModel = 'gemini-2.5-pro',
    proImageModel = 'gemini-3-pro-image',
    apiKey = process.env.GEMINI_API_KEY || '',
  } = options;

  const outputs = {};       // nodeId -> raw value (string | {kind,text,dataUrls})
  const trace = [];         // execution log
  const errors = [];
  const normalize = s => String(s).replace(/\s+/g, ' ').trim().toLowerCase();
  const inputIndex = new Map();
  for (const [k, v] of Object.entries(inputs)) {
    const n = wf.nodes.find(x => x.id === k) || wf.nodes.find(x => x.originalId === k)
            || wf.nodes.find(x => normalize(x.title) === normalize(k))
            || wf.nodes.find(x => normalize(x.label || '') === normalize(k));
    if (n) inputIndex.set(n.id, v); else errors.push(`Unknown input key "${k}"`);
  }

  const genNodes = wf.nodes.filter(n => n.capability === 'generate-text' || n.capability === 'generate-image');
  const routers = wf.nodes.filter(n => n.capability === 'router');
  const preds = new Map(), succs = new Map();
  for (const n of wf.nodes) { preds.set(n.id, []); succs.set(n.id, []); }
  for (const e of wf.edges) {
    if (preds.has(e.to)) preds.get(e.to).push(e.from);
    if (succs.has(e.from)) succs.get(e.from).push(e.to);
  }

  const fired = new Set(), skipped = new Set();
  const queue = [];
  const tryFire = id => {
    if (fired.has(id) || queue.includes(id)) return;
    const ps = preds.get(id) || [];
    if (ps.some(p => skipped.has(p))) { skipSubtree(id); return; }
    if (ps.every(p => fired.has(p))) queue.push(id);
  };
  const skipSubtree = id => {
    if (skipped.has(id)) return;
    skipped.add(id);
    for (const s of succs.get(id) || []) skipSubtree(s);
  };

  for (const n of wf.nodes) if ((preds.get(n.id) || []).length === 0) tryFire(n.id);

  let guard = 0;
  while (queue.length && guard++ < 5000) {
    const id = queue.shift();
    if (fired.has(id)) continue;
    const node = wf.nodes.find(n => n.id === id);
    if (!node) continue;
    fired.add(id);

    if (node.capability === 'input') {
      outputs[id] = inputIndex.get(id) ?? '';
      if (!outputs[id] && node.required) trace.push({ node: id, type: 'input', note: 'MISSING required input' });
      else trace.push({ node: id, type: 'input', value: stringify(outputs[id]).slice(0, 120) });
    } else if (node.capability === 'router') {
      const src = node.inputRefs?.[0]?.nodeId ? resolveNodeId(wf, node.inputRefs[0].nodeId) : (preds.get(id) || [])[0];
      const choice = outputs[src];
      const hit = pickRoute(node.routes || [], choice);
      if (!hit) {
        trace.push({ node: id, type: 'router', choice: stringify(choice), note: 'no matching route — downstream paused' });
        for (const s of succs.get(id) || []) skipSubtree(s);
        continue;
      }
      const target = resolveNodeId(wf, hit.targetId);
      trace.push({ node: id, type: 'router', choice: stringify(choice), route: hit.label });
      for (const s of succs.get(id) || []) {
        if (s === target || s === hit.targetId) { skipped.has(s) || fired.has(s) || queue.push(s); }
        else skipSubtree(s);
      }
    } else if (node.capability === 'output') {
      const rendered = renderTemplate(node.template, wf, outputs);
      // keep structured image results (with dataUrls) instead of flattening to text
      const m = /^@\{([^}]+)\}$/.exec(String(node.template || '').trim());
      const refId = m ? resolveNodeId(wf, m[1]) : null;
      outputs[id] = (refId && outputs[refId] && typeof outputs[refId] === 'object' && outputs[refId].dataUrls?.length)
        ? outputs[refId] : rendered;
      trace.push({ node: id, type: 'output', title: node.title });
    } else if (node.capability === 'generate-text' || node.capability === 'generate-image') {
      const rendered = renderTemplate(node.template, wf, outputs);
      const baseModel = node.model || 'gemini-2.5-pro';
      const model = allPro
        ? (modelRole(baseModel) === 'image' ? proImageModel : proTextModel)
        : baseModel;
      let result;
      try {
        if (apiKey) {
          const imgB64 = [];
          for (const p of preds.get(id) || []) {
            const v = outputs[p];
            if (v && typeof v === 'object' && v.dataUrls?.length) {
              for (const du of v.dataUrls) { const m = /^data:[^;]+;base64,(.+)$/.exec(du); if (m) imgB64.push(m[1]); }
            }
          }
          result = await callGemini({ model, prompt: rendered, imageRefs: node.capability === 'generate-image' ? imgB64 : [], apiKey });
        } else {
          result = simulate(node, rendered);
        }
        outputs[id] = result;
        trace.push({ node: id, type: node.capability, title: node.title, model, simulated: !apiKey, ok: true });
      } catch (err) {
        errors.push({ node: id, error: String(err.message || err) });
        outputs[id] = { kind: 'error', text: `[ERROR] ${err.message}`, dataUrls: [] };
        trace.push({ node: id, type: node.capability, title: node.title, model, ok: false, error: String(err.message) });
      }
    } else {
      trace.push({ node: id, type: node.capability, note: 'passthrough' });
      outputs[id] = '';
    }
    for (const s of succs.get(id) || []) tryFire(s);
  }

  const results = wf.nodes
    .filter(n => n.capability === 'output' && fired.has(n.id))
    .map(n => ({ nodeId: n.id, title: n.title, value: outputs[n.id] }))
    .filter(r => stringify(r.value).trim());
  // if no output nodes were reached, expose generated nodes directly
  if (!results.length) {
    for (const n of genNodes) if (fired.has(n.id) && outputs[n.id]) {
      results.push({ nodeId: n.id, title: n.title, value: outputs[n.id] });
    }
  }

  return { workflow: wf.id, title: wf.title, pendingInputs: getPendingInputs(wf, fired, inputIndex), trace, results, errors, simulated: !apiKey };
}

function getPendingInputs(wf, fired, inputIndex) {
  return wf.nodes
    .filter(n => n.capability === 'input' && fired.has(n.id) && !(n.id in Object.fromEntries(inputIndex)))
    .map(n => ({ nodeId: n.id, title: n.title, label: n.label || n.title }));
}

export function describeWorkflow(wf) {
  return {
    id: wf.id, title: wf.title, description: wf.description,
    inputs: wf.nodes.filter(n => n.capability === 'input').map(n => ({ nodeId: n.id, title: n.title, label: n.label })),
    models: [...new Set(wf.nodes.filter(n => n.model).map(n => n.model))],
    counts: { nodes: wf.nodes.length, edges: wf.edges.length },
  };
}
