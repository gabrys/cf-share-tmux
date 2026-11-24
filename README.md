# cf-share-tmux

Share one tmux window in the browser with [ttyd](https://github.com/tsl0922/ttyd),
locally or publicly through a Cloudflare quick tunnel.

```sh
cf-share-tmux work:2 --cf-tunnel -c 'alice:long-random-password'
```

prints the local and the public `https://….trycloudflare.com` URL and the PID,
then returns to the shell while sharing continues in the background. Stop it
with `kill <PID>`; it also stops by itself when you close the shared window.

## Requirements

Linux, Python 3.10 or newer (standard library only) and these executables on
your `PATH`:

- `tmux`
- [`ttyd`](https://github.com/tsl0922/ttyd)
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/),
  only for `--cf-tunnel`

Installing this package does not install any of them.

## Installation

```sh
uv tool install cf-share-tmux
```

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).
`uvx cf-share-tmux --help` runs it without installing. You can also run the
single file directly: `python3 cf_share_tmux.py --help`.

## Usage

```sh
cf-share-tmux TARGET [options]
```

`TARGET` is a tmux window, for example `work:2` or `work:editor`.

| Option | Default | Meaning |
|---|---|---|
| `-p`, `--port` | `7681` | Local HTTP port for ttyd |
| `-b`, `--bind` | `127.0.0.1` | Address ttyd listens on |
| `--base-path PATH` | none | Serve under a URL path, e.g. `/terminal` (passed as `ttyd -b`) |
| `-c`, `--credential USER:PASSWORD` | none | HTTP Basic Auth; also read from `TTYD_CREDENTIAL` |
| `--read-only` | off | Viewers cannot type |
| `--check-origin` | off | Make ttyd reject WebSocket connections from other origins |
| `--cf-tunnel` | off | Also publish through a Cloudflare quick tunnel; authentication is optional |
| `--json` | off | Print only `{"url": "<public URL>", "pid": <PID>}` on stdout; needs `--cf-tunnel` |
| `--foreground` | off | Stay in the foreground until Ctrl+C instead of going to the background |
| `--version` | | Print the version |

Without `--json`, stdout shows the window, the temporary session, the local
URL, whether auth is enabled, the PID and, with `--cf-tunnel`,
`Public:  <URL>`. With `--json`, stdout gets that single JSON line and nothing
else; errors still go to stderr.

For example, `cf-share-tmux work:2 --base-path /terminal` serves the terminal
at `http://127.0.0.1:7681/terminal`. The path must start with `/`; trailing
slashes are removed. Local and public URLs (including `--json`) include the
path. The `-b` option still selects the bind address.

The command returns as soon as the share is ready: with `--cf-tunnel` when the
public URL is known, without it once ttyd has been running for a second. Its
exit status is 0 then, or the error status if something failed before that, so
scripts can simply do:

```sh
url=$(cf-share-tmux work:2 --cf-tunnel --json --read-only -c "$CRED" | jq -r .url)
```

## How it works

1. The target is resolved to a stable window ID.
2. Unless `--foreground` is given, the process forks: the child detaches from
   the terminal and does the work, the parent waits until the child reports
   that it is ready and then exits.
3. A temporary session `serve-tmux-<port>-<random>` is created and the window
   is linked into it, so the browser starts on that window only.
4. ttyd serves `tmux attach-session` to that session.
5. With `--cf-tunnel`, cloudflared publishes ttyd. Its log is hidden; the
   public URL is taken from it. If cloudflared exits without a URL, the last
   20 lines of its log go to stderr.
6. Once in the background, the process no longer writes anywhere; later errors
   are not shown.
7. It stops on SIGTERM (`kill <PID>`), on Ctrl+C or SIGHUP in the foreground,
   when ttyd or cloudflared exits, or when the shared window is closed. Then
   ttyd and cloudflared are stopped and the temporary session is removed. The
   window itself stays in its original session.
8. ttyd and cloudflared are started with the Linux parent-death signal, so they
   are terminated even if the process is killed with `kill -9`; only the
   temporary tmux session is left behind in that case.

## Security

Read this before sharing anything.

- **Input is enabled by default.** Anyone who can open the page can type into
  your shell, with your user's permissions. Use `--read-only` unless you mean
  to hand over a shell.
- **The temporary session is not a sandbox.** With input enabled, a viewer can
  use tmux key bindings to switch to any of your other sessions and windows.
- **Use `--check-origin`.** Without it, any web page open in your browser can
  connect to the local ttyd WebSocket; browsers also send saved Basic Auth
  credentials with such connections.
- **The password is visible to local users.** It is passed to ttyd on the
  command line, so `ps` shows it, even when it comes from `TTYD_CREDENTIAL`.
- **Authentication is optional, including through the tunnel.** Without `-c`
  or `TTYD_CREDENTIAL`, anyone with the public URL can access the terminal.
  Set a long random password to require authentication.
- Without `--cf-tunnel`, `-b 0.0.0.0` without `-c` exposes your shell to the
  whole local network over plain HTTP.

## Development

```sh
uv run python -m unittest discover -s tests -v
uvx --from build pyproject-build
uvx twine check --strict dist/*
```

The tests need `tmux` for the end-to-end part (skipped otherwise). They run
their own tmux server and fake `ttyd` and `cloudflared`, so they do not touch
your sessions or the network.

## Publishing

The version lives in `__version__` in `cf_share_tmux.py` (currently `0.1.1`).
Build from a clean checkout using the commands above, ensuring `dist/` contains
only the current release, then upload:

```sh
uvx twine upload dist/*
```

Use an API token when Twine prompts for credentials. Never commit credentials.
For subsequent releases, bump `__version__` and build fresh artifacts; PyPI does
not allow replacing an existing release file. GitHub Actions runs tests and
validates distributions; publishing is manual.

## License

MIT; see [LICENSE](LICENSE).
