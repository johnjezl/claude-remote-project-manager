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
  its server (Ctrl-C first, then kill after `stop_timeout`). Servers run with
  `--no-create-session-in-dir`, so on stop Claude Code deletes the remote from
  claude.ai/code and archives its sessions instead of leaving it listed offline.
  (With `create_session_in_dir = true` you get a ready-made session in the main
  checkout, but stopped remotes then linger in the UI.)
* **Names.** Session names are prefixed with the project folder name
  (`name_prefix = "{project}"`), e.g. `sd-mux-graceful-unicorn`, so the web UI
  shows which project each session belongs to.
* **Config changes apply automatically.** If a project's `claude remote-control`
  command line would change (config edit, or a folder becoming a git repo), crpm
  restarts that project's server.
* **Self-healing.** A server that exits is restarted with backoff (5s → 15s → 60s → 300s).
* **Trust prompt.** The first time `remote-control` runs in a folder it asks
  `Trust <folder>? [y/N]`. By default you answer it yourself, once per folder:
  `crpm status` shows `WAITING FOR TRUST PROMPT`, the log says which project,
  and `crpm attach <project>` lets you answer `y` (then detach with Ctrl-b d).
  Set `auto_trust = true` to have crpm answer `y` for you. That makes it fully
  hands-off, but **everything you put in `~/projects` is then trusted**: its
  hooks, MCP servers and settings will run.
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
crpm sessions [PROJECT]         # running sessions and their session IDs (default: the project you're in,
                                #   else all; --ids for IDs only, --json for details, --remote for RC only)
crpm reconnect [SESSION_ID]     # bring a session back in tmux after its server stopped
                                #   (no ID: the session whose worktree you're in)
crpm new-session PROJECT ["first message"] [-n NAME] [--here]
                                # new Remote Control session in its own worktree; prints its claude.ai link
crpm restart-session [ID...]    # restart idle sessions to load new settings/MCP servers/etc.
                                #   (--project NAME for all of a project's sessions, --dry-run to preview)
crpm stop-all                   # stop every server (the daemon restarts them unless it is stopped too)
journalctl --user -u crpm -f    # daemon log
tmux -L crpm ls                 # raw tmux view
crpm web-token [USER]           # create/replace a web dashboard login (--revoke to remove it)
```

## Web dashboard

`crpm-web.service` serves a dashboard at <http://localhost:8765> that does what the CLI does,
from any browser, including your phone:

* every project with its server state, and the sessions running in it with their session IDs
  (copy one, or all of a project's; open them in claude.ai)
* **New session**: pick a project, type the first message, get the claude.ai link
* **Trust** a project waiting on its first-run trust prompt, without `crpm attach`
* restart a project's server, restart a session (or put a local session on Remote Control),
  reconnect a session whose server stopped, view a server's screen and last exit log

**Logging in.** `install.sh` prints a login link the first time. `crpm web-token` makes a new one
(and invalidates the old one); the token is in the link's `#fragment`, so it never reaches server
logs. Tokens are stored hashed in `~/.config/crpm/web-tokens`, one line per user.

**From other devices.** The dashboard listens on 127.0.0.1 only: anyone who can use it can run
Claude in your projects. To reach it from your phone or laptop, use one of these:

* **Tailscale** (HTTPS, only your tailnet's devices can reach it):
  `tailscale serve --bg 8765`, then open `https://<this machine>.<tailnet>.ts.net/#token=…`
* **SSH tunnel**: `ssh -L 8765:localhost:8765 <this machine>`, then <http://localhost:8765>

`web_listen` in the config changes the address; listening anywhere but loopback logs a warning.

## Uninstall

```bash
systemctl --user disable --now crpm && crpm stop-all
systemctl --user disable --now crpm-web
rm ~/.config/systemd/user/crpm.service ~/.config/systemd/user/crpm-web.service ~/.local/bin/crpm
```

## Notes

* A project's server is only restarted for a settings change (config edit, or the folder becoming a
  git repo) once it has no open sessions, since stopping it ends them. `crpm restart` forces it.
  A new repo stays in same-dir mode until its first commit, because worktrees are made from a commit.
* New folders are registered after `settle_seconds` (15s) so a `git clone` has time to finish.
* Folder names starting with `.` or `_` are ignored by default (`exclude`).
* Worktrees are created by Claude Code inside each repo. crpm doesn't manage or clean them up.
* If a server exits, the daemon logs its exit status and last screen to
  `~/.local/state/crpm/<session>.exit.log` (and a one-line summary to the journal), then restarts it.
* Requires Python ≥ 3.11 (stdlib only), tmux, and a logged-in `claude`.
