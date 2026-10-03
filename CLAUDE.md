# CLAUDE.md

Guidance for AI coding sessions working in this repository.

## Git identity (mandatory)

Every commit in this repository must be authored **and** committed by the
project owner. Before doing any other work in a fresh clone or session:

```sh
git config user.name "rafay-ah"
git config user.email "54492363+rafay-ah@users.noreply.github.com"
```

Rules:

- Commits must be authored and committed as
  `rafay-ah <54492363+rafay-ah@users.noreply.github.com>`.
- Never add `Co-Authored-By:` trailers, `Claude-Session:` trailers, or
  "Generated with Claude Code" lines to commit messages or pull requests.
  (`.claude/settings.json` disables Claude Code's automatic attribution.)
- Work directly on the `main` branch unless told otherwise.
- Commit often, with clear, descriptive messages.
- Before **every** push, verify authorship:

  ```sh
  git log --format='%an <%ae> | %cn <%ce>'
  ```

  Every line must read
  `rafay-ah <54492363+rafay-ah@users.noreply.github.com> | rafay-ah <54492363+rafay-ah@users.noreply.github.com>`.
  Fix any commit that isn't, e.g. `git commit --amend --reset-author --no-edit`
  for the last commit, or
  `git rebase -r <base> --exec 'git commit --amend --reset-author --no-edit'`
  for older ones. Also strip any attribution trailers while doing so.

## Project overview

QuotaGlance is a GNOME app (Python, GTK 4, libadwaita) that shows AI tool usage
limits: a GNOME Shell extension and tray icon in the top bar, a desktop widget,
a main window and notifications.

- `src/quotaglance/providers/`: one module per provider. Parsing lives in pure
  `parse_*` functions; `fetch()` does I/O through `FetchContext` (`ctx.http`,
  `ctx.run`, `ctx.secret`, `ctx.secrets.search`).
- `src/quotaglance/engine.py`: scheduling, threading, backoff, cache.
- `src/quotaglance/presenter.py`: view models shared by every UI surface.
- `src/quotaglance/gui/`: the GTK app, D-Bus API, tray (StatusNotifierItem).
- `extension/`: the GNOME Shell extension (GNOME 45+, ESM). It only talks to
  the app over D-Bus.
- `packaging/`: `.deb` and AppImage builds; `.github/workflows/` runs CI and
  publishes releases for `v*` tags.

## Commands

```sh
python3 -m pytest                      # unit tests (no GTK needed)
ruff check src tests scripts           # lint
PYTHONPATH=src python3 -m quotaglance --demo     # run the app with mock data
python3 scripts/capture.py --out /tmp/shots      # render UI to PNG (needs a display)
scripts/shell-capture/run-shell.sh /tmp/shell dark extension   # headless GNOME Shell
scripts/make-media.sh                  # regenerate docs/screenshots
python3 scripts/provider_table.py      # refresh the README provider table
```

## Provider rules

- Read other apps' files and databases read-only (use `sqlite_ro.connect_readonly`).
- Never refresh or rewrite tokens whose refresh rotates; tell the user to open
  the tool instead. In-memory refresh only where rotation cannot happen.
- Prefer local data, then the tool's CLI, then its usage endpoint, then API keys
  (stored in GNOME Keyring). No browser-cookie scraping.
- Reuse keys other tools already hold through `providers/keysources.py`
  (OpenCode's `auth.json`, Claude Code's `settings.json`) instead of copying
  that code into a provider.
- Pass `follow_redirects=False` when a token must never reach another host;
  `net.Http` already drops credentials on cross-host redirects.
- Hints and messages are plain text; wrap commands in backticks and the UI shows
  them in monospace.
- Window labels stay short (12 characters or fewer). Every new provider needs
  fixtures and tests in `tests/`.
