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
