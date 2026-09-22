var labExtractedReadings = [];

function openAddHomeWater() {
  switchAddTab('manual');
  backToLabUpload();
  var input = document.getElementById('add-home-water-ts');
  if (input) {
    var now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    input.value = now.toISOString().slice(0, 16);
  }
  openModal('modal-add-home-water');
}

function switchAddTab(which) {
  var manual = which === 'manual';
  document.getElementById('panel-manual').style.display = manual ? '' : 'none';
  document.getElementById('panel-lab').style.display = manual ? 'none' : '';
  document.getElementById('tab-manual').classList.toggle('pill-active', manual);
  document.getElementById('tab-lab').classList.toggle('pill-active', !manual);
}

function backToLabUpload() {
  document.getElementById('lab-upload-step').style.display = '';
  document.getElementById('lab-review-step').style.display = 'none';
  document.getElementById('lab-extract-status').style.display = 'none';
  document.getElementById('lab-extract-btn').disabled = false;
  labExtractedReadings = [];
}

function escHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function optSelect(options, selected, nameAttr, idx) {
  var html = '<select name="' + nameAttr + '" data-idx="' + idx + '" data-field="' + nameAttr + '">';
  options.forEach(function (pair) {
    var v = pair[0], lab = pair[1];
    html += '<option value="' + escHtml(v) + '"' + (v === (selected || '') ? ' selected' : '') + '>' + escHtml(lab) + '</option>';
  });
  html += '</select>';
  return html;
}

function renderLabReview(readings) {
  labExtractedReadings = readings;
  var host = document.getElementById('lab-review-list');
  if (!readings.length) {
    host.innerHTML = '<p class="muted">No readings extracted.</p>';
    return;
  }
  var html = '';
  readings.forEach(function (r, i) {
    var flags = (r.flags || []).map(function (f) {
      return '<div class="muted" style="font-size:.8rem;color:var(--warning)">⚠ ' + escHtml(f) + '</div>';
    }).join('');
    var tsLocal = '';
    if (r.timestamp) {
      // Show as datetime-local value in browser TZ when possible
      try { tsLocal = utcToLocalInputValue(r.timestamp); } catch (e) { tsLocal = (r.timestamp || '').replace(' ', 'T').slice(0, 16); }
    }
    html += '<div class="lab-review-card" data-idx="' + i + '" style="border:1px solid var(--border);border-radius:var(--radius);padding:.75rem;margin-bottom:.75rem;background:var(--card)">';
    html += '<label style="display:flex;align-items:center;gap:.4rem;margin-bottom:.6rem;font-weight:600">'
      + '<input type="checkbox" class="lab-row-check" data-idx="' + i + '" checked> Reading ' + (i + 1)
      + (r.is_lab_test ? ' <span class="badge badge-warning">Lab</span>' : '')
      + '</label>';
    html += flags;
    html += '<div class="form-group"><label>Date &amp; Time</label>'
      + '<input type="datetime-local" class="lab-field" data-idx="' + i + '" data-field="timestamp_local" value="' + escHtml(tsLocal) + '">'
      + '</div>';
    html += '<div class="form-grid">';
    ['gh','kh','ph','nitrate','tds','ammonia','nitrite','temp'].forEach(function (field) {
      var labels = {gh:'GH (dGH)',kh:'KH (dKH)',ph:'pH',nitrate:'NO₃ (ppm)',tds:'TDS',ammonia:'NH₃',nitrite:'NO₂',temp:'Temp °F'};
      var val = r[field] != null ? r[field] : '';
      html += '<div class="form-group"><label>' + labels[field] + '</label>'
        + '<input type="number" step="any" class="lab-field" data-idx="' + i + '" data-field="' + field + '" value="' + escHtml(val) + '">'
        + '</div>';
    });
    html += '</div>';
    html += '<div class="form-grid" style="margin-top:.5rem">';
    html += '<div class="form-group"><label>Sample point</label>' + optSelect(SAMPLE_POINTS, r.sample_point || 'tap', 'sample_point', i) + '</div>';
    html += '<div class="form-group"><label>Hard / soft blend</label>' + optSelect(WATER_BLENDS, r.water_blend || '', 'water_blend', i) + '</div>';
    html += '</div>';
    html += '<div class="form-group"><label>Notes</label>'
      + '<textarea class="lab-field" data-idx="' + i + '" data-field="notes" rows="2">' + escHtml(r.notes || '') + '</textarea></div>';
    html += '</div>';
  });
  host.innerHTML = html;
}

async function extractLabReport() {
  var fileInput = document.getElementById('lab-file');
  var file = fileInput.files && fileInput.files[0];
  if (!file) {
    alert('Choose a PDF or CSV lab report first.');
    return;
  }
  var status = document.getElementById('lab-extract-status');
  var btn = document.getElementById('lab-extract-btn');
  status.style.display = '';
  status.textContent = 'Extracting with AI… this can take 15–40s for PDFs.';
  btn.disabled = true;

  var fd = new FormData();
  fd.append('file', file);
  fd.append('sample_point', document.getElementById('lab-sample-point').value);
  fd.append('water_blend', document.getElementById('lab-water-blend').value);
  fd.append('user_notes', document.getElementById('lab-user-notes').value);

  try {
    var res = await fetch('/home-water/extract', { method: 'POST', body: fd });
    var body = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      throw new Error(body.detail || ('Extraction failed (' + res.status + ')'));
    }
    document.getElementById('lab-upload-step').style.display = 'none';
    document.getElementById('lab-review-step').style.display = '';
    renderLabReview(body.readings || []);
    status.style.display = 'none';
  } catch (err) {
    status.textContent = err.message || String(err);
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false;
  }
}

function collectLabReading(idx) {
  var card = document.querySelector('.lab-review-card[data-idx="' + idx + '"]');
  if (!card) return null;
  function val(field) {
    var el = card.querySelector('[data-field="' + field + '"]');
    return el ? el.value : '';
  }
  function num(field) {
    var v = val(field);
    if (v === '' || v == null) return null;
    var n = parseFloat(v);
    return isNaN(n) ? null : n;
  }
  var localTs = val('timestamp_local');
  var ts = localTs ? localDatetimeToUTCString(localTs) : null;
  return {
    timestamp: ts,
    gh: num('gh'),
    kh: num('kh'),
    ph: num('ph'),
    nitrate: num('nitrate'),
    tds: num('tds'),
    ammonia: num('ammonia'),
    nitrite: num('nitrite'),
    temp: num('temp'),
    sample_point: val('sample_point') || 'tap',
    water_blend: val('water_blend') || null,
    is_lab_test: 1,
    notes: val('notes') || null,
  };
}

async function saveLabReadings() {
  var checks = document.querySelectorAll('.lab-row-check:checked');
  if (!checks.length) {
    alert('Select at least one reading to save.');
    return;
  }
  var readings = [];
  checks.forEach(function (cb) {
    var r = collectLabReading(cb.dataset.idx);
    if (r) readings.push(r);
  });
  var btn = document.getElementById('lab-save-btn');
  btn.disabled = true;
  try {
    var res = await fetch('/home-water/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ readings: readings }),
    });
    var body = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(body.detail || ('Save failed (' + res.status + ')'));
    location.reload();
  } catch (err) {
    alert(err.message || String(err));
    btn.disabled = false;
  }
}

function openEditHomeWater(btn) {
  var d = btn.dataset;
  var form = document.getElementById('edit-home-water-form');
  form.action = '/home-water/' + d.id + '/update';
  document.getElementById('edit-home-water-ts').value = utcToLocalInputValue(d.timestamp);
  document.getElementById('edit-home-water-gh').value = d.gh;
  document.getElementById('edit-home-water-kh').value = d.kh;
  document.getElementById('edit-home-water-ph').value = d.ph;
  document.getElementById('edit-home-water-tds').value = d.tds;
  document.getElementById('edit-home-water-ammonia').value = d.ammonia;
  document.getElementById('edit-home-water-nitrite').value = d.nitrite;
  document.getElementById('edit-home-water-nitrate').value = d.nitrate;
  document.getElementById('edit-home-water-temp').value = d.temp;
  document.getElementById('edit-home-water-sample').value = d.samplePoint || 'tap';
  document.getElementById('edit-home-water-blend').value = d.waterBlend || '';
  document.getElementById('edit-home-water-notes').value = d.notes || '';
  openModal('modal-edit-home-water');
}
