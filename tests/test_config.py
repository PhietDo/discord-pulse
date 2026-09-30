import pytest

from pulse.config import ConfigError, ModelRef, load_config

BASE = '''
[server]
guild_id = "g1"
channel_ids = ["c1"]
team_member_ids = ["t1"]

[mod_queue]
reply_window_hours = 12
frustration_threshold = -2

[models]
triage = "anthropic:claude-haiku-4-5-20251001"
theme = "openai:gpt-x"
digest = "anthropic:claude-opus-5-5"
investigate = "openrouter:anthropic/claude-sonnet-5"

[budget]
daily_usd_cap = 5.0

[pricing."anthropic:claude-haiku-4-5-20251001"]
input = 1.0
output = 5.0
cache_read = 0.1

[pricing."anthropic:claude-opus-5-5"]
input = 5.0
output = 25.0

[pricing."openai:gpt-x"]
input = 1.25
output = 10.0

[[launches]]
name = "v2.0 SDK"
date = "2026-09-15"
keywords = ["v2", "migration"]
'''

ENV = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "OPENROUTER_API_KEY": "r"}


def write(tmp_path, text):
    path = tmp_path / "pulse.toml"
    path.write_text(text)
    return path


def test_loads_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, BASE), env=ENV)
    assert cfg.guild_id == "g1"
    assert cfg.channel_ids == ("c1",)
    assert cfg.team_member_ids == frozenset({"t1"})
    assert cfg.reply_window_hours == 12.0
    assert cfg.frustration_threshold == -2
    assert cfg.models["investigate"] == ModelRef("openrouter", "anthropic/claude-sonnet-5")
    assert cfg.pricing["anthropic:claude-haiku-4-5-20251001"].cache_read == 0.1
    assert cfg.pricing["openai:gpt-x"].cache_read == 0.0
    assert cfg.daily_usd_cap == 5.0
    assert cfg.launches[0].keywords == ("v2", "migration")
    assert cfg.db_path == tmp_path / "pulse.db"
    assert cfg.imports_dir == tmp_path / "imports"


def test_model_ref_splits_on_first_colon():
    ref = ModelRef.parse("openrouter:openai/gpt-x:free")
    assert ref == ModelRef("openrouter", "openai/gpt-x:free")
    assert str(ref) == "openrouter:openai/gpt-x:free"


def test_missing_key_for_used_provider_names_env_var(tmp_path):
    env = dict(ENV)
    del env["OPENAI_API_KEY"]
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_config(write(tmp_path, BASE), env=env)


def test_empty_key_counts_as_missing(tmp_path):
    env = dict(ENV, OPENROUTER_API_KEY="")
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_config(write(tmp_path, BASE), env=env)


def test_unused_provider_key_not_required(tmp_path):
    text = BASE.replace('theme = "openai:gpt-x"', 'theme = "anthropic:claude-opus-5-5"')
    env = dict(ENV)
    del env["OPENAI_API_KEY"]
    load_config(write(tmp_path, text), env=env)


def test_missing_pricing_for_non_openrouter_model(tmp_path):
    text = BASE.replace('digest = "anthropic:claude-opus-5-5"', 'digest = "anthropic:claude-unpriced"')
    with pytest.raises(ConfigError, match="anthropic:claude-unpriced"):
        load_config(write(tmp_path, text), env=ENV)


def test_openrouter_model_needs_no_pricing(tmp_path):
    cfg = load_config(write(tmp_path, BASE), env=ENV)
    assert "openrouter:anthropic/claude-sonnet-5" not in cfg.pricing


def test_bad_provider_rejected(tmp_path):
    with pytest.raises(ConfigError, match="provider"):
        load_config(write(tmp_path, BASE.replace("openai:gpt-x", "gemini:x")), env=ENV)


def test_bad_launch_date_rejected(tmp_path):
    with pytest.raises(ConfigError, match="launch"):
        load_config(write(tmp_path, BASE.replace("2026-09-15", "Sept 15")), env=ENV)


def test_missing_guild_id(tmp_path):
    with pytest.raises(ConfigError, match="guild_id"):
        load_config(write(tmp_path, BASE.replace('guild_id = "g1"', "")), env=ENV)


def test_missing_agent_model(tmp_path):
    with pytest.raises(ConfigError, match="digest"):
        load_config(write(tmp_path, BASE.replace('digest = "anthropic:claude-opus-5-5"', "")), env=ENV)


def test_paths_section_overrides_defaults(tmp_path):
    cfg = load_config(write(tmp_path, BASE + '\n[paths]\ndb = "data/p.db"\nimports = "in"\n'), env=ENV)
    assert cfg.db_path == tmp_path / "data" / "p.db"
    assert cfg.imports_dir == tmp_path / "in"
