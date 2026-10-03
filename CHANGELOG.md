# Changelog

All notable changes to QuotaGlance are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-03

First public release.

### Added

- Native GTK 4 / libadwaita app that runs quietly in the background and starts on login
  (toggle in Preferences).
- Top-bar usage meter: a GNOME Shell extension (GNOME 45+) with a popup listing every
  provider's session, weekly and monthly windows and reset countdowns, plus a
  StatusNotifierItem tray fallback that works out of the box on Ubuntu.
- Desktop widget in small, medium and large sizes, with an optional tinted background;
  the Shell extension can keep it on top, on every workspace, at a remembered position.
- Notifications at 80% and 95% usage, plus optional "limit reset" notices.
- Providers: Claude Code, Codex, Cursor, GitHub Copilot, Gemini CLI, Kiro, ElevenLabs,
  OpenCode (Go and Zen), z.ai, JetBrains AI and more; see the README for the full list
  and where each one gets its numbers.
- API keys are stored in GNOME Keyring; local sign-ins are read without modification.
- Demo mode with realistic mock data (`quotaglance --demo`).
- `quotaglance --status` and `--json` for the terminal.
- `.deb` and AppImage packages built by GitHub Actions.
