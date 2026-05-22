// Round-page hydration — paints the unified manifest diff and wires the
// copy buttons. The manifest is YAML, so there is no syntax highlighting:
//
//   1. The unified diff (#diff-source). Each line is wrapped in
//      <span class="diff-line diff-{add,del,ctx,hunk,meta}"> so the leading
//      +/- prefix character is coloured semantically. The line content is
//      rendered as plain (escaped) text.
//
//   2. The frozen-manifest snapshot (#frozen-manifest) needs no hydration —
//      it renders as plain text inside the .atom-code-block panel chrome.

(function () {
  function escapeHtml(s) {
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function classifyLine(line) {
    if (line.startsWith('@@')) return 'hunk';
    if (line.startsWith('+++') || line.startsWith('---')) return 'meta';
    if (line.startsWith('+')) return 'add';
    if (line.startsWith('-')) return 'del';
    return 'ctx';
  }

  function renderDiffLine(line) {
    const kind = classifyLine(line);
    if (kind === 'hunk' || kind === 'meta') {
      return '<span class="diff-line diff-' + kind + '">' + escapeHtml(line) + '</span>';
    }
    const prefix = line.slice(0, 1);
    const content = line.slice(1);
    return (
      '<span class="diff-line diff-' + kind + '">' +
      '<span class="diff-prefix">' + escapeHtml(prefix) + '</span>' +
      escapeHtml(content) +
      '</span>'
    );
  }

  function paintDiff() {
    const el = document.getElementById('diff-source');
    if (!el) return;
    const text = el.textContent;
    if (!text) return;
    const lines = text.split('\n');
    // split() on a trailing newline yields an empty entry; drop it so we
    // don't render a blank line at the bottom.
    if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
    el.innerHTML = lines.map(renderDiffLine).join('\n');
  }

  // Wire every .code-block-copy button on the round page to the <pre> it
  // names via data-copy-target. Copy goes through window.copyText, which
  // falls back to execCommand when navigator.clipboard is unavailable
  // (non-secure context, e.g. the plain-HTTP deploy); only a genuine
  // failure flashes the error glyph.
  function wireCopyButtons() {
    const COPY_GLYPH = '⧉';
    const OK_GLYPH = '✓';
    const ERR_GLYPH = '×';
    document.querySelectorAll('.code-block-copy').forEach((btn) => {
      // Capture the per-button title ONCE at wire-up time. Capturing inside
      // the click handler would treat the transient "Copied" / "Copy
      // failed: …" string as the new baseline on rapid re-clicks.
      const original = btn.title || 'Copy';
      let resetTimer = null;
      btn.addEventListener('click', async () => {
        const target = document.getElementById(btn.dataset.copyTarget);
        if (!target) return;
        if (resetTimer) clearTimeout(resetTimer);
        // Always clear both flash classes at the start of each click so a
        // re-click in the flash window can't leave conflicting classes
        // (e.g. .copied + .copy-failed) on the button.
        btn.classList.remove('copied', 'copy-failed');
        const ok = await window.copyText(target.textContent);
        if (ok) {
          btn.textContent = OK_GLYPH;
          btn.classList.add('copied');
          btn.title = 'Copied';
          resetTimer = setTimeout(() => {
            btn.textContent = COPY_GLYPH;
            btn.classList.remove('copied');
            btn.title = original;
          }, 1200);
        } else {
          btn.textContent = ERR_GLYPH;
          btn.classList.add('copy-failed');
          btn.title = 'Copy failed';
          resetTimer = setTimeout(() => {
            btn.textContent = COPY_GLYPH;
            btn.classList.remove('copy-failed');
            btn.title = original;
          }, 1500);
        }
      });
    });
  }

  paintDiff();
  wireCopyButtons();
})();
