function prefillDtLocal() {
  var now = new Date();
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
  var val = now.toISOString().slice(0, 16);
  document.querySelectorAll('input.dt-local').forEach(function (el) {
    if (!el.value) el.value = val;
  });
}
document.addEventListener('DOMContentLoaded', function () { prefillDtLocal(); });

function openEditVessel(btn) {
  const f = document.getElementById('edit-vessel-form');
  f.action = '/cultures/' + CULTURE_ID + '/vessels/' + btn.dataset.id + '/update';
  f.querySelector('[name=name]').value = btn.dataset.name || '';
  f.querySelector('[name=volume_gallons]').value = btn.dataset.volume || '';
  f.querySelector('[name=is_lit]').checked = btn.dataset.lit === '1';
  f.querySelector('[name=is_heated]').checked = btn.dataset.heated === '1';
  f.querySelector('[name=heater_set_f]').value = btn.dataset.heaterSet || '';
  f.querySelector('[name=status]').value = btn.dataset.status || 'active';
  f.querySelector('[name=sort_order]').value = btn.dataset.sort || '0';
  f.querySelector('[name=notes]').value = btn.dataset.notes || '';
  const hitch = f.querySelector('[name=hitchhikers]');
  if (hitch) hitch.value = btn.dataset.hitchhikers || '';
  openModal('modal-edit-vessel');
}

function syncOtherLogForm(form) {
  const kind = form.querySelector('[name=kind]').value;
  const tempKind = form.querySelector('[name=temp_kind]').value;
  const cups = form.querySelector('#other-cups-group');
  if (cups) cups.style.display = kind === 'seed' ? '' : 'none';
  const water = kind === 'temp' && tempKind === 'water';
  const air = kind === 'temp' && tempKind === 'air';
  form.querySelectorAll('.other-air-only').forEach(function (el) {
    el.style.display = air ? '' : 'none';
  });
  form.querySelectorAll('.other-water-only').forEach(function (el) {
    el.style.display = water ? '' : 'none';
    el.querySelectorAll('input, select, textarea').forEach(function (c) {
      c.disabled = !water;
    });
  });
  const tempNow = document.getElementById('other-temp-f-group');
  if (tempNow) {
    tempNow.style.display = water ? 'none' : '';
    tempNow.querySelectorAll('input').forEach(function (c) { c.disabled = water; });
  }
}
document.addEventListener('DOMContentLoaded', function () {
  const otherForm = document.querySelector('#modal-other form');
  if (otherForm) syncOtherLogForm(otherForm);
});

function openEditSched(btn) {
  const f = document.getElementById('edit-sched-form');
  f.action = '/cultures/' + CULTURE_ID + '/schedule/' + btn.dataset.id + '/update';
  f.querySelector('[name=category]').value = btn.dataset.category || 'feeding';
  f.querySelector('[name=description]').value = btn.dataset.description || '';
  f.querySelector('[name=tracking_mode]').value = btn.dataset.mode || 'logged';
  f.querySelector('[name=interval_days]').value = btn.dataset.interval || '';
  f.querySelector('[name=vessel_id]').value = btn.dataset.vessel || '';
  f.querySelector('[name=is_active]').value = btn.dataset.active === '0' ? '0' : '1';
  f.querySelector('[name=notes]').value = btn.dataset.notes || '';
  f.querySelector('[name=last_done]').value = btn.dataset.lastDone || '';
  f.querySelector('[name=next_due]').value = btn.dataset.nextDue || '';
  syncCultureSchedDates();
  openModal('modal-edit-sched');
}

function syncCultureSchedDates() {
  const f = document.getElementById('edit-sched-form');
  if (!f) return;
  const logged = f.querySelector('[name=tracking_mode]').value === 'logged';
  const lastG = document.getElementById('edit-last-done-group');
  const nextG = document.getElementById('edit-next-due-group');
  if (lastG) lastG.style.display = logged ? '' : 'none';
  if (nextG) nextG.style.display = logged ? '' : 'none';
}

function recomputeCultureNextDue() {
  const f = document.getElementById('edit-sched-form');
  if (!f) return;
  const last = f.querySelector('[name=last_done]').value;
  const intervalRaw = f.querySelector('[name=interval_days]').value;
  if (!last || !intervalRaw) return;
  const from = new Date(last + 'T12:00:00');
  if (isNaN(from.getTime())) return;
  const days = parseInt(intervalRaw, 10);
  if (!days) return;
  from.setDate(from.getDate() + days);
  const y = from.getFullYear();
  const m = String(from.getMonth() + 1).padStart(2, '0');
  const d = String(from.getDate()).padStart(2, '0');
  f.querySelector('[name=next_due]').value = y + '-' + m + '-' + d;
}

function cupsFromAmount(text) {
  if (!text) return '';
  const m = String(text).trim().match(/^(\d+(?:\.\d+)?)\s*cups?$/i);
  return m ? m[1] : '';
}

function setField(form, name, value) {
  const el = form.querySelector('[name="' + name + '"]');
  if (!el) return;
  if (el.type === 'checkbox') el.checked = !!value;
  else el.value = value == null ? '' : value;
}

function setBlockDisabled(el, disabled) {
  if (!el) return;
  el.querySelectorAll('input, select, textarea').forEach(function (c) {
    c.disabled = disabled;
  });
}

function syncEditLogForm(form) {
  if (!form) return;
  const kind = form.querySelector('[name=kind]').value;
  const isFeed = kind === 'feed';
  const isLook = kind === 'look';
  const isHarvest = kind === 'harvest' || kind === 'seed';
  const isTemp = kind === 'temp';
  form.querySelectorAll('.edit-log-feed').forEach(function (el) {
    el.style.display = isFeed ? '' : 'none';
  });
  form.querySelectorAll('.edit-log-look').forEach(function (el) {
    el.style.display = isLook ? '' : 'none';
    el.querySelectorAll('input, select, textarea').forEach(function (c) {
      c.disabled = !isLook;
    });
  });
  const food = document.getElementById('edit-log-food-group');
  if (food) food.style.display = isFeed ? '' : 'none';
  setBlockDisabled(food, !isFeed);
  const held = document.getElementById('edit-log-held-group');
  if (held) held.style.display = (isFeed || isLook) ? '' : 'none';
  const cups = document.getElementById('edit-log-cups-group');
  const amount = document.getElementById('edit-log-amount-group');
  const showCups = isHarvest && (!cups || cups.dataset.show !== '0');
  const showAmount = isHarvest && amount && amount.dataset.show === '1';
  if (cups) cups.style.display = showCups ? '' : 'none';
  if (amount) amount.style.display = showAmount ? '' : 'none';
  setBlockDisabled(cups, !showCups);
  setBlockDisabled(amount, isHarvest ? !showAmount : false);
  const tempKindEl = form.querySelector('[name=temp_kind]');
  const tempKind = tempKindEl ? tempKindEl.value : 'water';
  const waterOnBins = isLook || (isTemp && tempKind === 'water');
  const showCultureTempF = isTemp && tempKind === 'air';
  const tempGrid = document.getElementById('edit-log-temp-grid');
  if (tempGrid) tempGrid.style.display = isTemp ? '' : 'none';
  const tempF = document.getElementById('edit-log-temp-f-group');
  if (tempF) tempF.style.display = showCultureTempF ? '' : 'none';
  setBlockDisabled(tempF, !showCultureTempF);
  const tempFLabel = document.getElementById('edit-log-temp-f-label');
  if (tempFLabel) tempFLabel.textContent = 'Temp now (°F)';
  const tempKindGroup = document.getElementById('edit-log-temp-kind-group');
  if (tempKindGroup) tempKindGroup.style.display = isTemp ? '' : 'none';
  setBlockDisabled(tempKindGroup, !isTemp);
  form.querySelectorAll('.edit-log-water-temp').forEach(function (el) {
    el.style.display = waterOnBins ? '' : 'none';
    el.querySelectorAll('input, select, textarea').forEach(function (c) {
      c.disabled = !waterOnBins;
    });
  });
  form.querySelectorAll('.edit-log-air-only').forEach(function (el) {
    el.style.display = (isTemp && tempKind === 'air') ? '' : 'none';
    el.querySelectorAll('input, select, textarea').forEach(function (c) {
      c.disabled = !(isTemp && tempKind === 'air');
    });
  });
}

function openEditLog(item) {
  const f = document.getElementById('edit-log-form');
  f.reset();
  f.action = '/cultures/' + CULTURE_ID + '/log/' + item.id + '/update';
  document.getElementById('edit-log-title').textContent = 'Edit ' + (KIND_LABELS[item.kind] || item.kind);
  f.querySelector('[name=kind]').value = item.kind || '';
  setField(f, 'food', item.food || '');
  setField(f, 'notes', item.notes || '');
  setField(f, 'held', !!item.held);
  setField(f, 'temp_kind', item.temp_kind || 'water');
  setField(f, 'temp_f', item.temp_f);
  setField(f, 'temp_low', item.temp_low);
  setField(f, 'temp_high', item.temp_high);
  setField(f, 'rh', item.rh);
  setField(f, 'rh_low', item.rh_low);
  setField(f, 'rh_high', item.rh_high);
  const dt = f.querySelector('.dt-local');
  if (dt) dt.value = utcToLocalInputValue(item.timestamp || '');

  const cupsGroup = document.getElementById('edit-log-cups-group');
  const amountGroup = document.getElementById('edit-log-amount-group');
  const cupsVal = cupsFromAmount(item.amount_text);
  if (item.kind === 'harvest' || item.kind === 'seed') {
    if (cupsVal) {
      setField(f, 'cups', cupsVal);
      setField(f, 'amount_text', '');
      if (cupsGroup) cupsGroup.dataset.show = '1';
      if (amountGroup) amountGroup.dataset.show = '0';
    } else if (item.amount_text) {
      setField(f, 'cups', '');
      setField(f, 'amount_text', item.amount_text);
      if (cupsGroup) cupsGroup.dataset.show = '0';
      if (amountGroup) amountGroup.dataset.show = '1';
    } else {
      setField(f, 'cups', '');
      setField(f, 'amount_text', '');
      if (cupsGroup) cupsGroup.dataset.show = '1';
      if (amountGroup) amountGroup.dataset.show = '0';
    }
  } else {
    setField(f, 'cups', '');
    setField(f, 'amount_text', item.amount_text || '');
    if (cupsGroup) cupsGroup.dataset.show = '0';
    if (amountGroup) amountGroup.dataset.show = '0';
  }

  const bins = item.bins || [];
  const byId = {};
  bins.forEach(function (b) { byId[String(b.vessel_id)] = b; });
  f.querySelectorAll('[name=vessel_ids]').forEach(function (cb) {
    const b = byId[cb.value];
    cb.checked = !!b;
    const tint = f.querySelector('[name="tint_' + cb.value + '"]');
    const density = f.querySelector('[name="density_' + cb.value + '"]');
    const guts = f.querySelector('[name="guts_' + cb.value + '"]');
    const amt = f.querySelector('[name="amount_' + cb.value + '"]');
    const binTemp = f.querySelector('[name="temp_' + cb.value + '"]');
    if (!cb.checked) return;
    if (tint) tint.value = (b && b.tint) || item.tint || '';
    if (density) density.value = (b && b.density) || item.density || '';
    if (guts) guts.value = (b && b.guts) || item.guts || '';
    if (amt) amt.value = (b && b.amount_text) || '';
    if (binTemp) {
      const t = (b && b.temp_f != null) ? b.temp_f : item.temp_f;
      binTemp.value = t == null ? '' : t;
    }
    const binNotes = f.querySelector('[name="notes_' + cb.value + '"]');
    if (binNotes) binNotes.value = (b && b.notes) || '';
  });
  syncEditLogForm(f);
  openModal('modal-edit-log');
}
