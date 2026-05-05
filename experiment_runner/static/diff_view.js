// Round-page hydration — paints two read-only Python views with the same
// closed-palette syntax highlighting the code editor uses:
//
//   1. The unified diff (#diff-source). Each line is wrapped in
//      <span class="diff-line diff-{add,del,ctx,hunk,meta}"> so the leading
//      +/- prefix character is coloured semantically and the rest of the
//      line gets full Python token colouring.
//
//   2. The frozen-code snapshot (#frozen-code). Plain-Python with the same
//      tokenizer — no diff prefixes, no per-line wrappers, just the painted
//      buffer inside the existing .atom-code-block panel chrome.

(function () {
  if (!window.PythonHighlight) return;
  const escape = window.PythonHighlight.escapeHtml;
  const highlight = window.PythonHighlight.highlightPython;

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
      // Hunk headers and file-name metadata are rendered as a single
      // muted line — no Python highlighting, since they're not Python.
      return '<span class="diff-line diff-' + kind + '">' + escape(line) + '</span>';
    }
    // For added / removed / context lines, the first char is the prefix
    // marker; the rest is Python content.
    const prefix = line.slice(0, 1);
    const content = line.slice(1);
    return (
      '<span class="diff-line diff-' + kind + '">' +
      '<span class="diff-prefix">' + escape(prefix) + '</span>' +
      highlight(content) +
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

  function paintFrozenCode() {
    const el = document.getElementById('frozen-code');
    if (!el) return;
    el.innerHTML = highlight(el.textContent);
  }

  paintDiff();
  paintFrozenCode();
})();
