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

/* ── Chat Panel ─────────────────────────────────────────────────────────── */
function openChatPanel() {
  document.getElementById('chat-panel').style.display = 'flex';
  document.getElementById('chat-overlay').style.display = 'block';
  document.getElementById('chat-input').focus();
}
function closeChatPanel() {
  const p = document.getElementById('chat-panel');
  const o = document.getElementById('chat-overlay');
  if (p) p.style.display = 'none';
  if (o) o.style.display = 'none';
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  appendChatMsg('user', msg);

  const typingId = appendChatMsg('ai', '…');
  try {
    const res = await fetch(`/tanks/${TANK_ID}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Error');
    updateChatMsg(typingId, data.reply);
  } catch (e) {
    updateChatMsg(typingId, `Error: ${e.message}`);
  }
}

async function clearChat() {
  await fetch(`/tanks/${TANK_ID}/chat`, { method: 'DELETE' });
  document.getElementById('chat-messages').innerHTML = '';
}

let chatMsgCounter = 0;
function appendChatMsg(role, text) {
  const id = `chat-msg-${++chatMsgCounter}`;
  const el = document.createElement('div');
  el.id = id;
  el.className = `chat-msg chat-msg-${role}`;
  el.innerHTML = `<div class="chat-bubble">${escHtml(text)}</div>`;
  const container = document.getElementById('chat-messages');
  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
  return id;
}
function updateChatMsg(id, text) {
  const el = document.getElementById(id);
  if (el) el.querySelector('.chat-bubble').textContent = text;
  const container = document.getElementById('chat-messages');
  container.scrollTop = container.scrollHeight;
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

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
async function loadPopChart(tankId) {
  const canvas = document.getElementById('popChart');
  if (!canvas) return;
  try {
    const res = await fetch(`/tanks/${tankId}/charts/population`);
    const { current } = await res.json();
    if (!current.length) return;

    const labels = current.map(r => r.common_name || r.species || 'Unknown');
    const values = current.map(r => r.count);
    const colors = ['#00c4a0','#38bdf8','#a78bfa','#fb923c','#facc15','#34d399','#f87171'];

    new Chart(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [{ data: values, backgroundColor: colors.slice(0, values.length), borderRadius: 4 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: '#94a3b8' }, grid: { display: false } },
          y: { ticks: { color: '#94a3b8', stepSize: 1 }, grid: { color: '#2d3f5533' } },
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
