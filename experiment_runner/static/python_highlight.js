// Shared Python syntax highlighter — single source of truth for the four
// token categories used by the code editor and the diff view.
//
//   comments → --ink-mute
//   strings → --ink-soft
//   numbers + True/False/None → --negative (coral)
//   keywords → --accent (pink)
//
// Loaded as a defer script from base.html so every page can use it.
// Exposes a global namespace; no ES-module pipeline required.

(function () {
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
    return out;
  }

  window.PythonHighlight = { highlightPython, escapeHtml };
})();
