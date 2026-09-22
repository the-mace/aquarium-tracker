// Shared review UI for Import and Quick Log.
// Page scripts call configureImportReview() and then parseFile/parseText.

var previewData = null;
var serverFlags = [];
var reviewConfig = {
  inputSectionId: 'upload-section',
  resultId: 'import-result',
  flagBefore: 'importing',
  noneSelected: 'No rows selected to import.',
  busyLabel: 'Importing…',
  idleLabel: 'Import Selected',
  failPrefix: 'Import failed',
  doneHtml: function (summary) { return summary || ''; },
  dupBanner: 'Re-check to import anyway (e.g. two water changes same day).',
  dupSuffix: ' — check to import anyway',
};

function configureImportReview(opts) {
  reviewConfig = Object.assign({}, reviewConfig, opts || {});
}

function progressEls() {
  return {
    area: document.getElementById('progress-area'),
    label: document.getElementById('phase-label'),
    fill: document.getElementById('progress-fill'),
    pct: document.getElementById('progress-pct'),
  };
}

async function readExtractionStream(res, onProgress) {
  if (!res.ok) {
    var data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    throw new Error(data.detail || 'Parse failed');
  }
  var reader = res.body.getReader();
  var decoder = new TextDecoder();
  var buffer = '';
  var result = null;
  while (true) {
    var chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    var parts = buffer.split('\n\n');
    buffer = parts.pop();
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i];
      if (part.indexOf('data: ') !== 0) continue;
      var event = JSON.parse(part.slice(6));
      if (event.phase === 'analyzing' || event.phase === 'processing') {
        onProgress(event.current || 0, event.total || 1, event.label);
      } else if (event.phase === 'error') {
        throw new Error(event.message);
      } else if (event.phase === 'complete') {
        onProgress(1, 1, 'Done');
        result = event;
      }
    }
  }
  if (!result) throw new Error('No response received from server.');
  return result;
}

function clearReviewPanels() {
  document.getElementById(reviewConfig.inputSectionId).style.display = 'block';
  document.getElementById('progress-area').style.display = 'none';
  document.getElementById('preview-section').style.display = 'none';
  var result = document.getElementById(reviewConfig.resultId);
  if (result) result.style.display = 'none';
  document.getElementById('preview-flags').style.display = 'none';
  document.getElementById('preview-dups').style.display = 'none';
  document.getElementById('preview-data').innerHTML = '';
  document.getElementById('preview-counts').innerHTML = '';
}

async function confirmReviewed() {
  var editedData = getEditedData();
  var flagObs = getSelectedFlagObservations();
  if (flagObs.length > 0) {
    editedData.observations = (editedData.observations || []).concat(flagObs);
  }
  var totalRows = Object.keys(editedData).reduce(function (sum, key) {
    var v = editedData[key];
    return sum + (Array.isArray(v) ? v.length : 0);
  }, 0);
  if (totalRows === 0 && !editedData.tank_specs) {
    alert(reviewConfig.noneSelected);
    return;
  }
  var btn = document.getElementById('confirm-btn');
  btn.disabled = true;
  btn.textContent = reviewConfig.busyLabel;
  try {
    var res = await fetch('/tanks/' + TANK_ID + '/import/confirm', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ preview: editedData }),
    });
    var data = await res.json();
    if (!res.ok) throw new Error(data.detail || reviewConfig.failPrefix);
    document.getElementById('preview-section').style.display = 'none';
    var result = document.getElementById(reviewConfig.resultId);
    result.style.display = 'block';
    var summary = Object.keys(data.inserted || {}).map(function (k) {
      return data.inserted[k] + ' ' + k.replace(/_/g, ' ');
    }).join(', ');
    result.innerHTML = reviewConfig.doneHtml(summary);
  } catch (e) {
    alert(reviewConfig.failPrefix + ': ' + e.message);
    btn.disabled = false;
    btn.textContent = reviewConfig.idleLabel;
  }
}

// ── Section config ──────────────────────────────────────────────────────────
const SECTION_CONFIG = {
  test_results: {
    label: 'Test Results',
    fields:  ['timestamp','ph','gh','kh','ammonia','nitrite','nitrate','tds','temp','notes'],
    headers: ['Date','pH','GH','KH','NH₃','NO₂','NO₃','TDS','Temp','Notes'],
    types:   ['text','num','num','num','num','num','num','num','num','text'],
    widths:  ['130px','55px','55px','55px','60px','60px','60px','60px','60px','120px'],
  },
  events: {
    label: 'Events',
    fields:  ['timestamp','event_type','amount','notes'],
    headers: ['Date','Type','Amount','Notes'],
    types:   ['text','text','num','text'],
    widths:  ['130px','110px','70px','180px'],
  },
  purchases: {
    label: 'Purchases',
    fields:  ['purchase_date','item','category','vendor','cost','notes'],
    headers: ['Date','Item','Category','Vendor','Cost ($)','Notes'],
    types:   ['text','text','text','text','num','text'],
    widths:  ['95px','140px','100px','100px','70px','150px'],
  },
  inhabitants: {
    label: 'Inhabitants',
    fields:  ['common_name','species','count','added_date','source','notes'],
    headers: ['Common Name','Species','Count','Date Added','Source','Notes'],
    types:   ['text','text','num','text','text','text'],
    widths:  ['120px','120px','65px','95px','90px','120px'],
  },
  plants: {
    label: 'Plants',
    fields:  ['common_name','species','added_date','source','status','notes'],
    headers: ['Common Name','Species','Date Added','Source','Status','Notes'],
    types:   ['text','text','text','text','text','text'],
    widths:  ['120px','120px','95px','90px','80px','140px'],
  },
  equipment: {
    label: 'Equipment',
    fields:  ['category','brand','model','installed_date','notes'],
    headers: ['Category','Brand','Model','Installed','Notes'],
    types:   ['text','text','text','text','text'],
    widths:  ['80px','90px','130px','95px','160px'],
  },
  hardscape: {
    label: 'Hardscape',
    fields:  ['item','quantity','source','cost','added_date','notes'],
    headers: ['Item','Qty','Source','Cost ($)','Date','Notes'],
    types:   ['text','num','text','num','text','text'],
    widths:  ['140px','50px','90px','70px','95px','140px'],
  },
  issues: {
    label: 'Issues',
    fields:  ['title','description','status','opened_at','resolved_at','notes'],
    headers: ['Title','Description','Status','Opened','Resolved','Notes'],
    types:   ['text','text','text','text','text','text'],
    widths:  ['120px','180px','80px','95px','95px','120px'],
  },
  observations: {
    label: 'Observations / Journal',
    fields:  ['created_at','text'],
    headers: ['Date','Text'],
    types:   ['text','text'],
    widths:  ['130px','300px'],
  },
  recurring_schedule: {
    label: 'Recurring Schedule',
    fields:  ['day_of_week','time_of_day','category','description','interval_type','interval_days','last_done','next_due','notes'],
    headers: ['Day','AM/PM','Category','Description','Interval Type','Every (days)','Last Done','Next Due','Notes'],
    types:   ['text','text','text','text','text','num','text','text','text'],
    widths:  ['70px','60px','90px','160px','100px','80px','95px','95px','140px'],
  },
};

// ── Flag helpers ────────────────────────────────────────────────────────────
function buildFlagMap(flags) {
  const map = {};
  for (const f of flags) {
    const rowKey = `${f.section}:${f.index}`;
    if (!map[rowKey]) map[rowKey] = [];
    map[rowKey].push(f.message);
  }
  return map;
}

// ── Render ──────────────────────────────────────────────────────────────────
function showPreview(preview, counts, flags) {
  document.getElementById(reviewConfig.inputSectionId).style.display = 'none';
  document.getElementById('preview-section').style.display = 'block';

  const flagMap = buildFlagMap(flags || []);

  // Counts bar
  const countsEl = document.getElementById('preview-counts');
  countsEl.innerHTML = Object.entries(counts)
    .filter(([k]) => k === 'tank_specs' || SECTION_CONFIG[k])
    .map(([k, v]) => `<span class="count-chip"><strong>${esc(String(v))}</strong> ${esc(k.replace(/_/g,' '))}</span>`)
    .join('');

  // Flags banner
  const flagsEl = document.getElementById('preview-flags');
  if (flags && flags.length > 0) {
    flagsEl.style.display = 'block';
    flagsEl.innerHTML = `<strong>⚠ ${flags.length} item${flags.length !== 1 ? 's' : ''} flagged for review</strong> — verify the highlighted rows below before ${reviewConfig.flagBefore}.`;
  } else {
    flagsEl.style.display = 'none';
    flagsEl.innerHTML = '';
  }

  const dataEl = document.getElementById('preview-data');
  dataEl.innerHTML = '';

  // Tank specs (special)
  if (preview.tank_specs) {
    const specKeys = Object.keys(preview.tank_specs).filter(k => preview.tank_specs[k] !== null && preview.tank_specs[k] !== undefined);
    if (specKeys.length) dataEl.appendChild(renderSpecsSection(preview.tank_specs, specKeys));
  }

  // Array sections
  for (const [key, config] of Object.entries(SECTION_CONFIG)) {
    const items = preview[key];
    if (!items || items.length === 0) continue;
    dataEl.appendChild(renderSection(key, config, items, flagMap));
  }
}

function renderSpecsSection(specs, specKeys) {
  const div = document.createElement('div');
  div.className = 'preview-section';

  const fieldLabels = {
    volume_gallons: 'Volume (gallons)', dimensions_l: 'Length (in)', dimensions_w: 'Width (in)',
    dimensions_h: 'Height (in)', manufacturer: 'Manufacturer', model: 'Model',
    substrate_type: 'Substrate Type', substrate_brand: 'Substrate Brand',
    substrate_depth_inches: 'Substrate Depth (in)', setup_date: 'Setup Date', notes: 'Notes',
  };

  div.innerHTML = `
    <div class="preview-section-header">
      <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer">
        <input type="checkbox" id="apply-specs" checked>
        <strong>Tank Specifications</strong>
      </label>
      <span class="muted" style="font-size:.8rem">— apply proposed updates to this tank</span>
    </div>`;

  const table = document.createElement('table');
  table.className = 'specs-review-table';
  const tbody = document.createElement('tbody');

  for (const field of specKeys) {
    if (!Object.prototype.hasOwnProperty.call(fieldLabels, field)) continue;
    const val = specs[field];
    if (val === null || val === undefined) continue;
    const label = fieldLabels[field];
    const inputType = typeof val === 'number' ? 'number' : 'text';
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${esc(label)}</td><td><input type="${inputType}" step="any" data-specs-field="${esc(field)}" value="${esc(String(val))}"></td>`;
    tbody.appendChild(tr);
  }

  table.appendChild(tbody);
  div.appendChild(table);
  return div;
}

function renderSection(key, config, items, flagMap) {
  const div = document.createElement('div');
  div.className = 'preview-section';

  const header = document.createElement('div');
  header.className = 'preview-section-header';
  header.innerHTML = `
    <strong>${config.label}</strong>
    <span class="muted">(${items.length})</span>
    <button class="btn btn-xs btn-ghost" type="button" onclick="toggleAll('${key}',true)">All</button>
    <button class="btn btn-xs btn-ghost" type="button" onclick="toggleAll('${key}',false)">None</button>`;
  div.appendChild(header);

  const wrapper = document.createElement('div');
  wrapper.className = 'table-wrapper';

  const table = document.createElement('table');
  table.className = 'data-table preview-table';
  table.id = `table-${key}`;

  const thead = document.createElement('thead');
  thead.innerHTML = `<tr><th style="width:28px">✓</th>${config.headers.map((h, i) => `<th style="min-width:${config.widths[i]}">${h}</th>`).join('')}</tr>`;
  table.appendChild(thead);

  const tbody = document.createElement('tbody');

  items.forEach((item, idx) => {
    const rowKey = `${key}:${idx}`;
    const rowFlags = flagMap[rowKey] || [];
    const hasFlag = rowFlags.length > 0;

    const tr = document.createElement('tr');
    tr.dataset.idx = idx;
    tr.dataset.section = key;
    if (hasFlag) tr.className = 'flagged-row';

    let cells = `<td><input type="checkbox" class="row-check" data-section="${key}" data-idx="${idx}" checked></td>`;

    config.fields.forEach((field, fi) => {
      const val = item[field];
      const type = config.types[fi];
      const inputType = type === 'num' ? 'number' : 'text';
      const stepAttr = type === 'num' ? ' step="any"' : '';
      const w = config.widths[fi];

      // Special: inhabitants count_unknown
      if (key === 'inhabitants' && field === 'count' && item.count_unknown) {
        cells += `<td><span class="many-badge">many</span><input type="hidden" data-field="${field}" value=""></td>`;
      } else if (field === 'text' || field === 'notes' || field === 'description') {
        const displayVal = val !== null && val !== undefined ? esc(String(val)) : '';
        cells += `<td><input type="text" data-field="${field}" value="${displayVal}" style="width:${w}"></td>`;
      } else {
        let rawVal = val !== null && val !== undefined ? String(val) : '';
        if ((field === 'timestamp' || field === 'created_at') && rawVal.includes(' ')) {
          rawVal = rawVal.split(' ')[0];
        }
        const displayVal = esc(rawVal);
        cells += `<td><input type="${inputType}"${stepAttr} data-field="${field}" value="${displayVal}" style="width:${w}"></td>`;
      }
    });

    tr.innerHTML = cells;
    tbody.appendChild(tr);

    if (hasFlag) {
      const flagTr = document.createElement('tr');
      flagTr.className = 'flag-row';
      flagTr.innerHTML = `<td></td><td colspan="${config.fields.length}" class="flag-msg">⚠ ${rowFlags.map(esc).join(' | ')}</td>`;
      tbody.appendChild(flagTr);
    }
  });

  table.appendChild(tbody);
  wrapper.appendChild(table);
  div.appendChild(wrapper);
  return div;
}

function toggleAll(section, checked) {
  document.querySelectorAll(`.row-check[data-section="${section}"]`).forEach(cb => cb.checked = checked);
}

// ── Collect edited data ─────────────────────────────────────────────────────
function getEditedData() {
  const result = {};

  // Tank specs
  const applySpecs = document.getElementById('apply-specs');
  if (applySpecs && applySpecs.checked) {
    const specs = {};
    document.querySelectorAll('[data-specs-field]').forEach(input => {
      const f = input.dataset.specsField;
      const v = input.value.trim();
      if (v !== '') specs[f] = input.type === 'number' && !isNaN(v) ? parseFloat(v) : v;
    });
    if (Object.keys(specs).length) result.tank_specs = specs;
  }

  // Array sections
  for (const key of Object.keys(SECTION_CONFIG)) {
    const table = document.getElementById(`table-${key}`);
    if (!table) continue;

    const rows = [];
    table.querySelectorAll('tbody tr[data-idx]').forEach(tr => {
      const cb = tr.querySelector('.row-check');
      if (!cb || !cb.checked) return;

      const idx = parseInt(tr.dataset.idx);
      const original = JSON.parse(JSON.stringify(previewData[key][idx]));

      tr.querySelectorAll('[data-field]').forEach(input => {
        const f = input.dataset.field;
        const v = input.value.trim();
        original[f] = v === '' ? null : (input.type === 'number' && !isNaN(v) ? parseFloat(v) : v);
      });

      rows.push(original);
    });

    if (rows.length) result[key] = rows;
  }

  return result;
}

// ── Convert accepted flags to observations ──────────────────────────────────
const FLAG_DATE_FIELD = {
  test_results: 'timestamp', events: 'timestamp', purchases: 'purchase_date',
  inhabitants: 'added_date', plants: 'added_date', equipment: 'installed_date',
  hardscape: 'added_date', issues: 'opened_at', observations: 'created_at',
};

function getSelectedFlagObservations() {
  const obs = [];
  for (const flag of serverFlags) {
    const table = document.getElementById(`table-${flag.section}`);
    if (!table) continue;
    const tr = table.querySelector(`tbody tr[data-idx="${flag.index}"]`);
    if (!tr) continue;
    const cb = tr.querySelector('.row-check');
    if (!cb || !cb.checked) continue;

    let dateStr = null;
    const dateField = FLAG_DATE_FIELD[flag.section];
    if (dateField) {
      const inp = tr.querySelector(`[data-field="${dateField}"]`);
      if (inp && inp.value) dateStr = inp.value.split(' ')[0];
    }

    obs.push({
      text: `Import note: ${flag.message}`,
      created_at: dateStr ? `${dateStr} 00:00:00` : null,
    });
  }
  return obs;
}

// ── Duplicate detection ─────────────────────────────────────────────────────
async function checkDuplicates(preview) {
  try {
    const res = await fetch(`/tanks/${TANK_ID}/import/check-duplicates`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ preview }),
    });
    if (!res.ok) return;
    const data = await res.json();
    const dups = data.duplicates || [];
    if (dups.length > 0) applyDuplicates(dups);
  } catch (e) {
    // silent — dup check is best-effort
  }
}

function applyDuplicates(dups) {
  const dupsEl = document.getElementById('preview-dups');
  dupsEl.style.display = 'block';
  const n = dups.length;
  dupsEl.innerHTML = `<strong>↔ ${n} probable duplicate${n !== 1 ? 's' : ''} found</strong> — rows unchecked by default. ${reviewConfig.dupBanner}`;

  for (const dup of dups) {
    const table = document.getElementById(`table-${dup.section}`);
    if (!table) continue;
    const tr = table.querySelector(`tbody tr[data-idx="${dup.index}"]`);
    if (!tr) continue;

    const cb = tr.querySelector('.row-check');
    if (cb && dup.auto_uncheck !== false) cb.checked = false;
    tr.classList.add('dup-row');

    const colCount = tr.querySelectorAll('td').length;
    const dupTr = document.createElement('tr');
    dupTr.className = 'dup-flag-row';
    const suffix = dup.auto_uncheck === false ? '' : reviewConfig.dupSuffix;
    dupTr.innerHTML = `<td></td><td colspan="${colCount - 1}" class="dup-row-msg">↔ ${esc(dup.message)}${suffix}</td>`;
    tr.insertAdjacentElement('afterend', dupTr);
  }
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
