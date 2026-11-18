# Pinned staging and user activation (maintained fork)

`hermes update --stage <40-character-published-SHA> --branch live` prepares a
standalone sibling Git clone at that exact commit and a separate, relocatable
`venv` built by PM's own builder (`pm.build_environment`: pinned toolchain, locked
`all` extra, no Hermes wheel). A relative source binding survives relocation. The
running gateway, installed HEAD/index/refs, active dependencies, PM install state
and profile configuration are unchanged. Native update locks and receipts are
reused. A sibling `.hermes-agent.pin-stage.json` marker binds installation, profile,
prior SHA, candidate SHA and token. Failed candidates are retained for inspection;
no backups/previous installations are automatically deleted.

The module is `hermes_cli.update_pin_stage` (its dependency helpers are in
`hermes_cli.update_pin_stage_env`). Upstream's `hermes_cli.update_stage` is the
desktop progress helper: it is never modified or shadowed.

Once this implementation is **installed and loaded**, an authorized, explicit
user `/restart` validates and captures the candidate, enters the existing native
restart drain, and uses the existing Windows watcher to run activation from the
candidate interpreter after the old gateway exits. Activation refuses live venv
holders and changed/dirty revisions. It renames whole source+environment trees,
retains the previous install and probes the promoted candidate. Because PM keys
dependency state by checkout path, the candidate's PM generation and launchers
cannot be built at its staging path and moved: activation commits them at the final
path, in the promoted candidate's fresh interpreter, with the same explicit sync
`hermes update` runs (`pm.sync_venv`, then superseded-generation collection and
launcher publication). That sync is the last step before launch and commits nothing
when it fails, so promotion/probe/sync failures roll the trees back and attempt a
native recovery start. The first PM generation of a pre-PM install carries the
extras its old venv held (read from the retained previous tree, never imported);
afterwards PM's ledger owns the selection. Plugin dependencies are resolved by
PM from the profile's discovered plugins; a conflict fails the sync, never
disables a plugin.

After a successful sync the marker is consumed and native `gateway start` runs. A
fresh promoted interpreter polls stable process-table readiness plus matching live
PID/exact `code_sha`, including delayed status writes. Native Windows start/stop can
return zero after printed failure: exit status alone is not readiness or proof of
stop. Once a launch is attempted, never rename a possibly booting generation: retain
both trees, persist a failed receipt, and ask the user to inspect native
status/logs and run native `gateway start` if needed.

No activation fetches Git. Moving `live` after staging cannot change the pin.
There is no `--force-venv` override.

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

Staging builds only the core + `all` environment; it does not dry-run PM's plugin
selection, so a plugin-dependency conflict surfaces at activation (before launch,
with rollback), not at staging. The candidate's own `venv` is the pre-launch probe
and fallback interpreter; the running gateway uses PM's generation. The post-update
completion tail of an ordinary `hermes update` (config migration, skill sync) is not
run by activation: state/config migrations occur through normal next-start behavior.
A host crash between directory renames requires inspecting the retained previous
tree and marker; there is no new journal/daemon. Live Discord, real shutdown and
readiness I/O remain user acceptance, not claimed by offline tests.

## Bootstrap when the loaded gateway predates this implementation

A loaded gateway from before this module knows nothing about `update_pin_stage`.
**Do not send `/restart` expecting it to activate a candidate.** (The marker name
differs from earlier fork releases on purpose: a loaded earlier handler would run
`-m hermes_cli.update_stage --activate-stage`, which upstream's helper silently
ignores, and strand the gateway after draining it.) Run the new module from the
reviewed source checkout with its own interpreter and the production profile home.
Run it from a directory outside every checkout and keep `-I`, so the current
directory can never shadow the module:

```bash
cd ~
HERMES_HOME='C:/Users/2supe/Hermes-Workspace' \
  '<reviewed-checkout>/.venv/Scripts/python.exe' -I -m hermes_cli.update_pin_stage \
  --root 'C:/Users/2supe/AppData/Local/hermes/hermes-agent' \
  --stage '<published-full-SHA>' --branch live
```

This calls the same native staging operation without modifying the active
installation. Elliot, not an agent, then performs the one-time activation in a
terminal **outside** the installed/candidate directory, using the command staging
printed. First close every Hermes CLI/TUI/dashboard/Desktop process and any
terminal, editor or Explorer window inside either tree, and back up the profile's
`state.db` and `config.yaml` (downgrade compatibility after the new gateway's first
start is not verified):

```bash
hermes gateway stop
HERMES_HOME='C:/Users/2supe/Hermes-Workspace' \
  '<candidate>/venv/Scripts/python.exe' -I -m hermes_cli.update_pin_stage \
  --root 'C:/Users/2supe/AppData/Local/hermes/hermes-agent' \
  --activate-stage '<token>'
```

The activation argument is the exact token the native restart watcher passes on
subsequent user `/restart` commands. It never stops a gateway itself; a still-live
PID/holder refuses promotion. After bootstrap, verify native `hermes gateway status`
and the profile's `gateway_state.json` `code_sha` match the pinned published SHA.
Subsequent stages use normal `hermes update --stage`.
