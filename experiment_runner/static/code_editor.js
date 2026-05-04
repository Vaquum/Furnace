(function () {
  const editor = document.querySelector('.atom-code-editor');
  if (!editor) return;
  const expId = editor.dataset.experimentId;
  const buffer = document.getElementById('code-buffer');
  const gutter = document.getElementById('code-gutter');
  const activeLine = document.getElementById('code-active-line');
  const saveBtn = document.getElementById('code-save-btn');
  const dirtyIndicator = document.getElementById('code-save-dirty');
  const statusEl = document.getElementById('code-save-status');

  let version = parseInt(editor.dataset.baseVersion, 10);
  let dirty = false;
  let saving = false;

  // ---- Gutter ---------------------------------------------------------------

  function renderGutter() {
    const lineCount = (buffer.value.match(/\n/g) || []).length + 1;
    const lines = new Array(lineCount);
    for (let i = 0; i < lineCount; i++) lines[i] = i + 1;
    gutter.textContent = lines.join('\n');
  }

  // ---- Active-line marker ---------------------------------------------------

  function lineHeightPx() {
    return parseFloat(getComputedStyle(buffer).lineHeight);
  }
  function paddingTopPx() {
    return parseFloat(getComputedStyle(buffer).paddingTop);
  }
  function caretLineIndex() {
    const upTo = buffer.value.slice(0, buffer.selectionStart);
    return (upTo.match(/\n/g) || []).length;
  }
  function updateActiveLine() {
    if (document.activeElement !== buffer) {
      activeLine.style.display = 'none';
      return;
    }
    const h = lineHeightPx();
    const idx = caretLineIndex();
    const top = paddingTopPx() + idx * h - buffer.scrollTop;
    activeLine.style.display = 'block';
    activeLine.style.top = top + 'px';
    activeLine.style.height = h + 'px';
  }

  // ---- Scroll lock-step -----------------------------------------------------

  buffer.addEventListener('scroll', () => {
    gutter.scrollTop = buffer.scrollTop;
    updateActiveLine();
  });

  // ---- Edit / caret events --------------------------------------------------

  buffer.addEventListener('input', () => {
    renderGutter();
    markDirty();
    updateActiveLine();
  });
  buffer.addEventListener('keyup', updateActiveLine);
  buffer.addEventListener('click', updateActiveLine);
  buffer.addEventListener('focus', updateActiveLine);
  buffer.addEventListener('blur', () => { activeLine.style.display = 'none'; });

  // ---- Tab + ⌘S keybindings -------------------------------------------------

  const INDENT = '    ';

  buffer.addEventListener('keydown', (e) => {
    if (e.key === 'Tab') {
      e.preventDefault();
      handleTab(e.shiftKey);
      return;
    }
    if ((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 's' || e.key === 'S')) {
      e.preventDefault();
      save();
    }
  });

  function handleTab(reverse) {
    const start = buffer.selectionStart;
    const end = buffer.selectionEnd;
    const value = buffer.value;
    const selectionSpansMultipleLines =
      start !== end && value.slice(start, end).indexOf('\n') !== -1;

    if (!selectionSpansMultipleLines) {
      if (reverse) {
        // Outdent: remove up to 4 leading spaces from the current line.
        const lineStart = value.lastIndexOf('\n', start - 1) + 1;
        const m = value.slice(lineStart, start).match(/^( {1,4})/);
        if (!m) return;
        const removeCount = m[1].length;
        buffer.setSelectionRange(lineStart, lineStart + removeCount);
        execDelete();
      } else {
        // Indent: insert 4 spaces at caret (replacing any selection within the line).
        execInsertText(INDENT);
      }
    } else {
      // Multi-line: indent or outdent every selected line at its line start.
      const blockStart = value.lastIndexOf('\n', start - 1) + 1;
      const blockEnd = end;
      const block = value.slice(blockStart, blockEnd);
      const lines = block.split('\n');
      let rebuilt;
      if (reverse) {
        rebuilt = lines.map((l) => l.replace(/^ {1,4}/, '')).join('\n');
      } else {
        rebuilt = lines.map((l) => INDENT + l).join('\n');
      }
      buffer.setSelectionRange(blockStart, blockEnd);
      execInsertText(rebuilt);
      // Re-select the rebuilt block so further Tab/Shift-Tab keeps working.
      buffer.setSelectionRange(blockStart, blockStart + rebuilt.length);
    }
    markDirty();
    renderGutter();
    updateActiveLine();
  }

  function execInsertText(text) {
    // execCommand('insertText') is the only path that keeps the browser's
    // native undo stack intact for textarea edits. setRangeText is the
    // documented modern API but breaks undo, which is unacceptable here.
    if (!document.execCommand('insertText', false, text)) {
      const s = buffer.selectionStart;
      const e = buffer.selectionEnd;
      buffer.setRangeText(text, s, e, 'end');
    }
  }
  function execDelete() {
    if (!document.execCommand('delete')) {
      const s = buffer.selectionStart;
      const e = buffer.selectionEnd;
      buffer.setRangeText('', s, e, 'end');
    }
  }

  // ---- Dirty state ----------------------------------------------------------

  function markDirty() {
    if (dirty) return;
    dirty = true;
    dirtyIndicator.style.display = '';
    statusEl.textContent = '';
  }
  function clearDirty() {
    dirty = false;
    dirtyIndicator.style.display = 'none';
  }
  window.addEventListener('beforeunload', (e) => {
    if (dirty) {
      e.preventDefault();
      e.returnValue = '';
      return '';
    }
  });

  // ---- Save -----------------------------------------------------------------

  async function save() {
    if (saving) return;
    saving = true;
    saveBtn.disabled = true;
    statusEl.textContent = 'Saving…';
    statusEl.style.color = 'var(--ink-mute)';
    try {
      const r = await fetch(`/experiments/${encodeURIComponent(expId)}/code`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: buffer.value, if_match: version }),
      });
      if (r.status === 200) {
        const data = await r.json();
        version = data.version;
        clearDirty();
        statusEl.textContent = `Saved · v${version}`;
        statusEl.style.color = 'var(--ink-mute)';
      } else if (r.status === 409) {
        const data = await r.json();
        statusEl.textContent =
          `Conflict — server is at v${data.current_version}; reload to merge.`;
        statusEl.style.color = 'var(--negative)';
      } else {
        const text = await r.text();
        statusEl.textContent = `${r.status} ${text.slice(0, 200)}`;
        statusEl.style.color = 'var(--negative)';
      }
    } finally {
      saving = false;
      saveBtn.disabled = false;
    }
  }
  saveBtn.addEventListener('click', save);

  // ---- Init -----------------------------------------------------------------

  renderGutter();
  // Active-line is hidden until the user clicks into the buffer; that's
  // intentional — DS canon hairline marker only when the surface is "live".
})();
