/* ── Today panel navigation ───────────────────────────────────────────── */
function goToTank(event, tankId) {
  if (event.target.closest('a, input, label, button, form')) return;
  window.location.href = `/tanks/${tankId}`;
}

/* ── Mobile nav drawer ─────────────────────────────────────────────────── */
function openSidebar() {
  document.querySelector('.sidebar')?.classList.add('open');
  document.querySelector('.sidebar-overlay')?.classList.add('open');
}
function closeSidebar() {
  document.querySelector('.sidebar')?.classList.remove('open');
  document.querySelector('.sidebar-overlay')?.classList.remove('open');
}

/* ── Modals ────────────────────────────────────────────────────────────── */
function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = '';
}
function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = 'none';
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal').forEach(m => m.style.display = 'none');
    closeChatPanel();
  }
});

/* ── Chat (popup panel + full page) ─────────────────────────────────────── */
let activeConversationId = null;
let chatMsgCounter = 0;
let chatSending = false;

function isChatPage() {
  return !!document.getElementById('chat-page');
}

/** Prefer full-page chat UI when present; otherwise the right-side popup. */
function chatEls() {
  if (isChatPage()) {
    return {
      messages: document.getElementById('chat-page-messages'),
      input: document.getElementById('chat-page-input'),
      title: document.getElementById('chat-page-title'),
      deleteBtn: document.getElementById('chat-page-delete-btn'),
      isPage: true,
    };
  }
  return {
    messages: document.getElementById('chat-messages'),
    input: document.getElementById('chat-input'),
    title: document.getElementById('chat-panel-title'),
    deleteBtn: document.getElementById('chat-delete-btn'),
    isPage: false,
  };
}

function openChatPanel() {
  if (isChatPage()) return; // full page is already the chat surface
  const p = document.getElementById('chat-panel');
  const o = document.getElementById('chat-overlay');
  if (!p) return;
  p.style.display = 'flex';
  if (o) o.style.display = 'block';
  document.getElementById('chat-input')?.focus();
  _updateChatChrome();
}

function closeChatPanel() {
  const p = document.getElementById('chat-panel');
  const o = document.getElementById('chat-overlay');
  if (p) p.style.display = 'none';
  if (o) o.style.display = 'none';
}

function _updateChatChrome() {
  const { title, deleteBtn, isPage } = chatEls();
  if (!isPage && title) {
    const tankLabel = (typeof TANK_NAME !== 'undefined' && TANK_NAME) ? TANK_NAME : 'this tank';
    title.textContent = activeConversationId
      ? (title.dataset.convTitle || `Ask AI about ${tankLabel}`)
      : `Ask AI about ${tankLabel}`;
  }
  if (deleteBtn) deleteBtn.style.display = activeConversationId ? '' : 'none';
  document.querySelectorAll('.chat-conv-item').forEach(el => {
    el.classList.toggle('active', Number(el.dataset.id) === activeConversationId);
  });
}

/** Dashboard "Ask AI" button — opens the right-side popup for a quick new chat. */
function startNewChat() {
  if (typeof TANK_ID === 'undefined') return;
  if (isChatPage()) {
    window.location.href = `/tanks/${TANK_ID}/chat/new`;
    return;
  }
  activeConversationId = null;
  const msgs = document.getElementById('chat-messages');
  if (msgs) msgs.innerHTML = '';
  const titleEl = document.getElementById('chat-panel-title');
  if (titleEl) delete titleEl.dataset.convTitle;
  openChatPanel();
  closeSidebar();
}

async function deleteActiveChat() {
  if (!activeConversationId || typeof TANK_ID === 'undefined') return;
  if (!confirm('Delete this conversation? This cannot be undone.')) return;
  await deleteConversation(activeConversationId);
}

async function deleteConversation(conversationId, event) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  if (typeof TANK_ID === 'undefined') return;
  if (event && !confirm('Delete this conversation? This cannot be undone.')) return;
  try {
    const res = await fetch(`/tanks/${TANK_ID}/chat/conversations/${conversationId}`, {
      method: 'DELETE',
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || 'Delete failed');
    }
    if (activeConversationId === conversationId) {
      if (isChatPage()) {
        window.location.href = `/tanks/${TANK_ID}/chat/new`;
        return;
      }
      activeConversationId = null;
      const msgs = document.getElementById('chat-messages');
      if (msgs) msgs.innerHTML = '';
      const titleEl = document.getElementById('chat-panel-title');
      if (titleEl) delete titleEl.dataset.convTitle;
      closeChatPanel();
    }
    await loadConversations();
  } catch (e) {
    alert(`Could not delete conversation: ${e.message}`);
  }
}

async function sendChat() {
  if (typeof TANK_ID === 'undefined' || chatSending) return;
  const ui = chatEls();
  const input = ui.input;
  const msg = input?.value.trim();
  if (!msg) return;
  input.value = '';

  // Remove empty-state placeholder on first message (full page)
  document.getElementById('chat-page-empty')?.remove();

  appendChatMsg('user', msg);

  chatSending = true;
  const typingId = appendChatMsg('ai', '…');
  try {
    const payload = { message: msg };
    if (activeConversationId) payload.conversation_id = activeConversationId;
    const res = await fetch(`/tanks/${TANK_ID}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Error');
    updateChatMsg(typingId, data.reply);
    const wasNew = !activeConversationId;
    activeConversationId = data.conversation_id;

    if (ui.title && data.title) {
      if (!ui.isPage) ui.title.dataset.convTitle = data.title;
      ui.title.textContent = data.title;
    }
    // Full-page new chat: update URL + show delete once we have an id
    if (ui.isPage && wasNew && data.conversation_id) {
      history.replaceState({}, '', `/tanks/${TANK_ID}/chat/c/${data.conversation_id}`);
      const pageRoot = document.getElementById('chat-page');
      if (pageRoot) pageRoot.dataset.conversationId = String(data.conversation_id);
      document.title = `${data.title} — ${typeof TANK_NAME !== 'undefined' ? TANK_NAME : 'Fathom'} — Fathom`;
    }
    _updateChatChrome();
    await loadConversations();
  } catch (e) {
    updateChatMsg(typingId, `Error: ${e.message}`);
  } finally {
    chatSending = false;
  }
}

function _currentConversationId() {
  if (activeConversationId) return activeConversationId;
  const list = document.getElementById('chat-conv-list');
  if (list?.dataset.activeId) return Number(list.dataset.activeId);
  const m = location.pathname.match(/\/chat\/c\/(\d+)/);
  return m ? Number(m[1]) : null;
}

async function loadConversations() {
  if (typeof TANK_ID === 'undefined') return;
  const list = document.getElementById('chat-conv-list');
  if (!list) return;
  const currentId = _currentConversationId();
  try {
    const res = await fetch(`/tanks/${TANK_ID}/chat/conversations`);
    if (!res.ok) return;
    const data = await res.json();
    const convs = data.conversations || [];
    if (!convs.length) {
      list.innerHTML = '<div class="chat-conv-empty">No conversations yet</div>';
      return;
    }
    list.innerHTML = convs.map(c => {
      const active = Number(c.id) === currentId ? ' active' : '';
      const title = escHtml(c.title || 'Conversation');
      return `<a href="/tanks/${TANK_ID}/chat/c/${c.id}" class="chat-conv-item${active}" data-id="${c.id}" title="${title}">
        <span class="chat-conv-title">${title}</span>
        <button type="button" class="chat-conv-delete" onclick="deleteConversation(${c.id}, event)" title="Delete" aria-label="Delete conversation">×</button>
      </a>`;
    }).join('');
  } catch (_) {
    /* ignore list load errors */
  }
}

function appendChatMsg(role, text) {
  const id = `chat-msg-${++chatMsgCounter}`;
  const el = document.createElement('div');
  el.id = id;
  el.className = `chat-msg chat-msg-${role}`;
  el.innerHTML = `<div class="chat-bubble">${escHtml(text)}</div>`;
  const container = chatEls().messages;
  if (container) {
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
  }
  return id;
}
function updateChatMsg(id, text) {
  const el = document.getElementById(id);
  if (el) el.querySelector('.chat-bubble').textContent = text;
  const container = chatEls().messages;
  if (container) container.scrollTop = container.scrollHeight;
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

document.addEventListener('DOMContentLoaded', () => {
  if (typeof TANK_ID !== 'undefined') loadConversations();
});

/* ── Dashboard init ─────────────────────────────────────────────────────── */
function initDashboard(tankId) {
  loadWaterChart(tankId);
  loadPopChart(tankId);
  loadCostCharts(tankId);
}

/* ── Water Parameters Chart ─────────────────────────────────────────────── */
const PARAM_COLORS = {
  ph:      { color: '#38bdf8', label: 'pH' },
  ammonia: { color: '#f87171', label: 'NH₃' },
  nitrite: { color: '#fb923c', label: 'NO₂' },
  nitrate: { color: '#facc15', label: 'NO₃' },
  gh:      { color: '#a78bfa', label: 'GH' },
  kh:      { color: '#818cf8', label: 'KH' },
  temp:    { color: '#34d399', label: 'Temp' },
  tds:     { color: '#94a3b8', label: 'TDS' },
};
const ACTIVE_PARAMS = new Set(['ph', 'ammonia', 'nitrite', 'nitrate']);

async function loadWaterChart(tankId) {
  const canvas = document.getElementById('waterChart');
  if (!canvas) return;
  try {
    const res = await fetch(`/tanks/${tankId}/charts/water-params?limit=30`);
    const { data } = await res.json();
    if (!data.length) return;

    const labels = data.map(r => r.timestamp.slice(0, 10));
    const availParams = Object.keys(PARAM_COLORS).filter(p => data.some(r => r[p] !== null && r[p] !== undefined));
    const datasets = availParams.map(p => ({
      label: PARAM_COLORS[p].label,
      data: data.map(r => r[p]),
      borderColor: PARAM_COLORS[p].color,
      backgroundColor: PARAM_COLORS[p].color + '22',
      tension: 0.3,
      pointRadius: 3,
      hidden: !ACTIVE_PARAMS.has(p),
    }));

    const chart = new Chart(canvas, {
      type: 'line',
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#94a3b8', maxTicksLimit: 6 }, grid: { color: '#2d3f5533' } },
          y: { ticks: { color: '#94a3b8' }, grid: { color: '#2d3f5533' } },
        },
      },
    });

    const togglesEl = document.getElementById('param-toggles');
    if (togglesEl) {
      availParams.forEach((p, i) => {
        const btn = document.createElement('button');
        btn.className = 'chart-toggle-btn' + (ACTIVE_PARAMS.has(p) ? ' active' : '');
        btn.textContent = PARAM_COLORS[p].label;
        btn.style.color = PARAM_COLORS[p].color;
        btn.style.borderColor = ACTIVE_PARAMS.has(p) ? PARAM_COLORS[p].color : '';
        btn.addEventListener('click', () => {
          chart.data.datasets[i].hidden = !chart.data.datasets[i].hidden;
          btn.classList.toggle('active');
          btn.style.borderColor = btn.classList.contains('active') ? PARAM_COLORS[p].color : '';
          chart.update();
        });
        togglesEl.appendChild(btn);
      });
    }
  } catch (e) { console.warn('Water chart error:', e); }
}

/* ── Population Chart ───────────────────────────────────────────────────── */
// Pest/hitchhiker invertebrates (snails, worms, copepods, ostracods, etc.) commonly
// go from a countable number to "many" once they start breeding — once that happens
// they're not worth a line on the chart (nothing actionable, just clutter). Fish and
// true shrimp livestock stay on the chart even after going "many".
const NON_LIVESTOCK_MANY_PATTERN = /\b(snail|worm|copepod|ostracod|seed shrimp|hydra|planaria|scud|amphipod|isopod|daphnia|rotifer)/i;

async function loadPopChart(tankId) {
  const canvas = document.getElementById('popChart');
  if (!canvas) return;
  try {
    const res = await fetch(`/tanks/${tankId}/charts/population`);
    const { events, current } = await res.json();
    if (!events.length) return;

    // Sum each event's +/- delta into per-species, per-day buckets, then
    // walk the shared date axis accumulating a running total per species.
    // Species whose count is always null ("many"/unquantified, e.g. hitchhiker
    // pests) are skipped — a flat line at 0 would misrepresent "unknown" as "none".
    const deltaByDate = {};
    const speciesLabels = [];
    events.forEach(e => {
      if (e.count === null || e.count === undefined) return;
      const date = e.timestamp.slice(0, 10);
      const label = e.common_name || e.species || 'Unknown';
      const delta = (e.event_type === 'added' || e.event_type === 'born') ? e.count : -e.count;
      if (!deltaByDate[date]) deltaByDate[date] = {};
      deltaByDate[date][label] = (deltaByDate[date][label] || 0) + delta;
      if (!speciesLabels.includes(label)) speciesLabels.push(label);
    });
    if (!speciesLabels.length) return;

    // Current count per species, straight from the inhabitants table (not derived
    // from summed deltas) — authoritative for "today", and null when the species'
    // count has since become unknown/"many" (a state population_events can't record).
    const currentByLabel = {};
    (current || []).forEach(c => {
      currentByLabel[c.common_name || c.species || 'Unknown'] = c.count;
    });

    const today = new Date().toISOString().slice(0, 10);
    const dates = Object.keys(deltaByDate).sort();
    if (dates[dates.length - 1] !== today) dates.push(today);

    const colors = ['#00c4a0','#38bdf8','#a78bfa','#fb923c','#facc15','#34d399','#f87171'];
    const datasets = speciesLabels.map((label, i) => {
      const nowUnknown = Object.prototype.hasOwnProperty.call(currentByLabel, label) && currentByLabel[label] === null;
      if (nowUnknown && NON_LIVESTOCK_MANY_PATTERN.test(label)) return null;
      let running = 0;
      const data = dates.map(date => {
        if (date === today && Object.prototype.hasOwnProperty.call(currentByLabel, label)) {
          // Authoritative current value; null leaves a gap, signaling "now unknown"
          // instead of freezing the line at a stale, misleadingly-precise number.
          return currentByLabel[label];
        }
        running += (deltaByDate[date] && deltaByDate[date][label]) || 0;
        // A running total should never go below zero — a population can't be
        // negative. Data-entry mismatches (e.g. a "removed" event count exceeding
        // what was actually on hand) can otherwise push the sum negative.
        if (running < 0) running = 0;
        return running;
      });
      return {
        label: nowUnknown ? `${label} (now: many)` : label,
        data,
        borderColor: colors[i % colors.length],
        backgroundColor: colors[i % colors.length] + '22',
        stepped: true,
        pointRadius: 3,
      };
    }).filter(Boolean);
    if (!datasets.length) return;

    new Chart(canvas, {
      type: 'line',
      data: { labels: dates, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: { display: datasets.length > 1, labels: { color: '#94a3b8', font: { size: 11 } } },
        },
        scales: {
          x: { ticks: { color: '#94a3b8', maxTicksLimit: 6 }, grid: { color: '#2d3f5533' } },
          y: { ticks: { color: '#94a3b8', stepSize: 1, precision: 0 }, grid: { color: '#2d3f5533' } },
        },
      },
    });
  } catch (e) { console.warn('Pop chart error:', e); }
}

/* ── Cost Charts ────────────────────────────────────────────────────────── */
async function loadCostCharts(tankId) {
  const costCanvas = document.getElementById('costChart');
  const monthlyCanvas = document.getElementById('monthlyChart');
  if (!costCanvas && !monthlyCanvas) return;
  try {
    const res = await fetch(`/tanks/${tankId}/charts/costs`);
    const { by_category, by_month } = await res.json();

    if (costCanvas && by_category.length) {
      const colors = ['#00c4a0','#38bdf8','#a78bfa','#fb923c','#facc15','#34d399','#f87171','#e879f9'];
      new Chart(costCanvas, {
        type: 'doughnut',
        data: {
          labels: by_category.map(r => r.category),
          datasets: [{ data: by_category.map(r => r.total), backgroundColor: colors }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: true,
          plugins: {
            legend: { labels: { color: '#94a3b8', font: { size: 11 } } },
            tooltip: { callbacks: { label: ctx => ` ${ctx.label}: $${ctx.parsed.toFixed(2)}` } },
          },
        },
      });
    }

    if (monthlyCanvas && by_month.length) {
      new Chart(monthlyCanvas, {
        type: 'bar',
        data: {
          labels: by_month.map(r => r.month),
          datasets: [{ label: 'Spending', data: by_month.map(r => r.total), backgroundColor: '#00c4a022', borderColor: '#00c4a0', borderWidth: 1, borderRadius: 4 }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: true,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#94a3b8' }, grid: { display: false } },
            y: { ticks: { color: '#94a3b8', callback: v => `$${v}` }, grid: { color: '#2d3f5533' } },
          },
        },
      });
    }
  } catch (e) { console.warn('Cost chart error:', e); }
}

/* ── Timestamps: stored UTC, shown/entered in browser-local time ─────────── */
function _pad(n) { return String(n).padStart(2, '0'); }

// Build the "YYYY-MM-DDTHH:mm" a <input type="datetime-local"> needs, using
// local getters (not toISOString, which reports UTC and would mislabel the
// field's displayed value as local time when it isn't).
function localNowInputValue() {
  const d = new Date();
  return `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}T${_pad(d.getHours())}:${_pad(d.getMinutes())}`;
}

// A datetime-local value like "2026-07-02T13:15" is parsed by `Date` as local
// time; reformat it to the "YYYY-MM-DD HH:MM:SS" UTC string the backend stores.
function localDatetimeToUTCString(localValue) {
  if (!localValue) return '';
  const d = new Date(localValue);
  if (isNaN(d)) return '';
  return `${d.getUTCFullYear()}-${_pad(d.getUTCMonth() + 1)}-${_pad(d.getUTCDate())} ${_pad(d.getUTCHours())}:${_pad(d.getUTCMinutes())}:${_pad(d.getUTCSeconds())}`;
}

// Wire up on a form's onsubmit: for every visible .dt-local input, fill its
// paired hidden .dt-utc input (same .form-group) with the converted UTC value.
function prepareLocalTimestamps(form) {
  form.querySelectorAll('input.dt-local').forEach(local => {
    const group = local.closest('.form-group');
    const hidden = group && group.querySelector('input.dt-utc');
    if (!hidden) return;
    hidden.value = local.value ? localDatetimeToUTCString(local.value) : '';
  });
  return true;
}

// Parse a stored "YYYY-MM-DD HH:MM:SS" UTC string into a value suitable for
// a <input type="datetime-local"> (i.e. the inverse of localDatetimeToUTCString).
function utcToLocalInputValue(utcString) {
  if (!utcString) return '';
  const iso = utcString.includes('T') ? utcString : utcString.replace(' ', 'T') + 'Z';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}T${_pad(d.getHours())}:${_pad(d.getMinutes())}`;
}

// Parse a stored "YYYY-MM-DD HH:MM:SS" UTC string and format it for display
// in the browser's local timezone.
// dateOnly: true → "YYYY-MM-DD" only (compact as-of labels on composite cards).
function formatLocalTimestamp(utcString, dateOnly = false) {
  if (!utcString) return '';
  const iso = utcString.includes('T') ? utcString : utcString.replace(' ', 'T') + 'Z';
  const d = new Date(iso);
  if (isNaN(d)) return utcString;
  const day = `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}`;
  if (dateOnly) return day;
  return `${day} ${_pad(d.getHours())}:${_pad(d.getMinutes())}`;
}

function hydrateLocalTimestamps(root = document) {
  root.querySelectorAll('.ts-local[data-utc]').forEach(el => {
    const dateOnly = el.dataset.dateOnly === '1' || el.dataset.dateOnly === 'true';
    el.textContent = formatLocalTimestamp(el.dataset.utc, dateOnly);
  });
}
document.addEventListener('DOMContentLoaded', () => hydrateLocalTimestamps());
