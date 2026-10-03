<p align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.rafay_ah.QuotaGlance.svg" width="96" alt="QuotaGlance icon">
</p>

<h1 align="center">QuotaGlance</h1>

<p align="center">
  <b>Your AI coding quotas at a glance, natively on GNOME.</b><br>
  Session, weekly and monthly limits for Claude Code, Codex, Cursor, Copilot, Gemini CLI, Kiro,
  ElevenLabs, OpenCode and more, with reset countdowns, in your top bar and on your desktop.
</p>

<p align="center">
  <a href="https://github.com/rafay-ah/quotaglance/actions/workflows/ci.yml"><img src="https://github.com/rafay-ah/quotaglance/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/rafay-ah/quotaglance/releases/latest"><img src="https://img.shields.io/github/v/release/rafay-ah/quotaglance?color=3584e4" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/GNOME-45%E2%80%9351-4a86cf?logo=gnome&logoColor=white" alt="GNOME 45 to 51">
  <img src="https://img.shields.io/badge/GTK-4%20%2B%20libadwaita-5e5c64" alt="GTK 4 and libadwaita">
  <img src="https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white" alt="Python 3.10+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ec27e" alt="MIT license"></a>
</p>

<p align="center">
  <img src="docs/screenshots/hero.png" alt="QuotaGlance on GNOME: the top-bar popup, the desktop widget and the main window" width="100%">
</p>

You use five AI tools a day and each one has its own limits: a 5-hour session window here, a
weekly cap there, monthly credits somewhere else. You find out you hit one in the middle of a
task. **QuotaGlance** puts all of them in one place, so you can see what's left and when it
resets before you start the long refactor.

## Highlights

- **Top-bar meter.** A ring in the top bar shows your busiest quota at a glance. Click it for a
  popup with every provider's session, weekly and monthly usage and reset countdowns.
- **Desktop widget.** A small, medium or large card in the spirit of macOS widgets, optionally
  tinted with your busiest provider's colour. Pin it on top of other windows, on every
  workspace.
- **Heads-up notifications** at 80% and 95% (each can be turned off), plus optional "limit reset"
  notices. Every alert fires once per window, even across restarts.
- **Starts on login**, quietly in the background, with a switch to turn that off.
- **Private by design.** QuotaGlance reads the sign-ins your tools already keep on this machine,
  read-only, and talks directly to each provider. API keys you add live in GNOME Keyring. It
  never asks for passwords, and there is no QuotaGlance server and no telemetry.
- **Native and light.** Python, GTK 4 and libadwaita: no Electron, no web views. It follows your
  light/dark style and accent colour and uses GNOME's own notifications and keyring.
- **Many providers**, each one toggleable. See [Supported providers](#supported-providers).

<p align="center">
  <img src="docs/screenshots/tour.gif" alt="A tour: widget sizes, the top-bar popup and the main window" width="88%">
</p>

## Screenshots

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/main-light.png" alt="Main window, light"></td>
    <td width="50%"><img src="docs/screenshots/main-dark.png" alt="Main window, dark"></td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/screenshots/widgets.png" alt="Desktop widget in small, medium and large sizes, plain and tinted"></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/screenshots/notification.png" alt="An 82% usage notification in GNOME Shell"><br>
      <sub>Notifications use GNOME's own notification system.</sub></td>
    <td width="50%"><img src="docs/screenshots/tray.png" alt="Tray icon menu on Ubuntu"><br>
      <sub>Without the extension, a tray icon works out of the box on Ubuntu.</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/screenshots/preferences-light.png" alt="Preferences: general settings"></td>
    <td width="50%"><img src="docs/screenshots/providers-dark.png" alt="Preferences: providers"></td>
  </tr>
</table>

All screenshots use the built-in [demo mode](#demo-mode) and real GNOME Shell 46.

## Install

QuotaGlance needs a GNOME desktop on **Ubuntu 24.04 or newer** (or any distribution with
GTK 4.12+, libadwaita 1.5+ and Python 3.10+). Download the latest build from the
[releases page](https://github.com/rafay-ah/quotaglance/releases/latest).

### Ubuntu and Debian (.deb)

```sh
sudo apt install ./quotaglance_*_all.deb
```

Then open **QuotaGlance** from the app grid. The package pulls in its dependencies
(`python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`, `gir1.2-secret-1`) and installs the GNOME Shell
extension system-wide.

### AppImage

```sh
chmod +x QuotaGlance-*-x86_64.AppImage
./QuotaGlance-*-x86_64.AppImage
```

The AppImage bundles Python, GTK 4 and libadwaita and runs on any distribution with glibc 2.39
or newer. On first run it adds itself to the app grid (`~/.local/share/applications`) so that
notifications, autostart and the top bar can find it. If your system lacks FUSE 2, run it with
`--appimage-extract-and-run`.

### From source

```sh
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-secret-1
git clone https://github.com/rafay-ah/quotaglance.git
cd quotaglance
PYTHONPATH=src python3 -m quotaglance
```

## The top bar

There are two ways to show QuotaGlance in the top bar, and the app picks the best one
automatically:

1. **The QuotaGlance GNOME Shell extension** (recommended). This gives you the rich popup shown
   above, and it is what pins the desktop widget. The .deb installs it. To turn it on, open
   **Preferences → Top Bar → Install/Enable**, or run
   `gnome-extensions enable quotaglance@rafay-ah.github.io`. On Wayland, GNOME only discovers
   newly installed extensions at login, so **log out and back in once**.
2. **A tray icon** (StatusNotifierItem/AppIndicator). Ubuntu ships the AppIndicator extension
   enabled, so this works with no setup. It shows the same meter with a text menu. KDE Plasma and
   other desktops with a system tray show it too.

When the extension is active the tray icon hides itself, so you never see two meters.

## Supported providers

Turn providers on and off in **Preferences → Providers**. On first launch QuotaGlance switches on
every provider it finds signed in on this computer.

<!-- providers:start -->
<!-- providers:end -->

How the data is read, in short:

- **Local files first.** For example the Codex session logs, Cursor's and Windsurf's state
  databases, JetBrains' quota file and OpenCode's history are read directly, and never written.
- **The tool's own CLI** where it reports usage, such as `kiro-cli /usage`, `amp usage` and
  `agy /usage`.
- **The usage endpoint the tool itself calls**, authenticated with the sign-in it already saved,
  for example Claude Code's OAuth token or the Copilot plugins' GitHub token.
- **An API key you paste into Preferences** for API platforms. It is stored in GNOME Keyring;
  the usual environment variables (such as `ELEVENLABS_API_KEY`) work as well.

Tokens are **never refreshed or rewritten** when a refresh could rotate them and sign you out of
the original tool. If a sign-in has expired, QuotaGlance tells you to open that tool once.

> [!NOTE]
> Several providers have no public quota API, so QuotaGlance uses the same endpoints as their
> official apps and CLIs. They can change without notice. If a provider breaks, please
> [open an issue](https://github.com/rafay-ah/quotaglance/issues).

## Privacy and security

- QuotaGlance has **no backend and no telemetry**. Requests go straight from your computer to
  each provider, with the same credentials the provider's own tool uses.
- Other tools' credential files and databases are opened **read-only**. SQLite databases are
  opened so that not even lock files are created next to them.
- API keys you enter are stored in **GNOME Keyring** (libsecret) as "QuotaGlance: <provider> API
  key" items. You can inspect or delete them in *Passwords and Keys* (Seahorse).
- Settings live in `~/.config/quotaglance/config.json`; the last fetched numbers are cached in
  `~/.cache/quotaglance/` so the widget has something to show right after login.

## Command line

```text
quotaglance                 open the main window (starts the background service if needed)
quotaglance --background    start without a window (used for autostart)
quotaglance --widget        toggle the desktop widget
quotaglance --preferences   open Preferences
quotaglance --status        print current usage in the terminal and exit
quotaglance --json          print current usage as JSON and exit
quotaglance --demo          use realistic sample data
quotaglance --quit          quit the running instance
```

`--status` and `--json` do not need a display, so they are handy over SSH or in scripts:

```text
$ quotaglance --status
Claude Code  Max 5x
  Session             ███████░░░░░░░░░    42%  resets in 2h 13m
  Weekly              ███████████░░░░░    67%  resets in 3d 4h
```

## Demo mode

Run `quotaglance --demo` (or set `QUOTAGLANCE_DEMO=1`, or flip **Preferences → Demo**) to explore
everything with realistic mock data. The numbers drift upward while the app runs and windows roll
over at their reset time, so you can watch the meters, colours and notifications change.

## How it works

```mermaid
flowchart LR
  subgraph sources [Your machine and your accounts]
    F[Local files and databases<br/>Codex logs, Cursor, JetBrains, OpenCode...]
    C[Tool CLIs<br/>kiro-cli, amp, agy...]
    A[Provider usage APIs<br/>with your existing sign-in or API key]
  end
  K[(GNOME Keyring)] --> P
  F --> P
  C --> P
  A --> P
  P[Provider modules<br/>pure parsers, unit-tested] --> E[Engine<br/>schedule, backoff, last-good cache]
  E --> W[Main window]
  E --> D[Desktop widget]
  E --> N[Notifications]
  E --> T[Tray icon<br/>StatusNotifierItem]
  E -- D-Bus --> X[GNOME Shell extension<br/>top-bar meter and popup]
```

- `src/quotaglance/providers/` has one module per provider. Parsing lives in pure functions that
  are tested against recorded responses in `tests/fixtures/`.
- `src/quotaglance/engine.py` refreshes providers on a small thread pool: local sources every
  minute, network sources every 5 minutes by default, with exponential backoff on errors. When a
  fetch fails, the last good numbers stay visible, marked as stale.
- `src/quotaglance/gui/` is the GTK 4 / libadwaita app. It owns the session-bus name
  `io.github.rafay_ah.QuotaGlance` and exports the `io.github.rafay_ah.QuotaGlance1` interface
  the Shell extension reads.
- `extension/` is the GNOME Shell extension. It only reads from the app over D-Bus and never
  touches the network or any credentials.

## Development

```sh
# Run from the checkout
PYTHONPATH=src python3 -m quotaglance --demo

# Unit tests and lint (no GTK needed)
python3 -m pip install pytest ruff
python3 -m pytest
ruff check src tests scripts

# Pixel-exact screenshots of every surface (needs a display, e.g. weston --backend=headless)
python3 scripts/capture.py --out /tmp/shots --dark

# A throwaway headless GNOME Shell session with the extension, for end-to-end checks
scripts/shell-capture/run-shell.sh /tmp/shell dark extension

# Packages
packaging/deb/build-deb.sh dist
packaging/appimage/build-appimage.sh dist
```

### Adding a provider

1. Create `src/quotaglance/providers/<id>.py` with a `Provider` subclass: metadata (name,
   monogram, colour, category, where the data comes from), a cheap offline `detect()`, and a
   `fetch()` that returns a `ProviderSnapshot` of `UsageWindow`s.
2. Keep parsing in pure `parse_*` functions and add fixtures plus tests in
   `tests/test_<id>.py`; `tests/conftest.py` provides a fake HTTP client, fake home directory and
   fake CLI runner.
3. Register the class in `src/quotaglance/providers/__init__.py` and run
   `python3 scripts/provider_table.py` to refresh the table above.

Rules every provider follows: read other apps' files read-only, never refresh rotating tokens,
prefer local data, and turn every failure into a clear, actionable message.

## Troubleshooting

- **No top-bar icon.** Open Preferences → Top Bar. If it says the extension needs a re-login,
  log out and back in. On vanilla GNOME without the extension, there is no tray, so the
  extension is required there.
- **"Sign-in expired."** Open the tool in question once (for example run `claude`, `codex` or
  `gemini`) so it refreshes its own session; QuotaGlance picks it up on the next refresh.
- **A provider shows "Not set up".** Check its row in Preferences → Providers: it explains where
  QuotaGlance looked and what to do.
- **Debug logging:** `quotaglance --debug`, or `quotaglance --status` to test providers from a
  terminal.

## Credits

QuotaGlance was inspired by [CodexBar](https://github.com/steipete/CodexBar) for macOS, whose
open documentation of provider data sources was a great help. Thanks to the GNOME, GTK and
libadwaita projects for the platform.

## License

[MIT](LICENSE) © 2026 rafay-ah
