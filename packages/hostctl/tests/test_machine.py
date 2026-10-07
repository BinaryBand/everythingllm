import json
import sqlite3

from hostctl import machine


def anythingllm_db(storage, on, auto):
    con = sqlite3.connect(storage / "anythingllm.db")
    con.execute("CREATE TABLE system_settings (label TEXT, value TEXT)")
    con.executemany(
        "INSERT INTO system_settings VALUES (?, ?)",
        [
            ("default_agent_skills", json.dumps(on)),
            ("whitelisted_agent_skills", json.dumps(auto)),
            ("disabled_agent_skills", "[]"),
        ],
    )
    con.commit()
    con.close()


def test_jobs_are_kept_to_chats_only_with_the_job_tool_off_and_none_auto_approved(
    tmp_path,
):
    assert machine.jobs_kept_to_chats(tmp_path) is None  # no database: unknown
    anythingllm_db(
        tmp_path,
        ["create-scheduled-job", "filesystem-agent", "gmail-agent"],
        ["filesystem-write-text-file", "create-scheduled-job", "gmail-move-to-archive"],
    )
    assert machine.jobs_kept_to_chats(tmp_path) is False
    (tmp_path / "anythingllm.db").unlink()
    anythingllm_db(
        tmp_path, ["filesystem-agent", "gmail-agent"], ["gmail-move-to-archive"]
    )
    assert machine.jobs_kept_to_chats(tmp_path) is True


def test_one_auto_approved_file_tool_is_enough_to_fail(tmp_path):
    anythingllm_db(tmp_path, ["filesystem-agent"], ["filesystem-edit-file"])
    assert machine.jobs_kept_to_chats(tmp_path) is False
