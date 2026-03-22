const API_BASE = typeof __NOETIX_API_BASE__ !== 'undefined' ? __NOETIX_API_BASE__ : 'http://127.0.0.1:8788';
const LS_KEY = 'noetix.chatlite.v2';

const state = {
  tab: 'chat',
  kbs: [],
  sessions: [],
  activeSessionId: null,
  activeKbIds: [],
  messages: [],
  library: [],
  filters: { q: '', author: '', hasKb: '', org: '', course: '', tag: '' },
  inputQueue: [],
  thinkingDepth: 'xhigh',
  filterOptions: null,
  kbDocs: {},
  kbDeps: {},
  models: [],
  activeModel: null,
};

const $ = (id) => document.getElementById(id);
const esc = (s = '') => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');

async function api(path, opts = {}) {
  const r = await fetch(`${API_BASE}${path}`, opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j?.message || j?.detail || `HTTP ${r.status}`);
  return j;
}

function persist() {
  localStorage.setItem(
    LS_KEY,
    JSON.stringify({
      tab: state.tab,
      activeSessionId: state.activeSessionId,
      activeKbIds: state.activeKbIds,
      inputQueueLibraryIds: state.inputQueue.filter(e => e.type === 'library').map(e => e.id),
      thinkingDepth: state.thinkingDepth
    })
  );
}

function hydrate() {
  try {
    const x = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
    state.tab = x.tab || 'chat';
    state.activeSessionId = x.activeSessionId || null;
    state.activeKbIds = Array.isArray(x.activeKbIds) ? x.activeKbIds : (x.activeKbId ? [x.activeKbId] : []);
    state.thinkingDepth = x.thinkingDepth || 'xhigh';
    state._pendingQueueIds = x.inputQueueLibraryIds || [];
  } catch {}
}

function css() {
  return `
  <style>
    :root { color-scheme: dark; }
    body{margin:0;font-family:system-ui;background:#0b1020;color:#e9eefc}
    .top{padding:10px 12px;border-bottom:1px solid #22355f;background:#101834;display:flex;gap:8px;position:sticky;top:0;z-index:5}
    .top button{background:#1c2f5a;color:#fff;border:1px solid #2f4676;border-radius:8px;padding:6px 10px;cursor:pointer}
    .top button.active{background:#2f56ad}
    .wrap{padding:10px;height:calc(100vh - 58px);box-sizing:border-box}

    .chat{display:grid;grid-template-columns:260px 1fr;gap:10px;height:100%}
    .panel{border:1px solid #233b6b;border-radius:10px;background:#101a34;overflow:hidden;min-height:0}
    .panel-head{padding:8px;border-bottom:1px solid #233b6b;display:flex;gap:6px;align-items:center}
    .panel-body{padding:8px;overflow:auto;height:100%;min-height:0}

    .thread-row{display:grid;grid-template-columns:1fr auto;gap:6px;margin-bottom:6px}
    .threads button{width:100%;text-align:left;background:#15264d;border:1px solid #2a4373;color:#fff;border-radius:8px;padding:7px;cursor:pointer}
    .threads button.active{background:#2f56ad}
    .threads .del{padding:0 8px;background:#5a2230;border-color:#7c3040}

    .chat-main{display:flex;flex-direction:column;height:100%;min-height:0}
    .msgs{flex:1;overflow:auto;padding:8px;min-height:0}
    .msg{padding:7px 8px;border-radius:8px;margin-bottom:6px;white-space:pre-wrap;max-width:85%}
    .u{background:#1a3a6a;margin-left:auto;text-align:right;color:#d4e4ff}
    .a{background:transparent;padding-left:0;margin-right:auto}
    .e{background:#5a2230}
    .composer{padding:8px;border-top:1px solid #233b6b;display:grid;grid-template-columns:1fr auto;gap:8px;flex:0 0 auto;background:#101a34}

    input,select,textarea{background:#0e1834;border:1px solid #2e487a;color:#fff;border-radius:8px;padding:7px}
    textarea{resize:none;min-height:42px;max-height:120px}

    .library-top{display:grid;grid-template-columns:1fr 1fr 160px auto;gap:8px;margin-bottom:10px}
    .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px;overflow:auto;max-height:calc(100vh - 170px)}
    .tile{border:1px solid #284377;border-radius:10px;background:#0f1a36;padding:10px}
    .tile h4{margin:0 0 6px 0;font-size:15px;line-height:1.3}
    .meta{font-size:12px;color:#abc1f3;line-height:1.4}
    .tile .actions{display:flex;gap:6px;margin-top:8px}
    .tile button{background:#1c2f5a;color:#fff;border:1px solid #2f4676;border-radius:8px;padding:5px 8px;cursor:pointer;font-size:12px}
    .tile button.primary{background:#2f56ad}

    .left-nav{display:flex;flex-direction:column}
    .left-nav .panel-body.threads{flex:1;overflow:auto}
    .thinking-strip{padding:6px 8px;border-top:1px solid #233b6b;font-size:11px;color:#7ea8d4;line-height:1.4;max-height:60px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;flex:0 0 auto}
    .thinking-strip .status{color:#5bc48a}
    .thinking-strip .idle{color:#556688}
    .input-box{max-width:800px}
    .tile button.danger{background:#5a1c1c;border-color:#764040}
    .tile button.danger:hover{background:#7a2020}
    .tile .output-files{font-size:12px;color:#8ab4e8;margin:4px 0}
    .tile .output-files a{color:#6db3f8;cursor:pointer;text-decoration:underline}
    .tile .output-files .del-output{color:#aa4444;cursor:pointer;font-size:11px;margin-left:2px;border:none;background:none;padding:0 2px}
    .tile .output-files .del-output:hover{color:#ff4444}
    .tile.orphan{border-color:#7a6a20;background:#1a1800}
    .kb-multi-select{position:relative;display:inline-block}
    .kb-multi-select .kb-toggle{font-size:12px;padding:4px 8px;cursor:pointer;background:#1a2744;color:#ccc;border:1px solid #233b6b;border-radius:4px}
    .kb-multi-select .kb-dropdown{position:absolute;top:100%;left:0;z-index:10;background:#101828;border:1px solid #233b6b;border-radius:4px;padding:4px;min-width:200px;max-height:220px;overflow-y:auto}
    .kb-multi-select .kb-opt{display:block;padding:3px 6px;font-size:12px;color:#ccc;cursor:pointer;white-space:nowrap}
    .kb-multi-select .kb-opt:hover{background:#1a2744}
    .kb-multi-select .kb-opt input{margin-right:6px}
    .kb-deps{margin-top:6px;padding-top:6px;border-top:1px solid #233b6b}
    .kb-deps label{display:block;font-size:11px;padding:2px 4px;color:#8899aa;cursor:pointer}
    .kb-deps label:hover{color:#ccc}
    .kb-deps label input{margin-right:4px}
    .tile .orphan-badge{display:inline-block;background:#5a4a10;color:#e8d44d;font-size:10px;padding:1px 6px;border-radius:4px;margin-left:6px}
    .tile .tags{font-size:11px;color:#9bb7ff;margin:2px 0}
    .tile .tags span{background:#1a2a50;border:1px solid #2f4676;border-radius:4px;padding:1px 5px;margin-right:4px;display:inline-block;margin-bottom:2px}
    .queue-item{display:flex;align-items:center;gap:8px;padding:6px 8px;margin:3px 0;background:#15264d;border:1px solid #2a4373;border-radius:6px;cursor:grab}
    .queue-item.dragging{opacity:0.4}
    .queue-item .grip{color:#556;cursor:grab;user-select:none}
    .queue-item .name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .queue-item .remove{background:none;border:none;color:#aa6666;cursor:pointer;font-size:16px;padding:0 4px}
    .hint{font-size:12px;color:#9bb7ff}
    .modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:1000}
    .modal{background:#0f1a36;border:1px solid #284377;border-radius:12px;padding:20px;width:440px;max-height:80vh;overflow-y:auto}
    .modal h3{margin:0 0 16px 0}
    .modal label{display:block;font-size:12px;color:#abc1f3;margin:10px 0 4px}
    .modal input,.modal select{width:100%;box-sizing:border-box;padding:6px 8px;background:#1c2f5a;color:#fff;border:1px solid #2f4676;border-radius:6px;font-size:13px}
    .modal .modal-actions{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}
    .modal button{background:#1c2f5a;color:#fff;border:1px solid #2f4676;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:13px}
    .modal button.primary{background:#2f56ad}
    .modal button.cancel{background:transparent;border-color:#2f4676}
    .jobs-list{display:flex;flex-direction:column;gap:8px;overflow:auto;max-height:calc(100vh - 120px)}
    .job-card{border:1px solid #284377;border-radius:10px;background:#0f1a36;padding:12px}
    .job-card h4{margin:0 0 4px 0;font-size:14px}
    .job-card .job-meta{font-size:12px;color:#abc1f3;line-height:1.5}
    .job-card .progress-bar{height:6px;background:#1c2f5a;border-radius:3px;margin:6px 0}
    .job-card .progress-fill{height:100%;background:#2f56ad;border-radius:3px;transition:width 0.3s}
    .job-card .status-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
    .job-card .status-badge.completed{background:#1a3a2a;color:#5bc48a}
    .job-card .status-badge.failed{background:#3a1a1a;color:#e05050}
    .job-card .status-badge.running{background:#1a2a4a;color:#6db3f8}
    .job-card .status-badge.pending{background:#2a2a1a;color:#d4a64a}
  </style>`;
}

function renderShell() {
  $('app').innerHTML = `${css()}
  <div class="top">
    <button id="tab-chat">Chat</button>
    <button id="tab-input">Input</button>
    <button id="tab-library">Library</button>
    <button id="tab-kb">KB</button>
    <button id="tab-jobs">Jobs</button>
    <span style="margin-left:auto;font-weight:600;font-size:25px;color:#7ea8d4;align-self:center">Noetix</span>
  </div>
  <div class="wrap" id="view"></div>`;

  $('tab-chat').classList.toggle('active', state.tab === 'chat');
  $('tab-input').classList.toggle('active', state.tab === 'input');
  $('tab-library').classList.toggle('active', state.tab === 'library');
  $('tab-kb').classList.toggle('active', state.tab === 'kb');
  $('tab-jobs').classList.toggle('active', state.tab === 'jobs');

  $('tab-chat').onclick = () => switchTab('chat');
  $('tab-input').onclick = () => switchTab('input');
  $('tab-library').onclick = () => switchTab('library');
  $('tab-kb').onclick = () => switchTab('kb');
  $('tab-jobs').onclick = () => switchTab('jobs');
}

function switchTab(tab) {
  if (_jobsPollTimer) { clearInterval(_jobsPollTimer); _jobsPollTimer = null; }
  state.tab = tab;
  persist();
  renderShell();
  if (tab === 'chat') renderChat();
  else if (tab === 'input') renderInput();
  else if (tab === 'kb') renderKb();
  else if (tab === 'jobs') renderJobs();
  else renderLibrary();
}

function renderChat() {
  $('view').innerHTML = `
  <div class="chat">
    <div class="panel left-nav">
      <div class="panel-head"><button id="new-thread">+ New</button></div>
      <div class="panel-body threads" id="threads"></div>
      <div class="thinking-strip" id="thinking"><span class="idle">Idle</span></div>
    </div>

    <div class="panel chat-main">
      <div class="panel-head">
        <select id="model-select" title="Model" style="max-width:220px;font-size:12px;padding:4px"></select>
        <select id="thinking-select" title="Thinking depth" style="width:70px;font-size:12px;padding:4px"></select>
        <div id="kb-select" class="kb-multi-select"></div>
        <span class="hint">Enter to send • Shift+Enter newline</span>
      </div>
      <div class="msgs" id="msgs"></div>
      <div class="composer">
        <textarea id="chat-input" placeholder="Message"></textarea>
        <button id="send">Send</button>
      </div>
    </div>
  </div>`;

  $('new-thread').onclick = createThread;
  $('send').onclick = sendMessage;

  // Thinking depth selector
  const THINKING_LEVELS = [
    { value: 'xhigh', label: 'Max' },
    { value: 'high', label: 'High' },
    { value: 'medium', label: 'Med' }
  ];
  $('thinking-select').innerHTML = THINKING_LEVELS.map(l =>
    `<option value="${l.value}" ${l.value === state.thinkingDepth ? 'selected' : ''}>${l.label}</option>`
  ).join('');
  $('thinking-select').onchange = (e) => {
    state.thinkingDepth = e.target.value;
    persist();
  };

  // Model selector
  drawModelSelect();

  // KB multi-select handled by drawKbSelect()

  const input = $('chat-input');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  drawKbSelect();
  drawThreads();
  drawMessages();
}

function drawModelSelect() {
  const el = $('model-select');
  if (!el) return;

  // Group models by provider
  const grouped = {};
  for (const m of state.models) {
    if (!grouped[m.provider]) grouped[m.provider] = [];
    grouped[m.provider].push(m);
  }

  const activeKey = state.activeModel ? `${state.activeModel.provider}/${state.activeModel.model}` : '';

  let html = '';
  for (const [provider, models] of Object.entries(grouped)) {
    html += `<optgroup label="${esc(provider)}">`;
    for (const m of models) {
      const key = `${m.provider}/${m.model}`;
      const short = m.model.replace(/-202505\d\d$/, '').replace(/-202510\d\d$/, '');
      html += `<option value="${esc(key)}" ${key === activeKey ? 'selected' : ''}>${esc(short)}</option>`;
    }
    html += `</optgroup>`;
  }

  if (!html) html = `<option disabled>No models available</option>`;
  el.innerHTML = html;

  el.onchange = async (e) => {
    const [provider, ...rest] = e.target.value.split('/');
    const model = rest.join('/');
    el.disabled = true;

    try {
      await api('/v1/models/select', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ provider, model }),
      });
      state.activeModel = { provider, model };
    } catch (err) {
      alert(`Failed to switch model: ${err.message}`);
      // Revert selection
      el.value = activeKey;
    }
    el.disabled = false;
  };
}

function drawKbSelect() {
  const el = $('kb-select');
  if (!el) return;
  const selected = new Set(state.activeKbIds);
  const label = selected.size === 0 ? 'No KB' : selected.size === state.kbs.length ? 'All KBs' : `${selected.size} KB${selected.size > 1 ? 's' : ''}`;
  el.innerHTML = `<button class="kb-toggle">${esc(label)} ▾</button>
    <div class="kb-dropdown" style="display:none">${state.kbs.map(k =>
      `<label class="kb-opt"><input type="checkbox" value="${k.id}" ${selected.has(k.id) ? 'checked' : ''}> ${esc(k.name)}</label>`
    ).join('')}</div>`;

  const toggle = el.querySelector('.kb-toggle');
  const dropdown = el.querySelector('.kb-dropdown');
  toggle.onclick = () => { dropdown.style.display = dropdown.style.display === 'none' ? 'block' : 'none'; };
  el.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.onchange = () => {
      state.activeKbIds = [...el.querySelectorAll('input:checked')].map(c => c.value);
      toggle.textContent = (state.activeKbIds.length === 0 ? 'No KB' : state.activeKbIds.length === state.kbs.length ? 'All KBs' : `${state.activeKbIds.length} KB${state.activeKbIds.length > 1 ? 's' : ''}`) + ' ▾';
      persist();
    };
  });
  // Close on outside click
  document.addEventListener('click', (e) => { if (!el.contains(e.target)) dropdown.style.display = 'none'; }, { once: false });
}

function drawThreads() {
  $('threads').innerHTML = state.sessions
    .slice(0, 120)
    .map(
      (s) => `<div class="thread-row">
        <button data-id="${s.id}" class="thread-open ${s.id === state.activeSessionId ? 'active' : ''}">${esc(s.title)}</button>
        <button class="del" data-del="${s.id}" title="Delete thread">×</button>
      </div>`
    )
    .join('');

  [...$('threads').querySelectorAll('.thread-open')].forEach((b) => {
    b.onclick = async () => {
      state.activeSessionId = b.dataset.id;
      persist();
      state.messages = (await api(`/v1/chat/sessions/${state.activeSessionId}/messages`)).items || [];
      drawThreads();
      drawMessages();
    };
  });

  [...$('threads').querySelectorAll('[data-del]')].forEach((b) => {
    b.onclick = async () => {
      const id = b.dataset.del;
      if (!confirm('Delete this chat thread?')) return;
      try {
        await api(`/v1/chat/sessions/${id}`, { method: 'DELETE' });
        state.sessions = state.sessions.filter((s) => s.id !== id);
        if (state.activeSessionId === id) {
          state.activeSessionId = state.sessions[0]?.id || null;
          state.messages = state.activeSessionId
            ? (await api(`/v1/chat/sessions/${state.activeSessionId}/messages`)).items || []
            : [];
        }
        persist();
        drawThreads();
        drawMessages();
      } catch (e) {
        alert(`Delete failed: ${e.message}`);
      }
    };
  });
}

function drawMessages() {
  const box = $('msgs');
  box.innerHTML = state.messages
    .slice(-80)
    .map((m) => `<div class="msg ${m.role === 'assistant' ? 'a' : 'u'}">${esc(m.content || '')}</div>`)
    .join('');
  box.scrollTop = box.scrollHeight;
}

async function createThread() {
  const title = prompt('Thread title', `Thread ${state.sessions.length + 1}`)?.trim();
  if (!title) return;
  const s = await api('/v1/chat/sessions', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ title, default_kb_scope: { mode: 'selected', kb_ids: state.activeKbIds } })
  });
  state.sessions.unshift(s);
  state.activeSessionId = s.id;
  state.messages = [];
  persist();
  drawThreads();
  drawMessages();
}

async function sendMessage() {
  if (!state.activeSessionId) return alert('Create/select a thread first');

  const input = $('chat-input');
  const content = input.value.trim();
  if (!content) return;

  input.value = '';
  state.messages.push({ role: 'user', content });
  drawMessages();

  const sendBtn = $('send');
  if (sendBtn) sendBtn.disabled = true;
  const thinking = $('thinking');
  if (thinking) thinking.innerHTML = '<span class="status">Starting...</span>';

  try {
    const response = await fetch(`${API_BASE}/v1/chat/sessions/${state.activeSessionId}/messages`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'accept': 'text/event-stream' },
      body: JSON.stringify({ content, effort: state.thinkingDepth, kb_scope: state.activeKbIds.length ? { mode: 'selected', kb_ids: state.activeKbIds, top_k: 8 } : { mode: 'none' } })
    });

    const ct = response.headers.get('content-type') || '';

    if (ct.includes('text/event-stream')) {
      // SSE streaming mode
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Parse SSE events from buffer
        const lines = buffer.split('\n');
        buffer = lines.pop(); // keep incomplete last line in buffer

        let currentEvent = '';
        let currentData = '';
        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7);
          } else if (line.startsWith('data: ')) {
            currentData = line.slice(6);
          } else if (line === '' && currentEvent && currentData) {
            // Complete SSE event
            let parsed;
            try { parsed = JSON.parse(currentData); } catch { parsed = currentData; }
            handleSSEEvent(currentEvent, parsed);
            currentEvent = '';
            currentData = '';
          }
        }
      }
    } else {
      // JSON fallback (non-SSE)
      const r = await response.json();
      if (!response.ok) throw new Error(r?.message || r?.detail || `HTTP ${response.status}`);
      state.messages.push(r.assistant_message || { role: 'assistant', content: '(empty)' });
      drawMessages();
    }
  } catch (e) {
    const msg = e.message || '';
    if (/credentials|apiKey|api_key|OPENAI_API_KEY|ANTHROPIC_API_KEY|auth.*token|401/i.test(msg)) {
      // Auth failure — show login overlay
      try {
        const auth = await api('/v1/auth/status');
        showLoginOverlay(auth.provider, auth.method);
      } catch {
        showLoginOverlay('openai', 'oauth');
      }
      return;
    }
    state.messages.push({ role: 'assistant', content: `Error: ${msg}` });
    drawMessages();
  }

  if (sendBtn) sendBtn.disabled = false;
  if (thinking) thinking.innerHTML = '<span class="idle">Idle</span>';
}

function handleSSEEvent(event, data) {
  const thinking = $('thinking');
  switch (event) {
    case 'thinking':
      if (thinking) thinking.innerHTML = esc(data.delta || '');
      break;
    case 'status':
      if (thinking) thinking.innerHTML = `<span class="status">${esc(data.text || '')}</span>`;
      break;
    case 'text':
      if (thinking) thinking.innerHTML = '<span class="status">Responding...</span>';
      break;
    case 'done':
      state.messages.push(data.assistant_message || { role: 'assistant', content: '(empty)' });
      drawMessages();
      if (thinking) thinking.innerHTML = '<span class="idle">Idle</span>';
      break;
    case 'error':
      state.messages.push({ role: 'assistant', content: `Error: ${data.message || 'Unknown error'}` });
      drawMessages();
      if (thinking) thinking.innerHTML = '<span class="idle">Idle</span>';
      break;
  }
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

function renderInput() {
  const queue = state.inputQueue;
  const count = queue.length;
  const ALL_FORMATS = [
    { key: 'searchable_pdf', label: 'Searchable PDF' },
    { key: 'combined_pdf',   label: 'Combined PDF' },
    { key: 'm4b',            label: 'M4B Audiobook' },
    { key: 'knowledge_base', label: 'Knowledge Base' }
  ];

  const queueHtml = count === 0
    ? `<div class="hint" style="padding:12px 0">No items queued. Use "Use as Input" from Library tab or add files below.</div>`
    : queue.map((entry, i) => {
        const label = entry.type === 'library'
          ? `${esc(entry.title)}` : `${esc(entry.name)} (${formatFileSize(entry.size)})`;
        const badge = entry.type === 'library' ? 'library' : 'file';
        return `<div class="queue-item" draggable="true" data-idx="${i}">
          <span class="grip">☰</span>
          <span style="font-size:11px;color:#7ea8d4;min-width:48px">${badge}</span>
          <span class="name">${label}</span>
          <button class="remove" data-remove="${i}" title="Remove">×</button>
        </div>`;
      }).join('');

  const checkboxesHtml = ALL_FORMATS.map(f => {
    return `<label style="display:block;margin:4px 0">
      <input type="checkbox" name="out" value="${f.key}" />
      ${f.label}
    </label>`;
  }).join('');

  const kbOptions = state.kbs.map(k =>
    `<option value="${k.id}" ${state.activeKbIds.includes(k.id) ? 'selected' : ''}>${esc(k.name)}</option>`
  ).join('');

  $('view').innerHTML = `
  <div class="panel input-box">
    <div class="panel-head">
      Input Queue${count ? ` (${count} item${count > 1 ? 's' : ''})` : ''}
      ${count ? '<button id="clear-all" style="margin-left:auto;font-size:11px;padding:3px 8px">Clear All</button>' : ''}
    </div>
    <div class="panel-body">
      <div id="queue-list">${queueHtml}</div>
      <div style="margin:10px 0">
        <input type="file" id="file-input" multiple accept=".pdf,.mp3,.m4b,.m4a,.mp4,.mkv,.webm,.zip" style="display:none" />
        <button id="add-files">+ Add Files</button>
      </div>
      <div style="margin:10px 0;border-top:1px solid #233b6b;padding-top:10px">
        <b>Output formats:</b>
        ${checkboxesHtml}
      </div>
      <div id="kb-row" style="display:none;margin:6px 0"><label>Target KB: <select id="ingest-kb">${kbOptions}</select></label></div>
      <button id="run-ingest" disabled>Process ${count} Item${count !== 1 ? 's' : ''}</button>
      <div id="ingest-status" class="hint" style="margin-top:8px"></div>
    </div>
  </div>`;

  // Clear All
  if ($('clear-all')) {
    $('clear-all').onclick = () => {
      state.inputQueue = [];
      persist();
      renderInput();
    };
  }

  // Add Files button
  $('add-files').onclick = () => $('file-input').click();
  $('file-input').onchange = (e) => {
    for (const file of e.target.files) {
      state.inputQueue.push({ type: 'file', file, name: file.name, size: file.size });
    }
    persist();
    renderInput();
  };

  // Remove buttons
  [...document.querySelectorAll('.remove')].forEach(b => {
    b.onclick = () => {
      state.inputQueue.splice(parseInt(b.dataset.remove), 1);
      persist();
      renderInput();
    };
  });

  // Drag-and-drop reorder
  let dragIdx = null;
  [...document.querySelectorAll('.queue-item')].forEach(el => {
    el.addEventListener('dragstart', (e) => {
      dragIdx = parseInt(el.dataset.idx);
      el.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });
    el.addEventListener('dragend', () => {
      el.classList.remove('dragging');
      dragIdx = null;
    });
    el.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
    });
    el.addEventListener('drop', (e) => {
      e.preventDefault();
      const dropIdx = parseInt(el.dataset.idx);
      if (dragIdx !== null && dragIdx !== dropIdx) {
        const [moved] = state.inputQueue.splice(dragIdx, 1);
        state.inputQueue.splice(dropIdx, 0, moved);
        persist();
        renderInput();
      }
    });
  });

  // Output format checkboxes
  const checkboxes = [...document.querySelectorAll('input[name="out"]')];
  const btn = $('run-ingest');
  const kbRow = $('kb-row');

  const updateFormState = () => {
    const anyChecked = checkboxes.some(c => c.checked);
    btn.disabled = !anyChecked || count === 0;
    const kbChecked = checkboxes.find(c => c.value === 'knowledge_base')?.checked;
    if (kbRow) kbRow.style.display = kbChecked ? 'block' : 'none';
  };
  checkboxes.forEach(c => c.addEventListener('change', updateFormState));
  updateFormState();

  // Submit
  btn.addEventListener('click', async () => {
    const status = $('ingest-status');
    const checked = checkboxes.filter(c => c.checked).map(c => c.value);
    if (!checked.length || !queue.length) return;

    btn.disabled = true;
    const outputs = checked.join(',');
    const kbId = checked.includes('knowledge_base') ? ($('ingest-kb')?.value || state.activeKbIds[0]) : null;

    // Split into file uploads and library items
    const fileEntries = queue.filter(e => e.type === 'file');
    const libraryEntries = queue.filter(e => e.type === 'library');
    const totalJobs = (fileEntries.length ? 1 : 0) + libraryEntries.length;
    let jobNum = 0;
    const results = [];

    try {
      // Single job for all uploaded files
      if (fileEntries.length) {
        jobNum++;
        status.textContent = `Submitting job ${jobNum}/${totalJobs} (${fileEntries.length} file${fileEntries.length > 1 ? 's' : ''})...`;
        const fd = new FormData();
        fd.append('source_type', 'upload');
        fd.append('outputs', outputs);
        if (kbId) fd.append('kb_id', kbId);
        for (const entry of fileEntries) fd.append('files', entry.file);
        const r = await fetch(`${API_BASE}/v1/ingest/jobs`, { method: 'POST', body: fd });
        const j = await r.json();
        if (!r.ok) throw new Error(j?.message || `HTTP ${r.status}`);
        results.push(j.job_id || '(no id)');
      }

      // One job per library item
      for (const entry of libraryEntries) {
        jobNum++;
        status.textContent = `Submitting job ${jobNum}/${totalJobs} ("${entry.title}")...`;
        const fd = new FormData();
        fd.append('source_type', 'library_pdf');
        fd.append('library_id', entry.id);
        fd.append('outputs', outputs);
        if (kbId) fd.append('kb_id', kbId);
        const r = await fetch(`${API_BASE}/v1/ingest/jobs`, { method: 'POST', body: fd });
        const j = await r.json();
        if (!r.ok) throw new Error(j?.message || `HTTP ${r.status}`);
        results.push(j.job_id || '(no id)');
      }

      state.inputQueue = [];
      persist();
      renderInput();
      // Re-acquire status element after re-render
      const newStatus = $('ingest-status');
      if (newStatus) newStatus.textContent = `All ${totalJobs} job${totalJobs > 1 ? 's' : ''} submitted: ${results.join(', ')}`;
      return;
    } catch (e) {
      status.textContent = `Error on job ${jobNum}: ${e.message}`;
    }
    btn.disabled = false;
  });
}

let _jobsPollTimer = null;

async function renderJobs() {
  if (_jobsPollTimer) { clearInterval(_jobsPollTimer); _jobsPollTimer = null; }

  $('view').innerHTML = `
    <div class="input-box" style="max-width:900px">
      <h3 style="margin:0 0 12px">Jobs</h3>
      <div id="jobs-list" class="jobs-list"><span class="hint">Loading...</span></div>
    </div>`;

  async function refreshJobs() {
    try {
      const r = await fetch(`${API_BASE}/v1/ingest/jobs`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const jobs = await r.json();
      const container = $('jobs-list');
      if (!container) return;

      if (!jobs.length) {
        container.innerHTML = '<span class="hint">No jobs yet. Submit files from the Input tab.</span>';
        return;
      }

      container.innerHTML = jobs.map(j => {
        const pct = Math.round(j.progress || 0);
        const statusClass = j.status === 'completed' ? 'completed'
          : j.status === 'failed' ? 'failed'
          : ['pending','queued'].includes(j.status) ? 'pending'
          : 'running';
        const outputs = (j.requested_outputs || []).join(', ') || '\u2014';
        const outputLinks = j.status === 'completed' && j.output_files
          ? Object.keys(j.output_files).map(k =>
              `<a href="${API_BASE}/v1/ingest/jobs/${j.job_id}/download?output_type=${k}" target="_blank">${k}</a>`
            ).join(' \u00b7 ')
          : '';

        return `<div class="job-card">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <h4>${esc(j.title || j.job_id)}</h4>
            <span class="status-badge ${statusClass}">${j.status}</span>
          </div>
          <div class="job-meta">
            ${j.author ? esc(j.author) + ' \u00b7 ' : ''}Outputs: ${esc(outputs)}
            ${j.source_type ? ' \u00b7 ' + esc(j.source_type) : ''}
          </div>
          ${j.status !== 'completed' && j.status !== 'failed' ? `
            <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
            <div class="job-meta">${pct}% \u2014 ${esc(j.current_task || '')}</div>
          ` : ''}
          ${j.error ? `<div style="color:#e05050;font-size:12px;margin-top:4px">${esc(j.error)}</div>` : ''}
          ${outputLinks ? `<div class="output-files" style="margin-top:6px">${outputLinks}</div>` : ''}
        </div>`;
      }).join('');
    } catch (e) {
      const container = $('jobs-list');
      if (container) container.innerHTML = `<span class="hint" style="color:#e05050">Failed to load jobs: ${esc(e.message)}</span>`;
    }
  }

  await refreshJobs();

  _jobsPollTimer = setInterval(() => {
    if (state.tab !== 'jobs') { clearInterval(_jobsPollTimer); _jobsPollTimer = null; return; }
    refreshJobs();
  }, 3000);
}

async function loadFilterOptions() {
  if (!state.filterOptions) {
    try { state.filterOptions = await api('/v1/library/filters'); } catch { state.filterOptions = { organizations: [], course_codes: [], custom_tags: [] }; }
  }
  return state.filterOptions;
}

async function showEditModal(item) {
  const filters = await loadFilterOptions();
  const tags = item.tags || {};

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <h3>Edit Attributes</h3>
      <label>Title</label>
      <input id="ed-title" value="${esc(item.title || '')}" />
      <label>Author</label>
      <input id="ed-author" value="${esc(item.author || '')}" />
      <label>Organization</label>
      <input id="ed-org" list="dl-orgs" value="${esc(tags.organization || '')}" />
      <datalist id="dl-orgs">${(filters.organizations || []).map(o => `<option value="${esc(o)}">`).join('')}</datalist>
      <label>Course Code</label>
      <input id="ed-course" list="dl-courses" value="${esc(tags.course_code || '')}" />
      <datalist id="dl-courses">${(filters.course_codes || []).map(c => `<option value="${esc(c)}">`).join('')}</datalist>
      <label>Course ID</label>
      <input id="ed-courseid" value="${esc(tags.course_id || '')}" />
      <label>Module ID</label>
      <input id="ed-moduleid" value="${esc(tags.module_id || '')}" />
      <label>Custom Tags (comma-separated)</label>
      <input id="ed-tags" list="dl-tags" value="${esc((tags.custom_tags || []).join(', '))}" />
      <datalist id="dl-tags">${(filters.custom_tags || []).map(t => `<option value="${esc(t)}">`).join('')}</datalist>
      <div class="modal-actions">
        <button class="cancel" id="ed-cancel">Cancel</button>
        <button class="primary" id="ed-save">Save</button>
      </div>
    </div>`;

  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('#ed-cancel').onclick = close;
  overlay.querySelector('#ed-save').onclick = async () => {
    const body = {
      title: overlay.querySelector('#ed-title').value,
      author: overlay.querySelector('#ed-author').value,
      tags: {
        organization: overlay.querySelector('#ed-org').value || null,
        course_code: overlay.querySelector('#ed-course').value || null,
        course_id: overlay.querySelector('#ed-courseid').value || null,
        module_id: overlay.querySelector('#ed-moduleid').value || null,
        custom_tags: overlay.querySelector('#ed-tags').value
          .split(',').map(t => t.trim()).filter(Boolean)
      }
    };
    try {
      await api(`/v1/library/items/${item.id}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body)
      });
      close();
      state.filterOptions = null; // invalidate cached filters
      await loadLibrary();
    } catch (e) {
      alert(`Update failed: ${e.message}`);
    }
  };
}

async function refreshKbs() {
  state.kbs = await api('/v1/knowledge-bases');
  try { state.kbDeps = await api('/v1/knowledge-bases/dependencies'); } catch { state.kbDeps = {}; }
  if (state.tab === 'chat' && $('kb-select')) drawKbSelect();
}

async function renderKb() {
  $('view').innerHTML = `
  <div style="display:flex;gap:8px;margin-bottom:10px">
    <button id="kb-refresh">Refresh</button>
    <span class="hint" style="align-self:center">${state.kbs.length} knowledge base${state.kbs.length !== 1 ? 's' : ''}</span>
  </div>
  <div class="tiles" id="kb-tiles"></div>`;

  $('kb-refresh').onclick = async () => {
    await refreshKbs();
    state.kbDocs = {};
    renderKb();
  };

  drawKbTiles();
}

function drawKbTiles() {
  const container = $('kb-tiles');
  if (!container) return;

  container.innerHTML = state.kbs.map(kb => {
    const updated = kb.updated_at?.split('T')[0] || '';
    const docsHtml = state.kbDocs[kb.id]
      ? `<div class="kb-docs" style="margin-top:8px;border-top:1px solid #233b6b;padding-top:6px">
          ${state.kbDocs[kb.id].map(d => `<div class="meta" style="margin:2px 0">${esc(d.title || d.id)} — ${esc(d.author || '')} · ${d.total_pages ?? '?'} pg · ${d.chapter_count ?? '?'} ch</div>`).join('')}
        </div>`
      : '';
    const deps = state.kbDeps[kb.id] || [];
    const otherKbs = state.kbs.filter(k => k.id !== kb.id);
    const depsHtml = otherKbs.length ? `<div class="kb-deps">
      <div class="meta" style="font-size:11px;margin-bottom:2px">Dependencies (included when this KB is selected):</div>
      ${otherKbs.map(k => `<label><input type="checkbox" data-dep-kb="${kb.id}" data-dep-target="${k.id}" ${deps.includes(k.id) ? 'checked' : ''}> ${esc(k.name)}</label>`).join('')}
    </div>` : '';
    return `<div class="tile">
      <h4>${esc(kb.name)}</h4>
      ${kb.description ? `<div class="meta">${esc(kb.description)}</div>` : ''}
      <div class="meta">${kb.document_count ?? '?'} docs · ${kb.chunk_count ?? '?'} chunks</div>
      <div class="meta">Updated: ${esc(updated)}</div>
      <div class="meta" style="font-size:11px;color:#6688aa">ID: ${esc(kb.id)}</div>
      ${depsHtml}
      <div class="actions">
        <button data-kb-rename="${kb.id}">Rename</button>
        <button data-kb-delete="${kb.id}" class="danger">Delete</button>
        <button data-kb-docs="${kb.id}" class="primary">${state.kbDocs[kb.id] ? 'Hide Docs' : 'Documents'}</button>
      </div>
      ${docsHtml}
    </div>`;
  }).join('');

  // Rename handler
  [...container.querySelectorAll('[data-kb-rename]')].forEach(b => {
    b.onclick = async () => {
      const kb = state.kbs.find(k => k.id === b.dataset.kbRename);
      if (!kb) return;
      const name = prompt('New name:', kb.name)?.trim();
      if (!name || name === kb.name) return;
      try {
        await api(`/v1/knowledge-bases/${kb.id}`, {
          method: 'PATCH',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ name })
        });
        await refreshKbs();
        drawKbTiles();
      } catch (e) {
        alert(`Rename failed: ${e.message}`);
      }
    };
  });

  // Delete handler
  [...container.querySelectorAll('[data-kb-delete]')].forEach(b => {
    b.onclick = async () => {
      const kb = state.kbs.find(k => k.id === b.dataset.kbDelete);
      if (!kb) return;
      if (!confirm(`Delete KB "${kb.name}"? This cannot be undone.`)) return;
      try {
        await api(`/v1/knowledge-bases/${kb.id}`, { method: 'DELETE' });
        delete state.kbDocs[kb.id];
        await refreshKbs();
        drawKbTiles();
      } catch (e) {
        alert(`Delete failed: ${e.message}`);
      }
    };
  });

  // Documents toggle handler
  [...container.querySelectorAll('[data-kb-docs]')].forEach(b => {
    b.onclick = async () => {
      const kbId = b.dataset.kbDocs;
      if (state.kbDocs[kbId]) {
        delete state.kbDocs[kbId];
        drawKbTiles();
        return;
      }
      try {
        b.textContent = 'Loading...';
        const docs = await api(`/v1/knowledge-bases/${kbId}/documents`);
        state.kbDocs[kbId] = Array.isArray(docs) ? docs : (docs.items || docs.documents || []);
        drawKbTiles();
      } catch (e) {
        alert(`Failed to load documents: ${e.message}`);
      }
    };
  });

  // Dependency checkbox handler
  [...container.querySelectorAll('[data-dep-kb]')].forEach(cb => {
    cb.onchange = async () => {
      const kbId = cb.dataset.depKb;
      const allChecked = [...container.querySelectorAll(`[data-dep-kb="${kbId}"]:checked`)].map(c => c.dataset.depTarget);
      try {
        await api(`/v1/knowledge-bases/${kbId}/dependencies`, {
          method: 'PUT',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ dependencies: allChecked })
        });
        state.kbDeps[kbId] = allChecked;
      } catch (e) {
        alert(`Failed to update dependencies: ${e.message}`);
        cb.checked = !cb.checked;
      }
    };
  });
}

async function renderLibrary() {
  const filters = await loadFilterOptions();

  $('view').innerHTML = `
  <div class="library-top" style="grid-template-columns:1fr 1fr 160px 160px 160px 160px auto">
    <input id="f-q" placeholder="Filter by title text" />
    <input id="f-author" placeholder="Filter by author" />
    <select id="f-org"><option value="">Org: any</option>${(filters.organizations || []).map(o => `<option value="${esc(o)}" ${state.filters.org === o ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>
    <select id="f-course"><option value="">Course: any</option>${(filters.course_codes || []).map(c => `<option value="${esc(c)}" ${state.filters.course === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select>
    <select id="f-tag"><option value="">Tag: any</option>${(filters.custom_tags || []).map(t => `<option value="${esc(t)}" ${state.filters.tag === t ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
    <select id="f-haskb"><option value="">has_kb: any</option><option value="true" ${state.filters.hasKb === 'true' ? 'selected' : ''}>has_kb: true</option><option value="false" ${state.filters.hasKb === 'false' ? 'selected' : ''}>has_kb: false</option></select>
    <button id="lib-refresh">Refresh</button>
  </div>
  <div class="tiles" id="tiles"></div>`;

  $('f-q').value = state.filters.q;
  $('f-author').value = state.filters.author;

  $('f-q').oninput = () => {
    state.filters.q = $('f-q').value;
    drawTiles();
  };
  $('f-author').oninput = () => {
    state.filters.author = $('f-author').value;
    drawTiles();
  };
  $('f-org').onchange = () => {
    state.filters.org = $('f-org').value;
    loadLibrary();
  };
  $('f-course').onchange = () => {
    state.filters.course = $('f-course').value;
    loadLibrary();
  };
  $('f-tag').onchange = () => {
    state.filters.tag = $('f-tag').value;
    loadLibrary();
  };
  $('f-haskb').onchange = () => {
    state.filters.hasKb = $('f-haskb').value;
    loadLibrary();
  };
  $('lib-refresh').onclick = () => { state.filterOptions = null; loadLibrary(); renderLibrary(); };

  await loadLibrary();
}

function filteredLibrary() {
  const q = state.filters.q.trim().toLowerCase();
  const a = state.filters.author.trim().toLowerCase();
  return state.library.filter((x) => {
    if (q && !(x.title || '').toLowerCase().includes(q)) return false;
    if (a && !(x.author || '').toLowerCase().includes(a)) return false;
    return true;
  });
}

function buildTileHtml(x, kbMap) {
  // Orphan tiles (KB-only docs not linked to any library item)
  if (x._orphan) {
    const meta = [];
    if (x.author) meta.push(`Author: ${esc(x.author)}`);
    if (x.total_pages != null) meta.push(`${x.total_pages} pages`);
    if (x.chapter_count != null) meta.push(`${x.chapter_count} chapters`);
    if (x.source_file) meta.push(`Source: ${esc(x.source_file)}`);
    const kbLabel = x.kb_name || x.kb_id;
    return `<div class="tile orphan">
      <h4>${esc(x.title || '(untitled)')}<span class="orphan-badge">KB Only</span></h4>
      ${meta.length ? `<div class="meta">${meta.join(' &middot; ')}</div>` : ''}
      <div class="meta">KB: <span title="ID: ${esc(x.kb_id)}">${esc(kbLabel)}</span></div>
      <div class="meta">Doc ID: ${esc(x.document_id)}</div>
      ${x.created_at ? `<div class="meta">Created: ${esc(x.created_at?.split('T')[0] || '')}</div>` : ''}
      <div class="actions">
        <button data-orphan-delete="${esc(x.document_id)}" data-orphan-kb="${esc(x.kb_id)}" class="danger">Delete from KB</button>
      </div>
    </div>`;
  }

  const tags = x.tags || {};
  const tagParts = [];
  if (tags.organization) tagParts.push(esc(tags.organization));
  if (tags.course_code) tagParts.push(esc(tags.course_code));
  if (tags.custom_tags?.length) tagParts.push(...tags.custom_tags.map(t => esc(t)));
  const tagsHtml = tagParts.length
    ? `<div class="tags">${tagParts.map(t => `<span>${t}</span>`).join('')}</div>`
    : '';

  const outputs = x.output_files || {};
  const outputLines = Object.entries(outputs).filter(([type, info]) => {
    // Hide knowledge_base entries that have no document_id (incomplete ingest)
    if (type === 'knowledge_base' && !info.document_id) return false;
    return true;
  }).map(([type, info]) => {
    const size = info.file_size_mb != null ? ` (${info.file_size_mb} MB)` : '';
    const dur = info.duration_minutes != null ? ` ${Math.round(info.duration_minutes)} min` : '';
    const label = type.replace(/_/g, ' ').toUpperCase();
    const link = type === 'knowledge_base'
      ? `<a data-goto-kb="${esc(info.kb_id || '')}" style="cursor:pointer">${label}</a>`
      : `<a data-download-id="${x.id}" data-download-type="${esc(type)}">${label}${size}${dur}</a>`;
    return link
      + `<button class="del-output" data-delout-id="${x.id}" data-delout-type="${esc(type)}" title="Delete ${label}">&#x2717;</button>`;
  });
  const outputFilesHtml = outputLines.length
    ? `<div class="output-files">${outputLines.join(' &middot; ')}</div>`
    : '';

  // Only show KB link if there's a real document_id (complete ingest)
  const kbOut = x.output_files?.knowledge_base;
  const effectiveKbId = kbOut?.document_id ? (x.kb_id || kbOut.kb_id) : null;
  const kbName = effectiveKbId ? (kbMap?.[effectiveKbId] || effectiveKbId) : '';
  const kbMeta = effectiveKbId
    ? ` | KB: <span title="ID: ${esc(effectiveKbId)}">${esc(kbName)}</span>`
    : '';

  return `<div class="tile">
    <h4>${esc(x.title || '(untitled)')}</h4>
    <div class="meta">Author: ${esc(x.author || 'Unknown')}</div>
    <div class="meta">ID: ${esc(x.id)}${kbMeta}</div>
    ${tagsHtml}
    ${outputFilesHtml}
    <div class="meta">Created: ${esc(x.created_at?.split('T')[0] || '')}</div>
    <div class="actions">
      <button data-edit="${x.id}">Edit</button>
      <button data-delete="${x.id}" class="danger">Delete</button>
      <button class="primary" data-use="${x.id}">Use as Input</button>
    </div>
  </div>`;
}

function drawTiles() {
  const kbMap = {};
  for (const kb of state.kbs) kbMap[kb.id] = kb.name;
  const rows = filteredLibrary().slice(0, 250);
  $('tiles').innerHTML = rows.map(x => buildTileHtml(x, kbMap)).join('');

  // Edit handler — opens modal
  [...$('tiles').querySelectorAll('[data-edit]')].forEach((b) => {
    b.onclick = () => {
      const item = state.library.find((i) => i.id === b.dataset.edit);
      if (item) showEditModal(item);
    };
  });

  // Download handler — open file in new tab
  [...$('tiles').querySelectorAll('[data-download-id]')].forEach((a) => {
    a.onclick = () => {
      const id = a.dataset.downloadId;
      const type = a.dataset.downloadType;
      window.open(`${API_BASE}/v1/library/items/${id}/download?output_type=${type}`, '_blank');
    };
    a.style.cursor = 'pointer';
  });

  // KB link handler — switch to KB tab with docs expanded
  [...$('tiles').querySelectorAll('[data-goto-kb]')].forEach((a) => {
    a.onclick = async () => {
      const kbId = a.dataset.gotoKb;
      if (!kbId) return;
      // Pre-load docs so they show expanded when KB tab renders
      try {
        const docs = await api(`/v1/knowledge-bases/${kbId}/documents`);
        state.kbDocs[kbId] = Array.isArray(docs) ? docs : (docs.items || docs.documents || []);
      } catch {}
      switchTab('kb');
    };
  });

  // Output delete handler (red X buttons)
  [...$('tiles').querySelectorAll('.del-output')].forEach((b) => {
    b.onclick = async (e) => {
      e.stopPropagation();
      const id = b.dataset.deloutId;
      const type = b.dataset.deloutType;
      const label = type.replace(/_/g, ' ').toUpperCase();
      if (!confirm(`Delete ${label} output from this item?`)) return;
      try {
        await api(`/v1/library/items/${id}/output/${type}`, { method: 'DELETE' });
        await loadLibrary();
      } catch (e) {
        alert(`Delete output failed: ${e.message}`);
      }
    };
  });

  // Delete handler
  [...$('tiles').querySelectorAll('[data-delete]')].forEach((b) => {
    b.onclick = async () => {
      const id = b.dataset.delete;
      const item = state.library.find((i) => i.id === id);
      if (!confirm(`Delete "${item?.title || id}"? This removes the item and all associated files.`)) return;
      try {
        await api(`/v1/library/items/${id}`, { method: 'DELETE' });
        await loadLibrary();
      } catch (e) {
        alert(`Delete failed: ${e.message}`);
      }
    };
  });

  // Orphan KB doc delete handler
  [...$('tiles').querySelectorAll('[data-orphan-delete]')].forEach((b) => {
    b.onclick = async () => {
      const docId = b.dataset.orphanDelete;
      const kbId = b.dataset.orphanKb;
      if (!confirm(`Delete this KB document? This removes it from the knowledge base.`)) return;
      try {
        await api(`/v1/knowledge-bases/${kbId}/documents/${docId}`, { method: 'DELETE' });
        await loadLibrary();
      } catch (e) {
        alert(`Delete failed: ${e.message}`);
      }
    };
  });

  // Use as Input handler — append to inputQueue (skip duplicates)
  [...$('tiles').querySelectorAll('[data-use]')].forEach((b) => {
    b.onclick = () => {
      const id = b.dataset.use;
      if (state.inputQueue.some(e => e.type === 'library' && e.id === id)) {
        switchTab('input');
        return;
      }
      const item = state.library.find(i => i.id === id);
      if (!item) return;
      state.inputQueue.push({
        type: 'library',
        id: item.id,
        title: item.title,
        author: item.author,
        outputFiles: item.output_files
      });
      persist();
      switchTab('input');
    };
  });
}

async function loadLibrary() {
  const qs = new URLSearchParams();
  // has_kb filtered client-side (backend checks top-level kb_id which is always null)
  if (state.filters.org) qs.set('organization', state.filters.org);
  if (state.filters.course) qs.set('course_code', state.filters.course);
  if (state.filters.tag) qs.set('custom_tag', state.filters.tag);
  let items = await api(`/v1/library/items${qs.toString() ? `?${qs}` : ''}`);
  if (!Array.isArray(items)) items = [];

  // Client-side has_kb filter (requires document_id for a complete KB entry)
  if (state.filters.hasKb === 'true') {
    items = items.filter(i => i.output_files?.knowledge_base?.document_id);
  } else if (state.filters.hasKb === 'false') {
    items = items.filter(i => !i.output_files?.knowledge_base?.document_id);
  }

  // Fetch orphaned KB docs; inject unlinked docs into their parent items
  try {
    const { orphans = [], unlinked = [] } = await api('/v1/library/kb-orphans');
    // Inject unlinked KB docs into matching library items' output_files
    for (const doc of unlinked) {
      const parent = items.find(i => i.id === doc._library_item_id);
      if (parent) {
        if (!parent.output_files) parent.output_files = {};
        parent.output_files.knowledge_base = {
          kb_id: doc.kb_id,
          document_id: doc.document_id,
          kb_name: doc.kb_name
        };
      }
    }
    items.push(...orphans);
  } catch {}
  state.library = items;
  if (state.tab === 'library' && $('tiles')) drawTiles();
}

function showLoginOverlay(provider, method) {
  const isOauth = method === 'oauth';
  const providerName = provider === 'openai' ? 'OpenAI' : provider === 'anthropic' ? 'Anthropic' : provider;
  $('app').innerHTML = `${css()}
  <div class="modal-overlay">
    <div class="modal" style="width:400px">
      <h3>Sign in to ${esc(providerName)}</h3>
      ${isOauth ? `
        <button class="primary" id="login-oauth" style="width:100%;padding:10px;font-size:14px;margin-bottom:12px">
          Sign in with ${esc(providerName)}
        </button>
        <p id="login-oauth-status" style="font-size:12px;color:#5bc48a;text-align:center;margin:0 0 8px 0;display:none"></p>
        <div style="text-align:center;color:#556;font-size:12px;margin:8px 0">or paste a token / API key manually</div>
      ` : ''}
      <label>${isOauth ? 'Token or API key' : 'API key'}</label>
      <input type="password" id="login-token" placeholder="sk-..." autocomplete="off" />
      <div class="modal-actions">
        <button class="primary" id="login-submit">Sign in</button>
      </div>
      <p id="login-error" style="color:#e05050;font-size:12px;margin:8px 0 0 0;display:none"></p>
    </div>
  </div>`;

  // --- OAuth browser flow ---
  if (isOauth && $('login-oauth')) {
    $('login-oauth').onclick = async () => {
      $('login-oauth').disabled = true;
      $('login-oauth').textContent = 'Opening browser...';
      $('login-error').style.display = 'none';

      try {
        const start = await api(`/v1/auth/oauth/start?provider=${provider}`);
        window.open(start.url, '_blank');

        $('login-oauth').textContent = 'Waiting for sign-in...';
        const statusEl = $('login-oauth-status');
        if (statusEl) { statusEl.textContent = 'Complete sign-in in the browser tab that opened.'; statusEl.style.display = 'block'; }

        // Poll for completion
        const pollInterval = setInterval(async () => {
          try {
            const poll = await api(`/v1/auth/oauth/poll?state=${start.state}`);
            if (poll.completed) {
              clearInterval(pollInterval);
              if (statusEl) statusEl.textContent = `Signed in${poll.email ? ' as ' + poll.email : ''}`;
              await doBootstrap();
            } else if (poll.error) {
              clearInterval(pollInterval);
              throw new Error(poll.error);
            }
          } catch (e) {
            clearInterval(pollInterval);
            $('login-error').textContent = e.message;
            $('login-error').style.display = 'block';
            $('login-oauth').disabled = false;
            $('login-oauth').textContent = `Sign in with ${providerName}`;
            if (statusEl) statusEl.style.display = 'none';
          }
        }, 2000);

        // Stop polling after 3 minutes
        setTimeout(() => clearInterval(pollInterval), 180000);
      } catch (e) {
        $('login-error').textContent = e.message;
        $('login-error').style.display = 'block';
        $('login-oauth').disabled = false;
        $('login-oauth').textContent = `Sign in with ${providerName}`;
      }
    };
  }

  // --- Manual token / API key ---
  async function doLogin() {
    const val = $('login-token').value.trim();
    if (!val) return;
    $('login-submit').disabled = true;
    $('login-submit').textContent = 'Signing in...';
    try {
      const body = isOauth
        ? { method: 'oauth', token: val, provider }
        : { method: 'api_key', apiKey: val, provider };
      const r = await fetch(`${API_BASE}/v1/auth/login`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.error || 'Login failed');
      await doBootstrap();
    } catch (e) {
      $('login-error').textContent = e.message;
      $('login-error').style.display = 'block';
      $('login-submit').disabled = false;
      $('login-submit').textContent = 'Sign in';
    }
  }

  $('login-submit').onclick = doLogin;
  $('login-token').onkeydown = (e) => { if (e.key === 'Enter') doLogin(); };
}

async function loadModels() {
  try {
    const data = await api('/v1/models');
    state.models = data.models || [];
    state.activeModel = data.active || null;
  } catch { state.models = []; }
}

async function doBootstrap() {
  await loadModels();
  state.kbs = await api('/v1/knowledge-bases');
  try { state.kbDeps = await api('/v1/knowledge-bases/dependencies'); } catch { state.kbDeps = {}; }
  state.sessions = (await api('/v1/chat/sessions')).items || [];
  if (!state.activeSessionId && state.sessions.length) state.activeSessionId = state.sessions[0].id;
  if (state.activeSessionId) {
    state.messages = (await api(`/v1/chat/sessions/${state.activeSessionId}/messages`)).items || [];
  }
  await loadLibrary();
  // Rebuild inputQueue from persisted library IDs
  if (state._pendingQueueIds?.length) {
    for (const id of state._pendingQueueIds) {
      const item = state.library.find(i => i.id === id);
      if (item) {
        state.inputQueue.push({
          type: 'library',
          id: item.id,
          title: item.title,
          author: item.author,
          outputFiles: item.output_files
        });
      }
    }
    delete state._pendingQueueIds;
  }
  renderShell();
  switchTab(state.tab);
}

async function bootstrap() {
  hydrate();
  // Check auth before loading data
  try {
    const auth = await api('/v1/auth/status');
    if (!auth.authenticated) {
      showLoginOverlay(auth.provider, auth.method);
      return;
    }
  } catch {
    // Auth endpoint unavailable — proceed anyway (older server)
  }
  await doBootstrap();
}

bootstrap().catch((e) => {
  document.getElementById('app').innerHTML = `<div style="padding:16px;color:#ffb3b3">Server unavailable: ${esc(e.message)}</div>`;
});
