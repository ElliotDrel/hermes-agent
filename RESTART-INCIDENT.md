# Staged restart self-query deadlock

## Verified incident

On 2026-10-11 at 15:11:28 Eastern, an authorized Discord `/restart` reached the installed gateway at `fb089ae196e6aa64d248439a5c777882298bb246`. At 15:13:20, the event-loop watchdog reported three missed liveness probes and terminated the gateway with code 75. No external supervisor relaunched the detached process. The user manually started a replacement; its installed and loaded revision remained the old pin. The staged `93dae60ba3bb5e6a270960b58767f5e80cdc80b8` candidate was not promoted.

## Root cause

The watchdog stack establishes:

`_handle_restart_command -> restart_candidate -> _require_owner -> gateway_declares_external_supervisor -> identify_gateway -> _query_windows_pipe -> handle.read`

The async slash handler synchronously invokes preflight, which makes a blocking control request to the same gateway. The named-pipe server needs the event loop that the request has blocked. The client checks its nominal deadline only before a blocking read, so that deadline cannot release the deadlock. The watchdog kills the still-blocked process before normal drain, promotion, or replacement launch.

The relevant preflight, supervisor detection and control transport source files are unchanged between the old installed pin and the combined candidate. Successful out-of-process staging probes did not exercise the in-process self-query. Mocked staging tests and disposable external activation checks consequently missed this path. The readiness handoff should not have implied that they proved the actual loaded slash-command path.

## Recovery and correction gates

- Do not ask the user to repeat the currently loaded `/restart` path.
- Preserve active install, pending marker, candidates, previous installations and diagnostics.
- No agent shutdown, restart, activation, bootstrap repeat, or runtime source/dependency edit.
- A change-specific protected-lifecycle override is required before implementing the repair. Broad permission to continue does not substitute for it.
- Proposed repair: current-process supervisor detection from launch context without loopback IPC; run potentially blocking candidate validation off the async handler loop. Scope bounded Windows client reads separately if needed.
- Verify the real self-query boundary with disposable Windows loop/pipe regression tests, explicit user authorization/refusal invariants, adjacent restart/drain suites, and native disposable staging/promotion checks. No synthetic live restart.
- A fixed candidate alone cannot repair already-loaded old code. Resolve the user-operated activation route explicitly after tests; do not silently patch runtime or repeat obsolete bootstrap instructions.

## Reporting

The fork repository has issues disabled, as verified through `gh issue list`. This committed incident note records the fork-owned bug instead of filing a misleading upstream report. Paths, user identifiers, handoff tokens and raw private log excerpts are omitted.
