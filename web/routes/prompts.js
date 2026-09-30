import { api, fmt } from '/web/app.js';

const SORTS = [
  { key: 'cost',   label: 'Most expensive' },
  { key: 'tokens', label: 'Most tokens' },
  { key: 'recent', label: 'Most recent' },
];

const promptLabel = r => (r.prompt_text || '').trim() || '(image only)';
const cost = r => (r.estimated ? '~' : '') + fmt.usd4(r.cost_usd);

function readSort() {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)sort=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return SORTS.find(s => s.key === k) || SORTS[0];
}

function writeSort(key) {
  const base = (location.hash.replace(/^#/, '').split('?')[0]) || '/prompts';
  location.hash = '#' + base + '?sort=' + encodeURIComponent(key);
}

export default async function (root) {
  const sort = readSort();
  const rows = await api('/api/prompts?limit=100&sort=' + encodeURIComponent(sort.key));

  const sortTabs = `
    <div class="range-tabs" role="tablist">
      ${SORTS.map(s => `<button data-sort="${s.key}" class="${s.key === sort.key ? 'active' : ''}">${s.label}</button>`).join('')}
    </div>`;

  const subtitle = {
    cost:   'Your most expensive prompts. Cost covers every API call a prompt triggered — the whole tool loop and any subagents — until your next message.',
    tokens: 'Prompts ranked by billable tokens (input + output + cache writes) across every API call they triggered.',
    recent: 'Your latest prompts and what each one cost in total.',
  }[sort.key] + ' Click a row to see the full prompt.';

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Prompts</h2>
      <div class="spacer"></div>
      ${sortTabs}
    </div>

    <div class="card">
      <p class="muted" style="margin:0 0 14px">${subtitle}</p>
      <table id="prompts">
        <thead><tr>
          <th>${sort.key === 'recent' ? 'when' : 'cost'}</th>
          <th>prompt</th>
          <th>model</th>
          <th class="num" title="API calls this prompt triggered">calls</th>
          <th class="num">tokens</th>
          <th class="num">cache rd</th>
          <th>session</th>
        </tr></thead>
        <tbody>
          ${rows.map((r,i) => `
            <tr data-i="${i}" style="cursor:pointer">
              <td class="${sort.key === 'recent' ? 'mono' : 'num mono'}">${sort.key === 'recent' ? fmt.ts(r.timestamp) : cost(r)}</td>
              <td class="blur-sensitive">${fmt.htmlSafe(fmt.short(promptLabel(r), 110))}</td>
              <td>${r.model ? `<span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span>` : '<span class="muted">no reply</span>'}</td>
              <td class="num">${fmt.int(r.api_calls)}</td>
              <td class="num">${fmt.int(r.billable_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
              <td><a href="#/sessions/${encodeURIComponent(r.session_id)}" class="mono" onclick="event.stopPropagation()">${fmt.htmlSafe(r.session_id.slice(0,8))}…</a></td>
            </tr>`).join('') || '<tr><td colspan="7" class="muted">no prompts yet</td></tr>'}
        </tbody>
      </table>
    </div>
    <div id="drawer"></div>
  `;

  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeSort(btn.dataset.sort));
  });

  root.querySelectorAll('#prompts tbody tr').forEach(tr => {
    tr.addEventListener('click', () => {
      const r = rows[Number(tr.dataset.i)];
      const drawer = document.getElementById('drawer');
      drawer.innerHTML = `
        <div class="card">
          <h3 style="display:flex;align-items:center">
            <span>Prompt detail</span>
            <span class="spacer"></span>
            <span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span>
          </h3>
          <pre class="blur-sensitive">${fmt.htmlSafe(promptLabel(r))}</pre>
          <div class="flex" style="margin-top:12px;flex-wrap:wrap;gap:14px">
            <span class="muted">${fmt.ts(r.timestamp)}</span>
            <span class="muted">${fmt.int(r.api_calls)} API calls · ${fmt.int(r.billable_tokens)} billable · ${fmt.int(r.cache_read_tokens)} cache rd · ${cost(r)} total (${fmt.usd4(r.cache_read_usd)} of it cache reads)</span>
            <span class="spacer"></span>
            <a href="#/sessions/${encodeURIComponent(r.session_id)}">Open session →</a>
          </div>
        </div>`;
      drawer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  });
}
