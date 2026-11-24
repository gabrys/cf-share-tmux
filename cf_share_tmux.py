#!/usr/bin/env python3
"""cf-share-tmux: share a tmux window in the browser with ttyd, optionally
through a Cloudflare quick tunnel.

Linux only. Python standard library only. External executables: tmux, ttyd,
and cloudflared for --cf-tunnel.
"""

import argparse
import collections
import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

__version__ = "0.1.0"

# Skip api.trycloudflare.com, which cloudflared mentions in its error messages.
TUNNEL_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
TTYD_STARTUP = 1.0  # without a tunnel: ready once ttyd has survived this long
SESSION_CHECK_EVERY = 2.0  # how often to check that the shared window still exists
PR_SET_PDEATHSIG = 1


def tmux(*args):
    return subprocess.check_output(
        ["tmux", *args],
        text=True,
    ).strip()


def parse_args():
    parser = argparse.ArgumentParser(
        prog="cf-share-tmux",
        description="Expose a tmux window over HTTP using ttyd. Runs in the "
                    "background once it is ready; stop it with kill <PID>.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + __version__,
    )

    parser.add_argument(
        "target",
        help="Tmux window target, e.g. mysession:1",
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=7681,
        help="HTTP port",
    )
    parser.add_argument(
        "-b", "--bind",
        default="127.0.0.1",
        help="Bind address",
    )
    parser.add_argument(
        "-c", "--credential",
        metavar="USER:PASSWORD",
        default=None,
        help="HTTP Basic Auth credentials (or TTYD_CREDENTIAL env)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Disable terminal input",
    )
    parser.add_argument(
        "--check-origin",
        action="store_true",
        help="Enable WebSocket origin checking",
    )
    parser.add_argument(
        "--cf-tunnel",
        action="store_true",
        help="Expose terminal via Cloudflare Quick Tunnel",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help='Print only {"url": "<Cloudflare URL>", "pid": <PID>} on stdout '
             "(requires --cf-tunnel; errors still go to stderr)",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Stay in the foreground until Ctrl+C instead of going to the background",
    )

    args = parser.parse_args()

    args.credential = (
        args.credential or os.environ.get("TTYD_CREDENTIAL")
    )

    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")

    if args.credential:
        user, sep, password = args.credential.partition(":")
        if not sep or not user or not password:
            parser.error("Credentials must use USER:PASSWORD format")

    if args.cf_tunnel and not args.credential:
        parser.error("--cf-tunnel requires Basic Auth credentials")

    if args.json and not args.cf_tunnel:
        parser.error("--json requires --cf-tunnel")

    return args


def require_binary(binary):
    if not shutil.which(binary):
        sys.exit(f"Missing executable: {binary}")


def stop_process(process):
    if process is None or process.poll() is not None:
        return

    process.terminate()

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def daemonize():
    """Fork into the background; the parent exits once the child is ready.

    The parent waits on a pipe. The child writes to it when it is ready (see
    Handoff); then the parent exits with 0. If the child exits first, the
    parent exits with the child's status. Everything the child prints before
    it is ready still reaches the terminal. Ctrl+C in the parent stops the
    child. Returns the pipe's write end in the child.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    read_fd, write_fd = os.pipe()  # not inherited by ttyd/cloudflared (PEP 446)
    pid = os.fork()

    if pid == 0:
        os.close(read_fd)
        os.setsid()  # leave the terminal's session: no SIGINT/SIGHUP from it
        return write_fd

    os.close(write_fd)

    try:
        ready = os.read(read_fd, 1)
    except KeyboardInterrupt:
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
        os._exit(130)

    if ready:
        os._exit(0)

    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    os._exit(code if code >= 0 else 128 - code)


class Handoff:
    """Tells the waiting parent that the background child is ready.

    On ready() the child points stdin/stdout/stderr at /dev/null, because the
    terminal now belongs to the shell again, and releases the parent.
    Without a parent (--foreground) ready() does nothing.
    """

    def __init__(self, fd):
        self.fd = fd
        self.lock = threading.Lock()

    def ready(self):
        with self.lock:
            if self.fd is None:
                return

            sys.stdout.flush()
            sys.stderr.flush()
            devnull = os.open(os.devnull, os.O_RDWR)

            for fd in (0, 1, 2):
                os.dup2(devnull, fd)

            os.close(devnull)
            os.write(self.fd, b"1")
            os.close(self.fd)
            self.fd = None


def die_with_parent():
    """preexec_fn: the child gets SIGTERM when this process dies, even on SIGKILL."""
    libc = ctypes.CDLL(None, use_errno=True)
    parent = os.getpid()

    def preexec():
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM) != 0 or os.getppid() != parent:
            os._exit(1)  # could not arm the signal, or the parent is already gone

    return preexec


def relay(stream):
    """Copy a child's output to our stderr (which is /dev/null once in the background)."""
    for line in stream:
        sys.stderr.write(line)
        sys.stderr.flush()


def watch_tunnel(process, on_url):
    """Find the public URL in cloudflared's log; keep draining it afterwards.

    Calls on_url(url) once. The log itself is hidden; its last lines go to
    stderr only if cloudflared exits without ever reporting a URL.
    """
    tail = collections.deque(maxlen=20)
    found = False

    for line in process.stderr:
        if found:
            continue  # keep reading so cloudflared never blocks on a full pipe

        tail.append(line)
        match = TUNNEL_URL.search(line)

        if match:
            found = True
            on_url(match.group(0))

    if not found:
        sys.stderr.write("".join(tail))


def http_url(host, port):
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    return f"http://{host}:{port}"


def main():
    args = parse_args()

    # Informational stdout output; silenced with --json so stdout holds only the JSON.
    info = (lambda *a, **k: None) if args.json else print

    require_binary("tmux")
    require_binary("ttyd")

    if args.cf_tunnel:
        require_binary("cloudflared")

    # Fork before any thread or child process exists.
    handoff = Handoff(None if args.foreground else daemonize())

    session = f"serve-tmux-{args.port}-{uuid.uuid4().hex[:8]}"

    ttyd_process = None
    cf_process = None
    cf_watcher = None
    created = False
    stop_signal = None

    def request_stop(signum, frame):
        nonlocal stop_signal
        stop_signal = signum

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, request_stop)

    def on_url(url):
        if args.json:
            print(json.dumps({"url": url, "pid": os.getpid()}), flush=True)
        else:
            print(f"Public:  {url}", flush=True)
        handoff.ready()

    try:
        # Resolve the target to a stable window ID.
        window = tmux(
            "display-message",
            "-p",
            "-t", args.target,
            "#{window_id}",
        )

        if not window.startswith("@"):
            sys.exit(f"Cannot resolve window: {args.target}")

        # Create an independent temporary session.
        tmux(
            "new-session",
            "-d",
            "-s", session,
            "-n", "placeholder",
        )
        created = True

        placeholder = tmux(
            "display-message",
            "-p",
            "-t", f"{session}:",
            "#{window_id}",
        )

        # Link the original window into the temporary session.
        tmux(
            "link-window",
            "-s", window,
            "-t", f"{session}:",
        )

        tmux("kill-window", "-t", placeholder)

        # Build ttyd command.
        command = [
            "ttyd",
            "-i", args.bind,
            "-p", str(args.port),
        ]

        if not args.read_only:
            command.append("-W")

        if args.credential:
            command.extend(["-c", args.credential])

        if args.check_origin:
            command.append("-O")

        command.extend([
            "tmux",
            "attach-session",
            "-t", f"={session}",
        ])

        url = http_url(args.bind, args.port)

        info(f"Window:  {args.target} ({window})", flush=True)
        info(f"Session: {session}", flush=True)
        info(f"Local:   {url}", flush=True)
        info(
            f"Auth:    {'enabled' if args.credential else 'disabled'}",
            flush=True,
        )
        info(f"PID:     {os.getpid()} (stop with: kill {os.getpid()})", flush=True)

        if stop_signal is not None:
            return 128 + stop_signal

        # Start both children before any thread: preexec_fn is unsafe with threads.
        ttyd_process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
            preexec_fn=die_with_parent(),
        )
        ttyd_started = time.monotonic()

        # Optionally start Cloudflare Quick Tunnel.
        if args.cf_tunnel:
            origin = args.bind

            if origin == "0.0.0.0":
                origin = "127.0.0.1"
            elif origin == "::":
                origin = "::1"

            origin_url = http_url(origin, args.port)

            info("Starting Cloudflare Quick Tunnel...", flush=True)

            cf_process = subprocess.Popen(
                [
                    "cloudflared",
                    "tunnel",
                    "--no-autoupdate",
                    "--url", origin_url,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
                preexec_fn=die_with_parent(),
            )
            cf_watcher = threading.Thread(
                target=watch_tunnel,
                args=(cf_process, on_url),
                daemon=True,
            )

        threading.Thread(target=relay, args=(ttyd_process.stdout,), daemon=True).start()

        if cf_watcher is not None:
            cf_watcher.start()

        # Supervise both processes and the shared window.
        next_session_check = time.monotonic() + SESSION_CHECK_EVERY

        while stop_signal is None:
            rc = ttyd_process.poll()

            if rc is not None:
                print(
                    f"ttyd exited with status {rc}",
                    file=sys.stderr,
                )
                return rc

            if cf_process is not None:
                rc = cf_process.poll()

                if rc is not None:
                    cf_watcher.join(timeout=2)  # let it print the log tail
                    print(
                        f"cloudflared exited with status {rc}",
                        file=sys.stderr,
                    )
                    return rc or 1
            elif time.monotonic() - ttyd_started >= TTYD_STARTUP:
                handoff.ready()

            if time.monotonic() >= next_session_check:
                next_session_check = time.monotonic() + SESSION_CHECK_EVERY
                exists = subprocess.run(
                    ["tmux", "has-session", "-t", f"={session}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode == 0

                if not exists:
                    # tmux removes the session when its only window is closed.
                    info("The shared window was closed.", flush=True)
                    return 0

            time.sleep(0.25)

        return 128 + stop_signal

    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    finally:
        info("Shutting down...", flush=True)

        stop_process(cf_process)
        stop_process(ttyd_process)

        if created:
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={session}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


if __name__ == "__main__":
    sys.exit(main())
