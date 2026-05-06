function startEditor() {
  const editor = document.querySelector('.atom-code-editor');
  if (!editor) return;
  const expId = editor.dataset.experimentId;
  const buffer = document.getElementById('code-buffer');
  const gutter = document.getElementById('code-gutter');
  const highlight = document.getElementById('code-highlight');
  const activeLine = document.getElementById('code-active-line');
  const copyBtn = document.getElementById('code-copy-btn');
  const saveBtn = document.getElementById('code-save-btn');
  const dirtyIndicator = document.getElementById('code-save-dirty');
  const statusEl = document.getElementById('code-save-status');

  let version = parseInt(editor.dataset.baseVersion, 10);
  let dirty = false;
  let saving = false;
  let errorLine = null;

  // ---- Gutter ---------------------------------------------------------------

  function renderGutter() {
    const lineCount = (buffer.value.match(/\n/g) || []).length + 1;
    const parts = new Array(lineCount);
    for (let i = 0; i < lineCount; i++) {
      const n = i + 1;
      parts[i] = (n === errorLine)
        ? '<span class="gutter-error">' + n + '</span>'
        : String(n);
    }
    gutter.innerHTML = parts.join('\n');
  }
  function setErrorLine(line) {
    errorLine = line;
    renderGutter();
  }
  function clearErrorLine() {
    if (errorLine !== null) {
      errorLine = null;
      renderGutter();
    }
  }
  function scrollToLine(line) {
    const h = lineHeightPx();
    const target = paddingTopPx() + Math.max(0, line - 1) * h - buffer.clientHeight / 3;
    buffer.scrollTop = Math.max(0, target);
  }

  // ---- Syntax highlighting --------------------------------------------------
  //
  // Tokenization lives in static/python_highlight.js (loaded from base.html
  // so every page can reuse it — code editor here, diff view on the round
  // page). This file owns only the editor-specific rendering and the
  // trailing-newline fix that keeps the <pre> aligned with the textarea.

  function renderHighlight() {
    let out = window.PythonHighlight.highlightPython(buffer.value);
    // The textarea always renders one extra blank line after a trailing
    // newline; mirror that in the <pre> so the layers stay aligned at
    // end-of-buffer.
    if (out.endsWith('\n') || out.length === 0) out += ' ';
    highlight.innerHTML = out;
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
    highlight.scrollTop = buffer.scrollTop;
    highlight.scrollLeft = buffer.scrollLeft;
    updateActiveLine();
  });

  // ---- Edit / caret events --------------------------------------------------

  buffer.addEventListener('input', () => {
    clearErrorLine();
    renderGutter();
    renderHighlight();
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
    renderHighlight();
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
    // Snapshot the buffer at request-time. If the user keeps typing during
    // the round-trip, the post-200 clearDirty() must NOT fire — otherwise
    // those keystrokes lose their dirty marker, beforeunload stops warning,
    // and the tail of the edit silently drops on tab close. We compare
    // buffer.value back to the snapshot when the response lands; only a
    // bit-identical buffer is safe to clear.
    const snapshot = buffer.value;
    let r;
    try {
      r = await fetch(`/experiments/${encodeURIComponent(expId)}/code`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: snapshot, if_match: version }),
      });
    } catch (e) {
      // Network unreachable / aborted / DNS failure — surface and bail out.
      statusEl.textContent = `Save failed: ${e.message || e}`;
      statusEl.style.color = 'var(--negative)';
      saving = false;
      saveBtn.disabled = false;
      return;
    }
    try {
      if (r.status === 200) {
        const data = await r.json();
        version = data.version;
        if (buffer.value === snapshot) {
          clearDirty();
          clearErrorLine();
          statusEl.textContent = `Saved · v${version}`;
        } else {
          // The buffer drifted under us. The snapshot version is now on
          // disk; the tail keystrokes are still in the textarea. Keep
          // `dirty` true so beforeunload + the next save catch them.
          statusEl.textContent = `Saved · v${version} (unsaved tail)`;
        }
        statusEl.style.color = 'var(--ink-mute)';
      } else if (r.status === 409) {
        const data = await r.json();
        statusEl.textContent =
          `Conflict — server is at v${data.current_version}; reload to merge.`;
        statusEl.style.color = 'var(--negative)';
      } else if (r.status === 400) {
        let body = null;
        try { body = await r.json(); } catch (_) { /* response was not JSON */ }
        const detail = body && body.detail;
        if (detail && detail.kind === 'syntax_error') {
          statusEl.textContent =
            `SyntaxError on line ${detail.line}, col ${detail.col}: ${detail.msg}`;
          statusEl.style.color = 'var(--negative)';
          setErrorLine(detail.line);
          scrollToLine(detail.line);
        } else {
          statusEl.textContent = `400 ${(typeof detail === 'string' ? detail : JSON.stringify(detail)) || ''}`;
          statusEl.style.color = 'var(--negative)';
        }
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

  // ---- Copy-all -------------------------------------------------------------

  const COPY_GLYPH = '⧉';
  const COPIED_GLYPH = '✓';
  let copyResetTimer = null;

  copyBtn.addEventListener('click', async () => {
    if (copyResetTimer) clearTimeout(copyResetTimer);
    try {
      await navigator.clipboard.writeText(buffer.value);
      copyBtn.textContent = COPIED_GLYPH;
      copyBtn.classList.add('copied');
      copyBtn.title = 'Copied';
      copyResetTimer = setTimeout(() => {
        copyBtn.textContent = COPY_GLYPH;
        copyBtn.classList.remove('copied');
        copyBtn.title = 'Copy all';
      }, 1200);
    } catch (e) {
      // navigator.clipboard requires a secure context — http origins
      // outside localhost can deny the call. Surface the failure rather
      // than silently flashing ✓ as if the copy succeeded.
      copyBtn.textContent = '×';
      copyBtn.title = `Copy failed: ${e && e.message || e}`;
      copyBtn.style.color = 'var(--negative)';
      copyResetTimer = setTimeout(() => {
        copyBtn.textContent = COPY_GLYPH;
        copyBtn.style.color = '';
        copyBtn.title = 'Copy all';
      }, 1500);
    }
  });

  // ---- Init -----------------------------------------------------------------

  renderGutter();
  renderHighlight();
  // Active-line is hidden until the user clicks into the buffer; that's
  // intentional — DS canon hairline marker only when the surface is "live".
}

// PythonHighlight is loaded with `defer` from base.html, but this file is
// not necessarily deferred; in that case it could execute before
// PythonHighlight is defined. Defer-scripts always finish before
// DOMContentLoaded, so wait for that event when the highlighter isn't
// ready yet — and start immediately if it already is (covers the case
// where this script also happens to be deferred and runs late).
if (window.PythonHighlight) {
  startEditor();
} else {
  document.addEventListener('DOMContentLoaded', startEditor);
}
