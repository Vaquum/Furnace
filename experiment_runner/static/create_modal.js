// Creation modal — opened from the jobs-page "+". A new experiment is
// created from a name alone; the experiment is seeded with a starter
// manifest server-side, and the modal redirects straight into the
// manifest editor so the operator lands where they edit the definition.
(() => {
  const modal = document.getElementById('create-modal');
  const openBtn = document.getElementById('open-create');
  if (!modal || !openBtn) return;

  const closeBtn = document.getElementById('create-close');
  const submitBtn = document.getElementById('m-submit');
  const statusEl = document.getElementById('m-status');
  const nameInput = document.getElementById('m-new-name');

  function setStatus(text, kind) {
    statusEl.textContent = text || '';
    statusEl.style.color = kind === 'error' ? 'var(--negative)'
      : kind === 'ok' ? 'var(--accent)'
      : 'var(--ink-mute)';
  }

  function openModal() {
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
    setStatus('');
    nameInput.value = '';
    nameInput.focus();
  }
  function closeModal() {
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
  }

  async function submit() {
    const name = nameInput.value.trim();
    if (!name) { setStatus('Experiment name is required.', 'error'); return; }
    submitBtn.disabled = true;
    try {
      setStatus('Creating experiment…');
      let r;
      try {
        r = await fetch('/experiments', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
      } catch (e) {
        setStatus('Create failed', 'error');
        return;
      }
      if (!r.ok) {
        let detail = `Create failed (${r.status})`;
        try { detail = (await r.json()).detail || detail; } catch (_) {}
        setStatus(detail, 'error');
        return;
      }
      const created = await r.json();
      location.href = `/experiments/${encodeURIComponent(created.id)}/manifest`;
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
  submitBtn.addEventListener('click', submit);
  nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
})();
