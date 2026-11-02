# claude-remote-project-manager (crpm)

A systemd user service that keeps every folder in `~/projects` registered with
**Claude Remote Control**, so you can start a new session in any project from
claude.ai/code or the Claude mobile app.

* **Registration.** Each project gets its own `claude remote-control --spawn worktree`
  server running in a tmux session (`rc-<project>`) on a dedicated tmux socket (`crpm`).
* **Multiple sessions per project.** In worktree spawn mode, each session you open
  remotely runs in its own isolated git worktree, so sessions don't step on each other.
  Folders without `.git` fall back to `--spawn same-dir` (or are skipped; see config).
  If such a folder later becomes a git repo, crpm restarts it in worktree mode.
* **De-registration.** Delete or move a folder out of `~/projects` and crpm stops
  its server (Ctrl-C first, then kill after `stop_timeout`).
* **Self-healing.** A server that exits is restarted with backoff (5s → 15s → 60s → 300s).
* **Trust prompt.** The first time `remote-control` runs in a folder it asks
  `Trust <folder>? [y/N]`. With `auto_trust = true` (the default) crpm answers `y`.
  This means **everything you put in `~/projects` is trusted**: its hooks,
  MCP servers and settings will run. Set `auto_trust = false` to answer each one
  yourself with `crpm attach <project>`.
* **Survives restarts.** Stopping or restarting the service only stops the daemon.
  The tmux server keeps running and the daemon picks the sessions back up when it
  starts. `crpm stop-all` tears them down.

## Install

```bash
./install.sh            # symlinks ~/.local/bin/crpm, writes ~/.config/crpm/config.toml, enables the service
```

To have crpm start at boot without logging in, enable linger: `sudo loginctl enable-linger $USER`.

## Use

```bash
crpm status                     # project, state, sessions in use/capacity, spawn mode, path
crpm attach sd-mux              # watch or answer a project's remote-control TUI (detach: Ctrl-b d)
crpm restart sd-mux             # bounce one project
crpm stop-all                   # stop every server (the daemon restarts them unless it is stopped too)
journalctl --user -u crpm -f    # daemon log
tmux -L crpm ls                 # raw tmux view
```

## Uninstall

```bash
systemctl --user disable --now crpm && crpm stop-all
rm ~/.config/systemd/user/crpm.service ~/.local/bin/crpm
```

## Notes

* New folders are registered after `settle_seconds` (15s) so a `git clone` has time to finish.
* Folder names starting with `.` or `_` are ignored by default (`exclude`).
* Worktrees are created by Claude Code inside each repo. crpm doesn't manage or clean them up.
* Requires Python ≥ 3.11 (stdlib only), tmux, and a logged-in `claude`.
