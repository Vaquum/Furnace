/* Creation modal — one cohesive control reached from the jobs-page "+".
   Two targets: a brand-new experiment (name only — create + seed its args)
   or an existing experiment (load its working state, then start a round).
   The round-args controls are shared by both; only the submit action
   differs, so the primary button relabels itself per target. */
(() => {
  const modal = document.getElementById('create-modal');
  const openBtn = document.getElementById('open-create');
  if (!modal || !openBtn) return;

  const closeBtn = document.getElementById('create-close');
  const titleEl = document.getElementById('create-title');
  const submitBtn = document.getElementById('m-submit');
  const statusEl = document.getElementById('m-status');
  const nameInput = document.getElementById('m-new-name');
  const sfdInput = document.getElementById('m-sfd');
  const permInput = document.getElementById('m-perm');
  const prepCb = document.getElementById('m-prep');
  const randomCb = document.getElementById('m-random');
  const maintainCb = document.getElementById('m-maintain');
  const dsControls = document.getElementById('m-ds-controls');
  const dsGroup = document.getElementById('m-ds-group');
  const editBtn = document.getElementById('m-edit-manifest');

  const SPLIT_KEYS = ['train_start', 'train_end', 'val_start', 'val_end', 'test_start', 'test_end'];

  // Currently-loaded target state.
  let target = '__new__';            // '__new__' or an experiment id
  let loadedVersion = 0;
  let loadedCode = '';
  let loadedMethod = 'get_spot_klines';
  let manifestMethod = null;
  let savedParams = {};

  function splitEl(key) { return document.getElementById('m-' + key.replaceAll('_', '-')); }
  function splitVal(key) { const el = splitEl(key); return el ? el.value.trim() : ''; }

  function setStatus(text, kind) {
    statusEl.textContent = text || '';
    statusEl.style.color = kind === 'error' ? 'var(--negative)'
      : kind === 'ok' ? 'var(--accent)'
      : 'var(--ink-mute)';
  }

  function clearForm() {
    sfdInput.value = '';
    permInput.value = '1';
    prepCb.checked = false;
    randomCb.checked = false;
    maintainCb.checked = false;
    SPLIT_KEYS.forEach(k => { const el = splitEl(k); if (el) el.value = ''; });
    dsControls.innerHTML = '';
    dsGroup.hidden = true;
    savedParams = {};
    manifestMethod = null;
  }

  function openModal() {
    const firstRadio = modal.querySelector('input[name="create-target"][value="__new__"]');
    if (firstRadio) firstRadio.checked = true;
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
    setStatus('');
    selectTarget('__new__');
    nameInput.focus();
  }
  function closeModal() {
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
  }

  function renderDs(schema) {
    dsControls.innerHTML = '';
    const sig = (schema && schema.library_signature) || [];
    const manifest = (schema && schema.manifest_params) || {};
    manifestMethod = (schema && schema.manifest_method) || null;
    if (sig.length === 0) {
      dsGroup.hidden = true;
      return;
    }
    dsGroup.hidden = false;
    for (const param of sig) {
      const name = param.name;
      // start/end_date_limit are derived from the split-date fields below.
      if (name === 'start_date_limit' || name === 'end_date_limit') continue;
      const ann = (param.annotation || '').toLowerCase();
      const isBool = /\bbool\b/.test(ann);
      const isInt = /\bint\b/.test(ann) && !/\bfloat\b/.test(ann);
      const isNum = isInt || /\bfloat\b/.test(ann);
      const prefill = savedParams[name] !== undefined ? savedParams[name]
        : manifest[name] !== undefined ? manifest[name]
        : (param.default != null ? param.default : '');

      const cell = document.createElement('div');
      cell.className = 'col field';
      const label = document.createElement('label');
      label.htmlFor = 'm-ds-' + name;
      label.textContent = name;
      cell.appendChild(label);

      const el = document.createElement('input');
      if (isBool) {
        el.type = 'checkbox';
        el.checked = !!prefill;
      } else {
        el.type = isNum ? 'number' : 'text';
        if (prefill !== '' && prefill != null) el.value = prefill;
        if (isInt) el.step = '1';
      }
      el.id = 'm-ds-' + name;
      el.dataset.paramName = name;
      if (isInt) el.dataset.numKind = 'int';
      cell.appendChild(el);
      dsControls.appendChild(cell);
    }
  }

  async function inspect() {
    if (target === '__new__') return;
    try {
      const r = await fetch(`/experiments/${encodeURIComponent(target)}/inspect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: loadedCode, sfd_identifier: sfdInput.value.trim() }),
      });
      if (!r.ok) { renderDs(null); return; }
      const schema = await r.json();
      renderDs((schema && schema.data_source) || null);
    } catch (e) {
      renderDs(null);
    }
  }

  async function selectTarget(value) {
    target = value;
    clearForm();
    if (value === '__new__') {
      titleEl.textContent = 'New experiment';
      submitBtn.textContent = 'Create experiment';
      // A new experiment has no code yet — adding the manifest is a primary
      // path, so the button reads "Add manifest" and carries the accent.
      editBtn.textContent = 'Add manifest';
      editBtn.classList.add('primary');
      nameInput.disabled = false;
      loadedVersion = 0;
      loadedCode = '';
      loadedMethod = 'get_spot_klines';
      return;
    }
    titleEl.textContent = value;
    submitBtn.textContent = 'Start round';
    editBtn.textContent = 'Edit manifest';
    editBtn.classList.remove('primary');
    nameInput.disabled = true;
    setStatus(`Loading ${value}…`);
    let data;
    try {
      const r = await fetch(`/experiments/${encodeURIComponent(value)}/working.json`);
      if (!r.ok) { setStatus(`Failed to load ${value} (${r.status})`, 'error'); return; }
      data = await r.json();
    } catch (e) {
      setStatus(`Failed to load ${value}`, 'error');
      return;
    }
    loadedVersion = data.version || 0;
    loadedCode = data.code || '';
    const args = data.args || {};
    const ds = args.data_source || {};
    const uel = args.uel_run || {};
    const sd = args.split_dates || {};
    savedParams = ds.params || {};
    loadedMethod = ds.method || 'get_spot_klines';
    sfdInput.value = args.sfd_identifier || '';
    permInput.value = uel.n_permutations != null ? uel.n_permutations : 1;
    prepCb.checked = !!uel.prep_each_round;
    randomCb.checked = !!uel.random_search;
    maintainCb.checked = !!uel.maintain_details_in_params;
    SPLIT_KEYS.forEach(k => {
      const el = splitEl(k);
      if (!el) return;
      let v = sd[k] || '';
      if (!v && k === 'train_start') v = savedParams.start_date_limit || '';
      if (!v && k === 'test_end') v = savedParams.end_date_limit || '';
      el.value = v;
    });
    setStatus('');
    await inspect();
  }

  function gatherDsParams() {
    const fields = dsControls.querySelectorAll('input[data-param-name]');
    if (fields.length === 0) return { ...savedParams };
    const params = {};
    fields.forEach(f => {
      const name = f.dataset.paramName;
      if (f.type === 'checkbox') { params[name] = f.checked; return; }
      const s = f.value.trim();
      if (!s) return;
      if (f.type === 'number') {
        const n = Number(s);
        if (Number.isNaN(n)) return;
        params[name] = (f.dataset.numKind === 'int') ? Math.trunc(n) : n;
        return;
      }
      params[name] = s;
    });
    return params;
  }

  function gatherArgs() {
    const params = gatherDsParams();
    const trainStart = splitVal('train_start');
    const testEnd = splitVal('test_end');
    if (trainStart) params.start_date_limit = trainStart;
    else delete params.start_date_limit;
    if (testEnd) params.end_date_limit = testEnd;
    else delete params.end_date_limit;

    const vals = {};
    SPLIT_KEYS.forEach(k => { vals[k] = splitVal(k); });
    const allSix = SPLIT_KEYS.every(k => vals[k]);

    const args = {
      sfd_identifier: sfdInput.value.trim(),
      data_source: {
        method: manifestMethod || loadedMethod || 'get_spot_klines',
        params: params,
      },
      uel_run: {
        experiment_name: target === '__new__' ? '' : target,
        n_permutations: parseInt(permInput.value, 10) || 0,
        prep_each_round: prepCb.checked,
        random_search: randomCb.checked,
        maintain_details_in_params: maintainCb.checked,
      },
    };
    if (allSix) args.split_dates = vals;
    return args;
  }

  async function postJson(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  async function submit() {
    submitBtn.disabled = true;
    try {
      if (target === '__new__') {
        const name = nameInput.value.trim();
        if (!name) { setStatus('Experiment name is required.', 'error'); return; }
        setStatus('Creating experiment…');
        const cr = await postJson('/experiments', { name });
        if (!cr.ok) { setStatus(`Create failed (${cr.status})`, 'error'); return; }
        const created = await cr.json();
        const wr = await postJson(`/experiments/${encodeURIComponent(created.id)}/working`,
          { if_match: 0, args: gatherArgs() });
        if (!wr.ok) { setStatus(`Experiment created, but saving args failed (${wr.status})`, 'error'); return; }
        location.reload();
        return;
      }
      // Existing experiment: save the round args, then start the round.
      setStatus('Saving args…');
      const wr = await postJson(`/experiments/${encodeURIComponent(target)}/working`,
        { if_match: loadedVersion, args: gatherArgs() });
      if (!wr.ok) {
        let detail = `Save failed (${wr.status})`;
        try {
          const b = await wr.json();
          detail = b.detail || (b.current_version !== undefined
            ? 'This experiment changed elsewhere — reopen the modal.' : detail);
        } catch (_) {}
        setStatus(detail, 'error');
        return;
      }
      const saved = await wr.json();
      setStatus('Starting round…');
      const rr = await postJson(`/experiments/${encodeURIComponent(target)}/rounds`,
        { if_match: saved.version });
      if (!rr.ok) {
        let detail = `Start failed (${rr.status})`;
        try { const b = await rr.json(); detail = b.detail || b.error || detail; } catch (_) {}
        setStatus(detail, 'error');
        return;
      }
      location.reload();
    } finally {
      submitBtn.disabled = false;
    }
  }

  // Edit manifest — jump to the code editor (the SFD/manifest lives in code).
  // A new experiment must exist on disk first, so create it (+ seed args)
  // before navigating; an existing one is opened straight away.
  async function editManifest() {
    if (target === '__new__') {
      const name = nameInput.value.trim();
      if (!name) { setStatus('Experiment name is required.', 'error'); return; }
      editBtn.disabled = true;
      submitBtn.disabled = true;
      try {
        setStatus('Creating experiment…');
        const cr = await postJson('/experiments', { name });
        if (!cr.ok) { setStatus(`Create failed (${cr.status})`, 'error'); return; }
        const created = await cr.json();
        await postJson(`/experiments/${encodeURIComponent(created.id)}/working`,
          { if_match: 0, args: gatherArgs() });
        location.href = `/experiments/${encodeURIComponent(created.id)}/code`;
      } finally {
        editBtn.disabled = false;
        submitBtn.disabled = false;
      }
      return;
    }
    location.href = `/experiments/${encodeURIComponent(target)}/code`;
  }

  // Date fields open the native calendar on click — pick, don't type.
  SPLIT_KEYS.forEach(k => {
    const el = splitEl(k);
    if (el) {
      el.addEventListener('click', () => {
        try { el.showPicker(); } catch (e) {}
      });
    }
  });

  openBtn.addEventListener('click', openModal);
  closeBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modal.classList.contains('open')) closeModal();
  });
  modal.querySelectorAll('input[name="create-target"]').forEach(radio => {
    radio.addEventListener('change', () => { if (radio.checked) selectTarget(radio.value); });
  });
  sfdInput.addEventListener('change', () => { if (target !== '__new__') inspect(); });
  submitBtn.addEventListener('click', submit);
  editBtn.addEventListener('click', editManifest);
})();
