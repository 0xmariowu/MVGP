/* Review desk (看片台): the Claude Design "看片 v6 Light" layout on real platform data.
 * Picks, "都不行" and "再拍一批" are recorded through the independent human-decision endpoint (same
 * request as the viewer's confirm/decline). Notes are platform records the Agent reads;
 * every batch of a shot stays on the desk and pickable; the session renews before media. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
  const S = {openBatches: new Set(), session: null, csrf: null, projects: [], pid: null, filter: 'todo', data: {}, cut: false, pv: false, pvHandle: null, light: null,
             note: null, sticky: new Set(), log: {}, hover: null, durations: {}, cutIndex: 0, swap: {}, swapShot: null, trying: false};

  function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = String(text); return n; }
  function seg(v) { if (!ID.test(v)) throw new Error('无效的引用'); return encodeURIComponent(v); }
  function exact(r) { return r && ID.test(r.object_id) && Number.isInteger(r.revision) && r.revision > 0 && /^[a-f0-9]{64}$/.test(r.digest); }
  function mediaUrl(pid, r) { return `/v1/projects/${seg(pid)}/media/${seg(r.object_id)}?revision=${Number(r.revision)}`; }
  function refKey(r) { return `${r.object_id}:${r.revision}`; }
  function fmt(v) { v = Number.isFinite(v) ? Math.max(0, v) : 0; return `${Math.floor(v / 60)}:${(v % 60).toFixed(1).padStart(4, '0')}`; }
  async function api(path, body) {
    const send = async b => {
      const res = await fetch(path, {method: b === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: b === undefined ? {} : {'Content-Type': 'application/json'}, ...(b === undefined ? {} : {body: JSON.stringify(b)})});
      return [res, await res.json().catch(() => ({}))];
    };
    let [res, data] = await send(body);
    // A lapsed session or a stale token is refused before anything runs, so renewing silently and
    // resending this one request (same idempotency key, fresh token) cannot record anything twice.
    const refused = res.status === 401 || (res.status === 403 && /CSRF/.test(data.message || ''));
    if (refused && !path.startsWith('/v1/session') && await silentAccess()) {
      [res, data] = await send(body && typeof body === 'object' && 'csrf_token' in body ? {...body, csrf_token: csrf()} : body);
    }
    if (!res.ok) { const e = new Error(data.message || `操作没完成（${res.status}）`); e.status = res.status; e.code = data.code; throw e; }
    return data;
  }
  let toastTimer = null;
  function say(msg, action) {
    clearTimeout(toastTimer); $('toast-text').textContent = msg; $('toast').hidden = false;
    const b = $('toast-action'); b.hidden = !action; if (action) { b.textContent = action.label; b.onclick = () => { action.fn(); $('toast').hidden = true; }; }
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, action ? 4000 : 2200);
  }
  // ---------- session ----------
  // The confirmation token is shared by this site's tabs, so a login in one tab never strands another.
  const CSRF_KEY = 'mvgp-review-csrf';
  function keepCsrf(v) { S.csrf = v || null; try { if (S.csrf) localStorage.setItem(CSRF_KEY, S.csrf); else localStorage.removeItem(CSRF_KEY); } catch (_) { /* private mode */ } }
  function csrf() { try { return localStorage.getItem(CSRF_KEY) || S.csrf; } catch (_) { return S.csrf; } }
  // Cloudflare Access already vouches for the owner on every request; exchange it without a click.
  async function silentAccess() {
    try {
      const r = await api('/v1/session/access', {}); if (!r.csrf_token) return false;
      keepCsrf(r.csrf_token); S.session = await api('/v1/session'); return true;
    } catch (_) { return false; }
  }
  // video URLs need the session cookie and nothing renews it on a media load, so renew
  // when less than five minutes are left: before a lightbox or film video loads, on focus, and every minute.
  async function fresh() {
    const exp = S.session?.expires_at;
    if (typeof exp === 'number' && exp - Date.now() / 1000 < 300) await silentAccess();
  }
  function retryOnce(v) {
    v.addEventListener('error', async () => {
      if (v.dataset.retried || !v.getAttribute('src')) return; v.dataset.retried = '1';
      if (await silentAccess()) { const src = v.getAttribute('src'); v.removeAttribute('src'); v.load(); v.src = src; }
    });
  }
  setInterval(() => { if (!document.hidden && S.session) fresh(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && S.session) fresh(); });
  window.addEventListener('focus', () => { if (S.session) fresh(); });
  const stale = e => e.status === 401 || (e.status === 403 && /CSRF/.test(e.message || ''));
  async function guarded(work) {
    // Renewal happens per request inside api(); a task is never run twice.
    try { await work(); } catch (e) {
      if (stale(e)) { keepCsrf(null); showLogin(); }
      say(e.code === 'locked' ? '这个镜头已经定稿了，要改请让 Agent 走重开' : e.message || '出错了，请刷新');
    }
  }

  // ---------- notes (platform records the Agent reads) ----------
  const notesKey = pid => `mvgp-review-notes:${pid}`;  // where notes lived before; uploaded once, then removed
  function notes(pid = S.pid) { return S.data[pid]?.notes || []; }
  async function addNote({target, take = null, at = null, text}) {
    if (!exact(target)) return say('这个镜头还没有片子，没法记');
    await api(`/v1/projects/${seg(S.pid)}/owner-notes`, {idempotency_key: crypto.randomUUID(), target, take: exact(take) ? take : null,
      at_seconds: at == null ? null : Number(at), text, csrf_token: csrf()});
  }
  async function dropNote(n) {
    await api(`/v1/projects/${seg(S.pid)}/owner-notes/withdraw`, {idempotency_key: crypto.randomUUID(),
      note_id: n.id, expected_revision: n.revision, csrf_token: csrf()});
  }
  async function uploadOldNotes(pid) {
    let old = []; try { old = JSON.parse(localStorage.getItem(notesKey(pid)) || '[]'); } catch (_) { return; }
    if (!Array.isArray(old) || !old.length) return;
    const shots = S.data[pid]?.shots || [];
    for (const n of old) {
      const x = shots.find(y => y.label === n.shot); const target = x?.request?.details?.target || S.data[pid]?.final?.details?.target;
      if (!exact(target) || typeof n.text !== 'string' || !n.text.trim()) continue;
      const where = n.take ? `第${n.take}条${n.at ? ` @${n.at}s` : ''}：` : '';
      await api(`/v1/projects/${seg(pid)}/owner-notes`, {idempotency_key: `old-note-${pid.slice(-8)}-${n.id}`, target,
        text: `以前写的·${where}${n.text}`.slice(0, 2000), csrf_token: csrf()});
    }
    try { localStorage.removeItem(notesKey(pid)); } catch (_) { /* private mode */ }
  }

  // ---------- data ----------
  function shotLabel(content, fallback) { return typeof content?.shot === 'string' ? content.shot : fallback; }
  function shotLines(content) { const d = content?.Audio?.delivery; return Array.isArray(d) ? d.map(x => x && x.line).filter(x => typeof x === 'string') : []; }
  // Design v6: one plain line under the shot name — the card's goal of the shot.
  function shotIntent(content) {
    const goal = content?.Direction?.['the goal of the shot in one line'];
    return typeof goal === 'string' ? goal.trim() : '';
  }
  function when(iso) {
    const t = iso ? new Date(iso) : null; if (!t || Number.isNaN(t.getTime())) return '';
    const hm = t.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
    const today = new Date(); const same = t.toDateString() === today.toDateString();
    return same ? `今天 ${hm}` : `${t.getMonth() + 1}月${t.getDate()}日 ${hm}`;
  }
  async function loadProject(pid) {
    // One read: requests, picks resolved by creation order, shot cards and takes being made.
    const feed = await api(`/v1/projects/${seg(pid)}/review-feed`);
    const content = r => feed.shots?.[`${r.object_id}:${r.revision}`]?.details?.content ?? null;
    const now = Date.now() / 1000; const byShot = new Map(); let final = null; let confirmed = null;
    for (const r of feed.requests || []) {
      const d = r.details || {};
      if (d.purpose === 'shot-plan') continue;  // historical only: no shot-plan step
      if (d.purpose === 'final') {
        if (d.state === 'pending' && d.expires_at > now && exact(d.target) && (!final || d.expires_at > final.details.expires_at)) final = r;
        if (d.state === 'confirmed' && exact(d.target) && (!confirmed || (r.created_at || '') > (confirmed.created_at || ''))) confirmed = r;
        continue;
      }
      if (d.purpose !== 'take' || !exact(d.target)) continue;
      // every batch of a shot, newest first; a take request never expires.
      byShot.set(d.target.object_id, [...(byShot.get(d.target.object_id) || []), r]);
    }
    const same = (a, b) => exact(a) && exact(b) && a.object_id === b.object_id && a.revision === b.revision;
    const shots = [...byShot.values()].map(list => {
      list.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
      const [r, ...older] = list; const d = r.details; const takes = (d.evidence?.takes || []).filter(exact); const c = content(d.target);
      const pick = feed.picks?.[d.target.object_id]?.details?.take;
      const at = exact(pick) ? takes.findIndex(t => same(t, pick)) : -1;
      const batches = older.map(o => {
        const bt = (o.details.evidence?.takes || []).filter(exact);
        return {request: o, takes: bt, state: o.details.state, at: o.created_at || null, picked: exact(pick) ? bt.findIndex(t => same(t, pick)) : -1};
      });
      const source = feed.sources?.[refKey(d.target)];  // the source shot (recreation)
      return {key: d.target.object_id, request: r, takes, batches, current: r.current !== false,
              source: source && exact(source.media) ? source : null,
              label: shotLabel(c, d.target.object_id.slice(-8)), lines: shotLines(c),
              intent: shotIntent(c), at: r.created_at || null,
              state: d.state, picked: at >= 0 ? at : d.state === 'confirmed' ? -1 : null,
              // A pick from an earlier batch of this shot still stands in the film (dry run); play it in the strip.
              earlier: at < 0 && exact(pick) ? pick : null};
    });
    // Takes still being made (design v6: "生成中 done/total" with shimmering placeholders).
    const making = s => ['queued', 'dispatching', 'submitted', 'running'].includes(s);
    const live = new Map();
    for (const m of feed.making || []) {
      for (const k of (m.batch.details?.children || []).filter(k => exact(k.target) && k.job)) {
        const row = live.get(k.target.object_id) || {target: k.target, total: 0, pending: 0, ready: []};
        const st = m.jobs?.[k.job.object_id]; row.total += 1;
        if (making(st)) row.pending += 1; else if (st === 'succeeded' && exact(m.ready?.[k.job.object_id])) row.ready.push(m.ready[k.job.object_id]);
        live.set(k.target.object_id, row);
      }
    }
    for (const [key, row] of live) {
      const progress = {done: row.total - row.pending, total: row.total, pending: row.pending, ready: row.ready};
      const x = shots.find(y => y.key === key);
      if (x) { x.making = progress; continue; }
      const c = content(row.target);
      shots.push({key, request: null, takes: [], batches: [], current: true, label: shotLabel(c, key.slice(-8)), lines: shotLines(c),
                  intent: shotIntent(c), state: 'making', picked: null, making: progress});
    }
    // Shoot orders: waiting for the plan, being listened to, or stopped with a reason.
    for (const o of feed.shooting || []) {
      if (!exact(o.card)) continue;
      let x = shots.find(y => y.key === o.card.object_id);
      if (x && x.request && x.current) continue;
      if (!x) { const c = content(o.card);
        x = {key: o.card.object_id, request: null, takes: [], batches: [], current: true, label: shotLabel(c, o.shot || o.card.object_id.slice(-8)),
             lines: shotLines(c), intent: shotIntent(c), state: 'making', picked: null}; shots.push(x); }
      if (o.stage !== 'firing' || !x.making) x.shooting = {stage: o.stage, reason: o.reason || '', owner_reason: o.owner_reason || ''};
    }
    shots.sort((a, b) => a.label.localeCompare(b.label));
    const noteRows = (feed.notes || []).filter(n => exact(n.details?.target) && typeof n.details?.text === 'string').map(n => ({
      id: n.object_ref.object_id, revision: n.object_ref.revision, target: n.details.target, take: exact(n.details.take) ? n.details.take : null,
      at: typeof n.details.at_seconds === 'number' ? n.details.at_seconds.toFixed(1) : null, text: n.details.text,
      when: n.created_at ? new Date(n.created_at).toLocaleString('zh-CN', {hour12: false}) : ''}));
    // each fal 样片 (480p draft) and its 1080p 正片, by draft take id.
    const completions = {};
    for (const [id, c] of Object.entries(feed.completions || {})) {
      if (ID.test(id) && c && ['draft', 'making', 'ready', 'failed', 'expired', 'stopped'].includes(c.state))
        completions[id] = {state: c.state, take: c.state === 'ready' && exact(c.take) ? c.take : null,
                           expires: Number.isInteger(c.expires_at) ? c.expires_at : null,
                           note: typeof c.note === 'string' ? c.note : null};
    }
    S.data[pid] = {shots, final, confirmed, notes: noteRows, completions};
    return S.data[pid];
  }
  function completion(t, pid = S.pid) { return exact(t) ? S.data[pid]?.completions?.[t.object_id] || null : null; }
  // The 正片 plays wherever a picked take plays once it is ready; until then the 样片 does.
  function full(t) { const c = completion(t); return c && c.take ? c.take : t; }
  const RES = {draft: '样片', making: '正片生成中', ready: '正片', failed: '正片没做成', expired: '样片 · 已过期', stopped: '样片'};
  function status(x) {
    if (x.shooting?.stage === 'firing' && !x.making) return {text: '收片中', cls: 'wait', making: true};
    if (x.shooting?.stage === 'stopped') return {text: `停下了：${x.shooting.owner_reason || x.shooting.reason}`, cls: 'no', making: true};
    if (x.making) return {text: `生成中 ${x.making.done}/${x.making.total}`, cls: 'wait', making: true};
    if (x.state === 'pending' && x.current) return {text: '待审', cls: '', todo: true};
    if (x.state === 'pending') return {text: '卡改过了 · 等新一批', cls: 'wait'};
    if (x.state === 'confirmed' && x.picked >= 0) {
      const c = completion(x.takes[x.picked]);
      // A failed or expired 1080p leaves the 480p 样片 in the film: say so (bug hunt 2026-09-25).
      // the platform's own reason (预算不够，正片没做 / 正片失败，已重试 / 样片已过期).
      const more = !c ? '' : c.state === 'draft' || c.state === 'making' ? ' · 正片生成中'
        : ['failed', 'expired', 'stopped'].includes(c.state) ? ` · ${c.note || RES[c.state]}，成片先用样片` : '';
      return {text: `定了第 ${x.picked + 1} 条${more}`, cls: more.includes('生成中') ? 'wait' : more ? 'no' : 'ok'};
    }
    if (x.state === 'confirmed') return {text: '已选', cls: 'ok'};
    if (x.state === 'declined') return {text: '都不行', cls: 'no'};
    return {text: '已选', cls: 'ok'};
  }
  // Any offered take of any batch can be picked, and a pick withdrawn.
  const canPick = x => Boolean(x.request);
  const todoCount = pid => (S.data[pid]?.shots || []).filter(x => status(x).todo).length;
  const projectName = p => { const [, rest = p.title] = p.title.split('—'); const clean = rest.replace(/\((original|recreation)\)/, '').trim(); return `${clean} · ${p.branch === 'recreation' ? '复刻' : '原创'}`; };

  // ---------- decisions (platform) ----------
  async function decide(x, choice, {take = null, reason = null, request = x.request, takes = x?.takes} = {}) {
    if (!csrf()) { showLogin(); throw new Error('请先进入看片台'); }
    const d = request.details;
    const payload = {idempotency_key: crypto.randomUUID(), request_id: request.object_ref.object_id, target_hash: d.target.digest, choice, csrf_token: csrf()};
    if (d.purpose === 'take') { payload.reason = reason || null; if (choice === 'confirm') payload.selected_take = takes[take]; }
    await api(`/v1/projects/${seg(S.pid)}/human-decisions`, payload);
  }
  function logLine(text) { (S.log[S.pid] ||= []).push(text); }
  async function pick(x, i, batch = null) {
    if (!canPick(x)) return say('这个镜头现在不能选');
    const b = batch || {request: x.request, takes: x.takes, picked: x.picked, state: x.state};
    const name = batch ? `之前一批的第 ${i + 1} 条` : `第 ${i + 1} 条`;
    if (b.picked === i && b.state === 'confirmed') {
      await decide(x, 'decline', {reason: '取消', request: b.request, takes: b.takes});
      logLine(`${x.label} 取消${name}。`); S.sticky.add(x.key);
      await refresh(); return say(`${x.label} 取消了${name}`);
    }
    await decide(x, 'confirm', {take: i, reason: lastNote(b.takes[i]), request: b.request, takes: b.takes});
    logLine(`${x.label} 用${name}。`); S.sticky.add(x.key);
    await refresh(); say(`${x.label} 用${name}，已记到平台`);
  }
  const sameRef = (a, b) => exact(a) && exact(b) && a.object_id === b.object_id && a.revision === b.revision;
  function lastNote(take) { const n = notes().filter(n => sameRef(n.take, take)).pop(); return n ? n.text : null; }
  // 撤销 takes back 再拍一批 / 都不行 while the card is unchanged.
  async function undo(x) {
    await decide(x, 'undo'); logLine(`${x.label} 撤销了再拍一批/都不行。`); S.sticky.add(x.key);
    await refresh(); say(`${x.label} 撤销了，这一批又可以选了`);
  }
  async function decline(x, kind, text) {
    if (!status(x).todo) return say('这个镜头已经处理过了');
    const reason = kind === 'rebatch' ? `再拍一批：${text || '（没写原因）'}` : `都不行：${text}`;
    await decide(x, 'decline', {reason});
    const rebatch = text ? `再拍一批，交给 AI 改：${text}` : '原样再拍 4 条';
    logLine(kind === 'rebatch' ? `${x.label} ${rebatch}` : `${x.label} 都不行，重拍。${text}`); S.sticky.add(x.key);
    await refresh(); say(kind === 'rebatch' ? `已记下：${x.label} ${text ? '再拍一批，交给 AI 改' : '原样再拍 4 条'}` : `已记下：${x.label} 都不行`);
  }

  // ---------- rendering ----------
  function renderProjects() {
    const box = $('projects'); box.replaceChildren();
    for (const p of S.projects) {
      const b = el('button', `project${p.project_id === S.pid ? ' on' : ''}`); b.type = 'button';
      b.append(el('span', 't', projectName(p)));
      // where each project is (HF's four stages), so many projects stay findable.
      if (p.stage) { const chip = el('span', 'stage', p.stage); chip.style.cssText = 'margin-left:6px;font-size:11px;color:#71717a'; b.append(chip); }
      const n = todoCount(p.project_id); if (n) b.append(el('span', 'badge-n', n));
      b.onclick = () => guarded(() => openProject(p.project_id)); box.append(b);
    }
  }
  function renderBar() {
    const p = S.projects.find(p => p.project_id === S.pid); $('proj-title').textContent = p ? projectName(p) : '';
    // 项目页 opens in the main area; the project bar stays.
    let page = $('proj-page');
    if (!page) { page = el('button', 'proj-page'); page.type = 'button'; page.id = 'proj-page'; page.style.cssText = 'margin-left:10px;font-size:13px;color:#71717a;background:none;border:0;cursor:pointer;padding:0;white-space:nowrap;flex:none'; page.onclick = () => setProjectView(!S.pv); $('proj-title').after(page); }
    page.hidden = !p; page.textContent = S.pv ? '回到看片' : '项目页';
    const n = todoCount(S.pid);
    document.querySelectorAll('[data-filter]').forEach(b => { b.classList.toggle('on', b.dataset.filter === S.filter); const c = b.querySelector('.count'); if (c) { c.hidden = !n; c.textContent = n; } });
    const count = (S.log[S.pid] || []).length;
    const copy = $('copy-all'); copy.textContent = count ? `复制 ${count} 条给 Agent` : '复制给 Agent'; copy.classList.toggle('dark', Boolean(count)); copy.classList.toggle('muted', !count);
    $('cut-label').textContent = S.cut ? '回到看片' : '看成片'; $('toggle-cut').classList.toggle('dark', S.cut);
  }
  function video(pid, ref, {muted = true} = {}) {
    const v = el('video'); v.muted = muted; v.playsInline = true; v.preload = 'metadata'; v.loop = true; v.src = `${mediaUrl(pid, ref)}#t=0.1`;
    v.addEventListener('loadedmetadata', () => { if (Number.isFinite(v.duration)) S.durations[refKey(ref)] = v.duration; });
    retryOnce(v); return v;
  }
  function noteBox(tagText, placeholder, onSave, onCancel) {
    const box = el('div', 'note-box'); const tag = el('span', 'note-tag', tagText); const input = el('input'); input.placeholder = placeholder;
    const cancel = el('button', 'note-cancel', '取消'); cancel.type = 'button'; const save = el('button', 'note-save', '记下'); save.type = 'button';
    const go = () => guarded(() => onSave(input.value.trim()));
    input.onkeydown = e => { if (e.key === 'Enter') go(); if (e.key === 'Escape') onCancel(); };
    save.onclick = go; cancel.onclick = onCancel; box.append(tag, input, cancel, save); setTimeout(() => input.focus(), 0); return box;
  }
  function renderFeed() {
    const inner = $('feed-inner'); inner.replaceChildren();
    const data = S.data[S.pid]; if (!data) { for (let i = 0; i < 3; i++) inner.append(skeleton()); return; }
    const list = data.shots.filter(x => S.filter === 'all' || status(x).todo || status(x).making || S.sticky.has(x.key));
    if (!list.length) {
      const e = el('div', 'empty'); e.append(el('span', 'check', '✓'), el('strong', null, '都审完了'), el('span', null, 'Agent 拍出新的会出现在这里。'));
      const go = el('button', 'act', '连起来看成片'); go.type = 'button'; go.style.marginTop = '8px'; go.onclick = () => setCut(true); e.append(go); inner.append(e); return;
    }
    for (const x of list) inner.append(card(x));
  }
  function skeleton() { const s = el('div', 'skeleton'); const a = el('div', 'shim'); a.style.cssText = 'height:14px;width:220px'; const g = el('div', 'takes'); for (let i = 0; i < 4; i++) { const t = el('div', 'shim'); t.style.aspectRatio = '16/9'; t.style.borderRadius = '10px'; g.append(t); } s.append(a, g); return s; }
  function card(x) {
    const st = status(x); const c = el('section', `card${st.todo ? '' : ' done'}`);
    const head = el('div', 'card-head'); const left = el('div'); left.style.minWidth = '0';
    const shown = x.making ? x.making.ready : x.takes;
    // Design v6: shot · time · 刚拍好, then one plain line. No lines, no English prompt, no reviewer notes.
    const title = el('div', 'card-title'); title.append(el('span', 'card-name', x.label));
    if (x.at) title.append(el('span', 'card-id', when(x.at)));
    if (x.at && st.todo && Date.now() - new Date(x.at).getTime() < 10 * 60 * 1000) title.append(el('span', 'fresh', '刚拍好'));
    left.append(title);
    if (x.intent) left.append(el('div', 'card-intent', x.intent));
    // fal 样片 are 480p previews; the picked one becomes a 1080p 正片, within seven days.
    const drafts = shown.map(t => completion(t)).filter(Boolean);
    if (drafts.length && st.todo) {
      const ends = drafts.map(d => d.expires).filter(Number.isInteger);
      const until = ends.length ? new Date(Math.min(...ends) * 1000).toLocaleDateString('zh-CN') : null;
      left.append(el('div', 'card-intent draft-note', `这些是样片（480p，看得清动作和表演）。你选中的那条，10 分钟后自动做成 1080p 正片；${until ? `样片 ${until} 前有效，过期就只能用样片。` : '样片 7 天内有效。'}`));
    }
    head.append(left, el('span', `status ${st.cls}`, st.text)); c.append(head);
    const grid = el('div', 'takes'); const shotNotes = notes().filter(n => n.target.object_id === x.key);
    if (x.source && !x.making) { grid.classList.add('src'); grid.append(sourceTile(x.source)); }
    shown.forEach((t, i) => {
      const box = el('div', `take${x.picked === i ? ' picked' : ''}`); const v = video(S.pid, t); box.append(v);
      if (!x.making) { const open = el('button', 'open'); open.type = 'button'; open.onclick = () => openLight(x.key, i); box.append(open); }
      box.append(el('span', 'chip n', i + 1));
      const cp = completion(t); if (cp) box.append(el('span', `chip res ${cp.state}`, RES[cp.state]));
      const tn = shotNotes.filter(n => sameRef(n.take, t)).length; if (tn) box.append(el('span', 'chip notes', `${tn} 句`));
      if (x.picked === i) box.append(el('span', 'tick', '✓'));
      const bar = el('span', 'progress'); const fill = el('span'); bar.append(fill); bar.hidden = true; box.append(bar);
      v.addEventListener('timeupdate', () => { if (v.duration) fill.style.width = `${(v.currentTime / v.duration * 100).toFixed(1)}%`; });
      let actions = null;
      box.onmouseenter = () => {
        box.classList.add('hover'); bar.hidden = false; v.currentTime = 0; v.play().catch(() => {});
        if (canPick(x)) {
          actions = el('div', 'take-actions');
          const note = el('button', null, '写一句'); note.type = 'button'; note.onclick = e => { e.stopPropagation(); S.note = {shot: x.key, take: i + 1, kind: 'take'}; renderFeed(); };
          const use = el('button', 'use', x.picked === i ? '取消' : '用这条'); use.type = 'button'; use.onclick = e => { e.stopPropagation(); guarded(() => pick(x, i)); };
          actions.append(note, use); box.append(actions);
        }
      };
      box.onmouseleave = () => { box.classList.remove('hover'); bar.hidden = true; v.pause(); if (actions) actions.remove(); actions = null; };
      grid.append(box);
    });
    for (let i = 0; i < (x.making ? x.making.pending : 0); i++) {
      const box = el('div', 'take pending'); box.append(el('span', 'pending-label', '生成中')); grid.append(box);
    }
    c.append(grid);
    // earlier batches stay here and stay pickable (newest first); while a new batch is
    // being made, the latest offered batch is listed here too.
    const earlier = x.making && x.request ? [{request: x.request, takes: x.takes, state: x.state, at: x.at, picked: x.picked ?? -1}, ...x.batches] : x.batches;
    // Owner 2026-09-25 "这界面真看不懂": the card shows the current takes; earlier batches wait behind one line, and a
    // batch that only repeats takes already shown is dropped. While a new batch is made, the last offered one stays open.
    const seen = new Set(x.making ? [] : x.takes.map(refKey));
    const distinct = earlier.filter(b => { const fresh = b.takes.some(t => !seen.has(refKey(t))); b.takes.forEach(t => seen.add(refKey(t))); return fresh; });
    const always = x.making && x.request ? distinct.slice(0, 1) : [];
    const folded = distinct.slice(always.length);
    always.forEach((b, n) => c.append(batchRow(x, b, n)));
    if (folded.length) {
      const open = S.openBatches.has(x.key); const line = el('div', 'batch-toggle');
      const btn = el('button', 'act ghost', open ? '收起之前的批次' : `之前的批次（${folded.length}）`); btn.type = 'button';
      btn.onclick = () => { if (open) S.openBatches.delete(x.key); else S.openBatches.add(x.key); renderFeed(); };
      line.append(btn);
      const chosen = folded.find(b => b.picked >= 0);
      if (chosen && !open) line.append(el('span', 'card-id', `已选：之前一批的第 ${chosen.picked + 1} 条`));
      c.append(line);
      if (open) folded.forEach((b, n) => c.append(batchRow(x, b, n + always.length)));
    }
    if (shotNotes.length) {
      const list = el('div', 'notes');
      const all = [x.takes, ...x.batches.map(b => b.takes)];
      for (const n of shotNotes) {
        const k = n.take ? x.takes.findIndex(t => sameRef(t, n.take)) : -1;
        const where = n.take ? (k >= 0 ? `第 ${k + 1} 条` : all.some(ts => ts.some(t => sameRef(t, n.take))) ? '之前一批' : '一条') : '整段';
        const row = el('div', 'note'); row.append(el('span', 'note-tag-sm', `${where}${n.at ? ` @${n.at}s` : ''}`));
        const text = el('span', 'note-text', n.text); text.append(el('span', 'note-meta', ` · 你 · ${n.when}`)); row.append(text);
        const x2 = el('button', 'note-x', '×'); x2.type = 'button'; x2.onclick = () => guarded(async () => { await dropNote(n); await refresh(); }); row.append(x2); list.append(row);
      }
      c.append(list);
    }
    if (S.note && S.note.shot === x.key) {
      const kind = S.note.kind; const cancel = () => { S.note = null; renderFeed(); };
      const tag = kind === 'rebatch' ? '再拍一批' : kind === 'reject' ? '都不行' : `第 ${S.note.take} 条`;
      // a reasonless 再拍一批 re-fires the same card by itself; a sentence goes to the agent.
      const ph = kind === 'rebatch' ? '不写理由 = 原样再拍 4 条；写了理由 = 交给 AI 改（Enter 记下）' : kind === 'reject' ? '为什么都不行？一句就够' : `对第 ${S.note.take} 条说一句`;
      c.append(noteBox(tag, ph, async text => {
        if (kind === 'take') {
          if (!text) return say('先写一句');
          const b = S.note.batch; const takes = b ? b.takes : x.takes;
          await addNote({target: (b ? b.request : x.request)?.details?.target, take: takes[S.note.take - 1], text});
          S.note = null; await refresh(); say('记下了，Agent 会看到'); return;
        }
        if (kind === 'reject' && !text) return say('先写一句为什么');
        S.note = null; await decline(x, kind, text);
      }, cancel));
    }
    // Design v6 footer: 再拍一批 / 都不行 while the shot waits; 撤销选择 once a take is picked;
    // 撤销 takes back 再拍一批 / 都不行 while the card is unchanged.
    const undoable = x.state === 'declined' && x.current && !x.making;
    if (st.todo || undoable || (x.state === 'confirmed' && x.picked >= 0 && canPick(x))) {
      const actions = el('div', 'card-actions');
      if (st.todo) {
        const rb = el('button', 'act', '再拍一批'); rb.type = 'button'; rb.onclick = () => { S.note = {shot: x.key, kind: 'rebatch'}; renderFeed(); };
        const rj = el('button', 'act', '都不行'); rj.type = 'button'; rj.onclick = () => { S.note = {shot: x.key, kind: 'reject'}; renderFeed(); };
        actions.append(rb, rj);
      }
      if (undoable) { const u = el('button', 'act', '撤销（再拍一批 / 都不行）'); u.type = 'button'; u.onclick = () => guarded(() => undo(x)); actions.append(u); }
      actions.append(el('span', 'grow'));
      if (x.state === 'confirmed' && x.picked >= 0) {
        const undoPick = el('button', 'act ghost', '撤销选择'); undoPick.type = 'button'; undoPick.onclick = () => guarded(() => pick(x, x.picked));
        actions.append(undoPick);
      }
      c.append(actions);
    } else if (x.state === 'declined' && !x.current) {
      c.append(el('div', 'card-intent', 'Agent 已经照这句改了卡；想用这批哪条，直接点「用这条」。'));
    }
    return c;
  }
  // The source segment this recreation shot recreates, looping inside its range; watch only, never picked.
  function sourceTile(src) {
    const a = Number(src.start_seconds) || 0; const b = Number(src.end_seconds) || 0;
    const box = el('div', 'take source'); const v = el('video'); v.muted = true; v.playsInline = true; v.preload = 'metadata';
    v.src = `${mediaUrl(S.pid, src.media)}#t=${a},${b}`; retryOnce(v);
    v.addEventListener('timeupdate', () => { if (b > a && v.currentTime >= b) v.currentTime = a; });
    box.append(v, el('span', 'chip n', '原片'));
    box.onmouseenter = () => { v.currentTime = a; v.play().catch(() => {}); };
    box.onmouseleave = () => v.pause();
    // Owner 2026-09-25 "原片也点不开": a click opens the source segment large, with sound, like a take.
    const open = el('button', 'open'); open.type = 'button'; open.setAttribute('aria-label', '看原片');
    open.onclick = () => { const x = S.data[S.pid]?.shots.find(y => y.source === src); if (x) openLight(x.key, -1); };
    box.append(open);
    return box;
  }
  function batchRow(x, b, n) {
    const row = el('div', 'batch'); row.style.cssText = 'margin-top:10px';
    const label = b.state === 'confirmed' ? '已选' : b.state === 'declined' ? '再拍一批 / 都不行' : b.state === 'pending' ? '待审' : '';
    row.append(el('div', 'card-id', `之前一批${n ? `（${n + 1}）` : ''}${b.at ? ` · ${when(b.at)}` : ''}${label ? ` · ${label}` : ''}`));
    const grid = el('div', 'takes'); grid.style.opacity = '.92';
    b.takes.forEach((t, i) => {
      const box = el('div', `take${b.picked === i ? ' picked' : ''}`); const v = video(S.pid, t); box.append(v);
      box.append(el('span', 'chip n', i + 1)); if (b.picked === i) box.append(el('span', 'tick', '✓'));
      let actions = null;
      box.onmouseenter = () => {
        v.currentTime = 0; v.play().catch(() => {});
        actions = el('div', 'take-actions');
        const note = el('button', null, '写一句'); note.type = 'button'; note.onclick = e => { e.stopPropagation(); S.note = {shot: x.key, take: i + 1, kind: 'take', batch: b}; renderFeed(); };
        const use = el('button', 'use', b.picked === i && b.state === 'confirmed' ? '取消' : '用这条'); use.type = 'button';
        use.onclick = e => { e.stopPropagation(); guarded(() => pick(x, i, b)); };
        actions.append(note, use); box.append(actions);
      };
      box.onmouseleave = () => { v.pause(); if (actions) actions.remove(); actions = null; };
      grid.append(box);
    });
    row.append(grid); return row;
  }

  // ---------- lightbox ----------
  function shotByKey(key) { return S.data[S.pid]?.shots.find(x => x.key === key); }
  function openLight(key, i) { S.light = {key, i}; $('light').hidden = false; showTake(i); }
  function closeLight() { const v = $('l-video'); v.pause(); v.removeAttribute('src'); v.load(); S.light = null; $('light').hidden = true; $('l-note-box').hidden = true; }
  // The lightbox's order: the source segment first (recreation), then the takes.
  const lightOrder = x => [...(x.source ? [-1] : []), ...x.takes.map((_, k) => k)];
  function showSource(x) {
    S.light.i = -1; $('l-note-box').hidden = true;
    const a = Number(x.source.start_seconds) || 0; const b = Number(x.source.end_seconds) || 0; const v = $('l-video');
    S.light.range = [a, b];
    fresh().then(() => { if (S.light && S.light.i === -1) { v.src = `${mediaUrl(S.pid, x.source.media)}#t=${a},${b}`; v.muted = false; v.play().catch(() => {}); } });
    $('l-name').textContent = x.label; $('l-ref').textContent = `原片 ${a}–${b} 秒`; $('l-intent').textContent = x.intent ? `— ${x.intent}` : '';
    $('l-notes').replaceChildren(); $('l-note').hidden = true; $('l-pick').disabled = true; $('l-pick').textContent = '原片只看，不选'; $('l-pick').classList.remove('ok');
    lightRow(x, -1);
  }
  function lightRow(x, i) {
    const row = $('l-takes'); row.replaceChildren();
    if (x.source) {
      const b = el('button', `l-take${i === -1 ? ' on' : ''}`); b.type = 'button'; const th = el('div', 'thumb');
      const sv = el('video'); sv.muted = true; sv.preload = 'metadata'; sv.src = `${mediaUrl(S.pid, x.source.media)}#t=${Number(x.source.start_seconds) || 0}`;
      th.append(sv); b.append(th, el('span', null, '原片')); b.onclick = () => showTake(-1); row.append(b);
    }
    x.takes.forEach((t, k) => {
      const b = el('button', `l-take${k === i ? ' on' : ''}`); b.type = 'button'; const th = el('div', 'thumb'); th.append(video(S.pid, t));
      if (x.picked === k) th.append(el('span', 'tick', '✓')); b.append(th, el('span', null, `第 ${k + 1} 条`)); b.onclick = () => showTake(k); row.append(b);
    });
  }
  function showTake(i) {
    const x = shotByKey(S.light.key); if (!x) return;
    if (i === -1) { if (x.source) showSource(x); return; }
    if (!x.takes[i]) return; S.light.i = i; S.light.range = null; $('l-note-box').hidden = true; $('l-note').hidden = false;
    const v = $('l-video'); const src = mediaUrl(S.pid, x.takes[i]);
    fresh().then(() => { if (S.light && S.light.i === i) { v.src = src; v.play().catch(() => {}); } });
    $('l-name').textContent = x.label; $('l-ref').textContent = `第 ${i + 1} 条`; $('l-intent').textContent = x.intent ? `— ${x.intent}` : '';
    lightRow(x, i);
    const ns = notes().filter(n => sameRef(n.take, x.takes[i])); const box = $('l-notes'); box.replaceChildren();
    for (const n of ns) { const s = el('span', null, `“${n.text}” `); s.append(el('span', 'note-meta', n.at ? `@${n.at}s` : '')); box.append(s); }
    const btn = $('l-pick');
    btn.classList.toggle('ok', x.picked === i);
    btn.textContent = x.picked === i ? `已用第 ${i + 1} 条 · 撤销` : `用第 ${i + 1} 条`; btn.disabled = !canPick(x);
  }
  function lightPlayState() { const v = $('l-video'); $('l-play').textContent = v.paused ? '▶' : '❚❚'; $('l-big-play').classList.toggle('playing', !v.paused); }
  retryOnce($('l-video')); retryOnce($('cut-video'));
  $('l-video').addEventListener('play', lightPlayState); $('l-video').addEventListener('pause', lightPlayState);
  // The source segment stops at its end instead of running into the rest of the source clip.
  $('l-video').addEventListener('timeupdate', () => { const v = $('l-video'); const r = S.light?.range; if (r && r[1] > r[0] && v.currentTime >= r[1]) { v.pause(); v.currentTime = r[0]; } });
  $('l-video').addEventListener('timeupdate', () => { const v = $('l-video'); const pct = v.duration ? v.currentTime / v.duration * 100 : 0; $('l-fill').style.width = `${pct}%`; $('l-knob').style.left = `${pct}%`; $('l-time').textContent = `${fmt(v.currentTime)} / ${fmt(v.duration)}`; });
  const toggle = v => { if (v.paused) v.play().catch(() => {}); else v.pause(); };
  $('l-play').onclick = () => toggle($('l-video')); $('l-big-play').onclick = () => toggle($('l-video'));
  $('l-seek').onclick = e => { const v = $('l-video'); const r = e.currentTarget.getBoundingClientRect(); if (v.duration) v.currentTime = Math.max(0, Math.min(v.duration, (e.clientX - r.left) / r.width * v.duration)); };
  $('l-close').onclick = closeLight;
  const step = d => { const x = shotByKey(S.light.key); const o = lightOrder(x); const k = o.indexOf(S.light.i); showTake(o[(k + d + o.length) % o.length]); };
  $('l-prev').onclick = () => step(-1);
  $('l-next').onclick = () => step(1);
  $('l-pick').onclick = () => guarded(async () => { const x = shotByKey(S.light.key); const i = S.light.i; await pick(x, i); if (S.light) showTake(i); });
  $('l-note').onclick = () => { const box = $('l-note-box'); box.hidden = !box.hidden; if (!box.hidden) { $('l-note-tag').textContent = `第 ${S.light.i + 1} 条 @ ${$('l-video').currentTime.toFixed(1)}s`; $('l-note-input').value = ''; $('l-note-input').focus(); } };
  function saveLightNote() { guarded(async () => {
    const t = $('l-note-input').value.trim(); if (!t) return say('先写一句'); const x = shotByKey(S.light.key); const i = S.light.i;
    await addNote({target: x.request?.details?.target, take: x.takes[i], at: $('l-video').currentTime.toFixed(1), text: t});
    $('l-note-box').hidden = true; await refresh(); if (S.light) showTake(i); say('记下了，Agent 会看到'); }); }
  $('l-note-save').onclick = saveLightNote; $('l-note-input').onkeydown = e => { if (e.key === 'Enter') saveLightNote(); if (e.key === 'Escape') $('l-note-box').hidden = true; };

  // ---------- cut ----------
  // A tried take (S.swap) only changes this preview; the pick changes only through 用这条.
  // Stress-test cards (S01-901A-style numbers) are tests, never film segments (dry run).
  const isProbe = x => /-9\d\d[A-Z]$/.test(x.label || '');
  function cutSeq() {
    return (S.data[S.pid]?.shots || []).filter(x => !isProbe(x)).map(x => {
      const i = x.takes[S.swap[x.key]] ? S.swap[x.key] : x.picked >= 0 ? x.picked : null;
      const take = i == null ? (x.earlier || null) : x.takes[i];
      return {x, i, take: take && full(take)};  // plays the 1080p 正片 once it is ready
    });
  }
  function setCut(on) { if (on && S.pv) setProjectView(false); S.cut = on; $('cut-view').hidden = !on; $('feed').hidden = on; if (on) renderCut(); else $('cut-video').pause(); renderBar(); }
  function setProjectView(on) {
    if (on && !$('login').hidden) return;  // logged out: the login card is the only thing to show
    if (S.pvHandle) { S.pvHandle.close(); S.pvHandle = null; }
    if (on && S.cut) setCut(false);
    S.pv = Boolean(on && S.pid && window.MVGPProject);
    $('project-view').hidden = !S.pv; $('feed').hidden = S.pv || S.cut;
    if (S.pv) S.pvHandle = window.MVGPProject.mount($('project-view'), S.pid);
    renderBar();
  }
  // The platform's film is shown unless the owner is trying other takes for a segment.
  const shownFinal = () => (S.trying ? null : S.data[S.pid]?.final || null);
  function renderCut() {
    const data = S.data[S.pid]; const seq = cutSeq(); const ready = seq.filter(c => c.take); const final = shownFinal();
    const v = $('cut-video'); $('cut-empty').hidden = Boolean(final || ready.length);
    const strip = $('cut-strip'); strip.replaceChildren();
    seq.forEach((c, k) => {
      const b = el('button', `seg${c.take ? '' : ' missing'}${!final && k === S.cutIndex ? ' cur' : ''}`); b.type = 'button';
      const dur = c.take ? S.durations[refKey(c.take)] : null; if (dur) b.style.flex = `${dur} 1 0`;
      if (c.take) b.append(video(S.pid, c.take)); b.append(el('span', 'seg-id', c.take ? c.x.label : `${c.x.label}（未定）`));
      b.onclick = () => {
        if (!c.x.takes.length) return say(`${c.x.label} 还在生成，拍好了再来换`);
        S.swapShot = c.x.key; S.cutIndex = k;
        if (S.data[S.pid]?.final && !S.trying) { S.trying = true; const v = $('cut-video'); v.removeAttribute('src'); delete v.dataset.src; renderCut(); }
        renderSwap(); if (c.take) playSeq(true);
      };
      strip.append(b);
    });
    renderSwap();
    const approve = $('cut-approve');
    // dry run: after 这版可以 the desk said 成片生成中; a confirmed film plays as confirmed until a new one is offered.
    const done = !final && !S.trying ? data?.confirmed || null : null;
    if (done) {
      if (v.dataset.src !== done.object_ref.object_id) { v.src = mediaUrl(S.pid, done.details.target); v.dataset.src = done.object_ref.object_id; }
      $('cut-name').textContent = '成片'; $('cut-ref').textContent = ''; $('cut-intent').textContent = '';
      $('cut-status').textContent = '这版成片你已经确认了。想改哪个镜头，回看片台重新选一条，平台会再拼一版给你。';
      approve.disabled = true; approve.textContent = '已确认';
    } else if (final) {
      if (v.dataset.src !== final.object_ref.object_id) { v.src = mediaUrl(S.pid, final.details.target); v.dataset.src = final.object_ref.object_id; }
      $('cut-name').textContent = '成片'; $('cut-ref').textContent = ''; $('cut-intent').textContent = final.details.rationale ? `— ${final.details.rationale}` : '';
      $('cut-status').textContent = '这是平台按你选的各条拼好的成片。点「这版可以」就是最终确认；想换哪一段，点下面那一段试看别的条。';
      approve.disabled = false; approve.textContent = '这版可以';
    } else {
      delete v.dataset.src; if (!ready.length) { v.removeAttribute('src'); v.load(); }
      else if (!v.getAttribute('src')) loadSeq();
      const allPicked = seq.length > 0 && seq.every(c => c.x.picked >= 0 && !c.x.making);
      $('cut-status').textContent = S.trying ? '你在试换片段。按「用这条」，平台会按新的选择重新拼一版成片。'
        : allPicked ? '每个镜头都选好了，成片生成中：平台正在把你选的各条拼成成片，好了这里会换成真正的成片。'
        : '这里先按你选的各条连着看。每个镜头都选好后，平台会自动拼成成片，这里会换成真正的成片。';
      approve.disabled = true; approve.textContent = allPicked && !S.trying ? '成片生成中' : '等成片送审';
    }
    let back = $('cut-back');
    if (!back) { back = el('button', 'btn-light', '回到成片'); back.type = 'button'; back.id = 'cut-back'; approve.before(back);
      back.onclick = () => { S.trying = false; S.swapShot = null; S.swap = {}; renderCut(); }; }
    back.hidden = !(S.trying && S.data[S.pid]?.final);
    const list = $('cut-note-list'); list.replaceChildren();
    const filmTargets = new Set([data?.final?.details?.target?.object_id, data?.confirmed?.details?.target?.object_id].filter(Boolean));
    for (const n of notes().filter(n => filmTargets.has(n.target.object_id) || n.at != null && !n.take)) {
      const shot = (data?.shots || []).find(y => y.key === n.target.object_id);
      const row = el('div', 'note'); row.append(el('span', 'note-tag-sm', `${shot ? shot.label : '成片'}${n.at ? ` @${n.at}s` : ''}`)); row.append(el('span', 'note-text', n.text)); list.append(row);
    }
  }
  function renderSwap() {
    let row = $('cut-takes');
    if (!row) { row = el('div', 'l-takes'); row.id = 'cut-takes'; row.style.cssText = 'margin-top:10px;align-items:flex-end;flex-wrap:wrap'; $('cut-strip').after(row); }
    row.replaceChildren();
    const k = cutSeq().findIndex(c => c.x.key === S.swapShot); const c = k >= 0 ? cutSeq()[k] : null;
    row.hidden = !c || Boolean(shownFinal()); if (row.hidden) return;
    c.x.takes.forEach((t, i) => {
      const b = el('button', `l-take${i === c.i ? ' on' : ''}`); b.type = 'button'; const th = el('div', 'thumb'); th.append(video(S.pid, t));
      if (c.x.picked === i) th.append(el('span', 'tick', '✓'));
      b.append(th, el('span', null, `第 ${i + 1} 条`));
      b.onclick = () => { if (i === c.x.picked) delete S.swap[c.x.key]; else S.swap[c.x.key] = i; S.cutIndex = k; renderCut(); playSeq(true); };
      row.append(b);
    });
    if (c.i != null && c.i !== c.x.picked && canPick(c.x)) {
      const use = el('button', 'btn-dark', '用这条'); use.type = 'button';
      use.onclick = () => guarded(async () => { const i = c.i; await pick(c.x, i); delete S.swap[c.x.key]; S.trying = false; renderCut(); loadSeq(); });
      row.append(use);
    }
  }
  function loadSeq() {
    const seq = cutSeq(); let k = S.cutIndex; while (k < seq.length && !seq[k].take) k++; if (k >= seq.length) { k = 0; while (k < seq.length && !seq[k].take) k++; }
    if (k >= seq.length) return false; S.cutIndex = k; const c = seq[k]; const v = $('cut-video'); v.src = mediaUrl(S.pid, c.take); fresh();
    $('cut-name').textContent = c.x.label; $('cut-ref').textContent = `第 ${c.i + 1} 条${c.i !== c.x.picked ? ' · 试看' : ''}`; $('cut-intent').textContent = c.x.lines.length ? `— ${c.x.lines.join(' / ')}` : '';
    document.querySelectorAll('#cut-strip .seg').forEach((b, i) => b.classList.toggle('cur', i === k)); return true;
  }
  function playSeq(start) { if (loadSeq() && start) $('cut-video').play().catch(() => {}); }
  $('cut-video').addEventListener('ended', () => {
    if (shownFinal()) return; const seq = cutSeq(); let k = S.cutIndex + 1; while (k < seq.length && !seq[k].take) k++;
    if (k < seq.length) { S.cutIndex = k; playSeq(true); } else { S.cutIndex = 0; loadSeq(); }
  });
  function cutPlayState() { const v = $('cut-video'); $('cut-play').textContent = v.paused ? '▶' : '❚❚'; $('cut-big-play').classList.toggle('playing', !v.paused); }
  $('cut-video').addEventListener('play', cutPlayState); $('cut-video').addEventListener('pause', cutPlayState);
  $('cut-video').addEventListener('timeupdate', () => { const v = $('cut-video'); $('cut-time').textContent = `${fmt(v.currentTime)} / ${fmt(v.duration)}`; });
  $('cut-play').onclick = () => toggle($('cut-video')); $('cut-big-play').onclick = () => toggle($('cut-video'));
  $('cut-note').onclick = () => { const box = $('cut-note-box'); box.hidden = !box.hidden; if (!box.hidden) { const c = cutSeq()[S.cutIndex]; $('cut-note-tag').textContent = `${shownFinal() ? '成片' : c ? c.x.label : ''} @ ${$('cut-video').currentTime.toFixed(1)}s`; $('cut-note-input').value = ''; $('cut-note-input').focus(); } };
  function saveCutNote() { guarded(async () => {
    const t = $('cut-note-input').value.trim(); if (!t) return say('先写一句');
    const data = S.data[S.pid]; const film = shownFinal() || (!S.trying ? data?.confirmed : null); const c = cutSeq()[S.cutIndex];
    const target = film ? film.details.target : c?.x?.request?.details?.target; const take = film ? null : c?.take || null;
    await addNote({target, take, at: $('cut-video').currentTime.toFixed(1), text: t});
    $('cut-note-box').hidden = true; await refresh(); say('记下了，Agent 会看到'); }); }
  $('cut-note-save').onclick = saveCutNote; $('cut-note-input').onkeydown = e => { if (e.key === 'Enter') saveCutNote(); if (e.key === 'Escape') $('cut-note-box').hidden = true; };
  $('cut-approve').onclick = () => guarded(async () => {
    const final = shownFinal(); if (!final) return;
    await decide(null, 'confirm', {request: final}); logLine('成片：这版可以。'); await refresh(); say('已确认这版成片');
  });

  // ---------- chrome ----------
  function renderAll() { renderProjects(); renderBar(); if (S.cut) renderCut(); else renderFeed(); }
  async function refresh() { await loadProject(S.pid); renderAll(); }
  const makingSig = () => JSON.stringify((S.data[S.pid]?.shots || []).map(x => [x.key, x.making?.done, x.making?.total, x.state]));
  setInterval(() => {
    if (document.hidden || S.light || S.note || S.cut || !S.pid || !(S.data[S.pid]?.shots || []).some(x => x.making)) return;
    const before = makingSig();
    loadProject(S.pid).then(() => { if (makingSig() !== before) renderAll(); }).catch(() => { /* next tick retries */ });
  }, 20000);
  async function openProject(pid) { if (S.light) closeLight(); S.pid = pid; S.note = null; S.sticky = new Set(); S.cutIndex = 0; S.swap = {}; S.swapShot = null; S.trying = false; const v = $('cut-video'); v.pause(); v.removeAttribute('src'); delete v.dataset.src;
    if (S.pv) setProjectView(true);  // stay on the project page, now for the chosen film
    renderAll(); try { localStorage.setItem('mvgp-review-project', pid); } catch (_) { /* optional */ } await refresh();
    try { const had = localStorage.getItem(notesKey(pid)); if (had && csrf()) { await uploadOldNotes(pid); await refresh(); } } catch (_) { /* old notes stay for the next try */ } }
  document.querySelectorAll('[data-filter]').forEach(b => { b.onclick = () => { S.filter = b.dataset.filter; S.sticky = new Set(); if (S.cut) setCut(false); if (S.pv) setProjectView(false); renderAll(); }; });
  $('toggle-cut').onclick = () => setCut(!S.cut);
  $('copy-all').onclick = () => {
    const p = S.projects.find(p => p.project_id === S.pid); const lines = [...(S.log[S.pid] || [])];
    const label = n => (S.data[S.pid]?.shots || []).find(y => y.key === n.target.object_id)?.label || '成片';
    const ns = notes(); if (ns.length) lines.push('', '另外（这些 Agent 在平台上也看得到）：', ...ns.map(n => `${label(n)}${n.at ? ` @${n.at}s` : ''}：${n.text}`));
    if (!lines.length) return say('还没有决定');
    const text = `【${p ? projectName(p) : ''} · 看片决定】\n${lines.join('\n')}`;
    navigator.clipboard.writeText(text).then(() => say('已复制，贴给 Agent 就行'), () => say('复制没成功，请手动复制'));
  };
  document.querySelectorAll('[data-fullscreen]').forEach(b => { b.onclick = e => { const stage = e.currentTarget.closest('.cut-stage,.light-stage'); try { stage.requestFullscreen(); } catch (_) { /* unsupported */ } }; });
  // ---------- generation record: Settings → 生成记录 ----------
  const UNITS = {hf_credit: v => `${v} 点`, usd_micro: v => `$${(v / 1e6).toFixed(2)}`, apilio_quota: v => `${v} apilio 额度`};
  const ACCOUNTS = {hf_credit: 'Higgsfield 点数', usd_micro: '美元', apilio_quota: 'apilio 额度'};
  const money = (v, unit) => (UNITS[unit] || (x => `${x} ${unit}`))(v);
  function recordPanel() {
    let panel = $('record'); if (panel) return panel;
    panel = el('div', 'record'); panel.id = 'record'; panel.hidden = true;
    panel.style.cssText = 'position:fixed;inset:0;z-index:70;background:rgba(9,9,11,.45);display:flex;align-items:center;justify-content:center';
    const card = el('div', 'record-card'); card.style.cssText = 'width:min(1100px,94vw);height:86vh;background:#fff;border-radius:16px;display:flex;flex-direction:column;overflow:hidden';
    const head = el('div'); head.style.cssText = 'display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid #e4e4e7';
    const title = el('h2', null, '生成记录'); title.id = 'record-title'; title.style.cssText = 'margin:0;font-size:17px;flex:1';
    const kind = el('select'); kind.name = 'kind'; const status = el('select'); status.name = 'status';
    for (const [sel, opts] of [[kind, ['全部', '片子', '出图', '成片渲染', '看片理解', '导演看片', '导演看成片', '开拍前审查', '资产审查']],
                               [status, ['全部', '成功', '失败', '结果不明', '进行中', '已取消']]]) {
      for (const o of opts) { const opt = el('option', null, o); opt.value = o; sel.append(opt); }
      sel.onchange = () => paintRecord();
    }
    const close = el('button', 'btn-light', '关闭'); close.type = 'button'; close.onclick = () => { panel.hidden = true; };
    head.append(title, kind, status, close);
    const totals = el('div', 'record-totals'); totals.style.cssText = 'padding:10px 20px;color:#52525b;font-size:13px;border-bottom:1px solid #f4f4f5;display:grid;gap:4px';
    const list = el('div', 'record-list'); list.style.cssText = 'flex:1;overflow-y:auto;padding:8px 20px';
    card.append(head, totals, list); panel.append(card); document.body.append(panel);
    panel.onclick = e => { if (e.target === panel) panel.hidden = true; };
    return panel;
  }
  function paintRecord() {
    const panel = recordPanel(); const data = S.record; if (!data) return;
    const kind = panel.querySelector('select[name="kind"]').value; const status = panel.querySelector('select[name="status"]').value;
    const totals = panel.querySelector('.record-totals'); totals.replaceChildren();
    for (const t of Object.values(data.totals || {})) {
      totals.append(el('div', null, `${ACCOUNTS[t.unit] || t.unit}：已结算 ${money(t.settled, t.unit)} · 未结算最多 ${money(t.unsettled_max, t.unit)} · 共 ${t.count} 笔`));
    }
    const list = panel.querySelector('.record-list'); list.replaceChildren();
    const rows = (data.rows || []).filter(r => (kind === '全部' || r.what === kind) && (status === '全部' || r.status === status)).reverse();
    if (!rows.length) list.append(el('div', 'empty', '没有符合的记录'));
    for (const r of rows) {
      const row = el('div', 'record-row'); row.style.cssText = 'display:grid;grid-template-columns:128px 1fr auto;gap:14px;align-items:center;padding:10px 0;border-bottom:1px solid #f4f4f5';
      const thumb = el('div'); thumb.style.cssText = 'width:128px;aspect-ratio:16/9;border-radius:8px;background:#f4f4f5;overflow:hidden';
      if (exact(r.media)) { const v = video(S.pid, r.media); v.style.cssText = 'width:100%;height:100%;object-fit:cover'; thumb.append(v); }
      const mid = el('div'); const loc = r.location || {};
      mid.append(el('div', null, `${r.what}${r.model ? ' · ' + r.model : ''}`),
                 el('div', 'card-intent', [loc.scene, loc.shot, loc.take ? `第 ${loc.take} 条` : null].filter(Boolean).join(' · ') || '（不属于某颗镜头）'),
                 el('div', 'card-id', r.time ? new Date(r.time).toLocaleString('zh-CN', {hour12: false}) : ''));
      const right = el('div'); right.style.textAlign = 'right';
      right.append(el('div', null, r.status),
                   el('div', 'card-intent', r.cost ? (r.cost.settled ? money(r.cost.amount, r.cost.unit) : `最多 ${money(r.cost.amount, r.cost.unit)}（未结算）`) : ''));
      row.append(thumb, mid, right); list.append(row);
    }
  }
  async function openRecord() {
    const p = S.projects.find(p => p.project_id === S.pid); if (!p) return say('先选一个项目');
    const panel = recordPanel(); panel.querySelector('#record-title').textContent = `生成记录 · ${projectName(p)}`;
    S.record = await api(`/v1/projects/${seg(S.pid)}/generation-record`); $('settings').hidden = true; panel.hidden = false; paintRecord();
  }
  // ---------- 账本: where every cent went, and the platform's record next to fal's and apilio's own ----------
  const STATE = {held: '预占，还没结', settled: '已结', unknown: '结果不明（按最多算）', local: '本机，不花钱'};
  const fen = v => `¥${(v / 100).toFixed(2)}`;
  function ledgerPanel() {
    let panel = $('ledger'); if (panel) return panel;
    panel = el('div', 'record'); panel.id = 'ledger'; panel.hidden = true;
    panel.style.cssText = 'position:fixed;inset:0;z-index:70;background:rgba(9,9,11,.45);display:flex;align-items:center;justify-content:center';
    const card = el('div', 'record-card'); card.style.cssText = 'width:min(1100px,94vw);height:86vh;background:#fff;border-radius:16px;display:flex;flex-direction:column;overflow:hidden';
    const head = el('div'); head.style.cssText = 'display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid #e4e4e7';
    const title = el('h2', null, '账本'); title.style.cssText = 'margin:0;font-size:17px;flex:1';
    const scope = el('select'); scope.name = 'scope'; const view = el('select'); view.name = 'view';
    for (const [sel, opts] of [[scope, ['全部项目', '当前项目']], [view, ['按项目 · 镜头', '按天', '按供应商']]]) {
      for (const o of opts) { const opt = el('option', null, o); opt.value = o; sel.append(opt); }
    }
    scope.onchange = () => guarded(loadLedger); view.onchange = () => paintLedger();
    const close = el('button', 'btn-light', '关闭'); close.type = 'button'; close.onclick = () => { panel.hidden = true; };
    head.append(title, scope, view, close);
    const check = el('div', 'ledger-check'); check.style.cssText = 'padding:10px 20px;font-size:13px;border-bottom:1px solid #f4f4f5;display:grid;gap:4px';
    const list = el('div', 'record-list'); list.style.cssText = 'flex:1;overflow-y:auto;padding:8px 20px';
    card.append(head, check, list); panel.append(card); document.body.append(panel);
    panel.onclick = e => { if (e.target === panel) panel.hidden = true; };
    return panel;
  }
  async function loadLedger() {
    const panel = ledgerPanel(); const current = panel.querySelector('select[name="scope"]').value === '当前项目';
    if (current && !S.pid) return say('先选一个项目');
    S.ledger = await api(current ? `/v1/projects/${seg(S.pid)}/ledger` : '/v1/ledger'); paintLedger();
  }
  function paintLedger() {
    const panel = ledgerPanel(); const data = S.ledger; if (!data) return;
    // 平台记的 vs fal / apilio 自己的账, each of the last seven days; a difference is highlighted, never corrected.
    const check = panel.querySelector('.ledger-check'); check.replaceChildren(el('div', null, '平台记的 vs Higgsfield / fal / apilio 自己的账（按天，差额只列出，不自动改）'));
    for (const day of data.days || []) {
      for (const [name, p] of Object.entries(day.providers || {})) {
        if (name === 'higgsfield') {
          // each take matched to one Higgsfield spend; the account's other use listed apart.
          const pts = v => money(v || 0, 'hf_credit');
          const line = el('div', 'ledger-day');
          if (p.note) line.textContent = `${day.day} · Higgsfield：${p.note}`;
          else {
            const others = (p.not_the_platforms || []).map(t => `${t.model || '?'} ${pts(t.credits)}`);
            line.textContent = `${day.day} · Higgsfield：平台记的 ${pts(p.platform_settled)}（对上 ${p.matched} 条）· Higgsfield 账上扣了 ${pts(p.provider)}`
              + (others.length ? ` · 其中不是平台花的 ${pts(p.not_the_platforms_total)}（${others.join('、')}）` : '')
              + ` · 差 ${pts(p.difference)}` + (p.platform_unknown ? ` · 结果不明 ${pts(p.platform_unknown)}` : '')
              + ((p.platform_without_spend || []).length ? ` · 账上找不到的 ${p.platform_without_spend.length} 条` : '')
              + (p.balance && p.balance.credits != null ? ` · 余额 ${p.balance.credits} 点` : '');
            if (p.difference) { line.classList.add('diff'); line.style.cssText = 'color:#b91c1c;font-weight:600'; }
          }
          check.append(line);
          continue;
        }
        const show = name === 'fal' ? v => money(v, 'usd_micro') : fen;
        const line = el('div', 'ledger-day');
        const text = p.note ? `${day.day} · ${name}：${p.note}`
          : `${day.day} · ${name}：平台记的 ${show(p.platform_settled)} · ${name} 自己的 ${show(p.provider)} · 差 ${show(p.difference)}`
            + (p.platform_unknown ? ` · 结果不明 ${show(p.platform_unknown)}` : '');
        line.textContent = text;
        if (!p.note && p.difference) { line.classList.add('diff'); line.style.cssText = 'color:#b91c1c;font-weight:600'; }
        check.append(line);
      }
    }
    const list = panel.querySelector('.record-list'); list.replaceChildren();
    const rows = data.rows || [];
    if (!rows.length) { list.append(el('div', 'empty', '还没有花钱的记录')); return; }
    const view = panel.querySelector('select[name="view"]').value;
    const name = pid => projectName(S.projects.find(p => p.project_id === pid) || {title: pid});
    const key = r => view === '按天' ? (r.time || '').slice(0, 10) || '没有时间' : view === '按供应商' ? r.provider
      : `${name(r.project_id)} · ${r.shot || r.asset || '（不属于某颗镜头）'}`;
    const groups = new Map(); for (const r of rows) { const k = key(r); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); }
    for (const [k, items] of groups) {
      const sums = {}; for (const r of items) if (r.amount != null && r.unit && r.state !== 'held') sums[r.unit] = (sums[r.unit] || 0) + r.amount;
      const head = el('div', 'ledger-group', `${k} · ${Object.entries(sums).map(([u, v]) => money(v, u)).join(' + ') || '—'}`);
      head.style.cssText = 'margin:12px 0 4px;font-weight:600'; list.append(head);
      for (const r of items) {
        const row = el('div', 'ledger-row'); row.style.cssText = 'display:grid;grid-template-columns:150px 1fr auto;gap:12px;padding:6px 0;border-bottom:1px solid #f4f4f5;font-size:13px';
        row.append(el('div', 'card-id', r.time ? new Date(r.time).toLocaleString('zh-CN', {hour12: false}) : ''),
                   el('div', null, [r.what, r.provider, r.request_id ? `请求 ${r.request_id}` : null, r.attempt > 1 ? `第 ${r.attempt} 次` : null].filter(Boolean).join(' · ')),
                   el('div', null, `${STATE[r.state] || r.state || '—'}${r.amount != null && r.unit ? ' ' + money(r.amount, r.unit) : ''}`));
        list.append(row);
      }
    }
  }
  async function openLedger() { $('settings').hidden = true; ledgerPanel().hidden = false; await loadLedger(); }
  // ---------- the stress-test switch, off unless the owner turns it on ----------
  async function paintStress() {
    let box = $('steps'); if (!box) { box = el('div'); box.id = 'steps'; box.style.cssText = 'margin:0 0 14px;display:grid;gap:8px';
      document.querySelector('#settings .settings-foot').before(box); }
    const pid = S.pid; const p = S.projects.find(p => p.project_id === pid);
    const head = el('div', 'settings-sub', p ? `拍片设置 · ${projectName(p)}` : '拍片设置：先选一个项目');
    if (!p) { box.replaceChildren(head); return; }
    const current = (await api(`/v1/projects/${seg(pid)}/switches`)).switches || {};
    if (pid !== S.pid) return;
    box.replaceChildren(head);  // a slower earlier open never leaves a second checkbox behind
    const row = el('label'); row.style.cssText = 'display:flex;gap:10px;align-items:flex-start;cursor:pointer';
    const box2 = el('input'); box2.type = 'checkbox'; box2.checked = current.asset_stress_test === true; box2.style.marginTop = '3px';
    box2.onchange = () => guarded(async () => {
      if (pid !== S.pid) return say('项目已切换，请重新打开设置');
      try {
        const r = await api(`/v1/projects/${seg(pid)}/switches`, {idempotency_key: `stress-${Date.now()}`,
          switches: {asset_stress_test: box2.checked}, csrf_token: csrf()});
        box2.checked = r.switches.asset_stress_test === true; say(`素材压力测试：${box2.checked ? '已打开' : '已关闭'}`);
      } catch (e) {
        // Bug hunt r82: a failed save must not leave the box showing a setting the server does not hold.
        box2.checked = !box2.checked;
        api(`/v1/projects/${seg(pid)}/switches`).then(r => { box2.checked = r.switches?.asset_stress_test === true; }, () => {});
        throw e;
      }
    });
    const text = el('div'); text.append(el('div', null, '素材先做压力测试'),
      el('div', 'card-intent', '打开后，每个角色、地点先各拍 10 条测试片（放在「测试」里），凑满才能用进正片'));
    row.append(box2, text); box.append(row);
    // (owner 2026-09-27): 样片模式 sends this film's new orders to fal's cheap 480p drafts;
    // off (the default), shots go to Higgsfield at 1080p. Takes already made stay where they were made.
    const rowS = el('label'); rowS.style.cssText = 'display:flex;gap:10px;align-items:flex-start;cursor:pointer';
    const boxS = el('input'); boxS.type = 'checkbox'; boxS.name = 'sample_mode'; boxS.checked = current.sample_mode === true; boxS.style.marginTop = '3px';
    boxS.onchange = () => guarded(async () => {
      if (pid !== S.pid) return say('项目已切换，请重新打开设置');
      try {
        const r = await api(`/v1/projects/${seg(pid)}/switches`, {idempotency_key: `sample-${Date.now()}`,
          switches: {sample_mode: boxS.checked}, csrf_token: csrf()});
        boxS.checked = r.switches.sample_mode === true; say(`样片模式：${boxS.checked ? '已打开' : '已关闭'}`);
      } catch (e) {
        boxS.checked = !boxS.checked;
        api(`/v1/projects/${seg(pid)}/switches`).then(r => { boxS.checked = r.switches?.sample_mode === true; }, () => {});
        throw e;
      }
    });
    const tS = el('div'); tS.append(el('div', null, '样片模式'),
      el('div', 'card-intent', '打开后，新下的单走 fal 的 480p 草稿（便宜，选中后自动补成 1080p，只适合风格化人物）；关着时走 Higgsfield，直接拍 1080p'));
    rowS.append(boxS, tS); box.append(rowS);
    // 复刻先让 Gemini 看原片, recreation projects only (on by default for new ones).
    if (p.branch === 'recreation') {
      const row2 = el('label'); row2.style.cssText = 'display:flex;gap:10px;align-items:flex-start;cursor:pointer';
      const box3 = el('input'); box3.type = 'checkbox'; box3.name = 'source_reading'; box3.checked = current.source_reading === true; box3.style.marginTop = '3px';
      box3.onchange = () => guarded(async () => {
        if (pid !== S.pid) return say('项目已切换，请重新打开设置');
        try {
          const r = await api(`/v1/projects/${seg(pid)}/switches`, {idempotency_key: `reading-${Date.now()}`,
            switches: {source_reading: box3.checked}, csrf_token: csrf()});
          box3.checked = r.switches.source_reading === true; say(`复刻先看原片：${box3.checked ? '已打开' : '已关闭'}`);
        } catch (e) {
          box3.checked = !box3.checked;
          api(`/v1/projects/${seg(pid)}/switches`).then(r => { box3.checked = r.switches?.source_reading === true; }, () => {});
          throw e;
        }
      });
      const t3 = el('div'); t3.append(el('div', null, '复刻先让 Gemini 看原片'),
        el('div', 'card-intent', '打开时，Agent 要先让 Gemini 看过原片、写下看到了什么，才能拍复刻的镜头'));
      row2.append(box3, t3); box.append(row2);
    }
  }
  $('open-settings').onclick = () => {
    if (!$('open-record')) { const b = el('button', 'btn-light', '生成记录'); b.type = 'button'; b.id = 'open-record'; b.style.margin = '0 0 12px';
      b.onclick = () => guarded(openRecord); document.querySelector('#settings .settings-foot').before(b); }
    if (!$('open-ledger')) { const b = el('button', 'btn-light', '账本'); b.type = 'button'; b.id = 'open-ledger'; b.style.margin = '0 0 12px 8px';
      b.onclick = () => guarded(openLedger); $('open-record').after(b); }
    $('settings').hidden = false; guarded(paintStress);
  }; $('settings-close').onclick = () => { $('settings').hidden = true; };
  $('settings').onclick = e => { if (e.target === $('settings')) $('settings').hidden = true; };
  $('logout').onclick = () => guarded(async () => { await api('/v1/session/logout', {}); S.session = null; keepCsrf(null); $('settings').hidden = true; showLogin(); });
  window.addEventListener('keydown', e => {
    if (/INPUT|TEXTAREA/.test(e.target?.tagName || '')) return;
    if (S.light) {
      const x = shotByKey(S.light.key); if (!x) return;
      if (e.key === 'Escape') closeLight();
      else if (e.key === ' ') { e.preventDefault(); toggle($('l-video')); }
      else if (/^[1-9]$/.test(e.key) && x.takes[Number(e.key) - 1]) showTake(Number(e.key) - 1);
      else if (e.key === 'ArrowLeft') $('l-prev').onclick(); else if (e.key === 'ArrowRight') $('l-next').onclick();
      else if (e.key === 'Enter') { e.preventDefault(); if (!$('l-pick').disabled) $('l-pick').onclick(); }
      else if (e.key === 'n' || e.key === 'N') { e.preventDefault(); $('l-note').onclick(); }
    } else if (S.cut) {
      if (e.key === ' ') { e.preventDefault(); toggle($('cut-video')); } else if (e.key === 'n' || e.key === 'N') { e.preventDefault(); $('cut-note').onclick(); }
    }
  });

  // ---------- session ----------
  function showLogin() { if (S.pv) setProjectView(false); $('login').hidden = false; $('feed').hidden = true; $('cut-view').hidden = true; $('project-view').hidden = true; }
  async function start() {
    $('login').hidden = true; $('feed').hidden = false;
    S.projects = (await api('/v1/projects')).filter(p => !p.superseded_by?.length).sort((a, b) => String(a.title).localeCompare(String(b.title)));
    if (!S.projects.length) { $('feed-inner').replaceChildren(el('div', 'empty', '还没有项目')); return; }
    let saved = null; try { saved = localStorage.getItem('mvgp-review-project'); } catch (_) { /* optional */ }
    const first = S.projects.find(p => p.project_id === saved) || S.projects[0];
    await openProject(first.project_id);
    for (const p of S.projects) if (p.project_id !== S.pid) loadProject(p.project_id).then(renderProjects, () => {});
  }
  $('employee-login').onclick = () => guarded(async () => {
    $('employee-login').disabled = true;
    try { const r = await api('/v1/session/access', {}); keepCsrf(r.csrf_token); S.session = await api('/v1/session'); await start(); }
    finally { $('employee-login').disabled = false; }
  });
  // Boot: a live session with its token opens the desk at once; otherwise the owner's Cloudflare identity is
  // exchanged silently; the login card shows only when both fail (owner 2026-09-24: 登陆过，还反反复复提示).
  (async () => {
    try {
      try { S.session = await api('/v1/session'); } catch (e) { if (e.status !== 401) throw e; S.session = null; }
      if (S.session && csrf()) { S.csrf = csrf(); await start(); return; }
      if (await silentAccess()) { await start(); return; }
      showLogin();
    } catch (e) { showLogin(); say(e.message || '出错了，请刷新'); }
  })();
})();
