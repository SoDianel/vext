// FL preset choice and the native drag tail, shared by the main window (fl-launch.js) and the quick
// launcher (fl-launcher.js). Page-agnostic: the caller passes its own api, escape and toast helpers.
//   const preset = window.VextFlPreset.create({ api, esc, toast });
//   preset.preset(id, drag)     → the FL preset to use (a chooser when it is ambiguous), or null
//   preset.choosePreset(items)  → the chooser dialog's pick, or null
//   preset.startDrag(id, isHeld) → resolve the preset, then start the native drag only if isHeld() still is true
(() => {
  function create({ api, esc, toast }) {
    // The last choice per plugin, so a second click or drag doesn't ask again.
    const chosen = new Map();

    function choosePreset(items) {
      return new Promise(resolve => {
        const dialog = document.createElement('dialog');
        dialog.className = 'fl-launch-dialog';
        dialog.setAttribute('aria-label', 'Choose FL preset');
        dialog.innerHTML = `<h3>Choose FL preset</h3><p class="hint">Select the format and preset to use.</p>
          <div class="fl-launch-choices">${items.map((p, i) => `<button type="button" data-choice="${i}">
            <strong>${esc(p.name)}</strong><span>${esc(p.key)}</span></button>`).join('')}</div>
          <button type="button" data-cancel>Cancel</button>`;
        const finish = value => { dialog.close(); dialog.remove(); resolve(value); };
        dialog.addEventListener('cancel', event => { event.preventDefault(); finish(null); });
        dialog.querySelector('[data-cancel]').onclick = () => finish(null);
        dialog.querySelectorAll('[data-choice]').forEach(button => {
          button.onclick = () => finish(items[Number(button.dataset.choice)]);
        });
        document.body.appendChild(dialog);
        dialog.showModal();
      });
    }

    async function preset(id, drag = false) {
      const { presets } = await api('/api/fl-launch/presets/' + id);
      if (!presets.length) throw new Error('No matching FL preset. Scan it in FL’s Plugin Manager or link its preset in FL Studio Sync.');
      let pick = presets.find(p => p.key === chosen.get(id));
      if (!pick && presets.length === 1) pick = presets[0];
      if (!pick && drag) {
        pick = await choosePreset(presets);
        if (pick) { chosen.set(id, pick.key); toast('Preset selected. Drag the card again.'); }
        return null;
      }
      if (!pick) pick = await choosePreset(presets);
      if (pick) chosen.set(id, pick.key);
      return pick;
    }

    async function startDrag(id, isHeld) {
      const pick = await preset(id, true);
      // OLE needs the button held; a late API response must not start a drag after release.
      if (!pick || !isHeld()) return;
      await window.__TAURI__.core.invoke('fl_drag_preset', { pluginId: id, key: pick.key });
    }

    async function startDragKey(key, isHeld) {
      const pick = await api('/api/fl-launch/preset?key=' + encodeURIComponent(key));
      if (!pick || !isHeld()) return;
      await window.__TAURI__.core.invoke('fl_drag_preset', { pluginId: null, key: pick.key });
    }

    return { preset, choosePreset, startDrag, startDragKey };
  }

  window.VextFlPreset = { create };
})();
