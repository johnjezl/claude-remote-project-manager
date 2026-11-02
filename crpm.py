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
  stop-all          shut down every managed remote-control server
"""
from __future__ import annotations

import argparse
import fnmatch
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
    "auto_trust": True,          # answer the first-run "Trust <folder>?" prompt with y
    "permission_mode": "",       # e.g. "acceptEdits"; empty = claude default
    "capacity": 0,               # 0 = claude default (32)
    "create_session_in_dir": True,
    "name_prefix": "",           # prefix for auto-generated session names; empty = claude default (hostname)
    "extra_args": [],
    "exclude": [".*", "_*"],     # glob patterns of folder names to ignore
    "restart_backoff": [5, 15, 60, 300],
    "stop_timeout": 10,
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

    def sessions(self, prefix: str) -> dict[str, dict]:
        """Managed sessions -> {name: {path, mode}}."""
        fmt = "#{session_name}\t#{@crpm_path}\t#{@crpm_mode}"
        r = self.run("list-sessions", "-F", fmt)
        out = {}
        if r.returncode != 0:  # no server running
            return out
        for line in r.stdout.splitlines():
            name, path, mode = (line.split("\t") + ["", ""])[:3]
            if name.startswith(prefix):
                out[name] = {"path": path, "mode": mode}
        return out

    def new_session(self, name: str, cwd: str, command: str, opts: dict[str, str]) -> bool:
        r = self.run("new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50", command)
        if r.returncode != 0:
            log.error("tmux new-session %s failed: %s", name, r.stderr.strip())
            return False
        for k, v in opts.items():
            self.run("set-option", "-t", name, k, v)
        return True

    def capture(self, name: str) -> str:
        return self.run("capture-pane", "-p", "-J", "-t", name).stdout

    def send(self, name: str, *keys: str) -> None:
        self.run("send-keys", "-t", name, *keys)

    def kill(self, name: str) -> None:
        self.run("kill-session", "-t", name)

    def has(self, name: str) -> bool:
        return self.run("has-session", "-t", f"={name}").returncode == 0


def session_name(prefix: str, project: str) -> str:
    # tmux forbids '.' and ':' in session names
    return prefix + re.sub(r"[^A-Za-z0-9_-]", "_", project)


# --------------------------------------------------------------------------- daemon

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
    trust_pending: dict[str, float] = field(default_factory=dict)
    stopping: bool = False

    def __post_init__(self):
        self.tmux = Tmux(self.cfg["tmux_socket"])

    # ---- discovery
    def desired(self) -> dict[str, tuple[Path, str]]:
        """{session_name: (project_path, spawn_mode)} for projects that should be registered."""
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
            self.first_seen.setdefault(p.name, now)
            if now - self.first_seen[p.name] < self.cfg["settle_seconds"]:
                continue
            if (p / ".git").exists():
                mode = self.cfg["spawn_mode"]
            elif self.cfg["non_git"] == "skip":
                continue
            else:
                mode = self.cfg["non_git"]
            want[session_name(self.cfg["session_prefix"], p.name)] = (p.resolve(), mode)
        for gone in set(self.first_seen) - seen:
            del self.first_seen[gone]
        return want

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
            argv += ["--remote-control-session-name-prefix", c["name_prefix"]]
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
                                   {"@crpm_path": str(path), "@crpm_mode": mode})
        bo.started_at = time.time()
        if ok and self.cfg["auto_trust"]:
            self.trust_pending[name] = time.time() + 60
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
            self.tmux.send(name, "C-c")
            deadline = time.time() + self.cfg["stop_timeout"]
            while time.time() < deadline and self.tmux.has(name):
                time.sleep(0.5)
            if self.tmux.has(name):
                self.tmux.kill(name)
        self.backoff.pop(name, None)
        self.trust_pending.pop(name, None)

    def handle_trust_prompts(self, running: dict[str, dict]) -> None:
        now = time.time()
        for name, deadline in list(self.trust_pending.items()):
            if name not in running or now > deadline:
                del self.trust_pending[name]
                continue
            screen = self.tmux.capture(name)
            path = running[name]["path"]
            if re.search(rf"Trust {re.escape(path)}\? \[y/N\]\s*$", screen, re.M):
                log.info("auto-trusting %s", path)
                self.tmux.send(name, "y", "Enter")
                del self.trust_pending[name]
            elif "Connected" in screen:
                del self.trust_pending[name]

    # ---- loop
    def reconcile(self) -> None:
        want = self.desired()
        have = self.tmux.sessions(self.cfg["session_prefix"])

        # sessions that vanished without us stopping them => crashed/exited
        for name, bo in self.backoff.items():
            if name in want and name not in have and bo.started_at and bo.next_try <= bo.started_at:
                self.note_failure(name)

        for name in sorted(set(have) - set(want)):
            self.stop(name)

        for name, (path, mode) in sorted(want.items()):
            cur = have.get(name)
            if cur and (cur["path"] != str(path) or cur["mode"] != mode):
                log.info("%s changed (%s -> %s); restarting", name, cur, (path, mode))
                self.stop(name)
                cur = None
            if cur is None:
                self.start(name, path, mode)
            else:
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
        if "Connected" in screen:
            state = "connected"
        elif re.search(r"\[y/N\]", screen):
            state = "WAITING FOR TRUST PROMPT"
        else:
            state = "starting/unknown"
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
            "stop-all": cmd_stop_all}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
