# Safety and cleanup rules

VEXT scans plugin files and extracts metadata read-only. A scan never automatically
moves or deletes plugins.

## Review every file change

Cleanup actions are proposed in the database first. The UI shows the proposed
changes and requires explicit confirmation before applying them. A bulk action
uses one confirmation showing the count. A plugin cannot receive another proposal
while its current action is unresolved.

Resolved actions are removed from the working database. File-change history and
reasons are recorded in `actions.log`.

## Quarantine and deletion

Delete moves a plugin to quarantine while preserving its original path. Restore
and permanent deletion are available in the Quarantine tab. A plugin in its
original installation location is never permanently deleted by that action.
Out-of-scope entries and Storage caches have their own separate confirmations.

Shared runtimes such as WaveShell hosts are excluded from cleanup. Windows file
protection is never bypassed automatically. An Administrator relaunch is offered
only after a real permission-denied error and remains the user's choice.

## Duplicate suggestions

Format preference is CLAP, then VST3, then VST2. A 64-bit copy takes preference over
its 32-bit twin; newer Waves version folders take preference over older ones.
These rules produce suggestions, not automatic removal. Suggestions to remove
older formats are off by default because a DAW may still need them. AAX is not
superseded by these formats and is suggested for removal only when Pro Tools is absent.

## Local API and plugin hosting

The backend listens only on `127.0.0.1`. The desktop supplies a per-launch token
for authenticated API operations. VEXT has no telemetry or accounts. Network
requests happen only for user-requested links, pictures or optional AI research.

Plugin previews run in separate worker processes to contain crashes. These
workers are not security sandboxes: installed third-party plugins execute as
programs on the user's computer. FL native support is limited to the capabilities
listed in [compatibility](FL_NATIVE_COMPATIBILITY.md) and [scope](FL_NATIVE_SCOPE.md).
