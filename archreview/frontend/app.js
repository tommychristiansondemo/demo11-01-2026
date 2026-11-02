/* Enterprise Solution Review Assistant - front end (vanilla JS, no third-party code).
 * Output encoding: all server data is rendered with textContent / DOM APIs only (never innerHTML). */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const store = {
    get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private mode */ } },
    del(k) { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
  };
  let cfg = null, examples = [], session = null, pollTimer = null, elapsedTimer = null, current = null;

  // ---------- DOM helper (safe: text only) ----------
  function el(tag, attrs = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = String(v);
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, String(v));
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return n;
  }
  const cite = (s) => s ? el('span', { class: 'cite', title: 'Source MCP server and record ID', text: `${s.server === 'enterprise-standards' ? 'standards' : 'catalog'}:${s.record_id}` }) : null;
  const badge = (text, kind = '') => el('span', { class: `badge ${kind}`, text });

  // ---------- API ----------
  function cid() { return 'web-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)); }
  async function api(path, { method = 'GET', body, auth = true } = {}) {
    const headers = { 'X-Correlation-Id': cid() };
    if (body) headers['Content-Type'] = 'application/json';
    if (auth && session) headers.Authorization = session.id_token;
    let res;
    try {
      res = await fetch(cfg.apiUrl + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      throw { code: 'network', message: 'Could not reach the service. Check your connection and try again.' };
    }
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (res.status === 401 && auth) { signOut(); throw { code: 'unauthorized', message: 'Your session expired. Please sign in again.' }; }
    if (!res.ok) {
      const e = (data && data.error) || {};
      throw { status: res.status, code: e.code || 'error', message: e.message || `Request failed (${res.status})`, details: e.details, correlation_id: e.correlation_id || res.headers.get('X-Correlation-Id') };
    }
    return data;
  }

  // ---------- Auth ----------
  function showAuth(show) {
    $('#auth').classList.toggle('hidden', !show);
    $('#app').classList.toggle('hidden', show);
    $('#signout').classList.toggle('hidden', show);
    $('#user-chip').classList.toggle('hidden', show);
  }
  function setSession(s) {
    session = s;
    store.set('archrev.session', JSON.stringify(s));
    $('#user-chip').textContent = `${s.username} · ${s.role === 'governance' ? 'Governance Reviewer' : 'Solution Architect'}`;
    document.querySelectorAll('.gov-only').forEach(o => { o.disabled = s.role !== 'governance'; o.hidden = s.role !== 'governance'; });
    showAuth(false);
    loadHistory();
  }
  function signOut() {
    session = null; store.del('archrev.session'); stopPolling();
    showAuth(true);
  }
  function switchTab(which) {
    const login = which === 'login';
    $('#tab-login').classList.toggle('active', login); $('#tab-login').setAttribute('aria-selected', login);
    $('#tab-register').classList.toggle('active', !login); $('#tab-register').setAttribute('aria-selected', !login);
    $('#login-form').classList.toggle('hidden', !login); $('#register-form').classList.toggle('hidden', login);
  }
  async function doLogin(username, password, msgEl) {
    const r = await api('/auth/login', { method: 'POST', body: { username, password }, auth: false });
    setSession({ username: r.username, role: r.role, id_token: r.id_token, exp: Date.now() + (r.expires_in || 3600) * 1000 });
    msgEl.textContent = '';
  }
  function formMsg(form, text, kind) { const m = $('.form-msg', form); m.textContent = text; m.className = `form-msg ${kind || ''}`; }

  // ---------- Review form ----------
  const LIMITS = { title: [3, 120], description: [40, 4000], business_objective: [10, 1000] };
  function readForm() {
    const f = $('#review-form');
    const integ = f.external_integrations.value.split('\n').map(s => s.trim()).filter(Boolean);
    return {
      title: f.title.value.trim(), description: f.description.value.trim(), business_objective: f.business_objective.value.trim(),
      data_classification: f.data_classification.value, expected_users: Number(f.expected_users.value),
      availability_target: f.availability_target.value, external_integrations: integ,
    };
  }
  function clearErrors() {
    document.querySelectorAll('#review-form .err').forEach(e => { e.textContent = ''; });
    document.querySelectorAll('#review-form [aria-invalid]').forEach(e => e.removeAttribute('aria-invalid'));
  }
  function fieldError(name, msg) {
    const e = $(`#review-form .err[data-for="${name}"]`); if (e) e.textContent = msg;
    const i = $(`#review-form [name="${name}"]`); if (i) i.setAttribute('aria-invalid', 'true');
  }
  function validate(d) {
    const errs = {};
    for (const [k, [min, max]] of Object.entries(LIMITS)) {
      if (d[k].length < min) errs[k] = `Enter at least ${min} characters.`;
      else if (d[k].length > max) errs[k] = `Use at most ${max} characters.`;
    }
    if (!d.data_classification) errs.data_classification = 'Choose a classification.';
    if (!Number.isInteger(d.expected_users) || d.expected_users < 1 || d.expected_users > 10000000) errs.expected_users = 'Enter a whole number from 1 to 10,000,000.';
    if (!d.availability_target) errs.availability_target = 'Choose an availability target.';
    if (d.external_integrations.length > 10) errs.external_integrations = 'List at most 10 integrations.';
    if (d.external_integrations.some(s => s.length > 100)) errs.external_integrations = 'Each integration must be 100 characters or fewer.';
    return errs;
  }
  function fillForm(r) {
    const f = $('#review-form');
    for (const k of ['title', 'description', 'business_objective', 'data_classification', 'expected_users', 'availability_target']) f[k].value = r[k] ?? '';
    f.external_integrations.value = (r.external_integrations || []).join('\n');
    updateCounter(); clearErrors();
  }
  function updateCounter() { const d = $('#review-form').description.value.length; $('.counter[data-for="description"]').textContent = `${d} / 4000`; }

  async function submitReview(ev) {
    ev.preventDefault();
    clearErrors();
    const msg = $('#review-msg'); msg.textContent = ''; msg.className = 'form-msg';
    const data = readForm();
    const errs = validate(data);
    if (Object.keys(errs).length) { Object.entries(errs).forEach(([k, v]) => fieldError(k, v)); msg.textContent = 'Please correct the highlighted fields.'; msg.className = 'form-msg bad'; return; }
    const btn = $('#submit-btn'); btn.disabled = true;
    try {
      const r = await api('/reviews', { method: 'POST', body: data });
      showState('loading'); startElapsed(); markStep('queued');
      poll(r.review_id);
    } catch (e) {
      if (e.details) e.details.forEach(d => fieldError(d.field.split('.').pop(), d.message));
      msg.textContent = e.message; msg.className = 'form-msg bad';
      btn.disabled = false;
    }
  }

  // ---------- States ----------
  function showState(which) {
    for (const s of ['empty', 'loading', 'error']) $(`#state-${s}`).classList.toggle('hidden', s !== which);
    $('#result').classList.toggle('hidden', which !== 'result');
    $('#result-actions').classList.toggle('hidden', which !== 'result');
  }
  function markStep(step) {
    const order = ['queued', 'running', 'model']; const idx = order.indexOf(step);
    document.querySelectorAll('#steps li').forEach((li, i) => { li.className = i < idx ? 'done' : i === idx ? 'active' : ''; });
  }
  function startElapsed() {
    const t0 = Date.now(); clearInterval(elapsedTimer);
    elapsedTimer = setInterval(() => {
      const s = Math.round((Date.now() - t0) / 1000);
      $('#elapsed').textContent = `Elapsed ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
      if (s > 20) markStep('model');
    }, 1000);
  }
  function stopPolling() { clearTimeout(pollTimer); clearInterval(elapsedTimer); $('#submit-btn').disabled = false; }
  function showError(title, e) {
    stopPolling(); showState('error');
    const box = $('#state-error'); box.replaceChildren(el('b', { text: title }), el('p', { text: e.message || String(e) }));
    if (e.correlation_id) box.append(el('p', { class: 'hint', text: `Correlation ID: ${e.correlation_id}` }));
  }
  async function poll(id, started = Date.now()) {
    try {
      const r = await api(`/reviews/${encodeURIComponent(id)}`);
      if (r.status === 'queued' || r.status === 'running') {
        markStep(r.status === 'running' && Date.now() - started > 20000 ? 'model' : r.status);
        if (Date.now() - started > 6 * 60 * 1000) return showError('The review is taking longer than expected.', { message: 'Check the history list in a few minutes.', correlation_id: r.correlation_id });
        pollTimer = setTimeout(() => poll(id, started), 3000);
        return;
      }
      stopPolling();
      if (r.status === 'failed' || !r.result) return showError('The review could not be completed.', { message: r.error || 'Please try again.', correlation_id: r.correlation_id });
      renderResult(r);
      loadHistory();
    } catch (e) {
      if (e.code === 'network' && Date.now() - started < 6 * 60 * 1000) { pollTimer = setTimeout(() => poll(id, started), 5000); return; }
      showError('Could not load the review.', e);
    }
  }

  // ---------- Results ----------
  const VERDICT = { APPROVE_WITH_CONTROLS: ['Approve with controls', 'ok'], NEEDS_CHANGES: ['Needs changes', 'warn'], NOT_RECOMMENDED: ['Not recommended', 'bad'], INSUFFICIENT_INFORMATION: ['Insufficient information', 'info'] };
  const SEV = { low: 'info', medium: 'warn', high: 'bad', critical: 'bad' };
  const STATUS_KIND = { approved: 'ok', approved_with_constraints: 'warn', restricted: 'bad', retired: 'bad', not_approved: 'bad' };

  function renderResult(item) {
    current = item;
    const res = item.result;
    const root = $('#result'); root.replaceChildren();
    const [vText, vKind] = VERDICT[res.recommendation.verdict] || [res.recommendation.verdict, ''];
    root.append(el('div', { class: 'verdict' },
      badge(vText, vKind), badge(`Review ${res.status}`, res.status === 'complete' ? 'ok' : 'warn'),
      badge(`Risk level: ${res.derived_context.risk_level}`, SEV[res.derived_context.risk_level]),
      badge(res.derived_context.availability_tier, 'info'),
      el('span', { class: 'hint', text: `Servers used: ${res.servers_used.join(' + ') || 'none'}` })));
    (res.warnings || []).forEach(w => root.append(el('div', { class: 'notice warn', text: w })));
    (res.security_notices || []).forEach(n => root.append(el('div', { class: 'notice sec' },
      el('b', { text: 'Prompt-injection defense: ' }),
      n.source === 'user_input' ? `Instruction-like text found in the proposal (${n.patterns}). It was treated as data and not followed.`
        : `A retrieved record (${n.server}, ${n.path}) contained instruction-like text (${n.patterns}). That field was redacted before the agent saw it.`)));

    const sections = [
      ['Recommendation', () => [
        el('h3', { text: 'Executive summary' }), el('p', { text: res.executive_summary }),
        el('h3', { text: 'Recommendation (model judgment)' }), el('p', { text: res.recommendation.rationale }),
        el('h3', { text: 'How risk level was derived' }), el('ul', {}, res.derived_context.risk_reasons.map(r => el('li', { text: r }))),
      ]],
      ['Architecture', () => [
        el('p', { text: res.architecture.summary }),
        el('h3', { text: 'Approved services' }),
        list(res.architecture.components, c => [
          el('div', { class: 'row' }, el('b', { text: c.name }), badge(c.catalog_status, STATUS_KIND[c.catalog_status]), c.cost_band ? badge(c.cost_band) : null, cite(c.source)),
          el('p', { text: c.role }),
          c.constraints_to_apply && c.constraints_to_apply.length ? el('ul', {}, c.constraints_to_apply.map(x => el('li', { text: x }))) : null]),
        el('h3', { text: 'Reference patterns' }),
        list(res.architecture.reference_patterns, p => [el('div', { class: 'row' }, el('b', { text: p.name }), p.status ? badge(p.status, STATUS_KIND[p.status]) : null, cite(p.source)), el('p', { text: p.fit })]),
      ], res.architecture.components.length],
      ['Standards', () => [list(res.standards, s => [el('div', { class: 'row' }, el('b', { text: `${s.standard_id} · ${s.title}` }), s.category ? badge(s.category, 'info') : null, cite(s.source)), el('p', { text: s.relevance })])], res.standards.length],
      ['Risks', () => [list(res.risks, r => [el('div', { class: 'row' }, badge(r.severity, SEV[r.severity]), badge(r.category, 'info'),
        badge(r.basis === 'retrieved_fact' ? 'Retrieved fact' : 'Model recommendation', r.basis === 'retrieved_fact' ? 'ok' : ''), ...(r.sources || []).map(cite)), el('p', { text: r.description })])], res.risks.length],
      ['Controls', () => [list(res.controls, c => [el('div', { class: 'row' }, el('b', { text: `${c.control_id} · ${c.title}` }), c.required ? badge('Required', 'bad') : badge('Recommended'), c.standard_id ? el('span', { class: 'hint', text: `from ${c.standard_id}` }) : null, cite(c.source)),
        c.how_to_apply ? el('p', { text: c.how_to_apply }) : null])], res.controls.length],
      ['Questions', () => [
        res.conflicts.length ? el('h3', { text: 'Conflicting guidance' }) : null,
        res.conflicts.length ? list(res.conflicts, c => [el('div', { class: 'row' }, badge('Conflict', 'warn'), ...(c.sources || []).map(cite)), el('p', { text: c.description }), el('p', {}, el('b', { text: 'Suggested resolution: ' }), c.suggested_resolution)]) : null,
        el('h3', { text: 'Open questions' }),
        res.open_questions.length ? el('ol', {}, res.open_questions.map(q => el('li', { text: q }))) : el('p', { class: 'hint', text: 'No open questions.' }),
      ], res.open_questions.length + res.conflicts.length],
      ['Sources', () => [
        el('dl', { class: 'kv' },
          el('dt', { text: 'MCP servers used' }), el('dd', { text: res.servers_used.join(', ') }),
          el('dt', { text: 'Model' }), el('dd', { text: res.model_id || 'unavailable' }),
          el('dt', { text: 'Correlation ID' }), el('dd', { text: res.correlation_id }),
          el('dt', { text: 'Duration' }), el('dd', { text: `${Math.round(res.duration_ms / 1000)} s · ${res.sources.length} MCP calls · ${res.llm_turns} model turns` })),
        el('div', { class: 'table-wrap' }, el('table', {},
          el('thead', {}, el('tr', {}, ['MCP server', 'Tool', 'Result', 'Record IDs returned'].map(h => el('th', { text: h })))),
          el('tbody', {}, res.sources.map(s => el('tr', {}, el('td', { text: s.server }), el('td', { class: 'mono', text: s.tool }),
            el('td', {}, s.ok ? badge('ok', 'ok') : badge('failed', 'bad')), el('td', { class: 'mono', text: (s.record_ids || []).join(', ') || (s.error ? s.error : '—') }))))))],
      res.sources.length],
    ];
    const tabs = el('div', { class: 'rtabs', role: 'tablist' });
    const body = el('div');
    sections.forEach(([name, render, count], i) => {
      const b = el('button', { type: 'button', role: 'tab', class: i === 0 ? 'active' : '', onclick: () => {
        tabs.querySelectorAll('button').forEach(x => x.classList.remove('active')); b.classList.add('active');
        body.replaceChildren(...render().filter(Boolean));
      } }, name, count != null ? el('span', { class: 'count', text: `(${count})` }) : null);
      tabs.append(b);
    });
    body.replaceChildren(...sections[0][1]().filter(Boolean));
    root.append(tabs, body, el('p', { class: 'disclaimer', text: res.disclaimer }));
    showState('result');
  }
  function list(items, render) {
    if (!items || !items.length) return el('p', { class: 'hint', text: 'None.' });
    return el('ul', { class: 'items' }, items.map(it => el('li', {}, ...render(it).filter(Boolean))));
  }
  function download(name, text, type) {
    const a = el('a', { href: URL.createObjectURL(new Blob([text], { type })), download: name });
    document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  // ---------- History ----------
  async function loadHistory() {
    const ul = $('#hist-list');
    const scope = $('#hist-scope').value;
    try {
      const r = await api(`/reviews?scope=${scope}`);
      ul.replaceChildren();
      if (!r.items.length) { ul.append(el('li', { class: 'hint', text: 'No reviews yet.' })); return; }
      r.items.forEach(it => ul.append(el('li', { tabindex: '0', onclick: () => openReview(it.review_id), onkeydown: (e) => { if (e.key === 'Enter') openReview(it.review_id); } },
        el('div', { class: 't', text: it.title || it.review_id }),
        el('div', { class: 'm' }, badge(it.status, it.status === 'complete' ? 'ok' : it.status === 'failed' ? 'bad' : 'warn'), ' ',
          new Date(Number(it.created_at) * 1000).toLocaleString(), scope === 'all' ? ` · ${it.owner}` : ''))));
    } catch (e) {
      ul.replaceChildren(el('li', { class: 'hint', text: `Could not load history: ${e.message}` }));
    }
  }
  async function openReview(id) {
    stopPolling();
    try {
      const r = await api(`/reviews/${encodeURIComponent(id)}`);
      if (r.status === 'queued' || r.status === 'running') { showState('loading'); startElapsed(); poll(id); return; }
      if (r.request) fillForm(r.request);
      if (r.result) renderResult(r); else showError('This review did not complete.', { message: r.error || 'No result.', correlation_id: r.correlation_id });
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (e) { showError('Could not load the review.', e); }
  }

  // ---------- Init ----------
  async function init() {
    try {
      [cfg, examples] = await Promise.all([fetch('config.json').then(r => r.json()), fetch('examples.json').then(r => r.json())]);
    } catch {
      document.body.prepend(el('div', { class: 'notice warn', text: 'Configuration could not be loaded. Refresh the page.' }));
      return;
    }
    const sel = $('#example-select');
    examples.forEach((e, i) => sel.append(el('option', { value: String(i), text: e.label })));
    $('#load-example').addEventListener('click', () => fillForm(examples[Number(sel.value)].request));
    $('#clear-btn').addEventListener('click', () => { $('#review-form').reset(); updateCounter(); clearErrors(); });
    $('#review-form').addEventListener('submit', submitReview);
    $('#review-form').description.addEventListener('input', updateCounter);
    $('#tab-login').addEventListener('click', () => switchTab('login'));
    $('#tab-register').addEventListener('click', () => switchTab('register'));
    $('#signout').addEventListener('click', signOut);
    $('#hist-refresh').addEventListener('click', loadHistory);
    $('#hist-scope').addEventListener('change', loadHistory);
    $('#dl-md').addEventListener('click', () => current && download(`${current.review_id}.md`, current.result.narrative, 'text/markdown'));
    $('#dl-json').addEventListener('click', () => current && download(`${current.review_id}.json`, JSON.stringify(current.result, null, 2), 'application/json'));

    $('#login-form').addEventListener('submit', async (ev) => {
      ev.preventDefault(); const f = ev.target;
      if (!f.username.value.trim() || f.password.value.length < 6) return formMsg(f, 'Enter your user ID and a password of at least 6 characters.', 'bad');
      formMsg(f, 'Signing in...');
      try { await doLogin(f.username.value.trim(), f.password.value, $('.form-msg', f)); f.reset(); } catch (e) { formMsg(f, e.message, 'bad'); }
    });
    $('#register-form').addEventListener('submit', async (ev) => {
      ev.preventDefault(); const f = ev.target;
      const username = f.username.value.trim(), password = f.password.value, role = f.role.value;
      if (!/^[A-Za-z0-9_.-]{3,32}$/.test(username)) return formMsg(f, 'User ID must be 3-32 letters, numbers, dots, dashes, or underscores.', 'bad');
      if (password.length < 6) return formMsg(f, 'Password must be at least 6 characters.', 'bad');
      formMsg(f, 'Creating user...');
      try {
        await api('/auth/register', { method: 'POST', body: { username, password, role }, auth: false });
        await doLogin(username, password, $('.form-msg', f)); f.reset();
      } catch (e) { formMsg(f, e.details ? e.details.map(d => `${d.field}: ${d.message}`).join('; ') : e.message, 'bad'); }
    });

    updateCounter();
    const saved = store.get('archrev.session');
    let s = null; try { s = saved && JSON.parse(saved); } catch { s = null; }
    if (s && s.exp > Date.now() + 60000) setSession(s); else showAuth(true);
  }
  init();
})();
