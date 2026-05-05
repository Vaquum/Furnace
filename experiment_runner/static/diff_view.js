// Diff view — paints a server-rendered unified diff with the same closed-
// palette syntax highlighting the code editor uses, plus a per-line marker
// for added / removed / context / hunk-header lines.
//
// Looked-for shape (set by round.html):
//   <div class="atom-diff-view"><pre id="diff-source" data-diff>{diff_text}</pre></div>
//
// Each line is wrapped in a <span class="diff-line diff-{add,del,ctx,hunk,meta}">
// so the line-prefix character can be coloured semantically. The content
// portion (after the prefix char) gets PythonHighlight applied.

(function () {
  const el = document.getElementById('diff-source');
  if (!el) return;
  if (!window.PythonHighlight) return;
  const escape = window.PythonHighlight.escapeHtml;
  const highlight = window.PythonHighlight.highlightPython;

  const text = el.textContent;
  if (!text) return;

  const lines = text.split('\n');
  // The original split keeps a trailing empty entry when the diff ends in
  // '\n'; drop it so we don't render a blank line at the bottom.
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();

  function classifyLine(line) {
    if (line.startsWith('@@')) return 'hunk';
    if (line.startsWith('+++') || line.startsWith('---')) return 'meta';
    if (line.startsWith('+')) return 'add';
    if (line.startsWith('-')) return 'del';
    return 'ctx';
  }

  function renderLine(line) {
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

  el.innerHTML = lines.map(renderLine).join('\n');
})();
