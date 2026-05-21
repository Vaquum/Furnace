// Manifest editor — a YAML editor widget. For every `.manifest-editor` on the
// page it overlays a syntax-highlighted <pre> behind a transparent <textarea>
// (the caret and selection live in the textarea, the colour lives in the
// <pre>) and keeps a line-number gutter in sync. YAML is regular enough for a
// single-pass per-line tokenizer; there is no cross-line state to carry.
//
// The widget owns presentation only. The <textarea> stays the source of
// truth — save logic reads `.value`; to load text programmatically, set
// `.value` and dispatch an `input` event so the paint refreshes.
(() => {
  const KEYWORDS = new Set(['true', 'false', 'null', 'yes', 'no']);

  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Tokenize one YAML line to highlighted HTML.
  function highlightLine(line) {
    let out = '';
    let i = 0;
    const n = line.length;
    let colonSeen = false;
    while (i < n) {
      const c = line[i];
      // Comment — `#` at line start or after whitespace, outside a string.
      if (c === '#' && (i === 0 || /\s/.test(line[i - 1]))) {
        out += '<span class="tok-comment">' + escapeHtml(line.slice(i)) + '</span>';
        break;
      }
      // Quoted string.
      if (c === '"' || c === "'") {
        let j = i + 1;
        while (j < n && line[j] !== c) j++;
        out += '<span class="tok-string">' + escapeHtml(line.slice(i, Math.min(j + 1, n))) + '</span>';
        i = j + 1;
        continue;
      }
      // Whitespace, passed through verbatim so columns line up with the buffer.
      if (/\s/.test(c)) { out += c; i++; continue; }
      // Structural punctuation.
      if ('-:{}[],'.indexOf(c) !== -1) {
        out += '<span class="tok-punc">' + c + '</span>';
        if (c === ':') colonSeen = true;
        i++;
        continue;
      }
      // A bareword run.
      let j = i;
      while (j < n && /[^\s:{}[\],"'#]/.test(line[j])) j++;
      const word = line.slice(i, j);
      const isKey = !colonSeen && /^\s*:/.test(line.slice(j));
      let cls;
      if (isKey) cls = 'tok-key';
      else if (KEYWORDS.has(word.toLowerCase())) cls = 'tok-keyword';
      else if (/^-?\d[\d._]*$/.test(word)) cls = 'tok-number';
      else cls = 'tok-plain';
      out += '<span class="' + cls + '">' + escapeHtml(word) + '</span>';
      i = j;
    }
    return out;
  }

  function highlight(text) {
    return text.split('\n').map(highlightLine).join('\n');
  }

  function attach(root) {
    const buffer = root.querySelector('.manifest-buffer');
    const pre = root.querySelector('.manifest-highlight');
    const gutter = root.querySelector('.manifest-gutter');
    if (!buffer || !pre) return;
    if (root.dataset.editorReady === '1') return;
    root.dataset.editorReady = '1';

    function paint() {
      // Trailing newline: append a hair space so the <pre>'s final empty
      // line keeps height and stays aligned with the textarea.
      pre.innerHTML = highlight(buffer.value) + '​';
      if (gutter) {
        const count = buffer.value.split('\n').length;
        let g = '';
        for (let k = 1; k <= count; k++) g += k + '\n';
        gutter.textContent = g;
      }
    }
    function syncScroll() {
      pre.scrollTop = buffer.scrollTop;
      pre.scrollLeft = buffer.scrollLeft;
      if (gutter) gutter.scrollTop = buffer.scrollTop;
    }
    buffer.addEventListener('input', paint);
    buffer.addEventListener('scroll', syncScroll);
    // Tab inserts two spaces — YAML is indentation-significant, and a
    // focus-shift mid-edit is never what the operator wants here.
    buffer.addEventListener('keydown', (e) => {
      if (e.key !== 'Tab') return;
      e.preventDefault();
      const s = buffer.selectionStart;
      const en = buffer.selectionEnd;
      buffer.value = buffer.value.slice(0, s) + '  ' + buffer.value.slice(en);
      buffer.selectionStart = buffer.selectionEnd = s + 2;
      paint();
    });
    paint();
    syncScroll();
  }

  function init() {
    document.querySelectorAll('.manifest-editor').forEach(attach);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  window.ManifestEditor = { attach, highlight };
})();
