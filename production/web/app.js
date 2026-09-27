/* Read-only production viewer. Only independent human decisions can mutate work. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const state = {session: null, csrf: null, project: null, artifact: null, projects: [], tab: 'results', filter: 'all', epoch: 0};
  const idPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
  const phases = {preparation: '准备', production: '制作', review: '看片', confirmed: '已确认'};
  const statuses = {failed: '失败', unknown: '结果未明', queued: '排队中', running: '处理中', submitted: '已提交', dispatching: '处理中', initializing: '准备中', succeeded: '已生成', completed: '已完成', pending: '待处理', declined: '未确认', superseded: '已替换'};
  const kinds = {script: '剧本', scene: '场景', shot: '分镜', asset: '资产', candidate: '待生成内容', expectation: '具体预期', brief: '创作说明', 'source-understanding': '原片理解', media: '素材', cut: '剪辑', final: '确认版本', 'review-receipt': '独立审查', 'decision-request': '待你确认', 'human-take-selection': '你的选条', feedback: '反馈', job: '生成任务'};
  function node(tag, text, className) { const n = document.createElement(tag); if (text != null) n.textContent = String(text); if (className) n.className = className; return n; }
  function show(id, visible) { $(id).hidden = !visible; }
  function notice(message = '') { $('notice').textContent = message; show('notice', Boolean(message)); }
  function segment(value) { if (!idPattern.test(value)) throw new Error('无效的项目或内容引用'); return encodeURIComponent(value); }
  function refPath(pid, ref) { return `/projects/${segment(pid)}/objects/${segment(ref.object_id)}?revision=${Number(ref.revision)}`; }
  function mediaPath(pid, ref) { return `/v1/projects/${segment(pid)}/media/${segment(ref.object_id)}?revision=${Number(ref.revision)}`; }
  function exact(ref) { return ref && idPattern.test(ref.object_id) && Number.isInteger(ref.revision) && ref.revision > 0 && /^[a-f0-9]{64}$/.test(ref.digest); }
  function base(pid = state.project?.project_id) { return `/v1/projects/${segment(pid)}`; }
  function label(item) {
    if (item.confirmed_final) return '已确认成片';
    if (item.imported_unverified) return '导入 · 尚未验证';
    if (item.stale || item.current === false) return '旧版 · 需要复核';
    if (item.kind === 'review-receipt') return {pass: '审查通过 · 非最终确认', fail: '审查未通过', uncertain: '审查不确定'}[item.verdict] || '未经独立验证';
    return statuses[item.status] || (['media', 'cut'].includes(item.kind) ? '待看片' : '草稿');
  }
  async function api(path, body) {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : {'Content-Type': 'application/json'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    const data = await response.json();
    if (!response.ok) { const error = new Error(`操作未完成（${data.code || response.status}）。${data.message || '请刷新后重试。'}`); error.status = response.status; throw error; }
    return data;
  }
  function fail(error) {
    notice(error.message || '读取失败，请稍后刷新。');
    if (error.status === 401) { state.session = null; state.csrf = null; show('login', true); show('projects-view', false); show('project-view', false); show('logout', false); }
  }
  async function guarded(work) { try { await work(); } catch (error) { fail(error); } }
  function navigate(url) { history.pushState(null, '', url); guarded(route); }
  function link(title, url) { const a = node('a', title); a.href = url; a.addEventListener('click', event => { if (event.button === 0 && !event.metaKey && !event.ctrlKey) { event.preventDefault(); navigate(url); } }); return a; }
  function resetMedia() {
    for (const video of $('decision-takes').querySelectorAll('video')) { video.pause(); video.removeAttribute('src'); video.load(); }
    $('decision-takes').replaceChildren();
    for (const name of ['video', 'audio', 'image']) { const n = $(name); if (name !== 'image') n.pause(); n.removeAttribute('src'); n.style.removeProperty('aspect-ratio'); n.hidden = true; if (name !== 'image') n.load(); }
    show('media-stage', false); show('media-error', false); show('download', false); $('playback-time').textContent = '';
  }
  function tab(name) {
    state.tab = name;
    document.querySelectorAll('[data-tab]').forEach(n => n.setAttribute('aria-pressed', String(n.dataset.tab === name)));
    for (const key of ['plan', 'results', 'files']) show(`${key}-panel`, key === name);
    if (name === 'files' && !$('file-tree').childElementCount) guarded(() => tree('', $('file-tree'), state.epoch));
  }
  function row(item, pid) {
    const button = node('button', null, 'artifact-row'); button.type = 'button';
    const title = node('div'); title.append(node('strong', item.display_name), node('span', `  ${kinds[item.kind] || item.kind}`));
    const badge = node('span', label(item), `badge ${item.confirmed_final ? 'confirmed' : item.stale || ['failed', 'unknown'].includes(item.status) ? 'warning' : ''}`);
    button.append(title, badge); button.addEventListener('click', () => navigate(refPath(pid, item.object_ref))); return button;
  }
  function list(items, id, empty, pid) {
    $(id).replaceChildren(); const old = [], execution = [];
    const internal = new Set(['review-task', 'review-run', 'review-turn', 'provider-receipt', 'composition-job', 'asset-composition', 'agent-report']);
    for (const item of items) {
      if (item.stale || item.current === false) old.push(item);
      else if (id === 'result-list' && internal.has(item.kind)) execution.push(item);
      else $(id).append(row(item, pid));
    }
    if (execution.length) { const details = node('details', null, 'execution-records'); details.append(node('summary', `执行记录（${execution.length}）`)); execution.forEach(item => details.append(row(item, pid))); $(id).append(details); }
    if (old.length) { const details = node('details', null, 'old-results'); details.append(node('summary', `旧版记录（${old.length}）`)); old.forEach(item => details.append(row(item, pid))); $(id).append(details); }
    show(empty, items.length === 0);
  }
  async function tree(parent, container, epoch) {
    const pid = state.project.project_id;
    const entries = await api(`${base(pid)}/tree?parent=${encodeURIComponent(parent)}`);
    if (epoch !== state.epoch) return;
    const ul = node('ul'); container.replaceChildren(ul); if (!parent) show('files-empty', entries.length === 0);
    for (const entry of entries) {
      const li = node('li'); ul.append(li);
      if (entry.type === 'directory') {
        const button = node('button', `▸ ${entry.name}`); button.type = 'button'; button.setAttribute('aria-expanded', 'false');
        const children = node('div'); children.hidden = true; li.append(button, children);
        button.addEventListener('click', () => guarded(async () => { const open = button.getAttribute('aria-expanded') !== 'true'; if (open && !children.childElementCount) await tree(entry.path, children, epoch); children.hidden = !open; button.setAttribute('aria-expanded', String(open)); button.textContent = `${open ? '▾' : '▸'} ${entry.name}`; }));
      } else if (exact(entry.object_ref)) { const button = node('button', entry.name); button.type = 'button'; button.addEventListener('click', () => navigate(refPath(pid, entry.object_ref))); li.append(button); }
    }
  }
  function paintProjects() {
    $('project-list').replaceChildren(); $('project-history-list').replaceChildren();
    const projects = state.projects.filter(p => state.filter === 'all' || (state.filter === 'complete' ? p.phase === 'confirmed' : p.phase !== 'confirmed'));
    $('projects-empty').textContent = state.filter === 'all' ? '还没有可查看的项目。在 coding 环境里告诉 Agent，你想拍什么。' : '这个筛选下还没有项目。';
    const isHistory = p => Array.isArray(p.superseded_by) && p.superseded_by.length > 0;
    const history = state.projects.filter(isHistory);
    show('projects-empty', !projects.some(p => !isHistory(p)));
    show('project-history', history.length > 0);
    $('project-history-title').textContent = `历史制作记录（${history.length}）`;
    show('project-history-empty', !projects.some(isHistory));
    for (const p of projects) {
      const a = link('', `/projects/${segment(p.project_id)}`); a.className = 'project-card';
      const cover = node('div', null, 'project-cover'); cover.append(node('span', p.title.slice(0, 1) || '片'));
      const caption = node('div', null, 'project-caption'); caption.append(node('h2', p.title), node('p', `${p.branch === 'recreation' ? '复刻拍片' : '原创拍片'} · ${phases[p.phase] || (p.phaseError ? '进展读取失败，点击查看项目重试' : '读取进展中')}`));
      const created = new Date(p.created_at);
      const date = Number.isFinite(created.getTime()) ? created.toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '';
      caption.append(node('p', `${isHistory(p) ? '历史制作' : '当前制作'} · ${date ? `${date} · ` : ''}${p.project_id.slice(-8)}`, 'meta'));
      a.append(cover, caption); $(isHistory(p) ? 'project-history-list' : 'project-list').append(a);
    }
  }
  async function projectPhases(values, epoch) {
    let index = 0;
    await Promise.all(Array.from({length: Math.min(2, values.length)}, async () => {
      while (index < values.length) {
        const p = values[index++]; if (p.phase || p.phaseLoading) continue;
        p.phaseLoading = true; p.phaseError = false;
        try { const summary = await api(base(p.project_id)); if (epoch !== state.epoch) return; p.phase = summary.phase; paintProjects(); }
        catch (error) { if (epoch === state.epoch) { p.phaseError = true; fail(error); } }
        finally { p.phaseLoading = false; if (epoch === state.epoch) paintProjects(); }
      }
    }));
  }
  async function projects(epoch) {
    state.project = null; state.artifact = null; resetMedia(); show('project-view', false); show('projects-view', true);
    $('project-list').replaceChildren(node('p', '读取项目中…', 'meta'));
    const values = await api('/v1/projects'); if (epoch !== state.epoch) return;
    state.projects = values.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    $('project-history').open = false; paintProjects();
    await projectPhases(values.filter(p => !p.superseded_by?.length), epoch);
  }
  async function project(pid, epoch) {
    show('projects-view', false); show('project-view', false); notice('读取项目中…');
    const data = await api(base(pid)); if (epoch !== state.epoch) return false;
    state.project = data; state.artifact = null; resetMedia(); $('project-title').textContent = data.title;
    $('project-branch').textContent = data.branch === 'recreation' ? '复刻拍片' : '原创拍片';
    $('phase-strip').replaceChildren(); for (const [key, text] of Object.entries(phases)) { const li = node('li', text); if (data.phase === key) li.setAttribute('aria-current', 'step'); $('phase-strip').append(li); }
    $('project-state').textContent = {'blocked': '有问题待处理', 'processing': '正在处理', 'empty': '等待准备内容', 'ready-to-view': '可以查看'}[data.state] || '状态待核实';
    $('blockers').replaceChildren();
    if (data.blockers.length) {
      const details = node('details'); details.append(node('summary', `${data.blockers.length} 条问题记录 · 展开查看`));
      details.append(node('p', '包含历史尝试中的失败，请结合当前结果判断。具体处理可复制引用交给 Agent。'));
      data.blockers.forEach(item => details.append(row(item, pid))); $('blockers').append(details);
    }
    show('blockers', data.blockers.length > 0); show('budget', false); show('artifact-view', false); show('decision-view', false);
    $('file-tree').replaceChildren(); list(data.plan, 'plan-list', 'plan-empty', pid); list(data.results, 'result-list', 'results-empty', pid);
    show('project-view', true); notice(); tab(state.tab); return true;
  }
  function prose(value, container, depth = 0) {
    if (value == null) return;
    if (typeof value !== 'object') { container.append(node('p', value)); return; }
    if (depth > 3) { container.append(node('pre', JSON.stringify(value, null, 2))); return; }
    for (const [key, item] of Object.entries(value)) { container.append(node('h3', key)); prose(item, container, depth + 1); }
  }
  async function references(item, epoch) {
    const d = item.details || {}; const refs = [...(d.request?.references || []).map(r => r.object_ref), ...(d.content?.media_refs || [])];
    for (const key of ['media', 'take', 'target', 'returned_take', 'failed_take', 'result', 'appeal_of']) if (exact(d[key])) refs.push(d[key]);
    if (d.evidence) for (const key of ['cut', 'media', 'finishing', 'receipts']) { const value = d.evidence[key]; if (Array.isArray(value)) refs.push(...value); else if (exact(value)) refs.push(value); }
    const seen = new Set();
    for (const ref of refs) {
      if (!exact(ref) || seen.has(`${ref.object_id}:${ref.revision}`)) continue; seen.add(`${ref.object_id}:${ref.revision}`);
      if (seen.size > 32) break;
      const pid = state.project.project_id;
      const wrapper = node('p'); wrapper.append(link(`关联内容 · ${ref.object_id} · v${ref.revision}`, refPath(pid, ref))); $('artifact-content').append(wrapper);
      try { const asset = await api(`${base(pid)}/artifacts/${segment(ref.object_id)}?revision=${ref.revision}`); if (epoch !== state.epoch || state.artifact !== item) return;
        if (asset.kind === 'media' && asset.details.media_type?.startsWith('image/')) { const image = node('img'); image.alt = asset.display_name; image.loading = 'lazy'; image.src = mediaPath(pid, ref); wrapper.prepend(image); }
      } catch (error) { if (epoch === state.epoch && state.artifact === item) wrapper.append(node('span', '（关联内容暂不可读）')); }
    }
  }
  function decision(item) {
    show('decision-view', item.kind === 'decision-request'); if (item.kind !== 'decision-request') return;
    const d = item.details; $('decision-title').textContent = d.purpose === 'take' ? '选一条' : d.purpose === 'envelope' ? '确认额度变更' : '确认这一个版本';
    const budget = d.evidence?.budget_before;
    $('decision-description').textContent = (d.rationale || '') + (d.purpose === 'envelope' && budget ? `\n预算账户：${d.evidence.budget_key || 'legacy'}。额度：${budget.ceiling} → ${d.evidence.proposed_limit} ${d.evidence.budget_unit}；已花费 ${budget.spent}，已预留 ${budget.reserved}。` : '');
    $('decision-evidence').textContent = JSON.stringify({target: d.target, evidence: d.evidence}, null, 2);
    const expired = !Number.isFinite(d.expires_at) || d.expires_at * 1000 <= Date.now();
    const allowed = state.session?.role === 'human' && Boolean(state.csrf) && d.human_confirmation_available === true && d.state === 'pending' && !expired && exact(d.target);
    $('decision-takes').replaceChildren(); show('decision-takes', d.purpose === 'take');
    if (d.purpose === 'take') for (const [index, take] of (d.evidence?.takes || []).entries()) {
      if (!exact(take)) continue;
      const row = node('div'); row.append(node('p', `第 ${index + 1} 条`));
      const stage = node('div', null, 'media-stage'), video = node('video');
      video.controls = true; video.preload = 'metadata'; video.playsInline = true; video.src = mediaPath(state.project.project_id, take);
      stage.append(video); row.append(stage);
      if (allowed) {
        const label = node('label'), radio = node('input'); radio.type = 'radio'; radio.name = 'decision-take'; radio.value = String(index);
        label.append(radio, document.createTextNode('选这条')); row.append(label);
      }
      $('decision-takes').append(row);
    }
    if (d.purpose === 'take' && allowed) {
      const field = node('div'), label = node('label', '理由（可选）'), reason = node('textarea');
      field.id = 'decision-reason-field'; label.htmlFor = 'decision-reason'; reason.id = 'decision-reason';
      reason.placeholder = '说一句为什么'; reason.maxLength = 2000; reason.rows = 3;
      field.append(label, reason); $('decision-takes').append(field);
    }
    show('decision-actions', allowed);
    $('decision-limit').textContent = expired ? '这个请求已过期，请回到 Agent 请求新的确认。' : allowed ? '仅确认这里展示的版本；后续修改需要新的确认。' : '查看不代表确认。需独立的人类确认会话；刷新页面后请重新登录。';
  }
  async function artifact(oid, revision, seconds, epoch) {
    const pid = state.project.project_id;
    const [item, versions] = await Promise.all([api(`${base(pid)}/artifacts/${segment(oid)}${revision ? `?revision=${revision}` : ''}`), api(`${base(pid)}/artifacts/${segment(oid)}/history`)]);
    if (epoch !== state.epoch) return;
    state.artifact = item; resetMedia(); show('copy-fallback', false); $('artifact-content').replaceChildren(); $('artifact-details').open = false;
    $('artifact-title').textContent = item.display_name; $('artifact-status').textContent = `${kinds[item.kind] || item.kind} · ${label(item)}`;
    $('artifact-json').textContent = JSON.stringify(item.details, null, 2); $('versions').replaceChildren();
    for (const version of versions) { const opt = node('option', `v${version.object_ref.revision}`); opt.value = String(version.object_ref.revision); $('versions').append(opt); }
    $('versions').value = String(item.object_ref.revision);
    const d = item.details || {}; if (d.content != null) prose(d.content, $('artifact-content'));
    if (d.intent) prose(d.intent, $('artifact-content'));
    if (d.request?.params?.prompt || d.assembled_prompt) { $('artifact-content').append(node('h3', '实际提示词'), node('pre', d.request?.params?.prompt || d.assembled_prompt)); }
    if (item.kind === 'media') {
      const type = d.media_type?.split('/')[0]; const media = {video: $('video'), audio: $('audio'), image: $('image')}[type];
      if (media) {
        const {width, height} = d.probe || {};
        if (type === 'image' && Number.isSafeInteger(width) && width > 0 && Number.isSafeInteger(height) && height > 0) media.style.aspectRatio = `${width} / ${height}`;
        media.src = mediaPath(pid, item.object_ref); media.hidden = false; show('media-stage', true);
        if (type !== 'image') { media.onloadedmetadata = () => { if (state.artifact === item && Number.isFinite(seconds) && seconds >= 0 && seconds <= media.duration) media.currentTime = seconds; }; media.load(); }
        $('download').href = media.src; show('download', true);
      }
    }
    const url = refPath(pid, item.object_ref) + (Number.isFinite(seconds) ? `&seconds=${seconds}` : ''); history.replaceState(null, '', url);
    show('artifact-view', true); decision(item);
    $('artifact-view').scrollIntoView({block: 'start', behavior: 'instant'});
    await references(item, epoch);
  }
  async function route() {
    const epoch = ++state.epoch; state.artifact = null; resetMedia(); notice();
    if (!state.session) { show('login', true); return; }
    show('login', false);
    const match = location.pathname.match(/^\/projects(?:\/([A-Za-z0-9_-]+)(?:\/objects\/([A-Za-z0-9_-]+))?)?\/?$/);
    if (!match) { show('project-view', false); show('projects-view', false); notice('找不到这个页面。请从项目列表重新选择。'); return; }
    if (!match[1]) return projects(epoch);
    if (await project(match[1], epoch) && match[2]) {
      const query = new URLSearchParams(location.search); const revision = query.has('revision') ? Number(query.get('revision')) : null; const seconds = query.has('seconds') ? Number(query.get('seconds')) : null;
      if (revision !== null && (!Number.isInteger(revision) || revision < 1) || seconds !== null && (!Number.isFinite(seconds) || seconds < 0)) throw new Error('版本或播放时间无效。');
      await artifact(match[2], revision, seconds, epoch);
    }
  }
  $('employee-login').addEventListener('click', () => guarded(async () => {
    $('employee-login').disabled = true;
    try { const response = await api('/v1/session/access', {}); state.csrf = response.csrf_token; state.session = await api('/v1/session'); $('session-label').textContent = '你的会话'; show('logout', true); await route(); }
    finally { $('employee-login').disabled = false; }
  }));
  $('login-form').addEventListener('submit', event => { event.preventDefault(); guarded(async () => {
    const secret = $('session-secret').value; $('session-secret').value = ''; const button = event.submitter; if (button) button.disabled = true;
    try { const response = await api('/v1/session/exchange', {kind: $('session-kind').value, secret}); state.csrf = response.csrf_token || null; state.session = await api('/v1/session'); $('session-label').textContent = state.session.role === 'human' ? '独立确认会话' : '查看会话'; show('logout', true); await route(); } finally { if (button) button.disabled = false; }
  }); });
  $('logout').addEventListener('click', () => guarded(async () => { await api('/v1/session/logout', {}); state.session = null; state.csrf = null; state.artifact = null; ++state.epoch; resetMedia(); show('project-view', false); show('projects-view', false); show('login', true); show('logout', false); $('session-label').textContent = ''; notice(); }));
  $('refresh').addEventListener('click', () => guarded(route));
  $('project-history').addEventListener('toggle', () => {
    if ($('project-history').open) guarded(() => projectPhases(state.projects.filter(p => p.superseded_by?.length), state.epoch));
  });
  document.querySelectorAll('[data-tab]').forEach(n => n.addEventListener('click', () => tab(n.dataset.tab)));
  document.querySelectorAll('[data-filter]').forEach(n => n.addEventListener('click', () => { state.filter = n.dataset.filter; document.querySelectorAll('[data-filter]').forEach(button => button.setAttribute('aria-pressed', String(button === n))); paintProjects(); }));
  $('versions').addEventListener('change', () => { if (state.artifact) navigate(refPath(state.project.project_id, {...state.artifact.object_ref, revision: Number($('versions').value)})); });
  for (const name of ['video', 'audio']) { $(name).addEventListener('timeupdate', () => { $('playback-time').textContent = `${$(name).currentTime.toFixed(2)} 秒`; }); $(name).addEventListener('error', () => { if ($(name).getAttribute('src')) show('media-error', true); }); }
  $('image').addEventListener('error', () => { if ($('image').getAttribute('src')) show('media-error', true); });
  $('copy-reference').addEventListener('click', () => guarded(async () => {
    const item = state.artifact; if (!item) return; const player = !$('video').hidden ? $('video') : !$('audio').hidden ? $('audio') : null;
    const seconds = player && Number.isFinite(player.duration) ? player.currentTime : null;
    const payload = await api(`${base()}/reference`, {target: item.object_ref, seconds});
    if (state.artifact !== item) return;
    const url = new URL(refPath(payload.project_id, payload.object_ref), location.origin); if (seconds !== null) url.searchParams.set('seconds', String(seconds));
    const text = `MVGP 讨论引用\n${JSON.stringify({...payload, link: url.href}, null, 2)}\n我的反馈：`;
    $('copy-text').value = text;
    try { await navigator.clipboard.writeText(text); notice('引用已复制。粘贴给 Agent，再写下你的想法。'); } catch (_) { show('copy-fallback', true); $('copy-text').focus(); $('copy-text').select(); notice('请复制下面已选中的引用。'); }
  }));
  for (const choice of ['confirm', 'decline']) $(`${choice}-decision`).addEventListener('click', () => guarded(async () => {
    const item = state.artifact, d = item?.details;
    if (state.session?.role !== 'human' || !state.csrf || item?.kind !== 'decision-request' || !exact(d.target)) throw new Error('需要独立的人类确认会话。');
    const payload = {idempotency_key: crypto.randomUUID(), request_id: item.object_ref.object_id, target_hash: d.target.digest, choice, csrf_token: state.csrf};
    if (d.purpose === 'take') {
      payload.reason = $('decision-takes').querySelector('#decision-reason')?.value.trim() || null;
      if (choice === 'confirm') {
        const selected = $('decision-takes').querySelector('input[name="decision-take"]:checked');
        const take = selected && d.evidence?.takes?.[Number(selected.value)];
        if (!exact(take)) throw new Error('请先选一条。');
        payload.selected_take = take;
      }
    }
    $('confirm-decision').disabled = true; $('decline-decision').disabled = true;
    try { await api(`${base()}/human-decisions`, payload); await route(); notice(choice === 'confirm' ? '已记录你的确认。' : '已记录不确认。'); } finally { $('confirm-decision').disabled = false; $('decline-decision').disabled = false; }
  }));
  window.addEventListener('popstate', () => guarded(route));
  guarded(async () => {
    try { state.session = await api('/v1/session'); }
    catch (error) { if (error.status !== 401) throw error; show('login', true); return; }
    $('session-label').textContent = state.session.role === 'human' ? '独立确认会话' : '查看会话'; show('logout', true); await route();
  });
})();
