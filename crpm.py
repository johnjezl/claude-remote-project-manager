#!/usr/bin/env python3
"""claude-remote-project-manager (crpm)

Watches a projects folder and keeps one `claude remote-control` server running
(inside its own tmux session) for every project in it. New sessions opened from
claude.ai/code or the Claude app are spawned in isolated git worktrees, so each
project can have many concurrent sessions. Projects removed from the folder are
de-registered (their remote-control server is shut down).

Subcommands:
  daemon            run the reconcile loop (what the systemd unit runs)
  status            list managed projects and their state
  attach PROJECT    attach your terminal to a project's tmux session
  restart PROJECT   restart one project's remote-control server
  reconnect         bring stopped sessions back (--all: everything the last reboot interrupted)
  restart-session   restart idle sessions so they load new settings, MCP servers, etc.
  sessions [PROJECT] list running sessions and their session IDs
  new-session       start a new Remote Control session in a project (in its own worktree)
  stop-all          shut down every managed remote-control server
  web               serve the web dashboard (what crpm-web.service runs)
  web-token [USER]  create (or replace) a web dashboard login token
"""
from __future__ import annotations

import argparse
import fnmatch
import getpass
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger("crpm")

CONFIG_PATH = Path(os.environ.get("CRPM_CONFIG", "~/.config/crpm/config.toml")).expanduser()

DEFAULTS = {
    "projects_dir": "~/projects",
    "poll_interval": 5,          # seconds between scans
    "settle_seconds": 15,        # a new folder must exist this long before it is registered
    "tmux_socket": "crpm",       # dedicated tmux server: `tmux -L crpm ls`
    "session_prefix": "rc-",
    "claude_bin": "claude",
    "spawn_mode": "worktree",    # for git repos
    "non_git": "same-dir",       # "same-dir" | "skip" for folders that are not git repos
    "auto_trust": False,         # answer the first-run "Trust <folder>?" prompt with y (trusts everything in projects_dir)
    "permission_mode": "",       # e.g. "acceptEdits"; empty = claude default
    "capacity": 0,               # 0 = claude default (32)
    # false = no pre-created session in the main checkout. This also makes remote-control
    # delete its environment (the "remote" in claude.ai/code) and archive its sessions
    # when it stops, so removed projects disappear from the UI instead of going stale.
    "create_session_in_dir": False,
    # prefix for auto-generated session names, so sessions show which project they
    # belong to; "{project}" = folder name, "" = claude default (hostname)
    "name_prefix": "{project}",
    "extra_args": [],
    "exclude": [".*", "_*"],     # glob patterns of folder names to ignore
    "restart_backoff": [5, 15, 60, 300],
    "stop_timeout": 10,
    "log_dir": "~/.local/state/crpm",  # <session>.exit.log: exit status + last screen of servers that exited
    "exit_log_lines": 80,
    # web dashboard (crpm web). Keep it on loopback and reach it through `tailscale serve`
    # or an SSH tunnel: anyone who can use it can run Claude in your projects.
    "web_listen": "127.0.0.1:8765",
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            cfg.update(tomllib.load(f))
    cfg["projects_dir"] = Path(cfg["projects_dir"]).expanduser().resolve()
    return cfg


# --------------------------------------------------------------------------- tmux

class Tmux:
    def __init__(self, socket: str):
        self.socket = socket

    def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.socket, *args],
                              capture_output=True, text=True, check=check)

    FIELDS = ("path", "mode", "cmd", "dead", "status", "signal", "retained", "pid", "created")

    def sessions(self, prefix: str) -> dict[str, dict]:
        """Managed sessions -> {name: {path, mode, cmd, dead, status, signal, retained, pid, created}}."""
        fmt = "\t".join(["#{session_name}", "#{@crpm_path}", "#{@crpm_mode}", "#{@crpm_cmd}",
                         "#{pane_dead}", "#{pane_dead_status}", "#{pane_dead_signal}",
                         "#{remain-on-exit}", "#{pane_pid}", "#{session_created}"])
        r = self.run("list-sessions", "-F", fmt)
        out = {}
        if r.returncode != 0:  # no server running
            return out
        for line in r.stdout.splitlines():
            name, *vals = line.split("\t")
            if name.startswith(prefix):
                info = dict(zip(self.FIELDS, vals + [""] * len(self.FIELDS)))
                info["dead"] = info["dead"] == "1"
                info["retained"] = info["retained"] == "on"
                out[name] = info
        return out

    def new_session(self, name: str, cwd: str, command: str, opts: dict[str, str],
                    retain: bool = True) -> bool:
        # remain-on-exit is set in the same tmux call, so even an instant exit keeps its pane.
        # Only for daemon-managed sessions: nothing else clears dead panes.
        extra = [";", "set-option", "-w", "remain-on-exit", "on"] if retain else []
        r = self.run("new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50", command, *extra)
        if r.returncode != 0:
            log.error("tmux new-session %s failed: %s", name, r.stderr.strip())
            return False
        for k, v in opts.items():
            self.run("set-option", "-t", name, k, v)
        return True

    def capture(self, name: str, history: int = 0) -> str:
        extra = ["-S", str(-history)] if history else []
        return self.run("capture-pane", "-p", "-J", *extra, "-t", name).stdout

    def retain(self, name: str) -> None:
        """Keep the pane (and its exit status) after the command exits."""
        self.run("set-option", "-w", "-t", name, "remain-on-exit", "on")

    def is_dead(self, name: str) -> bool:
        r = self.run("display-message", "-p", "-t", name, "#{pane_dead}")
        return r.returncode == 0 and r.stdout.strip() == "1"

    def send(self, name: str, *keys: str) -> None:
        self.run("send-keys", "-t", name, *keys)

    def kill(self, name: str) -> None:
        self.run("kill-session", "-t", name)

    def has(self, name: str) -> bool:
        return self.run("has-session", "-t", f"={name}").returncode == 0


STATUS_LINE = re.compile(r"^·[^·\s]{1,3}· ([A-Z][a-z]+)", re.M)


def screen_state(screen: str) -> str:
    """Current state of a remote-control TUI from its pane text. The pane keeps
    old output, so only the last status line / last non-blank line count."""
    lines = [l for l in screen.splitlines() if l.strip()]
    if lines and lines[-1].rstrip().endswith(("[y/N]", "[y/N] y")):
        return "WAITING FOR TRUST PROMPT"
    for line in reversed(lines):
        if m := STATUS_LINE.match(line):
            return m.group(1).lower()
        if re.match(r"^\[[\d:]+\] Reconnected", line):
            return "connected"
        if re.match(r"^\[[\d:]+\] Reconnecting", line):
            return "reconnecting"
    return "starting/unknown"


def excluded(cfg: dict, name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in cfg["exclude"])


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def session_name(prefix: str, project: str) -> str:
    # tmux forbids '.' and ':' in session names
    return prefix + re.sub(r"[^A-Za-z0-9_-]", "_", project)


# --------------------------------------------------------------------------- daemon

TRUST_DELAY = 2.0


@dataclass
class Backoff:
    failures: int = 0
    next_try: float = 0.0
    started_at: float = 0.0


@dataclass
class Manager:
    cfg: dict
    tmux: Tmux = field(init=False)
    first_seen: dict[str, float] = field(default_factory=dict)
    backoff: dict[str, Backoff] = field(default_factory=dict)
    trust_pending: dict[str, float] = field(default_factory=dict)   # name -> give-up deadline
    prompt_seen: dict[str, float] = field(default_factory=dict)     # name -> when prompt first seen
    trust_hinted: set[str] = field(default_factory=set)
    committed: set[Path] = field(default_factory=set)     # git repos known to have a commit
    deferred: dict[str, str] = field(default_factory=dict)  # name -> command it will restart with
    registry: dict = field(default_factory=dict)          # the session registry (load_registry)
    registry_saved: float = 0.0
    stopping: bool = False

    def __post_init__(self):
        self.tmux = Tmux(self.cfg["tmux_socket"])

    # ---- discovery
    def desired(self, have: dict[str, dict]) -> dict[str, tuple[Path, str]]:
        """{session_name: (project_path, spawn_mode)} for projects that should be registered.

        The settle delay only gates *new* registrations; a folder that already
        has a running session (e.g. adopted after a daemon restart) keeps it."""
        root: Path = self.cfg["projects_dir"]
        now = time.time()
        want: dict[str, tuple[Path, str]] = {}
        seen = set()
        try:
            entries = sorted(root.iterdir())
        except FileNotFoundError:
            log.warning("projects dir %s does not exist", root)
            return want
        for p in entries:
            if not p.is_dir():
                continue
            if excluded(self.cfg, p.name):
                continue
            seen.add(p.name)
            sname = session_name(self.cfg["session_prefix"], p.name)
            self.first_seen.setdefault(p.name, now)
            if sname not in have and now - self.first_seen[p.name] < self.cfg["settle_seconds"]:
                continue
            if (p / ".git").exists() and self.has_commit(p):
                mode = self.cfg["spawn_mode"]
            elif self.cfg["non_git"] == "skip":
                continue
            else:
                mode = self.cfg["non_git"]
            want[sname] = (p.resolve(), mode)
        for gone in set(self.first_seen) - seen:
            del self.first_seen[gone]
        return want

    def has_commit(self, path: Path) -> bool:
        """Worktrees are made from a commit, so a repo without one still counts as non-git."""
        if path not in self.committed:
            r = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "-q", "HEAD"],
                               capture_output=True)
            if r.returncode != 0:
                return False
            self.committed.add(path)
        return True

    @staticmethod
    def sessions_in_use(server_pid: str) -> int:
        """Sessions a project's remote-control server is serving right now."""
        return sum(1 for s in live_sessions() if str(s.parent_pid) == server_pid)

    def command_for(self, path: Path, mode: str) -> str:
        c = self.cfg
        argv = [c["claude_bin"], "remote-control", "--spawn", mode]
        if c["permission_mode"]:
            argv += ["--permission-mode", c["permission_mode"]]
        if c["capacity"]:
            argv += ["--capacity", str(c["capacity"])]
        if not c["create_session_in_dir"]:
            argv += ["--no-create-session-in-dir"]
        if c["name_prefix"]:
            argv += ["--remote-control-session-name-prefix", c["name_prefix"].format(project=path.name)]
        argv += list(c["extra_args"])
        return shlex.join(argv)

    # ---- actions
    def start(self, name: str, path: Path, mode: str) -> None:
        bo = self.backoff.setdefault(name, Backoff())
        if time.time() < bo.next_try:
            return
        cmd = self.command_for(path, mode)
        log.info("register %s (%s, spawn=%s): %s", path.name, path, mode, cmd)
        ok = self.tmux.new_session(name, str(path), cmd,
                                   {"@crpm_path": str(path), "@crpm_mode": mode, "@crpm_cmd": cmd})
        bo.started_at = time.time()
        if ok and self.cfg["auto_trust"]:
            self.trust_pending[name] = time.time() + 120
        if not ok:
            self.note_failure(name)

    def note_failure(self, name: str) -> None:
        bo = self.backoff.setdefault(name, Backoff())
        steps = self.cfg["restart_backoff"]
        delay = steps[min(bo.failures, len(steps) - 1)]
        bo.failures += 1
        bo.next_try = time.time() + delay
        log.warning("%s exited/failed (%d in a row); retry in %ss", name, bo.failures, delay)

    def stop(self, name: str) -> None:
        log.info("de-register %s", name)
        if self.tmux.has(name):
            if not self.tmux.is_dead(name):
                self.tmux.send(name, "C-c")
                deadline = time.time() + self.cfg["stop_timeout"]
                while time.time() < deadline and self.tmux.has(name) and not self.tmux.is_dead(name):
                    time.sleep(0.5)
            if self.tmux.has(name):
                self.tmux.kill(name)
        self.backoff.pop(name, None)
        self.trust_pending.pop(name, None)
        self.prompt_seen.pop(name, None)
        self.deferred.pop(name, None)

    def record_exit(self, name: str, info: dict) -> None:
        """Log why a server exited (exit status + last screen), then clear its dead pane."""
        how = f"signal {info['signal']}" if info["signal"] else f"status {info['status'] or '?'}"
        screen = self.tmux.capture(name, history=self.cfg["exit_log_lines"])
        lines: list[str] = []
        for l in screen.splitlines():  # drop the pane's padding: no runs of blank lines
            l = l.rstrip()
            if l or (lines and lines[-1]):
                lines.append(l)
        while lines and not lines[-1]:
            lines.pop()
        tail = lines[-self.cfg["exit_log_lines"]:]
        last = next((l for l in reversed(tail) if l.strip() and not l.startswith("Pane is dead")), "")
        path = Path(self.cfg["log_dir"]).expanduser() / f"{name}.exit.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S %z')} {name} exited ({how})\n"
                        f"cmd: {info['cmd']}\n" + "\n".join(tail) + "\n\n")
            where = f"; screen saved to {path}"
        except OSError as e:
            where = f"; could not write {path}: {e}"
        log.warning("%s exited (%s); last line: %s%s", name, how, last.strip() or "(blank)", where)
        self.tmux.kill(name)

    def handle_trust_prompts(self, running: dict[str, dict]) -> None:
        """Answer `Trust <path>? [y/N]` with y. Claude rejects answers that arrive
        too quickly after the question, so wait TRUST_DELAY after first seeing the
        prompt, and keep answering until the session reports Connected."""
        now = time.time()
        for name, deadline in list(self.trust_pending.items()):
            if name not in running or now > deadline:
                if name in running:
                    log.warning("%s: gave up waiting for trust prompt / connection", name)
                self.trust_pending.pop(name, None)
                self.prompt_seen.pop(name, None)
                continue
            screen = self.tmux.capture(name)
            path = running[name]["path"]
            if re.search(rf"Trust {re.escape(path)}\? \[y/N\]\s*$", screen):
                seen = self.prompt_seen.setdefault(name, now)
                if now - seen >= TRUST_DELAY:
                    log.info("auto-trusting %s", path)
                    self.tmux.send(name, "y", "Enter")
                    self.prompt_seen[name] = now + 2  # don't re-answer before it redraws
            elif screen_state(screen) in ("connected", "ready"):
                self.trust_pending.pop(name, None)
                self.prompt_seen.pop(name, None)

    # ---- loop
    def reconcile(self) -> None:
        have = self.tmux.sessions(self.cfg["session_prefix"])
        for name, info in list(have.items()):
            if info["dead"]:
                self.record_exit(name, info)
                del have[name]
                self.note_failure(name)
            elif not info["retained"]:  # adopted from before exit logging existed
                self.tmux.retain(name)
        want = self.desired(have)

        # sessions that vanished without us stopping them => crashed/exited
        for name, bo in self.backoff.items():
            if name in want and name not in have and bo.started_at and bo.next_try <= bo.started_at:
                self.note_failure(name)

        for name in sorted(set(have) - set(want)):
            self.stop(name)

        for name, (path, mode) in sorted(want.items()):
            cur = have.get(name)
            new_cmd = self.command_for(path, mode)
            if cur and (cur["path"] != str(path) or cur["cmd"] != new_cmd):
                # folder became a git repo, or the config changed. Stopping the server ends (and
                # archives) every session it serves, so wait until it isn't serving any.
                in_use = self.sessions_in_use(cur["pid"])
                if in_use:
                    if self.deferred.get(name) != new_cmd:
                        self.deferred[name] = new_cmd
                        log.info("%s settings changed; will restart once its %d open session(s) end "
                                 "(or now: crpm restart %s): %s", name, in_use, path.name, new_cmd)
                    continue
                self.deferred.pop(name, None)
                log.info("%s settings changed; restarting with: %s", name, new_cmd)
                self.stop(name)
                cur = None
            if cur is None:
                self.start(name, path, mode)
            else:
                if name not in self.trust_pending and "[y/N]" in self.tmux.capture(name)[-300:]:
                    if self.cfg["auto_trust"]:
                        self.trust_pending[name] = time.time() + 120
                    elif name not in self.trust_hinted:
                        self.trust_hinted.add(name)
                        log.info("%s is waiting for you to trust it: run `crpm attach %s` and answer y",
                                 name, path.name)
                bo = self.backoff.get(name)
                if bo and bo.failures and time.time() - bo.started_at > 120:
                    bo.failures = 0  # stable again

        self.handle_trust_prompts(self.tmux.sessions(self.cfg["session_prefix"]))
        self.track_sessions()

    def track_sessions(self) -> None:
        """Keep the session registry current, so `crpm reconnect --all` knows what a reboot interrupted."""
        try:
            changed = track_sessions(self.cfg, self.registry, live_sessions())
            if changed or time.time() - self.registry_saved >= REGISTRY_HEARTBEAT:
                save_registry(self.cfg, self.registry)
                self.registry_saved = time.time()
        except OSError as e:
            log.warning("could not update the session registry: %s", e)

    def run(self) -> None:
        def on_term(signum, _frame):
            log.info("signal %s: exiting (managed sessions keep running)", signum)
            self.stopping = True
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
        log.info("watching %s (tmux socket '%s')", self.cfg["projects_dir"], self.cfg["tmux_socket"])
        self.registry = load_registry(self.cfg)
        boot = boot_id()
        cut = [e for e in self.registry["sessions"].values() if interrupted(self.registry, e, boot)
               and e.get("boot") != boot]
        if cut and boot not in self.registry["boots"]:
            log.info("%d session(s) were running when the machine restarted; bring them back with "
                     "`crpm reconnect --all` (or Reconnect all in the dashboard)", len(cut))
        self.registry["stopped"].pop(boot, None)  # watching again: sessions that end are seen to end
        while not self.stopping:
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            for _ in range(int(self.cfg["poll_interval"] * 10)):
                if self.stopping:
                    break
                time.sleep(0.1)
        if not system_stopping():
            # Stopped while the machine keeps running: from now on sessions can end unseen. If the
            # machine restarts before the daemon is back, don't call this boot's sessions interrupted.
            self.registry["stopped"][boot] = time.time()
            try:
                save_registry(self.cfg, self.registry)
            except OSError as e:
                log.warning("could not update the session registry: %s", e)


# --------------------------------------------------------------------------- operations
# Each operation returns data (or a message saying what it did) and raises CrpmError when it
# can't. The CLI and the web dashboard are thin layers over these, so both behave the same.

class CrpmError(Exception):
    pass


def resolve(cfg: dict, project: str) -> str:
    return project if project.startswith(cfg["session_prefix"]) else session_name(cfg["session_prefix"], project)


def project_dirs(cfg: dict) -> list[Path]:
    """Folders in projects_dir that crpm manages (or would, once they settle)."""
    try:
        entries = sorted(cfg["projects_dir"].iterdir(), key=lambda p: p.name.lower())
    except FileNotFoundError:
        return []
    return [p for p in entries if p.is_dir() and not excluded(cfg, p.name)]


@dataclass
class Server:
    """A project's `claude remote-control` server (tmux session rc-<project>)."""
    tmux: str
    project: str
    path: str
    mode: str
    state: str
    capacity: str
    pid: int
    created: int
    dead: bool


def servers(cfg: dict) -> list[Server]:
    t = Tmux(cfg["tmux_socket"])
    out = []
    for name, info in sorted(t.sessions(cfg["session_prefix"]).items()):
        screen = t.capture(name)
        if info["dead"]:
            state = f"exited ({'signal ' + info['signal'] if info['signal'] else 'status ' + info['status']})"
        else:
            state = screen_state(screen)
        cap = re.search(r"Capacity: (\d+/\d+)", screen)
        project = Path(info["path"]).name if info["path"] else name.removeprefix(cfg["session_prefix"])
        out.append(Server(name, project, info["path"], info["mode"], state, cap.group(1) if cap else "",
                          int(info["pid"] or 0), int(info["created"] or 0), info["dead"]))
    return out


def restart_project(cfg: dict, project: str) -> str:
    name = resolve(cfg, project)
    if not Tmux(cfg["tmux_socket"]).has(name):
        raise CrpmError(f"no server {name} is running")
    Manager(cfg).stop(name)
    return "stopped; the daemon will start it again within a few seconds"


def trust_project(cfg: dict, project: str) -> str:
    """Answer a project server's first-run `Trust <folder>? [y/N]` prompt with y."""
    t, name = Tmux(cfg["tmux_socket"]), resolve(cfg, project)
    if not t.has(name) or t.is_dead(name):
        raise CrpmError(f"no server {name} is running")
    if screen_state(t.capture(name)) != "WAITING FOR TRUST PROMPT":
        raise CrpmError(f"{name} is not waiting for a trust prompt")
    t.send(name, "y", "Enter")
    return f"answered y; {name} should connect in a few seconds"


def tail_lines(text: str, n: int) -> str:
    lines: list[str] = []
    for l in text.splitlines():  # collapse the pane's padding: no runs of blank lines
        l = l.rstrip()
        if l or (lines and lines[-1]):
            lines.append(l)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines[-n:])


def project_screen(cfg: dict, project: str, lines: int = 200) -> dict:
    """A project server's current screen, and the end of its exit log (why it last exited)."""
    t, name = Tmux(cfg["tmux_socket"]), resolve(cfg, project)
    screen = tail_lines(t.capture(name, history=lines), lines) if t.has(name) else ""
    exit_log = ""
    path = Path(cfg["log_dir"]).expanduser() / f"{name}.exit.log"
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - 32768))
            exit_log = tail_lines(f.read().decode(errors="replace"), 120)
    except OSError:
        pass
    return {"tmux": name, "running": t.has(name), "screen": screen, "exit_log": exit_log}


# Must not start with session_prefix: the daemon stops any such session that isn't a project.
RECONNECT_PREFIX = "reconnect-"
BRIDGE_WORKTREE = re.compile(r"^bridge-cse_([A-Za-z0-9]+)$")
RECONNECT_SETTLE = 3.0  # seconds a reconnect must survive before we call it started


def bridge_worktree(start: Path) -> tuple[Path, str] | None:
    """The spawned-session worktree containing `start` -> (worktree root, session ID)."""
    for d in (start, *start.parents):
        m = BRIDGE_WORKTREE.match(d.name)
        if m and d.parent.name == "worktrees" and d.parent.parent.name == ".claude":
            return d, f"session_{m.group(1)}"
    return None


def short_id(session_id: str) -> str:
    return session_id.removeprefix("session_").removeprefix("cse_")


def serving_pid(session_id: str) -> int | None:
    """PID of a local process already serving this session, if any."""
    for proc in Path("/proc").glob("[0-9]*"):
        argv = proc_argv(int(proc.name))
        if "remote-control" in argv and "--session-id" in argv and session_id in argv:
            return int(proc.name)
    for s in live_sessions():
        if s.session_id == session_id:
            return s.pid
    return None


def served_session_ids() -> set[str]:
    """Every session some local process is serving right now (serving_pid, for all sessions at once)."""
    ids = {s.session_id for s in live_sessions()}
    for proc in Path("/proc").glob("[0-9]*"):
        argv = proc_argv(int(proc.name))
        if "remote-control" in argv and "--session-id" in argv:
            i = argv.index("--session-id")
            if i + 1 < len(argv):
                ids.add(argv[i + 1])
    return ids


def launch_detached(cfg: dict, name: str, cwd: Path, cmd: str, what: str, hint: str = "") -> str:
    """Run `cmd` in a new tmux session the daemon ignores (not session_prefix) and check it stays up."""
    t = Tmux(cfg["tmux_socket"])
    attach = f"tmux -L {cfg['tmux_socket']} attach -t {name}"
    if t.has(name):
        if not t.is_dead(name):
            return f"{what} is already running in tmux: {attach}"
        t.kill(name)  # dead pane left by an older crpm
    if not t.new_session(name, str(cwd), cmd, {}, retain=False):
        raise CrpmError(f"could not start tmux session {name}")
    time.sleep(RECONNECT_SETTLE)
    if not t.has(name):
        raise CrpmError(f"claude exited right away. To see why, run this in {cwd}:\n  {cmd}" +
                        (f"\n{hint}" if hint else ""))
    return f"started {what}\n  watch: {attach}  (detach: Ctrl-b d)"


def reconnect_name(project: str, session_id: str) -> str:
    return session_name(RECONNECT_PREFIX, f"{project}-{short_id(session_id)}")


def find_bridge_worktree(cfg: dict, session_id: str) -> Path:
    short = short_id(session_id)
    if not re.fullmatch(r"[A-Za-z0-9]+", short):
        raise CrpmError(f"not a session ID: {session_id}")
    hits = sorted(cfg["projects_dir"].glob(f"*/.claude/worktrees/bridge-cse_{short}"))
    if not hits:
        raise CrpmError(f"no worktree for session_{short} under {cfg['projects_dir']}")
    return hits[0]


def reconnect_session(cfg: dict, worktree: Path, session_id: str, project: str | None = None) -> str:
    """Serve a spawned session again, from its worktree, after its project's server stopped."""
    name = reconnect_name(project or worktree.parents[2].name, session_id)
    t = Tmux(cfg["tmux_socket"])
    if not (t.has(name) and not t.is_dead(name)):
        pid = serving_pid(session_id)
        if pid:
            raise CrpmError(f"{session_id} is already being served by PID {pid}. "
                            "Stop that process first if it is stuck.")
    cmd = shlex.join([cfg["claude_bin"], "remote-control", "--session-id", session_id])
    return launch_detached(cfg, name, worktree, cmd, f"remote-control for {session_id}",
                           "Claude Code can only reattach for about 4 hours after the session's server stopped.")


def transcript_mtime(cwd: Path) -> float | None:
    """When Claude last wrote a transcript for a session run in `cwd`."""
    d = claude_config_dir() / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    times = []
    for f in d.glob("*.jsonl"):
        try:
            times.append(f.stat().st_mtime)
        except OSError:
            pass
    return max(times, default=None)


@dataclass
class Reconnectable:
    project: str         # "" for a session outside projects_dir
    session_id: str
    worktree: str        # the folder it runs in (a worktree, or the project folder)
    last_active: float
    kind: str = "spawned"
    name: str = ""
    interrupted: bool = False   # was running when the machine last restarted
    failed: str = ""            # why the last reconnect failed, if it did

    @property
    def waiting(self) -> bool:
        """Interrupted and not yet tried in vain: what `reconnect --all` and the dashboard banner do."""
        return self.interrupted and not self.failed


def reconnect_candidates(cfg: dict) -> list[Reconnectable]:
    """Sessions that nothing serves now and that a reconnect could bring back, interrupted ones first.

    From the session registry: every session that was running at the last reboot, and spawned or
    reattached sessions that stopped recently. Plus spawned-session worktrees from before the
    registry, whose server no longer knows them. (A project's server serves its own spawned
    sessions again on their next message, so only ones made by an earlier server need a reconnect.)
    App sessions past Claude Code's reattach window, and dismissed sessions, are left out."""
    served = served_session_ids()
    started = {Path(i["path"]).name: int(i["created"] or 0)
               for i in Tmux(cfg["tmux_socket"]).sessions(cfg["session_prefix"]).values()
               if not i["dead"] and i["path"]}
    reg, marks, boot, now = load_registry(cfg), load_reconnects(cfg), boot_id(), time.time()
    out: dict[str, Reconnectable] = {}
    for sid, e in reg["sessions"].items():
        if sid in served or not Path(e["cwd"]).is_dir():
            continue
        mark = marks.get(sid, {})
        if mark.get("at", 0) <= e["last_seen"]:
            mark = {}  # it ran again since
        if mark.get("dismissed"):
            continue
        cut = interrupted(reg, e, boot)
        ended = e.get("ended_at") or e["last_seen"]
        if e["kind"] != "terminal" and now - ended > REATTACH_WINDOW:
            continue  # Claude Code won't reattach it any more
        if not cut:
            if e["kind"] == "terminal":
                continue  # closed on purpose (or we can't tell)
            if e["kind"] == "spawned" and e.get("boot") == boot and \
                    started.get(e.get("project"), float("inf")) <= e.get("first_seen", 0):
                continue  # its server still knows it
        out[sid] = Reconnectable(e.get("project") or "", sid, e["cwd"], ended, e["kind"],
                                 e.get("name") or "", cut, str(mark.get("failed") or ""))
    for wt in cfg["projects_dir"].glob("*/.claude/worktrees/bridge-cse_*"):
        found = bridge_worktree(wt)
        if not found or found[1] in served or found[1] in out or found[1] in reg["sessions"] \
                or marks.get(found[1], {}).get("dismissed"):
            continue
        project = wt.parents[2].name
        try:
            made = (wt / ".git").stat().st_mtime
        except OSError:
            continue  # not (or no longer) a worktree
        if started.get(project, float("inf")) <= made:
            continue
        last = transcript_mtime(wt) or made
        if now - last < REATTACH_WINDOW:
            out[found[1]] = Reconnectable(project, found[1], str(wt), last,
                                          failed=str(marks.get(found[1], {}).get("failed") or ""))
    return sorted(out.values(), key=lambda r: (not r.waiting, not r.interrupted, -r.last_active))


# --------------------------------------------------------------------------- live sessions

RESTART_GAP = 2.0  # seconds between the old process exiting and the new one starting


@dataclass
class LiveSession:
    pid: int
    session_id: str     # session_... (claude.ai); "" for a local session not on Remote Control
    conversation: str   # local transcript ID, for --resume
    cwd: Path
    status: str         # "idle" | "busy"
    parent_pid: int
    parent_argv: list[str]
    name: str = ""      # the session's name in Claude Code
    tmux: str = ""      # tmux session it runs in, if any

    @property
    def kind(self) -> str:
        """spawned: created by a project's `remote-control` server, which serves it again on the next message.
        reattached: served by `remote-control --session-id/--continue`, which exits with it.
        terminal: an interactive `claude --remote-control` (or /remote-control) session,
        or a local session that isn't on Remote Control (restarting it puts it on)."""
        if "remote-control" in self.parent_argv:
            if {"--session-id", "--continue", "-c"} & set(self.parent_argv):
                return "reattached"
            return "spawned"
        return "terminal"

    @property
    def key(self) -> str:
        """Stable ID for any session: its claude.ai session ID, else its local conversation ID."""
        return self.session_id or self.conversation


def proc_start(pid: int) -> str | None:
    """Start time in clock ticks (/proc/<pid>/stat field 22), as Claude Code records it in procStart."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]


def proc_argv(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")[:-1]
    except OSError:
        return []


def proc_ppid(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def live_sessions(include_local: bool = False) -> list[LiveSession]:
    """Running Remote Control sessions (plus local-only ones with include_local), from Claude
    Code's per-process session records."""
    out = []
    for record in sorted((claude_config_dir() / "sessions").glob("*.json")):
        try:
            d = json.loads(record.read_text())
            pid = int(d["pid"])
            sid, conv, cwd = d.get("bridgeSessionId") or "", d["sessionId"], Path(d["cwd"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not (sid or include_local and d.get("kind") == "interactive") \
                or proc_start(pid) != str(d.get("procStart")):
            continue  # not a session we list, or the process is gone (or its PID was reused)
        ppid = proc_ppid(pid)
        out.append(LiveSession(pid, sid, conv, cwd, d.get("status", "?"), ppid, proc_argv(ppid),
                               str(d.get("name") or ""), str(d.get("tmux") or "").split(":")[0]))
    return out


def is_ancestor(pid: int) -> bool:
    p = os.getpid()
    while p > 1:
        if p == pid:
            return True
        p = proc_ppid(p)
    return False


def terminate(pid: int, timeout: float = 15) -> bool:
    """SIGTERM, then SIGKILL after `timeout`. True once the process is gone."""
    start = proc_start(pid)
    def alive() -> bool:
        return start is not None and proc_start(pid) == start and \
            Path(f"/proc/{pid}/status").read_text().find("State:\tZ") < 0
    for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                if not alive():
                    return True
            except OSError:
                return True
            time.sleep(0.2)
    return False


def find_pane(pid: int) -> tuple[str, str] | None:
    """(tmux socket path, pane ID) of the tmux pane running `pid`, on any of this user's tmux servers."""
    sock_dir = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}"
    for sock in sorted(sock_dir.glob("*")):
        if not sock.is_socket():
            continue
        r = subprocess.run(["tmux", "-S", str(sock), "list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            pane, ppid = line.split("\t")
            if ppid == str(pid):
                return str(sock), pane
    return None


def restart_session(cfg: dict, s: LiveSession) -> str:
    if s.kind == "spawned":
        if not terminate(s.pid):
            raise CrpmError(f"could not stop PID {s.pid}")
        return "stopped. Send it a message from the app and its server starts it again with the new config."

    if s.kind == "reattached":
        server = s.parent_pid
        if not terminate(s.pid) or (proc_start(server) and not terminate(server)):
            raise CrpmError(f"could not stop PID {s.pid} / its server {server}")
        time.sleep(RESTART_GAP)
        found = bridge_worktree(s.cwd)
        project = found[0].parents[2].name if found else s.cwd.name
        cmd = shlex.join([cfg["claude_bin"], "remote-control", "--session-id", s.session_id])
        return launch_detached(cfg, reconnect_name(project, s.session_id), found[0] if found else s.cwd,
                               cmd, f"remote-control for {s.session_id}")

    # terminal: resume the same conversation with Remote Control on, in the same tmux pane if it has one
    cmd = shlex.join([cfg["claude_bin"], "--resume", s.conversation, "--remote-control"])
    pane = find_pane(s.pid)
    if pane:
        sock, pane_id = pane
        tm = ["tmux", "-S", sock]
        subprocess.run([*tm, "set-option", "-p", "-t", pane_id, "remain-on-exit", "on"], capture_output=True)
    if not terminate(s.pid):
        raise CrpmError(f"could not stop PID {s.pid}")
    time.sleep(RESTART_GAP)
    if pane:
        r = subprocess.run([*tm, "respawn-pane", "-t", pane_id, "-c", str(s.cwd), cmd],
                           capture_output=True, text=True)
        subprocess.run([*tm, "set-option", "-p", "-u", "-t", pane_id, "remain-on-exit"], capture_output=True)
        if r.returncode != 0:
            raise CrpmError(f"respawn failed: {r.stderr.strip()}. Run in {s.cwd}:\n  {cmd}")
        time.sleep(RECONNECT_SETTLE)
        dead = subprocess.run([*tm, "display-message", "-p", "-t", pane_id, "#{pane_dead}"],
                              capture_output=True, text=True).stdout.strip()
        if dead != "0":
            raise CrpmError(f"claude exited right away. To see why, run in {s.cwd}:\n  {cmd}")
        name = subprocess.run([*tm, "display-message", "-p", "-t", pane_id, "#{session_name}"],
                              capture_output=True, text=True).stdout.strip()
        return f"restarted in the same tmux pane: tmux -S {sock} attach -t {name}"
    short = short_id(s.session_id) if s.session_id else s.conversation[:8]
    return launch_detached(cfg, reconnect_name(s.cwd.name, short), s.cwd, cmd, s.key)


def session_project(cfg: dict, s: LiveSession) -> str | None:
    """The projects_dir folder a session runs in (its worktree is inside it), if any."""
    root = cfg["projects_dir"]
    return s.cwd.relative_to(root).parts[0] if root in s.cwd.parents else None


def session_info(cfg: dict, s: LiveSession) -> dict:
    """A session as plain data, for `crpm sessions --json` and the web dashboard."""
    project = session_project(cfg, s)
    rel = Path(*s.cwd.relative_to(cfg["projects_dir"]).parts[1:]) if project else s.cwd  # within its project
    wt = bridge_worktree(s.cwd)
    parts = s.cwd.parts
    worktree = wt[0].name if wt else (parts[parts.index("worktrees") + 1]
                                      if ".claude" in parts and "worktrees" in parts[:-1] else "")
    return {"key": s.key, "session_id": s.session_id, "conversation": s.conversation,
            "remote": bool(s.session_id),
            "url": f"https://claude.ai/code/{s.session_id}" if s.session_id else "",
            "name": s.name, "kind": s.kind, "status": s.status, "pid": s.pid, "project": project,
            "folder": str(rel), "worktree": worktree, "tmux": s.tmux}


def project_sessions(cfg: dict, project: str | None = None, include_local: bool = True) -> list[dict]:
    """Running sessions (session_info), optionally only one project's, sorted by project and name."""
    out = [session_info(cfg, s) for s in live_sessions(include_local=include_local)]
    if project is not None:
        out = [i for i in out if i["project"] == project]
    return sorted(out, key=lambda i: ((i["project"] or "~").lower(), i["name"].lower(), i["key"]))


# --------------------------------------------------------------------------- new-session

NEW_SESSION_PREFIX = "sess-"   # must not start with session_prefix (the daemon would stop it)
REGISTER_TIMEOUT = 45.0        # seconds to wait for the new session to connect to claude.ai


def git_has_commit(path: Path) -> bool:
    return subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "-q", "HEAD"],
                          capture_output=True).returncode == 0


def wait_for_session(pane_pid: int, timeout: float) -> LiveSession | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in live_sessions():
            if pane_pid in (s.pid, s.parent_pid):
                return s
        if proc_start(pane_pid) is None:
            return None
        time.sleep(1)
    return None


@dataclass
class NewSession:
    name: str
    url: str
    folder: str
    tmux: str
    message: str


def new_session(cfg: dict, project: Path, prompt: str | None = None, name: str | None = None,
                here: bool = False) -> NewSession:
    """Start a Remote Control session in `project` (in a new worktree unless `here`) and wait
    until it connects to claude.ai."""
    project = project.resolve()
    if not project.is_dir():
        raise CrpmError(f"no project folder {project}")
    stamp = time.strftime("%m%d-%H%M%S")
    name = name or f"{project.name}-{stamp}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or stamp

    argv = [cfg["claude_bin"]]
    use_worktree = (project / ".git").exists() and not here
    if use_worktree:
        if not git_has_commit(project):
            raise CrpmError(f"{project.name} has no commits yet, and a worktree is made from one. "
                            "Commit first, or work in the project folder (--here).")
        if (project / ".claude" / "worktrees" / slug).exists():
            raise CrpmError(f"worktree {slug} already exists; pick another name")
        argv.append(f"--worktree={slug}")
    if cfg["permission_mode"]:
        argv += ["--permission-mode", cfg["permission_mode"]]
    argv.append(f"--remote-control={name}")
    if prompt:
        argv += ["--", prompt]
    cmd = shlex.join(argv)

    tname = session_name(NEW_SESSION_PREFIX, f"{project.name}-{slug}")
    t = Tmux(cfg["tmux_socket"])
    if t.has(tname):
        raise CrpmError(f"tmux session {tname} already exists; pick another name")
    started = launch_detached(cfg, tname, project, cmd,
                              f"{name} ({'worktree ' + slug if use_worktree else 'in ' + str(project)})")
    pane_pid = int(t.run("display-message", "-p", "-t", tname, "#{pane_pid}").stdout.strip() or 0)
    s = wait_for_session(pane_pid, REGISTER_TIMEOUT)
    if s:
        url = f"https://claude.ai/code/{s.session_id}"
        return NewSession(name, url, str(s.cwd), tname, f"{started}\n  connected: {url}\n  folder: {s.cwd}")
    if t.has(tname) and not t.is_dead(tname):
        raise CrpmError(f"{started}\n  not connected to claude.ai after {REGISTER_TIMEOUT:.0f}s; it may be "
                        f"waiting on a prompt. Attach to check: tmux -L {cfg['tmux_socket']} attach -t {tname}")
    raise CrpmError(f"claude exited. To see why, run in {project}:\n  {cmd}")


# --------------------------------------------------------------------------- session registry
# Claude Code deletes its per-process session records, so after a reboot nothing says which sessions
# were running. The daemon keeps its own list in log_dir/sessions.json: every Remote Control session
# it sees, tagged with the kernel boot it was seen in, and when it ended if it ended while the machine
# stayed up. A session still running at the end of the previous boot was interrupted by the restart,
# unless the daemon had been stopped before that boot ended: then it can't tell, and says nothing.
# Only the daemon writes sessions.json. The CLI and the dashboard record failed and dismissed
# reconnects in log_dir/reconnects.json instead.

REGISTRY_KEEP = 14 * 24 * 3600  # forget sessions not seen for this long
REGISTRY_HEARTBEAT = 15.0       # seconds between registry writes when nothing changes
SHUTDOWN_GRACE = 60.0           # sessions that ended this close to a boot's last heartbeat died with it
BOOTS_KEEP = 5                  # recent boots remembered even if no session ran in them
REATTACH_WINDOW = 6 * 3600      # Claude Code reattaches app sessions for about 4h after they stop

# Flags of an interactive session that resuming it keeps. Everything else is dropped: --resume,
# --continue, --worktree (it would make a new worktree), --print, and the first message.
RESUME_BOOL = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--strict-mcp-config",
               "--verbose", "--ide", "--chrome", "--no-chrome", "--brief", "--bare", "--safe-mode",
               "--restricted", "--disable-slash-commands", "--ax-screen-reader"}
RESUME_VALUE = {"--permission-mode", "--model", "--fallback-model", "--effort", "--agent", "--agents",
                "--settings", "--setting-sources", "--system-prompt", "--system-prompt-file",
                "--append-system-prompt", "--append-system-prompt-file", "--plugin-dir", "--plugin-url",
                "--autocompact", "--max-budget-usd", "--permission-prompts", "--debug-file", "--name", "-n"}
RESUME_MANY = {"--add-dir", "--mcp-config", "--allowedTools", "--allowed-tools", "--disallowedTools",
               "--disallowed-tools", "--tools", "--betas"}


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def registry_path(cfg: dict) -> Path:
    return Path(cfg["log_dir"]).expanduser() / "sessions.json"


def load_registry(cfg: dict) -> dict:
    """{"sessions": {session_id: entry}, "boots": {boot_id: last heartbeat},
    "stopped": {boot_id: when the daemon was stopped, if that boot then ended without it}}"""
    try:
        d = json.loads(registry_path(cfg).read_text())
        if isinstance(d.get("sessions"), dict) and isinstance(d.get("boots"), dict):
            d.setdefault("stopped", {})
            return d
    except (OSError, ValueError, AttributeError):
        pass
    return {"sessions": {}, "boots": {}, "stopped": {}}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def save_registry(cfg: dict, reg: dict) -> None:
    write_json(registry_path(cfg), reg)


def reconnects_path(cfg: dict) -> Path:
    return Path(cfg["log_dir"]).expanduser() / "reconnects.json"


def load_reconnects(cfg: dict) -> dict[str, dict]:
    """{session_id: {"at": time, "failed": error} or {"at": time, "dismissed": true}}"""
    try:
        d = json.loads(reconnects_path(cfg).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_reconnect(cfg: dict, sids: list[str], **what) -> None:
    """Record that reconnecting these sessions failed (failed=error) or was dismissed (dismissed=True).
    A mark only counts until the session is seen running again."""
    d, now = load_reconnects(cfg), time.time()
    d = {k: v for k, v in d.items() if now - v.get("at", 0) < REGISTRY_KEEP}
    for sid in sids:
        d[sid] = {"at": now, **what}
    write_json(reconnects_path(cfg), d)


def launch_flags(argv: list[str]) -> dict:
    """What resuming an interactive claude should keep from its command line:
    {"args": [kept flags], "rc_name": its --remote-control name or None}."""
    args: list[str] = []
    rc_name, i = None, 1
    while i < len(argv):
        arg = argv[i]
        i += 1
        if arg == "--":
            break  # the rest is the first message
        flag, eq, val = arg.partition("=")
        if flag in ("--remote-control", "--rc"):  # optional value
            if eq:
                rc_name = val
            elif i < len(argv) and not argv[i].startswith("-"):
                rc_name, i = argv[i], i + 1
        elif flag in RESUME_BOOL:
            args.append(arg)
        elif flag in RESUME_VALUE:
            if eq:
                args.append(arg)
            elif i < len(argv):
                args += [arg, argv[i]]
                i += 1
        elif flag in RESUME_MANY:
            vals = [val] if eq else []
            while i < len(argv) and not argv[i].startswith("-"):
                vals.append(argv[i])
                i += 1
            if vals:
                args += [flag, *vals]
    return {"args": args, "rc_name": rc_name}


def track_sessions(cfg: dict, reg: dict, live: list[LiveSession]) -> bool:
    """Update the registry from the sessions running now. True if anything but timestamps changed."""
    now, boot = time.time(), boot_id()
    sessions: dict[str, dict] = reg["sessions"]
    before = {k: (e.get("conversation"), e.get("boot"), e.get("ended_at") is None) for k, e in sessions.items()}
    seen = set()
    for s in live:
        if not s.session_id:
            continue
        seen.add(s.session_id)
        e = sessions.setdefault(s.session_id, {"session_id": s.session_id, "first_seen": now})
        if e.get("boot") != boot:
            e["first_seen"] = now  # running again after a reboot: a new run as far as its server goes
        e.update(conversation=s.conversation, cwd=str(s.cwd), name=s.name, kind=s.kind,
                 project=session_project(cfg, s) or "", tmux=s.tmux, boot=boot, last_seen=now, ended_at=None)
        if s.kind == "terminal":
            e["flags"] = launch_flags(proc_argv(s.pid))
    for sid, e in sessions.items():
        if sid not in seen and e.get("boot") == boot and e.get("ended_at") is None:
            e["ended_at"] = now
    for sid in [k for k, e in sessions.items() if now - e.get("last_seen", 0) > REGISTRY_KEEP]:
        del sessions[sid]
    reg["boots"][boot] = now
    recent = sorted(reg["boots"], key=reg["boots"].get, reverse=True)[:BOOTS_KEEP]
    used = {e.get("boot") for e in sessions.values()} | set(recent)
    reg["boots"] = {b: t for b, t in reg["boots"].items() if b in used}
    reg["stopped"] = {b: t for b, t in reg.get("stopped", {}).items() if b in reg["boots"]}
    return before != {k: (e.get("conversation"), e.get("boot"), e.get("ended_at") is None)
                      for k, e in sessions.items()}


def previous_boot(reg: dict, boot: str) -> str | None:
    earlier = [(t, b) for b, t in reg["boots"].items() if b != boot]
    return max(earlier)[1] if earlier else None


def interrupted(reg: dict, e: dict, boot: str) -> bool:
    """Was this session running when the machine last restarted?"""
    prev = previous_boot(reg, boot)
    if not prev or e.get("boot") != prev or prev in reg.get("stopped", {}):
        return False  # not from the last boot, or the daemon was stopped then and couldn't see
    return e.get("ended_at") is None or e["ended_at"] >= reg["boots"][prev] - SHUTDOWN_GRACE


def system_stopping() -> bool:
    """Is the machine shutting down? (Else the daemon is just being stopped or restarted.)"""
    try:
        r = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() == "stopping"
    except (OSError, subprocess.TimeoutExpired):
        return True  # can't ask: most likely going down


def resume_terminal(cfg: dict, e: dict) -> str:
    """Resume an interactive session's conversation, Remote Control on, in a detached tmux session."""
    sid, cwd, conv = e["session_id"], Path(e["cwd"]), e.get("conversation") or ""
    transcript = claude_config_dir() / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{conv}.jsonl"
    if not conv or not transcript.exists():
        raise CrpmError(f"its conversation's transcript ({transcript}) is gone, so there is nothing to resume")
    flags = e.get("flags") or {}
    rc_name = flags.get("rc_name") or e.get("name") or cwd.name
    argv = [cfg["claude_bin"], "--resume", conv, *flags.get("args", []), f"--remote-control={rc_name}"]
    tmux = e.get("tmux") or ""
    if not tmux.startswith(NEW_SESSION_PREFIX):  # only reuse names crpm gave it, on crpm's tmux server
        tmux = reconnect_name(e.get("project") or cwd.name, sid)
    return launch_detached(cfg, tmux, cwd, shlex.join(argv), f"{e.get('name') or sid} ({sid})")


def reconnect(cfg: dict, session_id: str) -> str:
    """Bring a stopped session back on Remote Control, whatever kind it was. Records a failure,
    so `reconnect --all` and the dashboard stop counting it as waiting."""
    short = short_id(session_id)
    if not re.fullmatch(r"[A-Za-z0-9]+", short):
        raise CrpmError(f"not a session ID: {session_id}")
    sid = f"session_{short}"
    try:
        return _reconnect(cfg, sid)
    except Exception as err:
        if not (isinstance(err, CrpmError) and "already" in str(err)):
            try:
                mark_reconnect(cfg, [sid], failed=str(err) or type(err).__name__)
            except OSError:
                pass
        raise


def _reconnect(cfg: dict, sid: str) -> str:
    e = load_registry(cfg)["sessions"].get(sid)
    if not e:
        return reconnect_session(cfg, find_bridge_worktree(cfg, sid), sid)
    cwd = Path(e["cwd"])
    if not cwd.is_dir():
        raise CrpmError(f"it ran in {cwd}, which no longer exists")
    pid = serving_pid(sid)
    if pid:
        raise CrpmError(f"{sid} is already running (PID {pid})")
    if e["kind"] == "terminal":
        return resume_terminal(cfg, e)
    found = bridge_worktree(cwd)
    return reconnect_session(cfg, found[0] if found else cwd, sid, e.get("project") or cwd.name)


def reconnect_all(cfg: dict, todo: list[Reconnectable] | None = None) -> list[dict]:
    """Reconnect every session the last restart interrupted, and wait for them to come back on."""
    from concurrent.futures import ThreadPoolExecutor
    todo = [r for r in reconnect_candidates(cfg) if r.waiting] if todo is None else todo

    def one(r: Reconnectable) -> dict:
        out = {**asdict(r), "url": f"https://claude.ai/code/{r.session_id}", "connected": False}
        try:
            return {**out, "ok": True, "message": reconnect(cfg, r.session_id)}
        except Exception as e:  # one session's failure must not hide what happened to the others
            return {**out, "ok": False, "message": str(e) or type(e).__name__}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, todo))
    pending = {x["session_id"] for x in results if x["ok"]}
    deadline = time.time() + REGISTER_TIMEOUT
    while pending and time.time() < deadline:
        pending -= {s.session_id for s in live_sessions()}
        if pending:
            time.sleep(1)
    for x in results:
        x["connected"] = x["ok"] and x["session_id"] not in pending
    return results


def dismiss(cfg: dict, session_ids: list[str] | None = None) -> list[str]:
    """Stop offering these sessions (default: every interrupted one) for reconnecting."""
    if session_ids is None:
        sids = [r.session_id for r in reconnect_candidates(cfg) if r.interrupted]
    else:
        sids = [f"session_{short_id(i)}" for i in session_ids]
    if sids:
        mark_reconnect(cfg, sids, dismissed=True)
    return sids


# --------------------------------------------------------------------------- CLI

def cmd_status(cfg: dict, _a) -> int:
    have = servers(cfg)
    if not have:
        print("no managed sessions running")
    for s in have:
        print(f"{s.tmux:40} {s.state:26} {s.capacity:8} {s.mode:9} {s.path}")
    cut = sum(r.waiting for r in reconnect_candidates(cfg))
    if cut:
        print(f"\n{cut} session(s) were running when the machine restarted: crpm reconnect --all")
    return 0


def cmd_attach(cfg: dict, a) -> int:
    os.execvp("tmux", ["tmux", "-L", cfg["tmux_socket"], "attach", "-t", resolve(cfg, a.project)])


def cmd_restart(cfg: dict, a) -> int:
    print(restart_project(cfg, a.project))
    return 0


def cmd_reconnect(cfg: dict, a) -> int:
    if a.dry_run and not a.all:
        raise CrpmError("--dry-run only goes with --all (to see everything, use --list)")
    if a.dismiss:
        sids = dismiss(cfg, [a.session_id] if a.session_id else None)
        print(f"dismissed {len(sids)} session(s)" if sids else "no interrupted sessions to dismiss")
        return 0
    if a.list or a.all:
        found = reconnect_candidates(cfg)
        todo = [r for r in found if r.waiting] if a.all else found
        if not todo:
            print("no sessions are waiting to be reconnected after the last restart" if a.all
                  else "nothing to reconnect")
            if a.all and found:
                print(f"({len(found)} other stopped or failed session(s): crpm reconnect --list)")
            return 0
        if a.list or a.dry_run:
            row = "{:36} {:10} {:16} {:28} {}"
            print(row.format("SESSION ID", "KIND", "STOPPED", "PROJECT", "NAME"))
            for r in todo:
                when = "at restart" if r.interrupted else ago(r.last_active)
                print(row.format(r.session_id, r.kind, when, (r.project or "-")[:28],
                                 r.name or Path(r.worktree).name))
                if r.failed:
                    print(f"  └ last reconnect failed: {r.failed.splitlines()[0]}")
            if a.list and any(r.waiting for r in todo):
                print("\nbring back the ones stopped at restart: crpm reconnect --all"
                      "\nor stop listing them:                   crpm reconnect --dismiss")
            return 0
        print(f"reconnecting {len(todo)} session(s)…", flush=True)
        failed = 0
        for x in reconnect_all(cfg, todo):
            label = f"{x['project'] or Path(x['worktree']).parent}: {x['name'] or Path(x['worktree']).name}"
            if x["connected"]:
                print(f"  ok      {label}\n          {x['url']}")
            elif x["ok"]:
                print(f"  started {label} (not on claude.ai yet; it may be waiting on a prompt)\n"
                      f"          {x['message'].splitlines()[-1].strip()}")
            else:
                failed += 1
                print(f"  FAILED  {label}: {x['message']}")
        return 1 if failed else 0
    if a.session_id:
        print(reconnect(cfg, a.session_id))
        return 0
    found = bridge_worktree(Path.cwd())
    if found:
        print(reconnect(cfg, found[1]))
        return 0
    here = [r for r in reconnect_candidates(cfg) if Path(r.worktree) == Path.cwd().resolve()]
    if len(here) == 1:
        print(reconnect(cfg, here[0].session_id))
        return 0
    raise CrpmError("pass a session ID (see crpm reconnect --list), --all, or run this from the folder "
                    "of the session to bring back" + (f" ({len(here)} stopped sessions ran here)" if here else ""))


def ago(t: float) -> str:
    d = max(0, time.time() - t)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{int(d // n)}{unit} ago"
    return "just now"


def cmd_restart_session(cfg: dict, a) -> int:
    sessions = live_sessions()
    if a.ids:
        wanted = {short_id(i) for i in a.ids}
        chosen = [s for s in sessions if short_id(s.session_id) in wanted]
        missing = wanted - {short_id(s.session_id) for s in chosen}
        for m in sorted(missing):
            print(f"crpm restart-session: no running session session_{m} on this machine", file=sys.stderr)
        if missing and not chosen:
            return 1
    elif a.project:
        root = (cfg["projects_dir"] / a.project).resolve()
        chosen = [s for s in sessions if s.cwd == root or root in s.cwd.parents]
    else:
        here = Path.cwd()
        chosen = [s for s in sessions if s.cwd == here or s.cwd in here.parents]
    if not chosen:
        raise CrpmError("no running Remote Control sessions match "
                        "(pass a session ID, --project NAME, or run it from a session's folder)")
    if len(chosen) > 1 and not (a.ids or a.project or a.all):
        print("Several sessions run in this folder; pass their IDs or --all:")
        for s in chosen:
            print(f"  {s.session_id}  {s.kind:10} {s.status}")
        return 1

    rc = 0
    for s in chosen:
        label = f"{s.session_id} ({s.kind}, {s.status}, {s.cwd})"
        if s.status != "idle" and not a.force:
            print(f"skip {label}: not idle. Wait for it to finish, or answer its question, or pass --force.")
            continue
        if a.dry_run:
            print(f"would restart {label}")
            continue
        if is_ancestor(s.pid) and not a.detached:
            # This command runs inside that session: finish from outside it, or we die with it.
            argv = [sys.executable, os.path.realpath(__file__), "restart-session", s.session_id,
                    "--force", "--detached"]
            log_path = Path(cfg["log_dir"]).expanduser() / "restart-session.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as f:
                subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=f, stderr=f)
            print(f"restarting {label} from the background, since this command runs inside it "
                  f"(log: {log_path})")
            continue
        print(f"restarting {label}")
        try:
            print("  " + restart_session(cfg, s))
        except CrpmError as e:
            print(f"  {e}", file=sys.stderr)
            rc = 1
    return rc


def project_from_cwd(cfg: dict) -> Path | None:
    here, root = Path.cwd(), cfg["projects_dir"]
    if here == root or root not in here.parents:
        return None
    return root / here.relative_to(root).parts[0]


def cmd_sessions(cfg: dict, a) -> int:
    if a.project:
        project = a.project.rstrip("/").rsplit("/", 1)[-1]
        if not (cfg["projects_dir"] / project).is_dir():
            raise CrpmError(f"no project folder {cfg['projects_dir'] / project}")
    else:
        here = project_from_cwd(cfg)
        project = here.name if here else None  # outside any project: list them all
    found = project_sessions(cfg, project, include_local=not a.remote)
    if a.json:
        print(json.dumps(found, indent=2))
        return 0
    if a.ids:
        print("\n".join(i["session_id"] for i in found if i["session_id"]))
        return 0
    if not found:
        print(f"no running sessions{' in ' + project if project else ''}")
        return 0
    def clip(text: str, n: int) -> str:
        return text if len(text) <= n else text[:n - 1] + "…"
    row = "  {:36} {:6} {:10} {:30} {}"
    print(row.format("SESSION ID", "STATUS", "KIND", "NAME", "WHERE"))
    last = object()
    for i in found:
        if i["project"] != last:
            last = i["project"]
            print(last or "(outside projects_dir)")
        where = f"worktree {i['worktree']}" if i["worktree"] else \
            (i["folder"] if i["folder"] != "." else "project folder")
        sid = i["session_id"] or i["conversation"]
        print(row.format(sid, i["status"] if i["remote"] else "local", i["kind"], clip(i["name"] or "-", 30), where))
    if any(not i["remote"] for i in found):
        print("\nlocal: running here but not on Remote Control, so the app can't reach it.\n"
              "  The ID is its conversation (claude --resume <id>). The dashboard's Connect button puts it back on.")
    return 0


def cmd_new_session(cfg: dict, a) -> int:
    if a.project and not a.prompt and "/" not in a.project and project_from_cwd(cfg) \
            and not (cfg["projects_dir"] / a.project).is_dir():
        a.project, a.prompt = None, a.project  # `crpm new-session "message"` from inside a project
    if a.project:
        project = Path(a.project).expanduser() if "/" in a.project else cfg["projects_dir"] / a.project
    else:
        project = project_from_cwd(cfg)
        if project is None:
            raise CrpmError("name a project, or run this from inside one")
    print(new_session(cfg, project, a.prompt, a.name, a.here).message)
    return 0


def cmd_stop_all(cfg: dict, _a) -> int:
    m = Manager(cfg)
    for name in m.tmux.sessions(cfg["session_prefix"]):
        m.stop(name)
    return 0


# --------------------------------------------------------------------------- web dashboard
# Auth is per user from the start: the tokens file maps each token (stored hashed) to a user, every
# API call runs as that User and is logged with its name. One line in the file today; more users,
# or an identity from `tailscale serve` headers, can be added without changing the API.

WEB_TOKENS = CONFIG_PATH.parent / "web-tokens"   # lines: "<user> <sha256 of token>"
WEB_PAGE = Path(os.path.realpath(__file__)).with_name("crpm_web.html")
WEB_MAX_BODY = 64 * 1024
USER_NAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


@dataclass(frozen=True)
class User:
    name: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_tokens() -> dict[str, User]:
    """{sha256(token): User}. Read per request, so `crpm web-token` applies without a restart."""
    out = {}
    try:
        text = WEB_TOKENS.read_text()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and not line.startswith("#"):
            out[parts[1]] = User(parts[0])
    return out


def write_tokens(lines: list[str]) -> None:
    WEB_TOKENS.parent.mkdir(parents=True, exist_ok=True)
    tmp = WEB_TOKENS.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(l + "\n" for l in lines))
    os.replace(tmp, WEB_TOKENS)


def set_token(user: str, revoke: bool = False) -> str:
    """Give `user` a new token (replacing any old one), or just remove theirs. Returns the token."""
    if not USER_NAME.match(user):
        raise CrpmError(f"invalid user name {user!r}")
    try:
        lines = WEB_TOKENS.read_text().splitlines()
    except FileNotFoundError:
        lines = ["# crpm web dashboard logins: <user> <sha256 of token>. Manage with `crpm web-token`."]
    lines = [l for l in lines if l.split()[:1] != [user]]
    token = "" if revoke else secrets.token_urlsafe(32)
    if token:
        lines.append(f"{user} {token_hash(token)}")
    write_tokens(lines)
    return token


def parse_listen(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    return host.strip("[]") or "127.0.0.1", int(port)


def web_project(cfg: dict, name: str) -> str:
    """A project the API may act on: a folder in projects_dir, or a running server. Names only, never paths."""
    if name in {p.name for p in project_dirs(cfg)} | {s.project for s in servers(cfg)}:
        return name
    raise CrpmError(f"no project {name!r}")


def api_status(cfg: dict, user: User, _body: dict) -> dict:
    by_name = {s.project: s for s in servers(cfg)}
    projects: dict[str, dict] = {}
    for p in project_dirs(cfg):
        projects[p.name] = {"name": p.name, "path": str(p), "git": (p / ".git").exists(),
                            "server": None, "sessions": [], "reconnectable": []}
    for name, s in by_name.items():
        projects.setdefault(name, {"name": name, "path": s.path, "git": s.mode == "worktree",
                                   "sessions": [], "reconnectable": []})["server"] = asdict(s)
    for p in projects.values():
        # new-session needs a commit for a worktree; a server in worktree mode implies one
        p["has_commit"] = p["git"] and ((p["server"] or {}).get("mode") == "worktree"
                                        or git_has_commit(Path(p["path"])))
    other = []
    for i in project_sessions(cfg):
        (projects[i["project"]]["sessions"] if i["project"] in projects else other).append(i)
    other_reconnectable = []
    for r in reconnect_candidates(cfg):
        item = {**asdict(r), "waiting": r.waiting}
        (projects[r.project]["reconnectable"] if r.project in projects else other_reconnectable).append(item)
    every = [r for p in projects.values() for r in p["reconnectable"]] + other_reconnectable
    return {"user": user.name, "host": socket.gethostname(), "time": time.time(),
            "projects": list(projects.values()), "other_sessions": other,
            "other_reconnectable": other_reconnectable, "interrupted": sum(r["waiting"] for r in every)}


def api_sessions(cfg: dict, _user: User, _body: dict, project: str) -> dict:
    name = web_project(cfg, project)
    return {"project": name, "sessions": project_sessions(cfg, name)}


def api_screen(cfg: dict, _user: User, _body: dict, project: str) -> dict:
    return project_screen(cfg, web_project(cfg, project))


def api_restart_project(cfg: dict, _user: User, _body: dict, project: str) -> dict:
    return {"message": restart_project(cfg, web_project(cfg, project))}


def api_trust(cfg: dict, _user: User, _body: dict, project: str) -> dict:
    return {"message": trust_project(cfg, web_project(cfg, project))}


def api_new_session(cfg: dict, _user: User, body: dict) -> dict:
    project = cfg["projects_dir"] / web_project(cfg, str(body.get("project") or ""))
    name = str(body.get("name") or "").strip() or None
    prompt = str(body.get("prompt") or "").strip() or None
    return asdict(new_session(cfg, project, prompt, name, bool(body.get("here"))))


def api_restart_session(cfg: dict, _user: User, body: dict, key: str) -> dict:
    s = next((s for s in live_sessions(include_local=True) if key in (s.session_id, s.conversation)), None)
    if not s:
        raise CrpmError(f"no running session {key}")
    if s.status != "idle" and not body.get("force"):
        raise CrpmError(f"{s.name or key} is {s.status}; wait for it to finish, or force the restart")
    if is_ancestor(s.pid):
        raise CrpmError("this dashboard runs inside that session; restart it from the CLI")
    return {"message": restart_session(cfg, s)}


def api_reconnect(cfg: dict, _user: User, _body: dict, session_id: str) -> dict:
    return {"message": reconnect(cfg, session_id)}


def api_reconnect_all(cfg: dict, _user: User, _body: dict) -> dict:
    results = reconnect_all(cfg)
    if not results:
        raise CrpmError("no sessions are waiting to be reconnected")
    up = sum(x["connected"] for x in results)
    bad = [f"{x['name'] or x['session_id']}: {x['message']}" for x in results if not x["ok"]]
    msg = f"reconnected {up} of {len(results)} session(s)" + ("".join("\n" + b for b in bad))
    return {"message": msg, "results": results}


def api_dismiss(cfg: dict, _user: User, body: dict) -> dict:
    """Stop offering a session (body.session_id), or every interrupted one, for reconnecting."""
    sid = str(body.get("session_id") or "").strip()
    sids = dismiss(cfg, [sid] if sid else None)
    return {"message": f"dismissed {len(sids)} session(s)"}


WEB_ROUTES = [
    ("GET", r"/api/status", api_status),
    ("GET", r"/api/projects/([^/]+)/sessions", api_sessions),
    ("GET", r"/api/projects/([^/]+)/screen", api_screen),
    ("POST", r"/api/projects/([^/]+)/restart", api_restart_project),
    ("POST", r"/api/projects/([^/]+)/trust", api_trust),
    ("POST", r"/api/sessions", api_new_session),
    ("POST", r"/api/sessions/([^/]+)/restart", api_restart_session),
    ("POST", r"/api/sessions/([^/]+)/reconnect", api_reconnect),
    ("POST", r"/api/reconnect-all", api_reconnect_all),
    ("POST", r"/api/reconnect-dismiss", api_dismiss),
]

PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "connect-src 'self'; img-src data:; manifest-src 'self'; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def make_handler(cfg: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "crpm"

        def log_message(self, fmt, *args):  # never log headers (the token is in one)
            log.debug("web %s " + fmt, self.address_string(), *args)

        def send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            for k, v in PAGE_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, code: int, data: dict) -> None:
            self.send(code, json.dumps(data).encode(), "application/json")

        def user(self) -> User | None:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            return load_tokens().get(token_hash(auth[7:].strip()))

        def handle_request(self, method: str) -> None:
            path = urlsplit(self.path).path
            if method == "GET" and path in ("/", "/index.html"):
                try:
                    return self.send(200, WEB_PAGE.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self.send(500, b"crpm_web.html is missing", "text/plain")
            for m, pattern, fn in WEB_ROUTES:
                match = re.fullmatch(pattern, path)
                if m == method and match:
                    break
            else:
                return self.send_json(404, {"error": "not found"})
            user = self.user()
            if not user:
                return self.send_json(401, {"error": "not logged in"})
            body: dict = {}
            if method == "POST":
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                    return self.send_json(403, {"error": "cross-origin request"})
                n = int(self.headers.get("Content-Length") or 0)
                if n > WEB_MAX_BODY:
                    return self.send_json(413, {"error": "request too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    assert isinstance(body, dict)
                except (ValueError, AssertionError):
                    return self.send_json(400, {"error": "body must be a JSON object"})
                log.info("web %s: %s %s %s", user.name, method, path,
                         json.dumps({k: v for k, v in body.items() if k != "prompt"}))
            try:
                self.send_json(200, fn(cfg, user, body, *(unquote(g) for g in match.groups())))
            except CrpmError as e:
                if method == "POST":
                    log.info("web %s: %s %s failed: %s", user.name, method, path, e)
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                log.exception("web %s %s failed", method, path)
                self.send_json(500, {"error": f"internal error: {e}"})

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

    return Handler


def cmd_web(cfg: dict, a) -> int:
    host, port = parse_listen(a.listen or cfg["web_listen"])
    if not load_tokens():
        raise CrpmError("no logins yet: run `crpm web-token` to create one")
    if host not in ("127.0.0.1", "::1", "localhost"):
        log.warning("listening on %s: anyone who can reach it and has a token can run Claude here. "
                    "Prefer 127.0.0.1 plus `tailscale serve` or an SSH tunnel.", host)
    ThreadingHTTPServer.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg))
    httpd.daemon_threads = True
    def on_term(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    log.info("web dashboard on http://%s:%d/", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("web dashboard stopping")
    return 0


def cmd_web_token(cfg: dict, a) -> int:
    user = a.user or getpass.getuser()
    token = set_token(user, revoke=a.revoke)
    if a.revoke:
        print(f"removed {user}'s login")
        return 0
    host, port = parse_listen(cfg["web_listen"])
    print(f"login for {user} (replaces any earlier one; shown only now):\n"
          f"  http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{port}/#token={token}\n"
          f"Through `tailscale serve`, open https://<this machine>.<tailnet>.ts.net/#token={token}\n"
          f"Or paste the token on the login page: {token}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="crpm", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon")
    sub.add_parser("status")
    sub.add_parser("stop-all")
    rc = sub.add_parser("reconnect", help="bring stopped sessions back on Remote Control (e.g. after a reboot)")
    rc.add_argument("session_id", nargs="?", metavar="SESSION_ID",
                    help="session_..., cse_..., or the bare ID (default: the session that ran in this folder)")
    rc.add_argument("--all", action="store_true", help="every session that was running at the last restart")
    rc.add_argument("--list", action="store_true", help="list sessions that can be reconnected")
    rc.add_argument("--dry-run", action="store_true", help="with --all: only show what would be reconnected")
    rc.add_argument("--dismiss", action="store_true",
                    help="stop offering this session (default: every interrupted one) for reconnecting")
    rs = sub.add_parser("restart-session", help="restart idle sessions so they load new config")
    rs.add_argument("ids", nargs="*", metavar="SESSION_ID",
                    help="session_..., cse_..., or the bare ID (default: sessions in the current folder)")
    rs.add_argument("--project", help="every running session in this project")
    rs.add_argument("--all", action="store_true", help="every session in the current folder")
    rs.add_argument("--force", action="store_true", help="also restart sessions that are not idle")
    rs.add_argument("--dry-run", action="store_true", help="only show what would be restarted")
    rs.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
    ss = sub.add_parser("sessions", help="list running sessions and their session IDs")
    ss.add_argument("project", nargs="?", help="project folder name (default: the project you're in, else all)")
    ss.add_argument("--ids", action="store_true", help="print only session IDs, one per line")
    ss.add_argument("--json", action="store_true", help="print full details as JSON")
    ss.add_argument("--remote", action="store_true", help="only sessions on Remote Control")
    ns = sub.add_parser("new-session", help="start a new Remote Control session in a project")
    ns.add_argument("project", nargs="?", help="project folder name or path (default: the project you're in)")
    ns.add_argument("prompt", nargs="?", help="first message for the session")
    ns.add_argument("-n", "--name", help="session name in the app, also used for the worktree "
                                         "(default: <project>-<date>-<time>)")
    ns.add_argument("--here", action="store_true", help="work in the project folder, not a new worktree")
    for n in ("attach", "restart"):
        sp = sub.add_parser(n)
        sp.add_argument("project")
    wb = sub.add_parser("web", help="serve the web dashboard")
    wb.add_argument("--listen", help="host:port (default: web_listen in the config, 127.0.0.1:8765)")
    wt = sub.add_parser("web-token", help="create or replace a web dashboard login")
    wt.add_argument("user", nargs="?", help="user name (default: your login name)")
    wt.add_argument("--revoke", action="store_true", help="remove the user's login instead")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(message)s" if os.environ.get("INVOCATION_ID")
                        else "%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    if a.cmd == "daemon":
        Manager(cfg).run()
        return 0
    try:
        return {"status": cmd_status, "attach": cmd_attach, "restart": cmd_restart,
                "reconnect": cmd_reconnect, "restart-session": cmd_restart_session,
                "sessions": cmd_sessions, "new-session": cmd_new_session, "stop-all": cmd_stop_all,
                "web": cmd_web, "web-token": cmd_web_token}[a.cmd](cfg, a)
    except CrpmError as e:
        print(f"crpm {a.cmd}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
