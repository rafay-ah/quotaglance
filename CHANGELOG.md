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
- 29 providers, each toggleable:
  - Coding agents: Claude Code, Codex, Gemini CLI, OpenCode (Go and Zen), Amp, Augment,
    Factory Droid, Warp, Codebuff.
  - Editors and IDEs: Cursor, GitHub Copilot, Kiro, JetBrains AI, Windsurf, Zed,
    Antigravity, Kilo Code, Cline.
  - Coding plans and API platforms: z.ai, Kimi Code, MiniMax, Synthetic, OpenRouter,
    DeepSeek, Moonshot, Vercel AI Gateway, Chutes, Poe.
  - Voice and media: ElevenLabs.

  The README lists where each one gets its numbers.
- Claude Code status-line bridge (opt-in): exact 5-hour and weekly usage with no network
  calls, while keeping your own status line.
- API keys are stored in GNOME Keyring. Local sign-ins and app databases are read without
  modification, and rotating tokens (Claude, Codex, Cursor, Kiro) are never refreshed, so
  QuotaGlance can't sign you out of your tools.
- Demo mode with realistic mock data (`quotaglance --demo`).
- `quotaglance --status` and `--json` for the terminal.
- `.deb` and AppImage packages built by GitHub Actions.
