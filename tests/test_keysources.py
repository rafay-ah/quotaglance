import json

from conftest import make_ctx, write
from quotaglance.providers.keysources import claude_code_env, opencode_keys


def test_opencode_keys_skip_oauth_entries_and_blanks(home):
    write(home / ".local/share/opencode/auth.json", json.dumps({
        "deepseek": {"type": "api", "key": " sk-deepseek "},
        "anthropic": {"type": "oauth", "refresh": "r", "access": "a"},
        "poe": {"type": "api", "key": "  "}}))
    assert opencode_keys(make_ctx(home)) == {"deepseek": "sk-deepseek"}


def test_opencode_keys_tolerate_a_broken_file(home):
    write(home / ".local/share/opencode/auth.json", "{nope")
    assert opencode_keys(make_ctx(home)) == {}


def test_claude_code_env_honours_claude_config_dir(home):
    settings = {"env": {"ANTHROPIC_BASE_URL": "https://api.moonshot.ai/anthropic",
                        "ANTHROPIC_AUTH_TOKEN": "sk-moon"}}
    write(home / "work-claude/settings.json", json.dumps(settings))
    ctx = make_ctx(home, env={"CLAUDE_CONFIG_DIR": str(home / "work-claude")})
    assert claude_code_env(ctx) == ("https://api.moonshot.ai/anthropic", "sk-moon")
    assert claude_code_env(make_ctx(home)) == ("", None)
