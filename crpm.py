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
  stop-all          shut down every managed remote-control server
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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

    FIELDS = ("path", "mode", "cmd", "dead", "status", "signal", "retained", "pid")

    def sessions(self, prefix: str) -> dict[str, dict]:
        """Managed sessions -> {name: {path, mode, cmd, dead, status, signal, retained, pid}}."""
        fmt = "\t".join(["#{session_name}", "#{@crpm_path}", "#{@crpm_mode}", "#{@crpm_cmd}",
                         "#{pane_dead}", "#{pane_dead_status}", "#{pane_dead_signal}",
                         "#{remain-on-exit}", "#{pane_pid}"])
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
            if any(fnmatch.fnmatch(p.name, pat) for pat in self.cfg["exclude"]):
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


# --------------------------------------------------------------------------- CLI

def resolve(cfg: dict, project: str) -> str:
    return project if project.startswith(cfg["session_prefix"]) else session_name(cfg["session_prefix"], project)


def cmd_status(cfg: dict, _a) -> int:
    t = Tmux(cfg["tmux_socket"])
    have = t.sessions(cfg["session_prefix"])
    if not have:
        print("no managed sessions running")
        return 0
    for name, info in sorted(have.items()):
        screen = t.capture(name)
        if info["dead"]:
            state = f"exited ({'signal ' + info['signal'] if info['signal'] else 'status ' + info['status']})"
        else:
            state = screen_state(screen)
        cap = re.search(r"Capacity: (\d+/\d+)", screen)
        print(f"{name:40} {state:26} {cap.group(1) if cap else '':8} {info['mode']:9} {info['path']}")
    return 0


def cmd_attach(cfg: dict, a) -> int:
    os.execvp("tmux", ["tmux", "-L", cfg["tmux_socket"], "attach", "-t", resolve(cfg, a.project)])


def cmd_restart(cfg: dict, a) -> int:
    m = Manager(cfg)
    m.stop(resolve(cfg, a.project))
    print("stopped; the daemon will start it again within a few seconds")
    return 0


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


def serving_pid(session_id: str) -> int | None:
    """PID of a local process already serving this session, if any."""
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            argv = (proc / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        if "remote-control" in argv and "--session-id" in argv and session_id in argv:
            return int(proc.name)
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    for record in (config_dir / "sessions").glob("*.json"):
        try:
            data = json.loads(record.read_text())
            if data.get("bridgeSessionId") == session_id and Path(f"/proc/{int(data['pid'])}").exists():
                return int(data["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def launch_detached(cfg: dict, name: str, cwd: Path, cmd: str, what: str, hint: str = "") -> int:
    """Run `cmd` in a new reconnect- tmux session (which the daemon ignores) and check it stays up."""
    t = Tmux(cfg["tmux_socket"])
    attach = f"tmux -L {cfg['tmux_socket']} attach -t {name}"
    if t.has(name):
        if not t.is_dead(name):
            print(f"{what} is already running in tmux: {attach}")
            return 0
        t.kill(name)  # dead pane left by an older crpm
    if not t.new_session(name, str(cwd), cmd, {}, retain=False):
        return 1
    time.sleep(RECONNECT_SETTLE)
    if not t.has(name):
        print(f"crpm: claude exited right away. To see why, run this in {cwd}:\n  {cmd}" +
              (f"\n{hint}" if hint else ""), file=sys.stderr)
        return 1
    print(f"started {what}\n  watch: {attach}  (detach: Ctrl-b d)")
    return 0


def reconnect_name(project: str, session_id: str) -> str:
    return session_name(RECONNECT_PREFIX, f"{project}-{session_id.removeprefix('session_')}")


def cmd_reconnect(cfg: dict, _a) -> int:
    found = bridge_worktree(Path.cwd())
    if not found:
        print("crpm reconnect: run this from inside a session worktree (.claude/worktrees/bridge-cse_*)",
              file=sys.stderr)
        return 1
    worktree, session_id = found
    name = reconnect_name(worktree.parents[2].name, session_id)
    t = Tmux(cfg["tmux_socket"])
    if not (t.has(name) and not t.is_dead(name)):
        pid = serving_pid(session_id)
        if pid:
            print(f"crpm reconnect: {session_id} is already being served by PID {pid}. "
                  "Stop that process first if it is stuck.", file=sys.stderr)
            return 1
    cmd = shlex.join([cfg["claude_bin"], "remote-control", "--session-id", session_id])
    return launch_detached(cfg, name, worktree, cmd, f"remote-control for {session_id}",
                           "Claude Code can only reattach for about 4 hours after the session's server stopped.")


# --------------------------------------------------------------------------- restart-session

RESTART_GAP = 2.0  # seconds between the old process exiting and the new one starting


@dataclass
class LiveSession:
    pid: int
    session_id: str     # session_... (claude.ai)
    conversation: str   # local transcript ID, for --resume
    cwd: Path
    status: str         # "idle" | "busy"
    parent_pid: int
    parent_argv: list[str]

    @property
    def kind(self) -> str:
        """spawned: created by a project's `remote-control` server, which serves it again on the next message.
        reattached: served by `remote-control --session-id/--continue`, which exits with it.
        terminal: an interactive `claude --remote-control` (or /remote-control) session."""
        if "remote-control" in self.parent_argv:
            if {"--session-id", "--continue", "-c"} & set(self.parent_argv):
                return "reattached"
            return "spawned"
        return "terminal"


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


def live_sessions() -> list[LiveSession]:
    """Running Remote Control sessions, from Claude Code's per-process session records."""
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    out = []
    for record in sorted((config_dir / "sessions").glob("*.json")):
        try:
            d = json.loads(record.read_text())
            pid = int(d["pid"])
            sid, conv, cwd = d["bridgeSessionId"], d["sessionId"], Path(d["cwd"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not sid or proc_start(pid) != str(d.get("procStart")):
            continue  # not a Remote Control session, or the process is gone (or its PID was reused)
        ppid = proc_ppid(pid)
        out.append(LiveSession(pid, sid, conv, cwd, d.get("status", "?"), ppid, proc_argv(ppid)))
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


def restart_session(cfg: dict, s: LiveSession) -> int:
    short = s.session_id.removeprefix("session_")
    if s.kind == "spawned":
        if not terminate(s.pid):
            print(f"  could not stop PID {s.pid}", file=sys.stderr)
            return 1
        print(f"  stopped. Send it a message from the app and its server starts it again with the new config.")
        return 0

    if s.kind == "reattached":
        server = s.parent_pid
        if not terminate(s.pid) or (proc_start(server) and not terminate(server)):
            print(f"  could not stop PID {s.pid} / its server {server}", file=sys.stderr)
            return 1
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
        print(f"  could not stop PID {s.pid}", file=sys.stderr)
        return 1
    time.sleep(RESTART_GAP)
    if pane:
        r = subprocess.run([*tm, "respawn-pane", "-t", pane_id, "-c", str(s.cwd), cmd],
                           capture_output=True, text=True)
        subprocess.run([*tm, "set-option", "-p", "-u", "-t", pane_id, "remain-on-exit"], capture_output=True)
        if r.returncode != 0:
            print(f"  respawn failed: {r.stderr.strip()}. Run in {s.cwd}:\n  {cmd}", file=sys.stderr)
            return 1
        time.sleep(RECONNECT_SETTLE)
        dead = subprocess.run([*tm, "display-message", "-p", "-t", pane_id, "#{pane_dead}"],
                              capture_output=True, text=True).stdout.strip()
        if dead != "0":
            print(f"  claude exited right away. To see why, run in {s.cwd}:\n  {cmd}", file=sys.stderr)
            return 1
        name = subprocess.run([*tm, "display-message", "-p", "-t", pane_id, "#{session_name}"],
                              capture_output=True, text=True).stdout.strip()
        print(f"  restarted in the same tmux pane: tmux -S {sock} attach -t {name}")
        return 0
    return launch_detached(cfg, reconnect_name(s.cwd.name, short), s.cwd, cmd, f"{s.session_id}")


def cmd_restart_session(cfg: dict, a) -> int:
    sessions = live_sessions()
    if a.ids:
        wanted = {i.removeprefix("session_").removeprefix("cse_") for i in a.ids}
        chosen = [s for s in sessions if s.session_id.removeprefix("session_") in wanted]
        missing = wanted - {s.session_id.removeprefix("session_") for s in chosen}
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
        print("crpm restart-session: no running Remote Control sessions match "
              "(pass a session ID, --project NAME, or run it from a session's folder)", file=sys.stderr)
        return 1
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
        rc |= restart_session(cfg, s)
    return rc


def cmd_stop_all(cfg: dict, _a) -> int:
    m = Manager(cfg)
    for name in m.tmux.sessions(cfg["session_prefix"]):
        m.stop(name)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="crpm", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon")
    sub.add_parser("status")
    sub.add_parser("stop-all")
    sub.add_parser("reconnect")
    rs = sub.add_parser("restart-session", help="restart idle sessions so they load new config")
    rs.add_argument("ids", nargs="*", metavar="SESSION_ID",
                    help="session_..., cse_..., or the bare ID (default: sessions in the current folder)")
    rs.add_argument("--project", help="every running session in this project")
    rs.add_argument("--all", action="store_true", help="every session in the current folder")
    rs.add_argument("--force", action="store_true", help="also restart sessions that are not idle")
    rs.add_argument("--dry-run", action="store_true", help="only show what would be restarted")
    rs.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
    for n in ("attach", "restart"):
        sp = sub.add_parser(n)
        sp.add_argument("project")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(message)s" if os.environ.get("INVOCATION_ID")
                        else "%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    if a.cmd == "daemon":
        Manager(cfg).run()
        return 0
    return {"status": cmd_status, "attach": cmd_attach, "restart": cmd_restart,
            "reconnect": cmd_reconnect, "restart-session": cmd_restart_session,
            "stop-all": cmd_stop_all}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
