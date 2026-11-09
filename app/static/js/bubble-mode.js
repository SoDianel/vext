// Bubble mode belongs to the desktop shell, independent of DAW setup and backend settings.
(() => {
  'use strict';
  const button = document.getElementById('bubble-enter');
  const invoke = window.__TAURI__?.core?.invoke;
  if (!button || !invoke) return;
  button.hidden = false;
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    try { await invoke('bubble_enter'); }
    catch (error) { window.VEXT.toast('Could not enter bubble mode: ' + (error.message || error), true); }
    finally { button.disabled = false; }
  });
})();
