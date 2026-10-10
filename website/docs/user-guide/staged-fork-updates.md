# Pinned staging and user activation (maintained fork)

`hermes update --stage <40-character-published-SHA> --branch live` prepares a
standalone sibling Git clone at that exact commit and a separate, relocatable
`venv`. Native managed `uv` does the locked `all` dependency installation without
building/installing a Hermes wheel. The relative source binding survives relocation.
CPython's bundled Windows installer stub supplies `hermes.exe`, `hermes-acp.exe` and
`hermes-agent.exe` with the candidate's declared entrypoints and FINAL installation
interpreter binding; they are used after promotion, not from the staged sibling.
Enabled plugin declarations and native active lazy/tool dependencies are restored
into the candidate only, under core constraints. Conflicts fail preparation,
never disable a plugin. Candidate imports are checked with an isolated probe home.

The running gateway, installed HEAD/index/refs, active dependencies and profile
configuration are unchanged. Native update locks and receipts are reused. The
single sibling `.hermes-agent.staged-update.json` marker binds installation,
profile, prior SHA, candidate SHA and token. Failed candidates are retained for
inspection; no backups/previous installations are automatically deleted.

Once this implementation is **installed and loaded**, an authorized, explicit
user `/restart` validates and captures the candidate, enters the existing native
restart drain, and uses the existing Windows watcher to run activation from the
candidate interpreter after the old gateway exits. Activation refuses live venv
holders and changed/dirty revisions. It renames whole source+environment trees,
retains the previous install, rolls back failed promotion/import checks, then
uses native `gateway start`. A fresh promoted interpreter polls stable process-table
readiness plus matching live PID/exact `code_sha`, including delayed status writes.
Native Windows start/stop can return zero after printed failure: exit status alone
is not readiness or proof of stop. The activation gate still refuses live holders.
Promotion/import failures roll back and attempt native recovery start. Once a launch
is attempted, never rename a possibly booting generation: retain both trees, remove
the consumed marker, persist a failed receipt, and ask the user to inspect native
status/logs and run native `gateway start` if needed. No automatic post-launch
rollback or second spawn is attempted.

No activation fetches Git or resolves dependencies. Moving `live` after staging
cannot change the pin. There is no `--force-venv` override.

## Two distinct update requests

- `/update`: deploy-existing-live preparation. No stable rebase or source publication.
- `/update stable`: explicitly prepare/rebase/test/publish stable, then stage the
  resulting published `live` SHA. Protected source decisions still need their
  own approval. Neither request grants installation, stop or restart permission.
- Ordinary CLI `hermes update` retains its existing native behavior.
- `--no-gateway-restart` is **not staging**: it still changes active source/deps
  and Windows preflight can stop/drain holders. Do not use it for inert preparation.

## Supported ownership and limitations

Native Windows standalone Git installation, one profile, detached gateway only.
Windows Startup/Task login persistence is not a runtime supervisor: native
Windows start launches detached even with these installed. Desktop-owned,
container/external-supervisor and multi-profile owners are refused. No fleet,
Desktop build or dashboard/UI migration implementation is added. Non-Windows
and linked Git worktrees are refused as deployment targets (tests run from an
isolated worktree against disposable standalone clones).

The candidate uses the current Python minor/base runtime and does not replace a
managed Python generation. Enabled declared plugins are restored; undeclared
third-party pip installs and out-of-tree native dependencies are not invented.
Lazy features known only to another interpreter may need explicit restoration;
this is not proof of every optional tool. State/config migrations occur through
normal next-start behavior, not preparation. A host crash between directory
renames requires inspecting the retained previous tree and marker; there is no
new journal/daemon. Live Discord, real shutdown and readiness I/O remain user
acceptance, not claimed by offline tests.

## One-time bootstrap for the old installed handler

The currently loaded old `/restart` knows nothing about staged candidates.
**Do not claim `/restart` alone activates the first candidate.** After publishing
a SHA that includes this feature, run the new module from the reviewed source
checkout with its own interpreter and the production profile home:

```bash
HERMES_HOME='C:/Users/2supe/Hermes-Workspace' \
  '<reviewed-checkout>/.venv/Scripts/python.exe' -m hermes_cli.update_stage \
  --root 'C:/Users/2supe/AppData/Local/hermes/hermes-agent' \
  --stage '<published-full-SHA>' --branch live
```

This calls the same native staging operation without modifying the active old
installation. Read the marker for the resulting candidate path/token. Elliot,
not an agent, then performs the one-time activation in a terminal **outside** the
installed/candidate directory:

```bash
hermes gateway stop
HERMES_HOME='C:/Users/2supe/Hermes-Workspace' \
  '<candidate>/venv/Scripts/python.exe' -I -m hermes_cli.update_stage \
  --root 'C:/Users/2supe/AppData/Local/hermes/hermes-agent' \
  --activate-stage '<token>'
```

The hidden activation argument is the exact token passed by the native restart
watcher on subsequent user `/restart` commands. It never stops a gateway itself;
a still-live PID/holder refuses promotion. After bootstrap, verify native
`hermes gateway status` and the profile's `gateway_state.json` `code_sha` match
the pinned published SHA. Subsequent stages use normal `hermes update --stage`.
