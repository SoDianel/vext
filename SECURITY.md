# Security policy

## Supported versions

Only the latest release gets security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Report it privately:

- On GitHub: **Security → Report a vulnerability** (private vulnerability reporting) on this repository.
- Or write to contact@dianel-records.com.

Include the VEXT version, what you did, what happened, and, if you can, a way to reproduce it. You will get
an answer within about a week, and a fix or a plan within about 30 days for a confirmed issue. Reporters are
credited in the release notes unless they prefer not to be.

## What counts

VEXT runs on your PC and talks to nothing but itself:

- The backend listens on `127.0.0.1` only, and the desktop shell passes it a per-launch API token.
- It reads your plugin folders and only moves or deletes files after an explicit confirm in the UI
  (see "Safety rules" in [AGENTS.md](AGENTS.md)).
- Plugins you preview are third-party code that VEXT loads in a separate worker process. A malicious plugin
  can do anything a program you run can do; VEXT is not a sandbox for plugins.

In scope: a way for a web page or another local program to use the backend without the token, path traversal
or an unconfirmed destructive action, a flaw in the installer or the update of the runtimes. Out of scope:
bugs in a plugin you load, or in Windows, WebView2, FL Studio and other software VEXT does not ship.
