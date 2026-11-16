/* ClipIn phone app. Talks only to GitHub: starts a cloud run with the link, follows its steps for live
   progress, and shows the finished clips from the `clips` branch. No AI keys on the phone. */
'use strict';
const VERSION = '1.2.0';
const $ = s => document.querySelector(s);
const store = {
  get(k, d) { try { const v = JSON.parse(localStorage.getItem('clipin.' + k)); return v ?? d; } catch { return d; } },
  set(k, v) { localStorage.setItem('clipin.' + k, JSON.stringify(v)); },
  del(k) { localStorage.removeItem('clipin.' + k); },
};

// same lists as the laptop app (core/templates.py, core/captions.py) ------------------------------------
const TEMPLATES = [
  { id: 'viral', name: 'Viral Pop', desc: 'Yellow word pop, zoom cuts, progress bar', caption_style: 'Bold Pop', zoom: true, bar: true, bar_color: 'FFD400' },
  { id: 'hype', name: 'Hype', desc: 'One huge word at a time, fast zooms', caption_style: 'One Word', zoom: true, bar: true, bar_color: 'FF7A1A' },
  { id: 'hustle', name: 'Hustle', desc: 'Two-tone punchy words, zoom cuts', caption_style: 'Two Tone', zoom: true },
  { id: 'podcast', name: 'Podcast', desc: 'Calm clean captions, no effects', caption_style: 'Clean' },
  { id: 'neon', name: 'Neon Night', desc: 'Glowing green words, green bar', caption_style: 'Neon', zoom: true, bar: true, bar_color: '39FF88' },
  { id: 'fun', name: 'Fun', desc: 'Colourful playful words', caption_style: 'Rainbow', zoom: true },
  { id: 'aesthetic', name: 'Aesthetic', desc: 'Soft pink lowercase', caption_style: 'Pastel', bar: true, bar_color: 'F9A8D4' },
  { id: 'cinematic', name: 'Cinematic', desc: 'Full shot on blur, small subtitles', layout: 'fit', caption_style: 'Subtitle', hook_on: false },
  { id: 'square', name: 'Square Feed', desc: '1:1 on black, boxed captions', format: '1:1', layout: 'fit_black', caption_style: 'Boxed' },
  { id: 'wide', name: 'YouTube Wide', desc: '16:9 for normal videos', format: '16:9', layout: 'fit_black', caption_style: 'Clean', hook_on: false },
];
const TPL_BASE = { format: '9:16', layout: 'auto', caption_style: 'Bold Pop', hook_on: true, zoom: false, bar: false, bar_color: 'FFFFFF' };
const CAPTIONS = ['Bold Pop', 'Two Tone', 'One Word', 'Neon', 'Fire', 'Ice', 'Rainbow', 'Pastel', 'Karaoke', 'Boxed',
  'Typewriter', 'Clean', 'Subtitle', 'None'];
const LENGTHS = [['15-30', '15–30 s'], ['30-60', '30–60 s'], ['60-90', '60–90 s']];
const COUNTS = [[3, '3 clips'], [6, '6 clips'], [9, '9 clips'], [12, '12 clips']];
const FORMATS = [['9:16', '9:16'], ['1:1', '1:1'], ['16:9', '16:9']];
const LAYOUTS = [['auto', 'Follow'], ['fit', 'Blur'], ['fit_black', 'Black'], ['center', 'Center']];
const LANGS = [['auto', 'Any language'], ['en', 'English'], ['ur', 'Urdu'], ['hi', 'Hindi'], ['ar', 'Arabic'],
  ['es', 'Spanish'], ['fr', 'French'], ['de', 'German'], ['pt', 'Portuguese'], ['tr', 'Turkish']];
const STEPS = [  // workflow step name, weight, what the phone says
  ['Get ready', 2, 'Starting a cloud computer'], ['Install tools', 10, 'Starting a cloud computer'],
  ['Download', 15, 'Downloading the video'], ['Transcribe', 18, 'Listening to every word'],
  ['Find moments', 8, 'AI is finding the best moments'], ['Render clips', 40, 'Making your clips'],
  ['Publish', 7, 'Saving your clips'],
];
const slug = n => n.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
const capImg = n => `captions/${slug(n)}.png`;

let cfg = store.get('cfg', null);           // {o, r, t, priv, branch}
let opts = store.get('opts', null) || { template: 'viral', length: '30-60', count: 6, language: 'auto', focus: '',
  ...TPL_BASE, ...TEMPLATES[0] };
delete opts.id; delete opts.name; delete opts.desc;
let pending = store.get('pending', []);     // jobs started from this phone and not finished yet
let index = store.get('index', []);         // finished jobs (from the clips branch)
const results = store.get('results', {});   // job -> result.json (finished results never change)
const media = new Map();                    // path -> object URL (private repos)

// helpers -------------------------------------------------------------------------------------------------
function toast(msg, ms = 2600) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove('show'), ms);
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function ago(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return 'just now'; if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`; return new Date(ms).toLocaleDateString();
}
function mmss(sec) { sec = Math.round(sec || 0); return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`; }
function setMsg(el, text, kind = '') { el.textContent = text; el.className = 'msg ' + kind; }

async function gh(path, { method = 'GET', body, raw = false } = {}) {
  let r;
  try {
    r = await fetch('https://api.github.com' + path, {
      method, cache: 'no-store', body: body ? JSON.stringify(body) : undefined,
      headers: { Authorization: 'Bearer ' + cfg.t, 'X-GitHub-Api-Version': '2022-11-28',
        Accept: raw ? 'application/vnd.github.raw+json' : 'application/vnd.github+json',
        ...(body ? { 'Content-Type': 'application/json' } : {}) },
    });
  } catch { throw new Error('No internet connection.'); }
  if (r.status === 401) throw new Error('The connect code stopped working. Make a new one on the laptop (Settings → Phone).');
  if (r.status === 404 && raw) return null;
  if (!r.ok) {
    let m = ''; try { m = (await r.json()).message; } catch { }
    throw new Error(m ? `GitHub: ${m}` : `GitHub error ${r.status}`);
  }
  if (r.status === 204) return true;
  return raw ? r.text() : r.json();
}
const repo = () => `/repos/${cfg.o}/${cfg.r}`;

async function mediaUrl(path) {
  if (!cfg.priv) return `https://raw.githubusercontent.com/${cfg.o}/${cfg.r}/clips/${path}`;
  if (media.has(path)) return media.get(path);
  const r = await fetch(`https://api.github.com${repo()}/contents/${path}?ref=clips`, {
    headers: { Authorization: 'Bearer ' + cfg.t, Accept: 'application/vnd.github.raw+json' } });
  if (!r.ok) throw new Error('Clip not found');
  const u = URL.createObjectURL(await r.blob()); media.set(path, u); return u;
}

// connect -------------------------------------------------------------------------------------------------
function decodeCode(text) {
  const m = text.trim().match(/clipin1\.([A-Za-z0-9_-]+)/);
  if (!m) throw new Error("That isn't a ClipIn connect code.");
  const b = m[1].replace(/-/g, '+').replace(/_/g, '/');
  const j = JSON.parse(decodeURIComponent(escape(atob(b + '==='.slice((b.length + 3) % 4)))));
  if (!j.o || !j.r || !j.t) throw new Error('The code is incomplete. Copy all of it.');
  return j;
}
async function connect() {
  const msg = $('#connectMsg');
  let c;
  try { c = decodeCode($('#code').value); } catch (e) { setMsg(msg, e.message, 'err'); return; }
  $('#connectBtn').disabled = true; setMsg(msg, 'Checking...');
  const prev = cfg; cfg = c;
  try {
    const info = await gh(repo());
    await gh(`${repo()}/actions/workflows/clip.yml`);
    cfg = { o: c.o, r: c.r, t: c.t, priv: !!info.private, branch: info.default_branch || 'main' };
    store.set('cfg', cfg);
    setMsg(msg, 'Connected!', 'ok');
    setTimeout(showApp, 400);
  } catch (e) {
    cfg = prev;
    setMsg(msg, e.message.includes('Not Found') ? "Can't find the ClipIn cloud with this code. Check the repository name and token." : e.message, 'err');
  } finally { $('#connectBtn').disabled = false; }
}

// create tab ----------------------------------------------------------------------------------------------
function seg(el, items, key, onPick) {
  el.innerHTML = items.map(([v, t]) => `<button data-v="${esc(v)}" class="${String(opts[key]) === String(v) ? 'on' : ''}">${esc(t)}</button>`).join('');
  el.onclick = e => {
    const b = e.target.closest('button'); if (!b) return;
    const v = items.find(i => String(i[0]) === b.dataset.v)[0];
    opts[key] = v; saveOpts(); [...el.children].forEach(x => x.classList.toggle('on', x === b)); onPick && onPick();
  };
}
function saveOpts() { store.set('opts', opts); }
function custom() { if (opts.template) { opts.template = ''; saveOpts(); drawTemplates(); } }
function drawTemplates() {
  $('#templates').innerHTML = TEMPLATES.map(t => `<div class="tpl ${opts.template === t.id ? 'on' : ''}" data-id="${t.id}">
    <img src="${capImg(t.caption_style)}" alt="" loading="lazy"><b>${esc(t.name)}</b><span>${esc(t.desc)}</span></div>`).join('');
}
function drawOptions() {
  drawTemplates();
  seg($('#seg-length'), LENGTHS, 'length');
  seg($('#seg-count'), COUNTS, 'count');
  seg($('#seg-format'), FORMATS, 'format', custom);
  seg($('#seg-layout'), LAYOUTS, 'layout', custom);
  $('#captionGrid').innerHTML = CAPTIONS.map(n => `<div class="cap ${opts.caption_style === n ? 'on' : ''}" data-n="${esc(n)}">
    ${n === 'None' ? '<div style="aspect-ratio:16/10;display:flex;align-items:center;justify-content:center">—</div>' : `<img src="${capImg(n)}" alt="" loading="lazy">`}${esc(n === 'None' ? 'No captions' : n)}</div>`).join('');
  $('#opt-hook').checked = !!opts.hook_on; $('#opt-zoom').checked = !!opts.zoom; $('#opt-bar').checked = !!opts.bar;
  $('#opt-lang').innerHTML = LANGS.map(([v, t]) => `<option value="${v}" ${opts.language === v ? 'selected' : ''}>${t}</option>`).join('');
  $('#opt-focus').value = opts.focus || '';
}
function wireOptions() {
  $('#templates').onclick = e => {
    const c = e.target.closest('.tpl'); if (!c) return;
    const t = TEMPLATES.find(x => x.id === c.dataset.id);
    Object.assign(opts, TPL_BASE, t, { template: t.id }); delete opts.id; delete opts.name; delete opts.desc;
    saveOpts(); drawOptions(); toast(`${t.name} template`);
  };
  $('#captionGrid').onclick = e => {
    const c = e.target.closest('.cap'); if (!c) return;
    opts.caption_style = c.dataset.n; saveOpts(); custom(); drawOptions();
  };
  for (const [id, key] of [['#opt-hook', 'hook_on'], ['#opt-zoom', 'zoom'], ['#opt-bar', 'bar']])
    $(id).onchange = e => { opts[key] = e.target.checked; saveOpts(); custom(); };
  $('#opt-lang').onchange = e => { opts.language = e.target.value; saveOpts(); };
  $('#opt-focus').oninput = e => { opts.focus = e.target.value; saveOpts(); };
  $('#pasteBtn').onclick = async () => {
    try { const t = (await navigator.clipboard.readText()).trim(); if (t) $('#url').value = t; else toast('Clipboard is empty'); }
    catch { toast('Long-press the box and tap Paste'); $('#url').focus(); }
  };
  $('#goBtn').onclick = start;
  $('#url').onkeydown = e => { if (e.key === 'Enter') start(); };
}

async function start() {
  const url = $('#url').value.trim(), msg = $('#goMsg');
  if (!/^https?:\/\/\S+$/i.test(url)) { setMsg(msg, 'Paste a video link first (https://...).', 'err'); return; }
  const job = 'j' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  const payload = { url, length: opts.length, count: opts.count, format: opts.format, layout: opts.layout,
    caption_style: opts.caption_style, hook_on: opts.hook_on, zoom: opts.zoom, bar: opts.bar, bar_color: opts.bar_color,
    language: opts.language, focus: (opts.focus || '').trim(), template: opts.template };
  $('#goBtn').disabled = true; setMsg(msg, 'Sending to the cloud...');
  try {
    await gh(`${repo()}/actions/workflows/clip.yml/dispatches`, { method: 'POST',
      body: { ref: cfg.branch || 'main', inputs: { job, options: JSON.stringify(payload) } } });
    pending.unshift({ job, url, created: Date.now(), stage: 'Waiting for a cloud computer', pct: 0 });
    store.set('pending', pending);
    $('#url').value = ''; setMsg(msg, '');
    toast('Started! It shows in Clips. You can leave the app.', 3500);
    drawActive(); showTab('clips'); schedule(3000);
  } catch (e) { setMsg(msg, e.message, 'err'); }
  finally { $('#goBtn').disabled = false; }
}

const ACTIVE = new Set(['working', 'laptop', 'laptop-working']);    // jobs still being made
const activeJobs = () => index.filter(j => ACTIVE.has(j.status));
const finishedJobs = () => index.filter(j => !ACTIVE.has(j.status));
function stageOf(j) {
  if (j.status === 'laptop') return '💻 Waiting for your laptop — open ClipIn on it';
  return (j.status === 'laptop-working' ? '💻 ' : '') + (j.stage || 'Working');
}
function drawActive() {
  const act = activeJobs();
  $('#activeWrap').classList.toggle('hidden', !pending.length && !act.length);
  $('#active').innerHTML = act.map(j => `<div class="job-live">
      <div class="t">${esc(j.title || 'Video')}</div>
      <div class="bar ${j.total ? '' : 'indet'}"><i style="width:${j.total ? Math.round(100 * (j.clips || 0) / j.total) : 0}%"></i></div>
      <div class="s"><span>${esc(stageOf(j))}</span><span>${j.created ? ago(j.created * 1000) : ''}</span></div>
    </div>`).join('') + pending.map(p => `<div class="job-live">
      <div class="t">${esc(p.title || p.url)}</div>
      <div class="bar ${p.pct ? '' : 'indet'}"><i style="width:${Math.round((p.pct || 0) * 100)}%"></i></div>
      <div class="s"><span>${esc(p.stage || 'Starting')}</span><span>${p.pct ? Math.round(p.pct * 100) + '%' : ''} · ${ago(p.created)}</span></div>
    </div>`).join('');
  $('#active').onclick = () => showTab('clips');
  const n = pending.length + act.length;
  const b = $('#badge'); b.textContent = n; b.classList.toggle('hidden', !n);
}

// following jobs ------------------------------------------------------------------------------------------
let timer = null, busy = false;
function schedule(ms) { clearTimeout(timer); timer = setTimeout(tick, ms); }
function onClipsTab() { return !$('#tab-clips').classList.contains('hidden'); }
async function tick() {
  if (busy || !cfg) return; busy = true;
  try {
    const was = new Map(activeJobs().map(j => [j.job, j]));
    if (pending.length || was.size) await loadIndex();
    if (pending.length) {
      const runs = (await gh(`${repo()}/actions/workflows/clip.yml/runs?per_page=30&event=workflow_dispatch`)).workflow_runs || [];
      for (const p of pending) {
        const run = runs.find(r => r.display_title === 'clip ' + p.job);
        if (!run) { if (Date.now() - p.created > 10 * 60e3) p.dead = true; continue; }
        if (['queued', 'waiting', 'pending'].includes(run.status)) { p.stage = 'Waiting for a free cloud computer'; p.pct = 0.01; continue; }
        if (run.status === 'completed') { p.done = p.done || Date.now(); p.conclusion = run.conclusion; continue; }
        p.stage = 'Checking the video'; p.pct = 0.03;
      }
      const known = new Set(index.map(j => j.job));
      pending = pending.filter(p => {
        if (known.has(p.job)) {
          const j = index.find(x => x.job === p.job);
          if (j.status === 'laptop') toast('YouTube blocked the cloud — your laptop will make these clips 💻', 4500);
          return false;
        }
        if (p.done && Date.now() - p.done > 90e3) { notePhoneError(p, `The cloud run ${p.conclusion || 'stopped'} without saving clips.`); return false; }
        if (p.dead) { notePhoneError(p, 'GitHub never started this job. Check Actions are enabled for the repository.'); return false; }
        return true;
      });
      store.set('pending', pending);
    }
    for (const [id, old] of was) {                // a job finished, or new clips arrived
      const now = index.find(j => j.job === id);
      if (now && !ACTIVE.has(now.status)) toast(now.error ? 'A video failed — see the Clips tab' : 'All your clips are ready 🎬', 3500);
      else if (now && (now.clips || 0) > (old.clips || 0)) toast(`Clip ${now.clips} is ready`, 2200);
    }
    drawActive();
    if (onClipsTab()) drawJobs();
  } catch (e) { console.warn(e); $('#cloudState').textContent = 'offline'; }
  finally { busy = false; }
  const live = pending.length || activeJobs().length;
  schedule(live ? (document.hidden ? 30000 : 8000) : 60000);
}
function notePhoneError(p, msg) {
  index.unshift({ job: p.job, title: p.url, created: p.created / 1000, clips: 0, error: msg, local: true, status: 'error' });
  store.set('index', index);
}

async function loadIndex() {
  const text = await gh(`${repo()}/contents/index.json?ref=clips`, { raw: true });
  const cloud = text ? JSON.parse(text) : [];
  const locals = index.filter(j => j.local && !cloud.some(c => c.job === j.job));
  index = [...cloud, ...locals].sort((a, b) => (b.created || 0) - (a.created || 0));
  store.set('index', index);
  $('#cloudState').textContent = 'cloud ready'; $('#cloudState').classList.add('on');
  return index;
}
async function getResult(job, fresh = false) {
  if (results[job] && !fresh) return results[job];
  const text = await gh(`${repo()}/contents/jobs/${job}/result.json?ref=clips`, { raw: true });
  if (!text) return null;
  const res = JSON.parse(text);
  if (!fresh) {                                  // finished results never change, so keep them
    results[job] = res;
    const keep = Object.keys(results).slice(-25); for (const k of Object.keys(results)) if (!keep.includes(k)) delete results[k];
    store.set('results', results);
  }
  return res;
}

// clips tab: jobs in progress first (clips appear one by one), then finished ones ----------------------------
async function drawJobs() {
  const box = $('#jobs');
  const live = [...pending.map(p => ({ job: p.job, title: p.url, created: p.created / 1000, status: 'starting', stage: p.stage || 'Starting' })),
    ...activeJobs()];
  const list = [...live, ...finishedJobs()];
  $('#jobsEmpty').classList.toggle('hidden', list.length > 0);
  const sig = list.map(j => `${j.job}:${j.status}:${j.clips}:${j.stage}`).join('|');
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = list.map(j => {
      const working = j.status === 'starting' || ACTIVE.has(j.status);
      const meta = working ? `<span class="live-dot"></span>${esc(stageOf(j))}`
        : `${j.created ? ago(j.created * 1000) : ''}${j.error ? '' : ` · ${j.clips} clips`}${j.by === 'laptop' ? ' · made on your laptop' : ''}`;
      return `<div class="job" data-job="${esc(j.job)}">
        <h3>${esc(j.title || 'Untitled video')}</h3><div class="meta">${meta}</div>
        ${j.error ? `<div class="err">${esc(j.error)}</div>` : `<div class="thumbs">${working ? skeletons(3) : ''}</div>`}
      </div>`;
    }).join('');
  }
  for (const j of list) {
    if (j.error || j.status === 'starting') continue;
    const working = ACTIVE.has(j.status);
    if (working && !j.total) continue;            // moments not picked yet
    getResult(j.job, working).then(res => fillThumbs(box, j, res, working)).catch(() => { });
  }
}
function skeletons(n) { return Array.from({ length: n }, () => '<div class="thumb making"><span>Making…</span></div>').join(''); }
function fillThumbs(box, j, res, working) {
  const el = box.querySelector(`.job[data-job="${j.job}"] .thumbs`);
  if (!el || !res) return;
  const fmt = (res.options || {}).format || '9:16';
  const cls = fmt === '1:1' ? 'sq' : fmt === '16:9' ? 'wide' : '';
  const ready = res.clips.filter(c => c.file);
  const waiting = working ? res.clips.filter(c => !c.file && c.status !== 'error') : [];
  const key = j.job + ':' + ready.length + ':' + waiting.length;
  if (el.dataset.key === key) return;
  el.dataset.key = key;
  el.innerHTML = ready.map((c, i) => `<div class="thumb ${cls}" data-i="${i}"><img alt="" loading="lazy">
      <div class="dur">${mmss(c.duration)}</div><div class="score"><small>SCORE</small><b>${c.score}</b></div></div>`).join('')
    + waiting.map(c => `<div class="thumb ${cls} making"><span>Making…</span><div class="score"><small>SCORE</small><b>${c.score}</b></div></div>`).join('');
  el.onclick = e => { const t = e.target.closest('.thumb[data-i]'); if (t) openPlayer({ ...res, clips: ready }, +t.dataset.i); };
  [...el.querySelectorAll('img')].forEach(async (img, i) => { if (ready[i].thumb) img.src = await mediaUrl(ready[i].thumb); });
}

// player --------------------------------------------------------------------------------------------------
const video = document.createElement('video');      // one video element, so sound keeps working while swiping
video.playsInline = true; video.loop = true; video.setAttribute('playsinline', '');
const blobs = new Map();                           // clip file -> Blob (ready for Share)
let current = null, observer = null;

function hashtags(c) { return ((edits(c).tags) ?? (c.yt?.hashtags || []).join(' ')).trim(); }
function edits(c) { return store.get('edit.' + c.file, {}); }
function details(c) {
  const e = edits(c), y = c.yt || {};
  return { title: e.title ?? y.title ?? c.title, desc: e.desc ?? y.description ?? '', tags: hashtags(c) };
}

function openPlayer(res, start) {
  const clips = res.clips.filter(c => c.file);
  const slides = $('#slides');
  slides.innerHTML = clips.map((c, i) => `<section class="slide" data-i="${i}">
      <img class="poster" alt="" style="position:absolute;inset:0;width:100%;height:100%;object-fit:contain">
      <div class="info"><div class="score"><small>SCORE</small><b>${c.score}</b></div>
        <b>${esc(details(c).title)}</b>
        <div class="tags">${esc(details(c).tags)}</div></div>
      <div class="actions">
        <button class="act wait" data-a="yt"><span class="ic yt"><svg viewBox="0 0 24 24"><path d="M10 8.5v7l5.5-3.5z" fill="#fff" stroke="none"/></svg></span>YouTube</button>
        <button class="act wait" data-a="save"><span class="ic"><svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 19h14"/></svg></span>Save</button>
        <button class="act" data-a="tags"><span class="ic"><svg viewBox="0 0 24 24"><path d="M9 4 7 20M17 4l-2 16M4 9h16M3 15h16"/></svg></span>Hashtags</button>
        <button class="act" data-a="info"><span class="ic"><svg viewBox="0 0 24 24"><path d="M5 6h14M5 12h14M5 18h9"/></svg></span>Details</button>
      </div></section>`).join('');
  current = { res, clips };
  $('#player').classList.remove('hidden');
  clips.forEach(async (c, i) => { if (c.thumb) slides.children[i].querySelector('.poster').src = await mediaUrl(c.thumb); });
  observer?.disconnect();
  observer = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting && e.intersectionRatio > 0.6) activate(+e.target.dataset.i); }), { threshold: [0.6] });
  [...slides.children].forEach(s => observer.observe(s));
  slides.scrollTop = slides.clientHeight * start;
  activate(start, true);
  slides.onclick = onAction;
}
async function activate(i, first) {
  if (!current || current.i === i && !first) return;
  current.i = i;
  const c = current.clips[i], slide = $('#slides').children[i];
  slide.insertBefore(video, slide.firstChild);
  const url = await mediaUrl(c.file);
  if (current.i !== i) return;
  video.src = url; video.muted = false;
  video.play().catch(() => { video.muted = true; video.play().catch(() => { }); showSound(); });
  prepare(c, slide);
  if (current.clips[i + 1]) prepare(current.clips[i + 1], $('#slides').children[i + 1]);
}
function showSound() {
  let b = $('#player .sound');
  if (!b) { b = document.createElement('button'); b.className = 'sound'; b.textContent = '🔇 Tap for sound'; $('#player').appendChild(b); }
  b.onclick = () => { video.muted = false; video.play(); b.remove(); };
}
async function prepare(c, slide) {          // download the clip ahead, so Share opens instantly when tapped
  if (blobs.has(c.file)) return ready(slide);
  try {
    const r = await fetch(await mediaUrl(c.file)); const b = await r.blob();
    blobs.set(c.file, b); ready(slide);
    if (blobs.size > 6) blobs.delete(blobs.keys().next().value);
  } catch { }
}
function ready(slide) { slide?.querySelectorAll('.act.wait').forEach(b => b.classList.remove('wait')); }

async function copy(text, label) {
  try { await navigator.clipboard.writeText(text); toast(`${label} copied`); } catch { toast("Couldn't copy"); }
}
function onAction(e) {
  const b = e.target.closest('.act'); if (!b) return;
  const i = +b.closest('.slide').dataset.i, c = current.clips[i], d = details(c);
  const a = b.dataset.a;
  if (a === 'tags') return copy(d.tags, 'Hashtags');
  if (a === 'info') return openSheet(c);
  const blob = blobs.get(c.file);
  if (!blob) return toast('Getting the clip ready, try again in a second');
  const name = (d.title || 'clip').replace(/[^\w\- ]+/g, '').slice(0, 50).trim() + '.mp4';
  const file = new File([blob], name || 'clip.mp4', { type: 'video/mp4' });
  if (a === 'yt') navigator.clipboard?.writeText(`${d.title}\n\n${d.desc}\n\n${d.tags}`).catch(() => { });
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    navigator.share({ files: [file] }).then(() => {
      if (a === 'yt') toast('Title, description and hashtags are copied — paste them in YouTube', 4500);
    }).catch(() => { });
    if (a === 'yt') toast('Pick YouTube in the share menu. Details are copied.', 3500);
    else toast('Pick "Save Video" to put it in Photos', 3000);
  } else {
    window.open(URL.createObjectURL(blob), '_blank');
    toast('Long-press the video to save it', 3500);
  }
}
function closePlayer() {
  video.pause(); video.removeAttribute('src'); video.load();
  observer?.disconnect(); current = null;
  $('#player').classList.add('hidden'); $('#player .sound')?.remove();
}

function openSheet(c) {
  const d = details(c);
  $('#shTitle').value = d.title; $('#shDesc').value = d.desc; $('#shTags').value = d.tags;
  $('#sheetAi').textContent = c.yt?.ai ? `· by ${c.yt.ai.split(' (')[0]}` : '';
  $('#sheet').classList.remove('hidden');
  const save = () => store.set('edit.' + c.file, { title: $('#shTitle').value, desc: $('#shDesc').value, tags: $('#shTags').value });
  ['#shTitle', '#shDesc', '#shTags'].forEach(s => $(s).oninput = save);
  $('#copyTags').onclick = () => copy($('#shTags').value, 'Hashtags');
  $('#copyAll').onclick = () => copy(`${$('#shTitle').value}\n\n${$('#shDesc').value}\n\n${$('#shTags').value}`, 'Title, description and hashtags');
}

// tabs, settings, start -------------------------------------------------------------------------------------
function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('hidden', t.id !== 'tab-' + name));
  document.querySelectorAll('.tabbar button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  window.scrollTo(0, 0);
  if (name === 'clips') { drawJobs(); loadIndex().then(drawJobs).catch(e => toast(e.message)); }
  if (name === 'settings') checkStatus();
}
async function checkStatus() {
  $('#setRepo').textContent = `${cfg.o}/${cfg.r}`; $('#setStatus').textContent = 'Checking...';
  try { await gh(repo()); $('#setStatus').textContent = '✓ Connected'; }
  catch (e) { $('#setStatus').textContent = e.message; }
}
function showApp() {
  $('#connect').classList.add('hidden'); $('#app').classList.remove('hidden');
  drawOptions(); drawActive(); showTab('create');
  loadIndex().then(drawActive).catch(e => { $('#cloudState').textContent = 'offline'; toast(e.message, 4000); });
  tick();
}
function showConnect() { $('#app').classList.add('hidden'); $('#connect').classList.remove('hidden'); }

document.addEventListener('DOMContentLoaded', () => {
  $('#version').textContent = `ClipIn phone ${VERSION}`;
  $('#connectBtn').onclick = connect;
  wireOptions();
  document.querySelectorAll('.tabbar button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
  $('#refreshBtn').onclick = () => loadIndex().then(drawJobs).then(() => toast('Up to date')).catch(e => toast(e.message));
  $('#reconnectBtn').onclick = () => { $('#code').value = ''; showConnect(); };
  $('#updateBtn').onclick = async () => {
    const reg = await navigator.serviceWorker?.getRegistration(); await reg?.update();
    toast('Updating...'); setTimeout(() => location.reload(), 800);
  };
  $('#closePlayer').onclick = closePlayer;
  $('#closeSheet').onclick = () => $('#sheet').classList.add('hidden');
  $('#sheet').onclick = e => { if (e.target.id === 'sheet') $('#sheet').classList.add('hidden'); };
  document.addEventListener('visibilitychange', () => { if (!document.hidden && cfg) tick(); });
  const code = new URLSearchParams(location.hash.slice(1)).get('code');   // links like ...#code=clipin1.xxx
  if (code) { $('#code').value = code; history.replaceState(null, '', location.pathname); }
  if (cfg) showApp(); else showConnect();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => { });
});
