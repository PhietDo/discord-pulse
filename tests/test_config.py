import pytest

from pulse.config import ClassifierConfig, ConfigError, ModelRef, Price, load_config

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


def test_missing_config_file_hints_example(tmp_path):
    with pytest.raises(ConfigError, match="pulse.toml.example"):
        load_config(tmp_path / "pulse.toml", env=ENV)


CLASSIFIER = '''
[classifier]
enabled = true
model = "jev:jev-latest"
needs_reply_threshold = 0.75
min_confidence = 0.5
escalate_kinds = ["bug", "docs"]

[pricing."jev:jev-latest"]
per_request = 0.0000387
'''


def test_classifier_section_parsed(tmp_path):
    cfg = load_config(write(tmp_path, BASE + CLASSIFIER), env=ENV)
    assert cfg.classifier == ClassifierConfig(
        enabled=True, model=ModelRef("jev", "jev-latest"), needs_reply_threshold=0.75,
        min_confidence=0.5, escalate_kinds=("bug", "docs"),
    )
    assert cfg.pricing["jev:jev-latest"] == Price(0.0, 0.0, 0.0, per_request=0.0000387)


def test_classifier_absent_is_none(tmp_path):
    assert load_config(write(tmp_path, BASE), env=ENV).classifier is None


def test_classifier_defaults(tmp_path):
    cfg = load_config(write(tmp_path, BASE + '\n[classifier]\nenabled = true\n'), env=ENV)
    assert cfg.classifier.model == ModelRef("jev", "jev-latest")
    assert cfg.classifier.needs_reply_threshold == 0.7
    assert cfg.classifier.min_confidence == 0.6
    assert cfg.classifier.escalate_kinds == ("bug", "docs", "feature_request", "praise")


def test_enabled_classifier_requires_openrouter_key(tmp_path):
    text = BASE.replace('investigate = "openrouter:anthropic/claude-sonnet-5"', 'investigate = "anthropic:claude-opus-5-5"')
    env = dict(ENV)
    del env["OPENROUTER_API_KEY"]
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY.*classifier"):
        load_config(write(tmp_path, text + CLASSIFIER), env=env)


def test_disabled_classifier_needs_no_key(tmp_path):
    text = BASE.replace('investigate = "openrouter:anthropic/claude-sonnet-5"', 'investigate = "anthropic:claude-opus-5-5"')
    env = dict(ENV)
    del env["OPENROUTER_API_KEY"]
    cfg = load_config(write(tmp_path, text + CLASSIFIER.replace("enabled = true", "enabled = false")), env=env)
    assert cfg.classifier.enabled is False


def test_classifier_rejects_non_jev_provider(tmp_path):
    with pytest.raises(ConfigError, match="provider"):
        load_config(write(tmp_path, BASE + CLASSIFIER.replace('model = "jev:jev-latest"', 'model = "openai:gpt-x"')), env=ENV)


def test_classifier_rejects_unknown_kind(tmp_path):
    with pytest.raises(ConfigError, match="escalate_kinds"):
        load_config(write(tmp_path, BASE + CLASSIFIER.replace('["bug", "docs"]', '["bug", "rants"]')), env=ENV)


def test_classifier_rejects_threshold_out_of_range(tmp_path):
    with pytest.raises(ConfigError, match="needs_reply_threshold"):
        load_config(write(tmp_path, BASE + CLASSIFIER.replace("0.75", "1.5")), env=ENV)


def test_llm_agents_still_reject_jev_provider(tmp_path):
    with pytest.raises(ConfigError, match="provider"):
        load_config(write(tmp_path, BASE.replace('theme = "openai:gpt-x"', 'theme = "jev:jev-latest"')), env=ENV)
