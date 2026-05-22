// Clipboard helper — copy text with a fallback for non-secure contexts.
// navigator.clipboard is undefined over plain HTTP (the deploy is served
// on http://...:8502, not a secure context), so when it is unavailable
// or denied, fall back to a hidden <textarea> + document.execCommand
// ('copy'), which needs no secure context. window.copyText returns a
// promise resolving to true on success, false on failure — every copy
// button reports its result off that.
window.copyText = async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      // Secure-context API present but denied — fall through to execCommand.
    }
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '-1000px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (err) {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
};
