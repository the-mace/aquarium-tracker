let activeStatus = 'all';
let _goalReviewOriginal = null; // first-written draft for "Save as first written"
let _goalReviewBusy = false;

function _todayLocalDateStr() {
  const d = new Date();
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

function _resetDepPicker(selectedIds) {
  const selected = new Set((selectedIds || []).map(String));
  document.querySelectorAll('#goal-dep-picker input[type="checkbox"]').forEach(cb => {
    cb.checked = selected.has(cb.value);
  });
}

function _showGoalFormStep() {
  document.getElementById('goal-form-fields').style.display = 'block';
  document.getElementById('goal-review-panel').style.display = 'none';
  document.getElementById('goal-form-footer').style.display = 'flex';
  document.getElementById('goal-review-footer').style.display = 'none';
  document.getElementById('goal-review-error').style.display = 'none';
}

function _showGoalReviewStep() {
  document.getElementById('goal-form-fields').style.display = 'none';
  document.getElementById('goal-review-panel').style.display = 'block';
  document.getElementById('goal-form-footer').style.display = 'none';
  document.getElementById('goal-review-footer').style.display = 'flex';
  const box = document.querySelector('#modal-goal-form .modal-box');
  if (box) box.scrollTop = 0;
}

function openAddGoalModal() {
  const form = document.getElementById('goal-form');
  form.action = `/tanks/${GOAL_TANK_ID}/goals`;
  document.getElementById('goal-form-mode').value = 'create';
  document.getElementById('goal-form-heading').textContent = 'New Goal';
  const btn = document.getElementById('goal-form-submit');
  btn.textContent = 'Review Goal';
  btn.type = 'button';
  btn.setAttribute('onclick', 'startGoalReview()');
  document.getElementById('goal-update-deps').value = '1';
  document.getElementById('goal-title').value = '';
  document.getElementById('goal-target').value = '';
  document.getElementById('goal-description').value = '';
  document.getElementById('goal-notes').value = '';
  document.getElementById('goal-status').value = 'in_progress';
  document.querySelectorAll('#goal-dep-picker .goal-dep-option').forEach(el => {
    el.style.display = '';
  });
  _resetDepPicker([]);
  _goalReviewOriginal = null;
  _goalReviewBusy = false;
  _showGoalFormStep();
  openModal('modal-goal-form');
}

function openEditGoalModal(btn) {
  const card = btn.closest('.goal-card');
  const goalId = card.dataset.goalId;
  const form = document.getElementById('goal-form');
  form.action = `/tanks/${GOAL_TANK_ID}/goals/${goalId}/update`;
  document.getElementById('goal-form-mode').value = 'edit';
  document.getElementById('goal-form-heading').textContent = 'Edit Goal';
  const submitBtn = document.getElementById('goal-form-submit');
  submitBtn.textContent = 'Save Changes';
  submitBtn.type = 'submit';
  submitBtn.removeAttribute('onclick');
  document.getElementById('goal-update-deps').value = '1';
  document.getElementById('goal-title').value = card.dataset.goalTitle || '';
  document.getElementById('goal-target').value = card.dataset.goalTarget || '';
  document.getElementById('goal-description').value = card.dataset.goalDescription || '';
  document.getElementById('goal-notes').value = card.dataset.goalNotes || '';
  document.getElementById('goal-status').value = card.dataset.goalStatus || 'in_progress';
  document.querySelectorAll('#goal-dep-picker .goal-dep-option').forEach(el => {
    el.style.display = el.dataset.pickerId === goalId ? 'none' : '';
  });
  const deps = (card.dataset.goalDeps || '').split(',').filter(Boolean);
  _resetDepPicker(deps);
  _goalReviewOriginal = null;
  _goalReviewBusy = false;
  _showGoalFormStep();
  openModal('modal-goal-form');
}

function _goalFormDraftFromFields() {
  return {
    title: document.getElementById('goal-title').value.trim(),
    target: document.getElementById('goal-target').value.trim(),
    description: document.getElementById('goal-description').value.trim(),
    notes: document.getElementById('goal-notes').value.trim(),
    status: document.getElementById('goal-status').value,
    depends_on: Array.from(document.querySelectorAll('#goal-dep-picker input[name="depends_on"]:checked')).map(cb => cb.value),
  };
}

function _goalFormDraftFromReview() {
  return {
    title: document.getElementById('review-title').value.trim(),
    target: document.getElementById('review-target').value.trim(),
    description: document.getElementById('review-description').value.trim(),
    notes: document.getElementById('review-notes').value.trim(),
    status: document.getElementById('goal-status').value,
    depends_on: Array.from(document.querySelectorAll('#goal-dep-picker input[name="depends_on"]:checked')).map(cb => cb.value),
  };
}

function _applyDraftToFormFields(draft) {
  document.getElementById('goal-title').value = draft.title || '';
  document.getElementById('goal-target').value = draft.target || '';
  document.getElementById('goal-description').value = draft.description || '';
  document.getElementById('goal-notes').value = draft.notes || '';
  if (draft.status) document.getElementById('goal-status').value = draft.status;
  if (draft.depends_on) _resetDepPicker(draft.depends_on);
}

function _applyDraftToReviewFields(draft) {
  document.getElementById('review-title').value = draft.title || '';
  document.getElementById('review-target').value = draft.target || '';
  document.getElementById('review-description').value = draft.description || '';
  document.getElementById('review-notes').value = draft.notes || '';
}

function _setReviewUi(result) {
  const statusEl = document.getElementById('goal-review-status');
  const ok = !!result.reasonable;
  statusEl.textContent = ok ? 'Looks reasonable' : 'Could be improved';
  statusEl.className = 'goal-review-status ' + (ok ? 'ok' : 'warn');
  document.getElementById('goal-review-summary').textContent = result.summary || '';
  const ul = document.getElementById('goal-review-suggestions');
  ul.innerHTML = '';
  (result.suggestions || []).forEach(s => {
    const li = document.createElement('li');
    li.textContent = s;
    ul.appendChild(li);
  });
  // Prefer proposed fields for the editable review form
  const proposed = result.proposed || result.draft || {};
  _applyDraftToReviewFields(proposed);
  const origBtn = document.getElementById('goal-review-save-original');
  if (result.changed) {
    origBtn.style.display = '';
  } else {
    origBtn.style.display = 'none';
  }
}

async function _runGoalReview(draft) {
  const fd = new FormData();
  fd.append('title', draft.title);
  fd.append('target', draft.target || '');
  fd.append('description', draft.description || '');
  fd.append('notes', draft.notes || '');
  (draft.depends_on || []).forEach(id => fd.append('depends_on', id));
  const res = await fetch(`/tanks/${GOAL_TANK_ID}/goals/review`, {
    method: 'POST',
    body: fd,
    headers: { 'Accept': 'application/json' },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Review failed (${res.status})`);
  }
  return res.json();
}

/** Create-flow entry point — called by Review Goal button (type=button). Safari-safe. */
async function startGoalReview() {
  if (_goalReviewBusy) return;
  const mode = document.getElementById('goal-form-mode').value;
  if (mode !== 'create') return;
  const draft = _goalFormDraftFromFields();
  if (!draft.title) {
    const titleEl = document.getElementById('goal-title');
    titleEl.focus();
    if (typeof titleEl.reportValidity === 'function') titleEl.reportValidity();
    else alert('Title is required.');
    return;
  }
  _goalReviewBusy = true;
  const btn = document.getElementById('goal-form-submit');
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Reviewing...';
  document.getElementById('goal-form-heading').textContent = 'Reviewing goal...';
  try {
    if (!_goalReviewOriginal) {
      _goalReviewOriginal = {
        title: draft.title,
        target: draft.target,
        description: draft.description,
        notes: draft.notes,
        status: draft.status,
        depends_on: draft.depends_on.slice(),
      };
    }
    const result = await _runGoalReview(draft);
    _setReviewUi(result);
    document.getElementById('goal-form-heading').textContent = 'Review goal';
    _showGoalReviewStep();
  } catch (err) {
    console.error('Goal review failed', err);
    document.getElementById('goal-form-heading').textContent = 'New Goal';
    alert((err && err.message) ? err.message : 'Review failed');
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
    _goalReviewBusy = false;
  }
}

function goalReviewBackToEdit() {
  // Always restore what the user originally typed — not the AI rewrite.
  const draft = _goalReviewOriginal || _goalFormDraftFromFields();
  _applyDraftToFormFields(draft);
  document.getElementById('goal-form-heading').textContent = 'New Goal';
  const btn = document.getElementById('goal-form-submit');
  btn.textContent = 'Review Goal';
  btn.type = 'button';
  btn.setAttribute('onclick', 'startGoalReview()');
  // Next Review should re-capture original if they change the draft again.
  _goalReviewOriginal = null;
  _showGoalFormStep();
}

async function goalReReview() {
  if (_goalReviewBusy) return;
  const draft = _goalFormDraftFromReview();
  if (!draft.title) {
    document.getElementById('goal-review-error').style.display = '';
    document.getElementById('goal-review-error').textContent = 'Title is required.';
    return;
  }
  _goalReviewBusy = true;
  const btn = document.getElementById('goal-review-rereview');
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Reviewing…';
  document.getElementById('goal-review-error').style.display = 'none';
  try {
    const result = await _runGoalReview(draft);
    _setReviewUi(result);
  } catch (err) {
    document.getElementById('goal-review-error').style.display = '';
    document.getElementById('goal-review-error').textContent = err.message || 'Review failed';
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
    _goalReviewBusy = false;
  }
}

async function goalSaveVersion(which) {
  if (_goalReviewBusy) return;
  let draft;
  if (which === 'original') {
    draft = _goalReviewOriginal || _goalFormDraftFromReview();
  } else {
    draft = _goalFormDraftFromReview();
  }
  if (!draft.title) {
    document.getElementById('goal-review-error').style.display = '';
    document.getElementById('goal-review-error').textContent = 'Title is required.';
    return;
  }
  _goalReviewBusy = true;
  const saveBtn = document.getElementById('goal-review-save');
  const prev = saveBtn.textContent;
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';
  try {
    const fd = new FormData();
    fd.append('title', draft.title);
    fd.append('target', draft.target || '');
    fd.append('description', draft.description || '');
    fd.append('notes', draft.notes || '');
    fd.append('status', draft.status || 'in_progress');
    (draft.depends_on || []).forEach(id => fd.append('depends_on', id));
    const res = await fetch(`/tanks/${GOAL_TANK_ID}/goals`, {
      method: 'POST',
      body: fd,
      headers: { 'Accept': 'application/json' },
    });
    if (!res.ok) throw new Error(await res.text() || `Save failed (${res.status})`);
    window.location.href = `/tanks/${GOAL_TANK_ID}/goals`;
  } catch (err) {
    document.getElementById('goal-review-error').style.display = '';
    document.getElementById('goal-review-error').textContent = err.message || 'Save failed';
    saveBtn.disabled = false;
    saveBtn.textContent = prev;
    _goalReviewBusy = false;
  }
}

function openAchieveModal(btn) {
  const card = btn.closest('.goal-card');
  const form = document.getElementById('achieve-goal-form');
  form.action = `/tanks/${GOAL_TANK_ID}/goals/${card.dataset.goalId}/update`;
  document.getElementById('achieve-goal-title').value = card.dataset.goalTitle;
  form.querySelector('textarea[name="comment"]').value = '';
  document.getElementById('achieve-goal-date').value = _todayLocalDateStr();
  openModal('modal-achieve-goal');
}

function openAddNoteModal(btn) {
  const card = btn.closest('.goal-card');
  const form = document.getElementById('add-note-goal-form');
  form.action = `/tanks/${GOAL_TANK_ID}/goals/${card.dataset.goalId}/update`;
  document.getElementById('add-note-goal-title').value = card.dataset.goalTitle;
  document.getElementById('add-note-goal-status').value = card.dataset.goalStatus;
  document.getElementById('add-note-goal-text').value = '';
  openModal('modal-add-note-goal');
}

function prepareGoalNoteSubmit(form) {
  const textarea = form.querySelector('#add-note-goal-text');
  // Server also stamps dates; keep raw text — backend prefixes YYYY-MM-DD
  return true;
}

function setStatusFilter(btn, status) {
  activeStatus = status;
  document.querySelectorAll('#status-pills .pill').forEach(p => p.classList.remove('pill-active'));
  btn.classList.add('pill-active');
  filterGoals();
}

function filterGoals() {
  const q = (document.getElementById('goal-search')?.value || '').toLowerCase();
  const cards = Array.from(document.querySelectorAll('#goal-list .goal-card'));
  let visibleCount = 0;
  cards.forEach(card => {
    let statusMatch;
    if (activeStatus === 'all') statusMatch = true;
    else if (activeStatus === 'blocked') statusMatch = card.dataset.blocked === '1';
    else statusMatch = card.dataset.status === activeStatus;
    const textMatch = !q || card.dataset.search.includes(q);
    const show = statusMatch && textMatch;
    card.style.display = show ? '' : 'none';
    if (show) visibleCount++;
  });
  const empty = document.getElementById('goal-empty');
  if (empty) empty.style.display = visibleCount === 0 ? '' : 'none';
}
