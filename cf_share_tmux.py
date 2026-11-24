#!/usr/bin/env python3

import argparse
import collections
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

# Skip api.trycloudflare.com, which cloudflared mentions in its error messages.
TUNNEL_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")


def tmux(*args):
    return subprocess.check_output(
        ["tmux", *args],
        text=True,
    ).strip()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Expose a tmux window over HTTP using ttyd.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
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
        help='Print only {"url": "<Cloudflare URL>"} on stdout '
             "(requires --cf-tunnel; errors still go to stderr)",
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


def watch_tunnel(process, as_json=False):
    """Find the public URL in cloudflared's log; keep draining it afterwards.

    The URL is printed as text, or as {"url": ...} with as_json. The log
    itself is hidden; its last lines go to stderr only if cloudflared exits
    without ever reporting a URL.
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
            url = match.group(0)
            print(json.dumps({"url": url}) if as_json else f"Public:  {url}", flush=True)

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

        if stop_signal is not None:
            return 128 + stop_signal

        ttyd_process = subprocess.Popen(
            command,
            # With --json keep stdout clean: anything ttyd prints goes to stderr.
            stdout=sys.stderr if args.json else None,
            start_new_session=True,
        )

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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
            )
            cf_watcher = threading.Thread(
                target=watch_tunnel,
                args=(cf_process, args.json),
                daemon=True,
            )
            cf_watcher.start()

        # Supervise both processes.
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

