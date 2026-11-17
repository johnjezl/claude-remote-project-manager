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
  reconnect         from a session's worktree, bring that session back in tmux
  restart-session   restart idle sessions so they load new settings, MCP servers, etc.
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

    def run(self) -> None:
        def on_term(signum, _frame):
            log.info("signal %s: exiting (managed sessions keep running)", signum)
            self.stopping = True
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)
        log.info("watching %s (tmux socket '%s')", self.cfg["projects_dir"], self.cfg["tmux_socket"])
        while not self.stopping:
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            for _ in range(int(self.cfg["poll_interval"] * 10)):
                if self.stopping:
                    break
                time.sleep(0.1)


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


def reconnect_session(cfg: dict, worktree: Path, session_id: str) -> str:
    """Serve a spawned session again, from its worktree, after its project's server stopped."""
    name = reconnect_name(worktree.parents[2].name, session_id)
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
    project: str
    session_id: str
    worktree: str
    last_active: float


def reconnect_candidates(cfg: dict) -> list[Reconnectable]:
    """Spawned-session worktrees that nothing serves and whose server no longer knows them.

    A project's server serves its own spawned sessions again on their next message, so only
    worktrees made before that server started (i.e. by an earlier server) need a reconnect."""
    served = served_session_ids()
    started = {Path(i["path"]).name: int(i["created"] or 0)
               for i in Tmux(cfg["tmux_socket"]).sessions(cfg["session_prefix"]).values()
               if not i["dead"] and i["path"]}
    out = []
    for wt in cfg["projects_dir"].glob("*/.claude/worktrees/bridge-cse_*"):
        found = bridge_worktree(wt)
        if not found or found[1] in served:
            continue
        project = wt.parents[2].name
        try:
            made = (wt / ".git").stat().st_mtime
        except OSError:
            continue  # not (or no longer) a worktree
        if started.get(project, float("inf")) <= made:
            continue
        out.append(Reconnectable(project, found[1], str(wt), transcript_mtime(wt) or made))
    return sorted(out, key=lambda r: -r.last_active)


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


# --------------------------------------------------------------------------- CLI

def cmd_status(cfg: dict, _a) -> int:
    have = servers(cfg)
    if not have:
        print("no managed sessions running")
    for s in have:
        print(f"{s.tmux:40} {s.state:26} {s.capacity:8} {s.mode:9} {s.path}")
    return 0


def cmd_attach(cfg: dict, a) -> int:
    os.execvp("tmux", ["tmux", "-L", cfg["tmux_socket"], "attach", "-t", resolve(cfg, a.project)])


def cmd_restart(cfg: dict, a) -> int:
    print(restart_project(cfg, a.project))
    return 0


def cmd_reconnect(cfg: dict, a) -> int:
    if a.session_id:
        worktree, session_id = find_bridge_worktree(cfg, a.session_id), f"session_{short_id(a.session_id)}"
    else:
        found = bridge_worktree(Path.cwd())
        if not found:
            raise CrpmError("pass a session ID, or run this from inside a session worktree "
                            "(.claude/worktrees/bridge-cse_*)")
        worktree, session_id = found
    print(reconnect_session(cfg, worktree, session_id))
    return 0


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
RECONNECT_SHOW = 24 * 3600  # older stopped sessions are past Claude Code's ~4h reattach window anyway
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


def session_json(cfg: dict, s: LiveSession) -> dict:
    root = cfg["projects_dir"]
    rel = Path(*s.cwd.relative_to(root).parts[1:]) if root in s.cwd.parents else s.cwd  # within its project
    wt = bridge_worktree(s.cwd)
    parts = s.cwd.parts
    worktree = wt[0].name if wt else (parts[parts.index("worktrees") + 1]
                                      if ".claude" in parts and "worktrees" in parts[:-1] else "")
    return {"key": s.key, "session_id": s.session_id, "remote": bool(s.session_id),
            "url": f"https://claude.ai/code/{s.session_id}" if s.session_id else "",
            "name": s.name, "kind": s.kind, "status": s.status, "pid": s.pid,
            "folder": str(rel), "worktree": worktree, "tmux": s.tmux}


def api_status(cfg: dict, user: User, _body: dict) -> dict:
    root = cfg["projects_dir"]
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
    for s in live_sessions(include_local=True):
        top = s.cwd.relative_to(root).parts[0] if root in s.cwd.parents else None
        (projects[top]["sessions"] if top in projects else other).append(session_json(cfg, s))
    for r in reconnect_candidates(cfg):
        if r.project in projects and time.time() - r.last_active < RECONNECT_SHOW:
            projects[r.project]["reconnectable"].append(asdict(r))
    return {"user": user.name, "host": socket.gethostname(), "time": time.time(),
            "projects": list(projects.values()), "other_sessions": other}


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
    sid = f"session_{short_id(session_id)}"
    return {"message": reconnect_session(cfg, find_bridge_worktree(cfg, sid), sid)}


WEB_ROUTES = [
    ("GET", r"/api/status", api_status),
    ("GET", r"/api/projects/([^/]+)/screen", api_screen),
    ("POST", r"/api/projects/([^/]+)/restart", api_restart_project),
    ("POST", r"/api/projects/([^/]+)/trust", api_trust),
    ("POST", r"/api/sessions", api_new_session),
    ("POST", r"/api/sessions/([^/]+)/restart", api_restart_session),
    ("POST", r"/api/sessions/([^/]+)/reconnect", api_reconnect),
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
    rc = sub.add_parser("reconnect", help="serve a spawned session again after its server stopped")
    rc.add_argument("session_id", nargs="?", metavar="SESSION_ID",
                    help="session_..., cse_..., or the bare ID (default: the session worktree you're in)")
    rs = sub.add_parser("restart-session", help="restart idle sessions so they load new config")
    rs.add_argument("ids", nargs="*", metavar="SESSION_ID",
                    help="session_..., cse_..., or the bare ID (default: sessions in the current folder)")
    rs.add_argument("--project", help="every running session in this project")
    rs.add_argument("--all", action="store_true", help="every session in the current folder")
    rs.add_argument("--force", action="store_true", help="also restart sessions that are not idle")
    rs.add_argument("--dry-run", action="store_true", help="only show what would be restarted")
    rs.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
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
                "new-session": cmd_new_session, "stop-all": cmd_stop_all,
                "web": cmd_web, "web-token": cmd_web_token}[a.cmd](cfg, a)
    except CrpmError as e:
        print(f"crpm {a.cmd}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
