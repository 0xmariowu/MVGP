/* Project view: the HF mirror page one to one — folder tree left, a dark
 * masonry wall right, and an item opens in HF's full-screen viewer (media left; name, PROMPT with copy, DETAILS and
 * 下载 right). Markup and classes follow the mirror's hf.js (feature/hf-mirror 7610d5d2b). Read-only; text is never
 * HTML. Mounted by the review desk (项目页) or by the standalone page /p/<project>. */
'use strict';
(() => {
  const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
  const COL_GAP = 10;
  // What the platform may add to the writer's prompt (production/prompt.py), in the owner's words.
  const ADDED = { bind: '图片编号', descriptor: '角色描述', look: '画风', duration: '时长', no_music: '不要音乐' };
  function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = String(text); return n; }
  function seg(v) { if (!ID.test(v)) throw new Error('无效的引用'); return encodeURIComponent(v); }
  function mediaUrl(pid, r) { return `/v1/projects/${seg(pid)}/media/${seg(r.object_id)}?revision=${Number(r.revision)}`; }
  // the page renews a lapsed session from the owner's Cloudflare identity (the desk's
  // silent login) instead of asking him to log in again, and keeps the desk tab's token in step.
  async function renew() {
    try {
      const res = await fetch('/v1/session/access', {method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json'}, body: '{}'});
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.csrf_token) return false;
      try { localStorage.setItem('mvgp-review-csrf', data.csrf_token); } catch (_) { /* private mode */ }
      return true;
    } catch (_) { return false; }
  }
  async function api(path) {
    const get = () => fetch(path, {credentials: 'same-origin'});
    let res = await get();
    if (res.status === 401 && await renew()) res = await get();
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const e = new Error(data.message || `读取失败（${res.status}）`); e.status = res.status; throw e; }
    return data;
  }
  function retryOnce(v) {
    v.addEventListener('error', async () => {
      if (v.dataset.retried || !v.getAttribute('src')) return; v.dataset.retried = '1';
      if (await renew()) { const src = v.getAttribute('src'); v.removeAttribute('src'); v.load(); v.src = src; }
    });
    return v;
  }
  function columnCountFor(width) { return width >= 1400 ? 6 : width >= 1100 ? 5 : width >= 820 ? 4 : width >= 560 ? 3 : 2; }
  const AVATAR_PALETTE = ['#84cc16', '#06b6d4', '#ec4899', '#a855f7', '#eab308', '#64748b'];
  function avatarColorFor(name) { let h = 0; for (const c of String(name || '')) h = (h * 31 + c.charCodeAt(0)) >>> 0; return AVATAR_PALETTE[h % AVATAR_PALETTE.length]; }
  function fmtDur(sec) { const s = Math.round(Number(sec) || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }

  const ratios = new Map();  // object_id → width/height, learned when an image loads
  function mount(root, pid, {standalone = false} = {}) {
    const S = {tree: null, active: '_all', open: new Set(), shown: [], index: -1};
    root.replaceChildren(); root.classList.add('pj');
    const head = el('div', 'hf-head'); const titles = el('div'); const title = el('h1', null, '项目'); const lede = el('p', 'lede');
    titles.append(title, lede); head.append(titles);
    const split = el('div', 'hf-split'); split.hidden = true;
    const nav = el('nav', 'hf-folders'); nav.setAttribute('aria-label', '文件夹');
    const tree = el('div', 'tree'); tree.id = 'hf-tree'; nav.append(el('div', 'hd', '文件夹'), tree);
    const wall = el('section', 'hf-wall'); wall.setAttribute('aria-label', '生成记录墙');
    const wallHead = el('div', 'hf-wall__head'); const wallTitle = el('h2', null, '素材'); const wallCount = el('span', 'meta');
    wallHead.append(wallTitle, wallCount);
    const about = el('div'); const grid = el('div', 'hf-grid'); const emptySlot = el('div', 'hf-empty-slot');
    wall.append(wallHead, about, grid, emptySlot); split.append(nav, wall);
    // HF's full-screen item viewer (hfx), in this view so the scoped tokens apply.
    const hfx = el('div', 'hfx'); hfx.hidden = true;
    const backdrop = el('div', 'hfx__backdrop'); const box = el('div', 'hfx__box'); box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true');
    const close = el('button', 'hfx__close', '✕'); close.type = 'button'; close.setAttribute('aria-label', '关闭 · Esc');
    const prev = el('button', 'hfx__nav hfx__nav--prev', '‹'); prev.type = 'button'; prev.setAttribute('aria-label', '上一条 · ←');
    const next = el('button', 'hfx__nav hfx__nav--next', '›'); next.type = 'button'; next.setAttribute('aria-label', '下一条 · →');
    const media = el('div', 'hfx__media'); const side = el('aside', 'hfx__side');
    box.append(close, prev, next, media, side); hfx.append(backdrop, box);
    root.append(head, split, hfx);

    const children = parent => S.tree.folders.filter(f => (f.parent || null) === parent);
    const byId = id => S.tree.folders.find(f => f.id === id);
    function subtree(id) {
      if (id === '_all') return null;
      const ids = new Set([id]); let grew = true;
      while (grew) { grew = false; for (const f of S.tree.folders) if (f.parent && ids.has(f.parent) && !ids.has(f.id)) { ids.add(f.id); grew = true; } }
      return ids;
    }
    function ancestors(id) { const out = []; let cur = byId(id); while (cur && cur.parent) { out.push(cur.parent); cur = byId(cur.parent); } return out; }
    const count = id => { const ids = subtree(id); return S.tree.items.filter(i => !ids || ids.has(i.folder)).length; };

    function row(id, name, depth, hasKids) {
      const a = el('a'); a.href = '#'; a.dataset.fid = id; a.style.setProperty('--pad', `${8 + depth * 16}px`);
      if (id === S.active) a.dataset.on = '1';
      const nm = el('span', 'nm'); const dir = el('span', 'dir'); dir.setAttribute('aria-hidden', 'true');
      nm.append(el('span', 'g', !hasKids ? '·' : S.open.has(id) ? '▾' : '▸'), dir, el('span', 'note', name));
      a.append(nm, el('span', 'pill d', count(id)));
      a.onclick = ev => {
        ev.preventDefault();
        if (id === S.active && id !== '_all') { if (S.open.has(id)) S.open.delete(id); else S.open.add(id); }
        else { S.active = id; if (id !== '_all') S.open.add(id); if (standalone) try { history.replaceState(null, '', `?f=${encodeURIComponent(id)}`); } catch (_) { /* optional */ } paintWall(); }
        paintTree();
      };
      return a;
    }
    function level(parent, depth) {
      for (const f of children(parent)) {
        const kids = children(f.id).length > 0;
        tree.append(row(f.id, f.name, depth, kids));
        if (kids && S.open.has(f.id)) level(f.id, depth + 1);
      }
    }
    function paintTree() { tree.replaceChildren(row('_all', '全部素材', 0, false)); level(null, 0); }

    function tile(item, n) {
      const a = el('a', 'hf-tile'); a.href = '#';
      const fr = el('span', 'fr'); fr.style.aspectRatio = '16/9';
      if (item.kind === 'video') {
        // a recreation shot's source segment plays its own range.
        const range = item.details?.source ? `#t=${Number(item.details.source_start) || 0},${Number(item.details.source_end) || 0}` : '#t=0.1';
        const v = retryOnce(el('video')); v.muted = true; v.preload = 'metadata'; v.playsInline = true; v.src = `${mediaUrl(pid, item.media)}${range}`;
        v.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block'; fr.append(v);
        const play = el('span', 'play'); play.setAttribute('aria-hidden', 'true'); play.append(el('i')); fr.append(play);
        if (item.details?.duration) fr.append(el('span', 'badge', fmtDur(item.details.duration)));
      } else if (item.kind === 'image') {
        const i = el('img'); i.alt = ''; i.loading = 'lazy'; i.decoding = 'async'; i.src = mediaUrl(pid, item.media); fr.append(i);
        const known = ratios.get(item.media.object_id); if (known) fr.style.aspectRatio = String(known);
        i.onload = () => { if (i.naturalWidth && i.naturalHeight) { const r = i.naturalWidth / i.naturalHeight; ratios.set(item.media.object_id, r); fr.style.aspectRatio = String(r); } };
      } else fr.append(el('span', 'hf-fail', '还没有图'));
      if (item.details?.picked) fr.append(el('span', 'tick', '✓'));  // MVGP: the owner's pick
      if (item.details?.source) fr.append(el('span', 'badge', '原片'));
      a.append(fr); a.title = item.name; a.onclick = ev => { ev.preventDefault(); openItem(n); };
      return a;
    }
    // HF's project records — stage and brief on 全部素材, the shotlist on a scene,
    // the version log (version / what changed / verdict) and the 10–15 advice on a shot. Read-only text.
    function briefBlock() {
      const b = S.tree.brief || {}; const box = el('div', 'hf-brief');
      const strip = el('div', 'hf-stage');
      for (const name of ['开发', '前期', '拍摄', '后期']) { const s = el('span', name === b.stage ? 'on' : null, name); strip.append(s); }
      box.append(strip);
      const row = (k, v) => { if (v == null || v === '' || (Array.isArray(v) && !v.length)) return; const d = el('div', 'row'); d.append(el('span', 'k', k), el('span', 'v', Array.isArray(v) ? v.join(' · ') : v)); box.append(d); };
      const counts = o => o ? Object.entries(o).map(([k, v]) => `${k} ${v === true ? '是' : v === false ? '否' : v}`).join(' · ') : null;
      row('一句话', b.logline); row('关于', b.about); row('用到的模型', b.tools);
      row('前期', counts(b.pre_production)); row('拍摄', counts(b.production)); row('后期', counts(b.post_production));
      return box;
    }
    function shotlistBlock(folder) {
      const shots = children(folder.id).filter(f => f.id.startsWith('shot:'));
      if (!shots.length) return null;
      const table = el('table', 'hf-shotlist'); const head = el('tr');
      for (const h of ['镜头', '目标', '景别', '镜头焦段', '时长', '版本', '条', '状态']) head.append(el('th', null, h));
      table.append(head);
      for (const f of shots) {
        const r = f.shot || {}; const tr = el('tr');
        for (const v of [f.name, r.goal, r.size, r.lens, r.duration != null ? `${r.duration} 秒` : '', r.versions, r.takes, r.status]) tr.append(el('td', null, v ?? ''));
        table.append(tr);
      }
      return table;
    }
    function logBlock(folder) {
      const box = el('div', 'hf-log');
      for (const v of folder.log || []) {
        const d = el('div', 'row');
        d.append(el('span', 'k', `第 ${v.version} 版`), el('span', 'v', v.change_note || '—'), el('span', 'n', `${v.takes} 条`),
                 el('span', 'verdict', v.verdict ? `${v.verdict}${v.reason ? '：' + v.reason : ''}` : '还没看'));
        box.append(d);
      }
      return box;
    }
    // (Q6): the reviewer's notes with the writer's answers, 没审 / 手册不是最新, and the quote.
    function money(value, unit) {
      if (value == null) return '—';
      return unit === 'usd_micro' ? `US$${(value / 1e6).toFixed(2)}` : `${value} ${unit || ''}`.trim();
    }
    function quoteLine(folder) {
      const q = S.quote; if (!q) return null;
      const row = (q.cards || []).find(c => 'shot:' + c.card?.object_id === folder.id); if (!row) return null;
      if (row.advice) return row.advice;
      const parts = [`拍一批 ${row.takes} 条约 ${money(row.drafts, row.unit)}`];
      if (row.completion != null) parts.push(`选中那条做 1080p 正片约 ${money(row.completion, row.unit)}`);
      if (q.envelope) parts.push(`预算还剩 ${money(q.envelope.left, q.envelope.unit)}`);
      return parts.join('；') + (q.advice?.length ? `。${q.advice.join('；')}` : '');
    }
    function reviewBlock(folder) {
      const r = folder.review || {}; const box = el('div', 'hf-review');
      const chips = el('div', 'chips');
      if (r.ordered && !r.reviewed) chips.append(el('span', 'chip warn', '没审'));
      if (r.manuals_current === false) chips.append(el('span', 'chip warn', '手册不是最新'));
      if (chips.childNodes.length) box.append(chips);
      for (const n of r.notes || []) {
        const d = el('div', 'row'); d.append(el('span', 'k', n.line), el('span', 'v', n.note), el('span', 'n', `改了：${n.answer}`)); box.append(d);
      }
      const q = quoteLine(folder); if (q) box.append(el('div', 'quote', q));
      return box.childNodes.length ? box : null;
    }
    function paintAbout() {
      about.replaceChildren(); about.className = 'hf-about';
      const folder = byId(S.active);
      if (folder && folder.id.startsWith('shot:')) {
        if (folder.text) about.append(el('div', 'hf-goal', folder.text));
        if (folder.log?.length) about.append(logBlock(folder));
        const review = reviewBlock(folder); if (review) about.append(review);
        if (folder.advice) about.append(el('div', 'hf-advice', folder.advice));
        return;
      }
      if (S.active === '_all' && S.tree.brief) about.append(briefBlock());
      if (folder && folder.id.startsWith('scene:')) { const t = shotlistBlock(folder); if (t) about.append(t); }
      if (S.active !== '_all' && !folder?.text) return;
      const notesBox = el('div', 'hf-notes');
      const notes = S.active === '_all' ? (S.tree.notes || []) : [{name: '场景说明', text: folder.text}];
      for (const note of notes) { const d = el('details'); d.append(el('summary', null, note.name), el('pre', null, note.text || '')); notesBox.append(d); }
      if (notes.length) about.append(notesBox);
    }
    function paintWall() {
      const ids = subtree(S.active);
      S.shown = S.tree.items.filter(i => !ids || ids.has(i.folder));
      // HF does not repeat the folder name over the wall; it is highlighted in the tree.
      wallCount.textContent = `${S.shown.length} 项`;
      paintAbout();
      grid.replaceChildren(); emptySlot.replaceChildren();
      if (!S.shown.length) { emptySlot.append(el('div', 'hf-empty', '这个文件夹还没有项目')); return; }
      // HF's hand-rolled masonry: equal columns, each tile into the currently shortest one.
      const width = grid.clientWidth || grid.getBoundingClientRect().width || 900;
      const cols = Array.from({length: columnCountFor(width)}, () => el('div', 'hf-col'));
      const heights = cols.map(() => 0);
      S.shown.forEach((item, n) => {
        const at = heights.indexOf(Math.min(...heights));
        cols[at].append(tile(item, n)); heights[at] += 1 / (ratios.get(item.media?.object_id) || 16 / 9) + 0.01;
      });
      grid.append(...cols);
    }

    function section(name, bodyEl, extra) {
      const d = el('div', 'hfx__section'); const h = el('div', 'hfx__phead'); h.append(el('div', 'h', name)); if (extra) h.append(extra);
      d.append(h, bodyEl); return d;
    }
    function openItem(n) {
      const item = S.shown[n]; if (!item) return; S.index = n;
      media.replaceChildren(); side.replaceChildren();
      if (item.kind === 'video') {
        const v = retryOnce(el('video')); v.controls = true; v.preload = 'metadata'; v.playsInline = true;
        v.src = mediaUrl(pid, item.media) + (item.details?.source ? `#t=${Number(item.details.source_start) || 0},${Number(item.details.source_end) || 0}` : ''); media.append(v);
        const speed = el('select', 'hfx__speed'); speed.title = '播放速度';
        for (const rate of [0.5, 1, 1.5, 2]) { const o = el('option', null, `${rate}×`); o.value = String(rate); if (rate === 1) o.selected = true; speed.append(o); }
        speed.onchange = () => { v.playbackRate = parseFloat(speed.value); }; media.append(speed); v.play().catch(() => {});
      } else if (item.kind === 'image') {
        const i = el('img'); i.alt = ''; i.src = mediaUrl(pid, item.media); media.append(i);
      } else media.append(el('div', 'hf-fail', '还没有图'));
      const d = item.details || {};
      const folder = byId(item.folder);
      const where = [...ancestors(item.folder).reverse().map(id => byId(id)?.name), folder?.name].filter(Boolean).join(' / ');
      const author = el('div', 'hfx__author'); const who0 = String(d.shot || d.element || item.name || '?').replace(/^[@\s]+/, ''); const avatar = el('span', 'hfx__avatar', who0.charAt(0).toUpperCase()); avatar.style.background = avatarColorFor(who0);
      const who = el('span', 'hfx__who'); who.append(el('span', 'name', item.name), el('span', 'cap', where));
      author.append(avatar, who); side.append(author);
      if (d.prompt) {
        const pbox = el('div', 'hfx__prompt'); const pre = el('pre', 'clamp'); const more = el('button', 'hfx__more', '展开全部'); more.type = 'button'; more.hidden = true;
        // the writer's text as sent, each platform addition marked.
        const added = Array.isArray(d.added) ? d.added : [];
        let at = 0;
        for (const [start, end, kind] of added) {
          if (start > at) pre.append(d.prompt.slice(at, start));
          const mark = el('mark', 'hfx__add', d.prompt.slice(start, end)); mark.title = '平台补的：' + (ADDED[kind] || kind); pre.append(mark);
          at = end;
        }
        pre.append(d.prompt.slice(at));
        more.onclick = () => { const on = pre.classList.toggle('expanded'); more.textContent = on ? '收起' : '展开全部'; };
        pbox.append(pre, more);
        if (typeof d.sent_is_writer_plus_additions === 'boolean') {
          const count = d.additions ? ` + ${d.additions} 处补充` : '，平台没补';
          pbox.append(el('div', 'hfx__proof' + (d.sent_is_writer_plus_additions ? '' : ' hfx__proof--bad'),
            `发出去的 = 写手原文${count} ${d.sent_is_writer_plus_additions ? '✓' : '✗ 对不上'}`));
        }
        const copy = el('button', 'hfx__copy', '复制'); copy.type = 'button';
        copy.onclick = () => navigator.clipboard.writeText(d.prompt).then(() => { copy.textContent = '已复制'; copy.dataset.done = '1';
          setTimeout(() => { copy.textContent = '复制'; delete copy.dataset.done; }, 1400); }, () => { copy.textContent = '复制没成功'; });
        side.append(section('PROMPT', pbox, copy));
        requestAnimationFrame(() => { if (pre.scrollHeight > pre.clientHeight + 2) more.hidden = false; });
      }
      for (const [key, name] of [['descriptor', '描述'], ['voice', '声音'], ['behavior', '表演']]) {
        if (d[key]) { const p = el('div', 'hfx__prompt'); p.append(el('pre', null, d[key])); side.append(section(name, p)); }
      }
      const kv = el('dl', 'kv');
      const add = (k, v) => { if (v == null || v === '') return; kv.append(el('dt', null, k), el('dd', null, v)); };
      add('功能', d.model); add('质量/分辨率', d.resolution); add('画幅', d.aspect_ratio); add('时长', d.duration != null ? `${d.duration} 秒` : null);
      if (d.source) add('原片区间', `${d.source_start}–${d.source_end} 秒`);
      if (item.kind === 'video' && item.folder.startsWith('shot:')) {
        add('老板选了', d.picked ? '✓ 这一条' : '—');
        // the manuals the writer used, the sections that are the writer's, what this version changed.
        add('手册版本', d.playbook_version || '—'); add('写手段落', d.writer); add('这版改了什么', d.change_note || '—');
      }
      add('成片状态', d.final === 'confirmed' ? '已定稿' : d.final === 'pending' ? '待确认' : d.final);
      if (kv.childNodes.length) {
        const details = el('div', 'hfx__section hfx__details');
        const toggle = el('button', 'hfx__dtoggle'); toggle.type = 'button'; toggle.setAttribute('aria-expanded', 'true');
        const chev = el('span', 'chev', '▾'); chev.setAttribute('aria-hidden', 'true'); toggle.append(el('span', 'h', 'DETAILS'), chev);
        toggle.onclick = () => { const open = toggle.getAttribute('aria-expanded') !== 'false'; toggle.setAttribute('aria-expanded', open ? 'false' : 'true'); kv.hidden = open; chev.textContent = open ? '▸' : '▾'; };
        details.append(toggle, kv); side.append(details);
      }
      if (item.media) {
        const actions = el('div', 'hfx__actions'); const dl = el('a', 'btn btn--primary hfx__actions-btn', '下载');
        dl.href = mediaUrl(pid, item.media); dl.setAttribute('download', ''); actions.append(dl); side.append(actions);
      }
      prev.hidden = n <= 0; next.hidden = n >= S.shown.length - 1;
      hfx.hidden = false;
    }
    function closeItem() { const v = media.querySelector('video'); if (v) v.pause(); media.replaceChildren(); hfx.hidden = true; S.index = -1; }
    function step(delta) { const j = S.index + delta; if (j >= 0 && j < S.shown.length) openItem(j); }
    close.onclick = closeItem; backdrop.onclick = closeItem; prev.onclick = () => step(-1); next.onclick = () => step(1);
    const onKey = e => {
      if (hfx.hidden || !root.isConnected || root.closest('[hidden]')) return;
      if (/^(INPUT|TEXTAREA)$/.test(e.target?.tagName || '') || e.target?.isContentEditable) return;
      // Inside the desk, its own overlays (settings, generation record, lightbox) own the keyboard.
      if (document.querySelector('#settings:not([hidden]), #record:not([hidden]), #light:not([hidden])')) return;
      if (e.key === 'Escape') { e.preventDefault(); closeItem(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
    };
    window.addEventListener('keydown', onKey);
    // HF re-flows the wall when its width crosses a column breakpoint.
    let cols = 0;
    const resize = typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
      const n = columnCountFor(grid.clientWidth); if (S.tree && n !== cols) { cols = n; paintWall(); }
    }) : null;
    if (resize) resize.observe(grid);

    (async () => {
      try {
        if (!ID.test(pid)) throw Object.assign(new Error('项目地址不对'), {status: 404});
        if (standalone) await api('/v1/session');
        S.tree = await api(`/v1/projects/${seg(pid)}/project-tree`);
        S.quote = await api(`/v1/projects/${seg(pid)}/quote`).catch(() => null);  // read-only; the page works without it
      } catch (e) {
        if (e.status === 401 || e.status === 403) { title.textContent = '请先在看片台登录，再打开项目页'; return; }
        title.textContent = e.message || '读取失败'; return;
      }
      title.textContent = S.tree.title;
      if (standalone) document.title = `${S.tree.title} · MVGP 项目`;
      lede.textContent = `${S.tree.folders.length} 个文件夹 · ${S.tree.items.length} 项`;
      const wanted = standalone ? new URLSearchParams(location.search).get('f') : null;
      if (wanted && S.tree.folders.some(f => f.id === wanted)) { S.active = wanted; S.open.add(wanted); for (const a of ancestors(wanted)) S.open.add(a); }
      split.hidden = false;
      paintTree(); paintWall();
    })();
    return {close: () => { closeItem(); window.removeEventListener('keydown', onKey); if (resize) resize.disconnect(); }};
  }

  window.MVGPProject = {mount};
  const page = document.getElementById('pj-root');
  if (page) mount(page, decodeURIComponent(location.pathname.replace(/^\/p\//, '').split('/')[0] || ''), {standalone: true});
})();
