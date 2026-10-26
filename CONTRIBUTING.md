# Contributing to VEXT

Thanks for helping. VEXT is free software (GPL-3.0-or-later); by contributing you agree your work is released
under the same license. Please read the [Code of Conduct](CODE_OF_CONDUCT.md) first.

## Before you start

- **Bugs and ideas**: open an issue with the template. For a bigger change, open an issue first so we agree
  on the approach before you spend time on it.
- **Security problems**: not in a public issue, see [SECURITY.md](SECURITY.md).
- [AGENTS.md](AGENTS.md) is the project's source of truth: architecture, the **safety rules** (scanning is
  read-only; every cleanup action is propose, review, explicit apply; "delete" moves to quarantine), the color
  palette and the logo. A change that weakens a safety rule will not be accepted.

## Dev setup

Windows 10/11 x64, Python 3.12+, Node.js, Rust and the MSVC C++ Build Tools. Details in
[LOCAL_SETUP.md](LOCAL_SETUP.md); the short version:

```
python -m venv .venv-native
.venv-native\Scripts\pip install -r requirements.txt
.venv-native\Scripts\python -m uvicorn app.web:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000>. Use a scratch data folder so you never touch your real library:
`set VEXT_ROOT=%TEMP%\vext-dev` before starting. The desktop shell: `cd desktop`, `npm install`,
`npm run tauri dev` (rebuild the sidecar with `.\build_sidecar.ps1` after any change under `app/`).
32-bit plugin support: `.\tools\setup_x86_host.ps1`.

## Tests

The tests are plain scripts in `tools\test_*.py`. Each sets its own temporary roots (`VEXT_ROOT`,
`LOCALAPPDATA`), so they never touch real data, and **each must run in its own process**:

```
.venv-native\Scripts\python tools\test_storage.py
.venv-native\Scripts\python tools\test_signal_gen.py
```

Run the ones that cover what you changed, and say which in the pull request. Release builds are checked with
the clean-PC test (`tools\sandbox\run.ps1`, Windows Sandbox); maintainers run that.

## Style

- Match the surrounding code: naming, comment density and idiom. Python is formatted with `ruff`; the frontend
  is vanilla JS and CSS (no build step, no framework).
- Colors come from the official palette in AGENTS.md; do not add new ones.
- Keep user-facing text short and plain. New behavior that touches files needs a confirm step.
- Comments say why, not what. Document decisions worth keeping in AGENTS.md in a dated section.

## Pull requests

Work on a topic branch based on `main`, with one topic per PR. GitHub **Rebase and merge** is the only
allowed merge method: no merge commits or squash merges. Rebase your topic branch onto `origin/main` when
needed; never amend or force-push `main`. CI runs on branch pushes and PRs; each `main` push builds and
retains the Windows downloads for 90 days.

Fill in the template. Don't include build outputs (`release\`, `dist\`,
`desktop\src-tauri\target\`) or personal data (paths with your username, your plugin lists). Screenshots must
come from sample data, not your own library.
