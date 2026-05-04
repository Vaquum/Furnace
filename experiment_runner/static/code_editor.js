(function () {
  const editor = document.querySelector('.atom-code-editor');
  if (!editor) return;
  const expId = editor.dataset.experimentId;
  const buffer = document.getElementById('code-buffer');
  const gutter = document.getElementById('code-gutter');
  const highlight = document.getElementById('code-highlight');
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

  // ---- Syntax highlighting (Design-System closed accent palette) ------------
  //
  // Four token categories, each painted in a colour from the closed palette:
  //   comments → ink-mute, strings → positive (yellow-green),
  //   numbers + True/False/None → negative (coral),
  //   keywords → accent (pink). Everything else stays default ink.
  // The textarea renders its text as `color: transparent`, so what the user
  // *sees* is the <pre> behind it; the textarea owns caret + selection only.

  const PY_KEYWORDS = new Set([
    'def', 'class', 'if', 'elif', 'else', 'for', 'while', 'return',
    'import', 'from', 'as', 'with', 'try', 'except', 'finally', 'raise',
    'pass', 'break', 'continue', 'yield', 'lambda', 'async', 'await',
    'global', 'nonlocal', 'assert', 'del', 'in', 'is', 'not', 'and', 'or',
    'self', 'cls',
  ]);
  const PY_CONSTANTS = new Set(['True', 'False', 'None']);

  // Master regex with named alternatives. Order matters:
  //   triple-quoted strings before single/double, comments before identifiers.
  const PY_TOKENIZER = new RegExp(
    [
      '(?<comment>#[^\\n]*)',
      '(?<string>"""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\'' +
        '|"(?:[^"\\\\\\n]|\\\\.)*"|\'(?:[^\'\\\\\\n]|\\\\.)*\')',
      '(?<number>\\b\\d[\\d_]*(?:\\.\\d[\\d_]*)?(?:[eE][+-]?\\d+)?[jJ]?\\b)',
      '(?<ident>\\b[A-Za-z_][A-Za-z0-9_]*\\b)',
    ].join('|'),
    'g',
  );

  function escapeHtml(s) {
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function highlightPython(text) {
    let out = '';
    let lastIndex = 0;
    PY_TOKENIZER.lastIndex = 0;
    let m;
    while ((m = PY_TOKENIZER.exec(text)) !== null) {
      const offset = m.index;
      if (offset > lastIndex) out += escapeHtml(text.slice(lastIndex, offset));
      const g = m.groups;
      if (g.comment) {
        out += '<span class="tok-comment">' + escapeHtml(g.comment) + '</span>';
      } else if (g.string) {
        out += '<span class="tok-string">' + escapeHtml(g.string) + '</span>';
      } else if (g.number) {
        out += '<span class="tok-number">' + escapeHtml(g.number) + '</span>';
      } else if (g.ident) {
        const w = g.ident;
        if (PY_KEYWORDS.has(w)) {
          out += '<span class="tok-keyword">' + escapeHtml(w) + '</span>';
        } else if (PY_CONSTANTS.has(w)) {
          out += '<span class="tok-number">' + escapeHtml(w) + '</span>';
        } else {
          out += escapeHtml(w);
        }
      }
      lastIndex = offset + m[0].length;
    }
    if (lastIndex < text.length) out += escapeHtml(text.slice(lastIndex));
    // The textarea always renders one extra blank line after a trailing newline;
    // mirror that in the <pre> so the layers stay aligned at end-of-buffer.
    if (out.endsWith('\n') || out.length === 0) out += ' ';
    return out;
  }

  function renderHighlight() {
    highlight.innerHTML = highlightPython(buffer.value);
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
  renderHighlight();
  // Active-line is hidden until the user clicks into the buffer; that's
  // intentional — DS canon hairline marker only when the surface is "live".
})();
