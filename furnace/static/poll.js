const POLL_MS = 2000;
const ACTIVE = new Set(['pending', 'running', 'terminating']);

function hasActiveRow(targetSel) {
  const t = document.querySelector(targetSel);
  if (!t) return false;
  return Array.from(t.querySelectorAll('.state')).some(el =>
    ACTIVE.has((el.textContent || '').trim().toLowerCase())
  );
}

async function pollOnce(url, target) {
  const r = await fetch(url);
  if (!r.ok) return;
  const html = await r.text();
  target.innerHTML = html;
}

function startTablePolling() {
  document.querySelectorAll('[data-poll-url][data-poll-target]').forEach(el => {
    const url = el.dataset.pollUrl;
    const target = document.querySelector(el.dataset.pollTarget);
    if (!target) return;
    setInterval(async () => {
      try {
        if (!hasActiveRow(el.dataset.pollTarget)) return;
        await pollOnce(url, target);
      } catch (e) {}
    }, POLL_MS);
  });
}

function sourceFromUrl(url) {
  const m = url.match(/[?&]source=([^&]+)/);
  return m ? decodeURIComponent(m[1]) : 'log';
}

async function pollLogOnce(el) {
  const url = el.dataset.logUrl;
  const offset = parseInt(el.dataset.offset || '0', 10);
  const sep = url.includes('?') ? '&' : '?';
  const r = await fetch(`${url}${sep}since=${offset}`);
  if (!r.ok) return;
  const next = parseInt(r.headers.get('X-Next-Offset') || offset, 10);
  const tailMode = r.headers.get('X-Tail-Mode') || 'append';
  const text = await r.text();
  const source = sourceFromUrl(url);

  if (tailMode === 'replace') {
    if (text || next > 0) {
      el.classList.remove('is-empty');
      el.textContent = text;
    } else {
      el.classList.add('is-empty');
      el.textContent = `(no ${source} output)`;
    }
  } else {
    if (text) {
      el.classList.remove('is-empty');
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
      el.appendChild(document.createTextNode(text));
      if (atBottom) el.scrollTop = el.scrollHeight;
    } else if (offset === 0 && next === 0) {
      el.classList.add('is-empty');
      el.textContent = `(no ${source} output)`;
    }
  }
  el.dataset.offset = String(next);
}

function startLogTail() {
  document.querySelectorAll('[data-log-url]').forEach(el => {
    el.dataset.offset = '0';
    setInterval(async () => {
      try { await pollLogOnce(el); } catch (e) {}
    }, POLL_MS);
    pollLogOnce(el).catch(() => {});
  });
}

function bindLogSourceSwitch() {
  const sel = document.getElementById('log-source');
  const viewer = document.getElementById('log-viewer');
  if (!sel || !viewer) return;
  sel.addEventListener('change', () => {
    const base = viewer.dataset.logBase;
    viewer.dataset.logUrl = `${base}?source=${sel.value}`;
    viewer.dataset.offset = '0';
    viewer.classList.remove('is-empty');
    viewer.textContent = '';
    pollLogOnce(viewer).catch(() => {});
  });
}

document.addEventListener('DOMContentLoaded', () => {
  startTablePolling();
  startLogTail();
  bindLogSourceSwitch();
});
