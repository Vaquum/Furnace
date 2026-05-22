// Creation modal — the cohesive control reached from the jobs-page "+".
// Left pane: pick a target — a brand-new experiment (name only) or an
// existing one. Right pane: that target's manifest, in a full YAML editor.
// Either way the action is the same — save the manifest and start a round
// on it — so the operator always lands on a running round.
(() => {
  const modal = document.getElementById('create-modal');
  const openBtn = document.getElementById('open-create');
  if (!modal || !openBtn) return;

  const closeBtn = document.getElementById('create-close');
  const titleEl = document.getElementById('create-title');
  const submitBtn = document.getElementById('m-submit');
  const statusEl = document.getElementById('m-status');
  const nameInput = document.getElementById('m-new-name');
  const buffer = document.getElementById('m-manifest');
  const errorsBox = document.getElementById('m-errors');
  const copyBtn = document.getElementById('m-copy');

  let target = '__new__';   // '__new__' or an existing experiment id
  let createdEid = null;    // set once a __new__ submit has created the experiment

  function setStatus(text, kind) {
    statusEl.textContent = text || '';
    statusEl.style.color = kind === 'error' ? 'var(--neg)'
      : kind === 'ok' ? 'var(--accent)'
      : 'var(--fg-3)';
  }

  function clearErrors() {
    errorsBox.hidden = true;
    errorsBox.textContent = '';
  }

  function renderErrors(errors) {
    errorsBox.textContent = '';
    for (const e of errors) {
      const row = document.createElement('div');
      row.className = 'manifest-error-row';
      const loc = [];
      if (e.line !== null && e.line !== undefined) loc.push('line ' + e.line);
      if (e.path) loc.push(e.path);
      row.textContent = (loc.length ? '[' + loc.join(' · ') + '] ' : '') + e.message;
      errorsBox.appendChild(row);
    }
    errorsBox.hidden = false;
  }

  // Load text into the YAML editor — dispatch `input` so the highlight
  // overlay and gutter repaint (setting .value alone does not fire it).
  function setManifest(text) {
    buffer.value = text || '';
    buffer.dispatchEvent(new Event('input'));
  }

  function openModal() {
    const firstRadio = modal.querySelector('input[name="create-target"][value="__new__"]');
    if (firstRadio) firstRadio.checked = true;
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
    selectTarget('__new__');
    nameInput.focus();
  }

  function closeModal() {
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
  }

  async function selectTarget(value) {
    target = value;
    createdEid = null;
    clearErrors();
    setStatus('');
    if (value === '__new__') {
      titleEl.textContent = 'New experiment';
      submitBtn.textContent = 'Create & start round';
      nameInput.disabled = false;
      setManifest(window.STARTER_MANIFEST || '');
      return;
    }
    titleEl.textContent = value;
    submitBtn.textContent = 'Start round';
    nameInput.disabled = true;
    setStatus(`Loading ${value}…`);
    try {
      const r = await fetch(`/experiments/${encodeURIComponent(value)}/working.json`);
      if (!r.ok) { setStatus(`Failed to load ${value} (${r.status})`, 'error'); return; }
      const data = await r.json();
      setManifest(data.manifest || '');
      setStatus('');
    } catch (e) {
      setStatus(`Failed to load ${value}`, 'error');
    }
  }

  async function currentVersion(eid) {
    const r = await fetch(`/experiments/${encodeURIComponent(eid)}/working.json`);
    return (await r.json()).version;
  }

  async function submit() {
    clearErrors();
    submitBtn.disabled = true;
    try {
      let eid = target === '__new__' ? createdEid : target;

      if (target === '__new__' && !eid) {
        const name = nameInput.value.trim();
        if (!name) { setStatus('Experiment name is required.', 'error'); return; }
        setStatus('Creating experiment…');
        let cr;
        try {
          cr = await fetch('/experiments', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
          });
        } catch (e) { setStatus('Create failed', 'error'); return; }
        if (!cr.ok) {
          let detail = `Create failed (${cr.status})`;
          try { detail = (await cr.json()).detail || detail; } catch (_) {}
          setStatus(detail, 'error');
          return;
        }
        eid = (await cr.json()).id;
        createdEid = eid;   // a retry must not create a second experiment
      }

      // Read the current version fresh, save the edited manifest against it,
      // then start the round on the saved version.
      setStatus('Saving manifest…');
      let version;
      try {
        version = await currentVersion(eid);
      } catch (e) {
        setStatus('Could not read the experiment version', 'error');
        return;
      }
      let wr;
      try {
        wr = await fetch(`/experiments/${encodeURIComponent(eid)}/manifest`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ manifest: buffer.value, if_match: version }),
        });
      } catch (e) { setStatus('Save failed', 'error'); return; }
      if (wr.status === 400) {
        let d = {};
        try { d = await wr.json(); } catch (_) {}
        if (d.detail && d.detail.kind === 'manifest_invalid' && Array.isArray(d.detail.errors)) {
          renderErrors(d.detail.errors);
          setStatus(`Manifest invalid — ${d.detail.errors.length} error${d.detail.errors.length === 1 ? '' : 's'}`, 'error');
          return;
        }
        setStatus('Save failed (400)', 'error');
        return;
      }
      if (wr.status !== 200) {
        setStatus(`Save failed (${wr.status})`, 'error');
        return;
      }
      const saved = await wr.json();

      setStatus('Starting round…');
      let rr;
      try {
        rr = await fetch(`/experiments/${encodeURIComponent(eid)}/rounds`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ if_match: saved.version }),
        });
      } catch (e) { setStatus('Start failed', 'error'); return; }
      if (rr.status === 200) {
        const d = await rr.json();
        location.href = `/experiments/${encodeURIComponent(eid)}/rounds/${d.round_id}`;
        return;
      }
      let detail = `Start failed (${rr.status})`;
      try {
        const b = await rr.json();
        detail = (typeof b.detail === 'string' ? b.detail : (b.error || detail));
      } catch (_) {}
      setStatus(detail, 'error');
    } finally {
      submitBtn.disabled = false;
    }
  }

  openBtn.addEventListener('click', openModal);
  closeBtn.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modal.classList.contains('open')) closeModal();
  });
  modal.querySelectorAll('input[name="create-target"]').forEach(radio => {
    radio.addEventListener('change', () => { if (radio.checked) selectTarget(radio.value); });
  });
  submitBtn.addEventListener('click', submit);

  // Copy the manifest editor's full text to the clipboard. Uses
  // window.copyText so it works over plain HTTP too (execCommand
  // fallback) — the button reports success / failure on its glyph.
  if (copyBtn) {
    let copyResetTimer = null;
    copyBtn.addEventListener('click', async () => {
      if (copyResetTimer) clearTimeout(copyResetTimer);
      copyBtn.classList.remove('copied', 'copy-failed');
      const ok = await window.copyText(buffer.value);
      copyBtn.textContent = ok ? '✓' : '×';
      copyBtn.classList.add(ok ? 'copied' : 'copy-failed');
      copyBtn.title = ok ? 'Copied' : 'Copy failed';
      copyResetTimer = setTimeout(() => {
        copyBtn.textContent = '⧉';
        copyBtn.classList.remove('copied', 'copy-failed');
        copyBtn.title = 'Copy manifest';
      }, 1200);
    });
  }
})();
