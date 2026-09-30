import { api, fmt } from '/web/app.js';

export default async function (root) {
  const rows = await api('/api/projects');
  root.innerHTML = `
    <div class="card">
      <h2>Projects</h2>
      <p class="muted" style="margin:-8px 0 14px">Sorted by estimated API cost. Cache reads are cheap per token, but long sessions re-read the whole context on every call — the cache-read cost column shows how much of each project's bill that adds up to.</p>
      <table>
        <thead><tr><th>project</th><th class="num">sessions</th><th class="num">prompts</th><th class="num">billable tokens</th><th class="num">cache reads</th><th class="num">cache-read cost</th><th class="num">cost</th></tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td title="${fmt.htmlSafe(r.project_slug)}">${fmt.htmlSafe(r.project_name || r.project_slug)}</td>
              <td class="num">${fmt.int(r.sessions)}</td>
              <td class="num">${fmt.int(r.turns)}</td>
              <td class="num">${fmt.int(r.billable_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
              <td class="num">${fmt.usd(r.cache_read_usd)}</td>
              <td class="num">${r.cost_estimated ? '~' : ''}${fmt.usd(r.cost_usd)}</td>
            </tr>`).join('') || '<tr><td colspan="7" class="muted">no projects yet</td></tr>'}
        </tbody>
      </table>
    </div>`;
}
