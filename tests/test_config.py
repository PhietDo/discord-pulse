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


def test_classifier_enabled_must_be_bool(tmp_path):
    text = CLASSIFIER.replace("enabled = true", 'enabled = "false"')
    with pytest.raises(ConfigError, match="enabled"):
        load_config(write(tmp_path, BASE + text), env=ENV)


def test_llm_agents_still_reject_jev_provider(tmp_path):
    with pytest.raises(ConfigError, match="provider"):
        load_config(write(tmp_path, BASE.replace('theme = "openai:gpt-x"', 'theme = "jev:jev-latest"')), env=ENV)


def test_require_keys_false_skips_env_checks(tmp_path):
    from pulse.config import load_config, missing_keys

    path = tmp_path / "pulse.toml"
    path.write_text(
        '[server]\nguild_id = "1"\n[models]\ntriage = "openai:t"\ntheme = "openai:t"\n'
        'digest = "anthropic:d"\ninvestigate = "openai:t"\n'
        '[pricing."openai:t"]\ninput = 1\noutput = 2\n[pricing."anthropic:d"]\ninput = 1\noutput = 2\n'
        '[classifier]\nenabled = true\n'
    )
    config = load_config(path, {}, require_keys=False)
    assert config.classifier.enabled
    assert missing_keys(config, {"OPENAI_API_KEY": "x"}) == ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"]
    assert missing_keys(config, {"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y", "OPENROUTER_API_KEY": "z"}) == []


def test_integrations_section(tmp_path):
    from pulse.config import ConfigError, IntegrationsConfig, load_config

    base = (
        '[server]\nguild_id = "1"\n[models]\ntriage = "anthropic:m"\ntheme = "anthropic:m"\n'
        'digest = "anthropic:m"\ninvestigate = "anthropic:m"\n[pricing."anthropic:m"]\ninput = 1\noutput = 2\n'
    )
    path = tmp_path / "pulse.toml"
    path.write_text(base)
    assert load_config(path, {}, require_keys=False).integrations == IntegrationsConfig()
    path.write_text(base + '[integrations.github]\nrepo = "acme/sdk"\nlabels = ["community"]\n'
                           '[integrations.linear]\nteam_id = "abc-123"\n')
    cfg = load_config(path, {}, require_keys=False).integrations
    assert (cfg.github_repo, cfg.github_labels, cfg.linear_team_id) == ("acme/sdk", ("community",), "abc-123")
    path.write_text(base + '[integrations.github]\nrepo = "not a repo"\n')
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(path, {}, require_keys=False)


@pytest.mark.parametrize("repo", ["../x", "owner/..", "./x"])
def test_integrations_rejects_dot_segments_in_repo(tmp_path, repo):
    from pulse.config import ConfigError, load_config

    base = (
        '[server]\nguild_id = "1"\n[models]\ntriage = "anthropic:m"\ntheme = "anthropic:m"\n'
        'digest = "anthropic:m"\ninvestigate = "anthropic:m"\n[pricing."anthropic:m"]\ninput = 1\noutput = 2\n'
    )
    path = tmp_path / "pulse.toml"
    path.write_text(base + f'[integrations.github]\nrepo = "{repo}"\n')
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(path, {}, require_keys=False)


@pytest.mark.parametrize("labels_toml", ['labels = "community"', "labels = 5"])
def test_integrations_rejects_non_list_labels(tmp_path, labels_toml):
    from pulse.config import ConfigError, load_config

    base = (
        '[server]\nguild_id = "1"\n[models]\ntriage = "anthropic:m"\ntheme = "anthropic:m"\n'
        'digest = "anthropic:m"\ninvestigate = "anthropic:m"\n[pricing."anthropic:m"]\ninput = 1\noutput = 2\n'
    )
    path = tmp_path / "pulse.toml"
    path.write_text(base + f'[integrations.github]\nrepo = "acme/sdk"\n{labels_toml}\n')
    with pytest.raises(ConfigError, match="labels must be a list of strings"):
        load_config(path, {}, require_keys=False)


_MIN = (
    '[server]\nguild_id = "1"\n[models]\ntriage = "anthropic:m"\ntheme = "anthropic:m"\n'
    'digest = "anthropic:m"\ninvestigate = "anthropic:m"\n[pricing."anthropic:m"]\ninput = 1\noutput = 2\n'
)


@pytest.mark.parametrize("extra, match", [
    ("alerts = true\n", r"\[alerts\] must be a table"),
    ("bot = 5\n", r"\[bot\] must be a table"),
    ("integrations = 5\n", r"\[integrations\] must be a table"),
    ('[integrations]\ngithub = "org/repo"\n', r"\[integrations.github\] must be a table"),
    ('[integrations]\nlinear = "abc"\n', r"\[integrations.linear\] must be a table"),
    ('[alerts]\nspike_min_volume = "5"\n', "spike_min_volume must be a whole number"),
    ("[alerts]\nspike_min_volume = 5.7\n", "spike_min_volume must be a whole number"),
    ('[alerts]\nspike_trend = "nan"\n', "spike_trend must be a number"),
    ("[alerts]\nspike_trend = nan\n", "spike_trend must be a finite number"),
    ("[alerts]\nfrustrated_hours = inf\n", "frustrated_hours must be a finite number"),
    ("[integrations.linear]\nteam_id = [1]\n", r"\[integrations.linear\] team_id must be a string"),
])
def test_config_rejects_wrong_section_and_value_types(tmp_path, extra, match):
    path = tmp_path / "pulse.toml"
    path.write_text(extra + _MIN)  # first, so top-level keys stay top-level
    with pytest.raises(ConfigError, match=match):
        load_config(path, {}, require_keys=False)


def test_config_accepts_numeric_alert_values(tmp_path):
    path = tmp_path / "pulse.toml"
    path.write_text(_MIN + "[alerts]\nspike_min_volume = 3\nspike_trend = 2\nfrustrated_hours = 6.5\n")
    a = load_config(path, {}, require_keys=False).alerts
    assert (a.spike_min_volume, a.spike_trend, a.frustrated_hours) == (3, 2.0, 6.5)
