import shutil
from datetime import datetime, timezone
from pathlib import Path

from pulse.models import to_iso
from pulse.sources.file_source import FileSource

FIXTURES = Path(__file__).parent / "fixtures"


def imports_with(tmp_path, *names):
    for name in names:
        shutil.copy(FIXTURES / name, tmp_path / name)
    return tmp_path


def by_id(messages):
    return {m.id: m for m in messages}


def test_reads_dce_channel_export(tmp_path):
    src = FileSource(imports_with(tmp_path, "dce_channel.json"))
    msgs = by_id(src.fetch())
    assert set(msgs) == {"1001", "1002", "1004"}  # pinned-message system event skipped
    alice = msgs["1001"]
    assert (alice.guild_id, alice.channel_id, alice.channel_name) == ("900", "100", "help")
    assert alice.author_name == "Alice"  # nickname preferred
    assert alice.author_avatar_url == "https://cdn.discordapp.com/avatars/u1/a.png"
    assert to_iso(alice.created_at) == "2026-09-20T14:00:00.123456Z"
    assert alice.thread_id is None
    staff = msgs["1002"]
    assert staff.reply_to_id == "1001"
    assert staff.author_avatar_url is None  # local media path dropped
    assert to_iso(staff.edited_at) == "2026-09-20T14:35:00.000000Z"
    assert msgs["1004"].is_bot is True
    assert src.errors == []


def test_thread_export_sets_thread_id(tmp_path):
    msgs = by_id(FileSource(imports_with(tmp_path, "dce_thread.json")).fetch())
    m = msgs["3001"]
    assert (m.channel_id, m.thread_id) == ("300", "300")
    assert m.author_name == "alice"  # null nickname falls back to name
    assert m.parent_channel_id == "100"


def test_reads_csv_and_reports_bad_row_with_line(tmp_path):
    src = FileSource(imports_with(tmp_path, "messages.csv"))
    msgs = by_id(src.fetch())
    assert set(msgs) == {"2001", "2003"}
    assert msgs["2003"].reply_to_id == "2001"
    assert msgs["2001"].reply_to_id is None
    assert msgs["2001"].content == "Docs for auth are confusing, love the CLI though"
    assert len(src.errors) == 1
    assert "messages.csv:3" in src.errors[0]
    assert "created_at" in src.errors[0]


def test_bad_json_file_is_skipped_and_others_proceed(tmp_path):
    imports_with(tmp_path, "dce_thread.json")
    (tmp_path / "bad.json").write_text("{not json")
    src = FileSource(tmp_path)
    msgs = by_id(src.fetch())
    assert set(msgs) == {"3001"}
    assert len(src.errors) == 1 and "bad.json" in src.errors[0]


def test_json_that_is_not_a_dce_export(tmp_path):
    (tmp_path / "other.json").write_text('{"hello": "world"}')
    src = FileSource(tmp_path)
    assert list(src.fetch()) == []
    assert "other.json" in src.errors[0] and "DiscordChatExporter" in src.errors[0]


def test_csv_missing_required_columns(tmp_path):
    (tmp_path / "x.csv").write_text("a,b\n1,2\n")
    src = FileSource(tmp_path)
    assert list(src.fetch()) == []
    assert "x.csv" in src.errors[0] and "missing required columns" in src.errors[0]


def test_since_filters_and_other_files_ignored(tmp_path):
    imports_with(tmp_path, "dce_channel.json", "messages.csv")
    (tmp_path / "notes.txt").write_text("ignore me")
    since = datetime(2026, 9, 21, tzinfo=timezone.utc)
    src = FileSource(tmp_path)
    assert set(by_id(src.fetch(since))) == {"2001", "2003"}
    assert any("notes.txt" in e for e in src.errors)


def test_missing_imports_dir_yields_nothing(tmp_path):
    src = FileSource(tmp_path / "nope")
    assert list(src.fetch()) == []
    assert src.errors == [f"imports folder {tmp_path / 'nope'} not found"]


def test_unsupported_file_is_reported(tmp_path):
    (tmp_path / "help.html").write_text("<html></html>")
    (tmp_path / ".DS_Store").write_text("")
    src = FileSource(tmp_path)
    assert list(src.fetch()) == []
    assert len(src.errors) == 1
    assert "help.html" in src.errors[0]
    assert "JSON or CSV" in src.errors[0]
