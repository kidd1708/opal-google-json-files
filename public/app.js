// app.js — interactive flow runner for the Content Factory web UI
const $ = s => document.querySelector(s);
let currentWf = null;

async function api(pathname, opts) {
  const r = await fetch(pathname, opts);
  if (!r.ok) throw new Error((await r.text()).slice(0, 200));
  return r.json();
}

(async function init() {
  try {
    const info = await api('/api/workflows');
    $('#mode-badge').textContent = 'engine: ' + (info.provider || 'gemini') + ' · models: ' + Object.keys(info.models || {}).length;
    const list = $('#workflows');
    for (const wf of info.workflows) {
      const el = document.createElement('div');
      el.className = 'wf-card';
      el.innerHTML = `<h3>${esc(wf.title || wf.id)}</h3>
        <div class="meta">${wf.counts.nodes} nodes · ${wf.counts.edges} edges<br>${esc((wf.description || '').slice(0, 140))}</div>`;
      el.onclick = () => { document.querySelectorAll('.wf-card').forEach(c => c.classList.remove('selected')); el.classList.add('selected'); selectWorkflow(wf.id); };
      list.appendChild(el);
    }
  } catch (e) { $('#mode-badge').textContent = 'API unreachable: ' + e.message; }
})();

async function selectWorkflow(id) {
  currentWf = await api('/api/workflow/' + encodeURIComponent(id));
  const box = $('#inputs'); box.innerHTML = '';
  for (const n of currentWf.nodes.filter(x => x.capability === 'input')) {
    const lab = document.createElement('label');
    lab.className = 'field';
    lab.innerHTML = `<span>${esc(n.label || n.title)}${n.required ? ' *' : ''}</span>`;
    const ta = document.createElement('textarea');
    ta.dataset.nodeId = n.id;
    ta.placeholder = n.title;
    lab.appendChild(ta);
    box.appendChild(lab);
  }
  $('#inputs-section').classList.remove('hidden');
  $('#results-section').classList.add('hidden');
}

$('#run-btn').onclick = async () => {
  if (!currentWf) return;
  const inputs = {};
  document.querySelectorAll('#inputs textarea').forEach(t => { if (t.value.trim()) inputs[t.dataset.nodeId] = t.value.trim(); });
  const btn = $('#run-btn'); btn.disabled = true; btn.textContent = '⏳ running…';
  try {
    const out = await api('/api/run', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ workflow: currentWf.id, inputs, all_pro: $('#all-pro').checked }),
    });
    render(out);
  } catch (e) {
    $('#results').innerHTML = `<div class="result"><h4>Error</h4><pre>${esc(e.message)}</pre></div>`;
    $('#results-section').classList.remove('hidden');
  } finally { btn.disabled = false; btn.textContent = '▶ Run workflow'; }
};

function render(out) {
  $('#trace').textContent = (out.trace || []).map(t =>
    `${t.type.padEnd(15)} ${t.title || t.node}${t.model ? ' [' + t.model + (t.simulated ? ' SIM' : '') + ']' : ''}${t.route ? ' → route "' + t.route + '"' : ''}${t.error ? ' ✗ ' + t.error : ''}`
  ).join('\n');
  const box = $('#results'); box.innerHTML = '';
  if (out.simulated) box.innerHTML += `<div class="result"><h4>Simulation mode</h4><pre>No GEMINI_API_KEY on the server — prompts were rendered but not sent to Gemini. Restart the server with a key for live runs.</pre></div>`;
  for (const r of out.results || []) {
    const v = r.value;
    const el = document.createElement('div'); el.className = 'result';
    let inner = `<h4>${esc(r.title || r.nodeId)}</h4>`;
    const text = typeof v === 'string' ? v : (v.text || '');
    const imgs = typeof v === 'object' ? (v.dataUrls || []) : [];
    if (text) inner += `<pre>${esc(text)}</pre>`;
    for (const src of imgs) inner += `<img alt="${esc(r.title)}" src="${src}" />`;
    el.innerHTML = inner;
    box.appendChild(el);
  }
  if (!(out.results || []).length) box.innerHTML += `<div class="result"><pre>No outputs produced — fill in the required inputs (see trace).</pre></div>`;
  $('#results-section').classList.remove('hidden');
}

function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
