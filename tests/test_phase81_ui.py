from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select

from bangumi_local.config import Settings, get_settings
from bangumi_local.db.models import Base, UiJob, Work
from bangumi_local.db.session import session_scope
from bangumi_local.domain.steam import load_steam_rules
from bangumi_local.services.configuration import (
    SettingsEditError,
    save_steam_rule_configuration,
    update_dotenv,
)
from bangumi_local.services.jobs import enqueue_job
from bangumi_local.services.pull import pull_collections
from bangumi_local.web import create_app
from conftest import make_remote_collection


def _database(tmp_path: Path, name: str = "phase81.sqlite3") -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def _csrf(client: TestClient, path: str = "/settings") -> dict[str, str]:
    assert client.get(path).status_code == 200
    token = client.cookies.get("bld_csrf")
    assert token
    return {"origin": "http://localhost", "x-csrf-token": token}


def test_works_page_combines_personal_tags_and_complete_pagination(
    tmp_path: Path,
) -> None:
    database_url = _database(tmp_path, "works.sqlite3")
    remotes = [
        make_remote_collection(
            subject_id=1000 + index,
            tags=("RPG", "Action") if index == 0 else ("RPG",),
        )
        for index in range(13)
    ]
    with session_scope(database_url) as session:
        pull_collections(session, remotes)
    settings = Settings(_env_file=None, database_url=database_url)
    client = TestClient(create_app(settings), base_url="http://localhost")

    with client:
        first = client.get(
            "/works",
            params=[
                ("page_size", "12"),
                ("tag_include", "RPG"),
                ("tag_exclude", "Action"),
            ],
        )
        second = client.get(
            "/works",
            params=[("page_size", "12"), ("page", "2"), ("tag_include", "RPG")],
        )
        beyond = client.get(
            "/works?page_size=12&page=99&tag_include=RPG",
            follow_redirects=False,
        )

    assert first.status_code == 200
    assert "12 项" in first.text
    assert "RPG" in first.text
    assert first.text.count('class="work-card"') == 12
    assert "跳至第" in first.text
    assert second.status_code == 200 and "第 2 / 2 页" in second.text
    assert beyond.status_code == 303
    assert "page=2" in beyond.headers["location"]
    assert "tag_include=RPG" in beyond.headers["location"]
    assert '<details class="works-filter-advanced" open>' in first.text
    assert "Ctrl（macOS 为 Command）" in first.text
    assert "再次选择已选 Tag 会取消" in first.text

    with client:
        plain = client.get("/works")
    assert '<details class="works-filter-advanced">' in plain.text
    assert 'class="works-filter-advanced" open' not in plain.text


def test_job_result_prefers_summary_and_links_to_plan(tmp_path: Path) -> None:
    database_url = _database(tmp_path, "job-result.sqlite3")
    plan_id = "12345678-1234-1234-1234-123456789abc"
    with session_scope(database_url) as session:
        job = enqueue_job(
            session,
            kind="bulk_tag_plan",
            capability="remote_read",
            config={"operation": "add"},
        )
        job.status = "succeeded"
        job.result_json = json.dumps(
            {"plan_id": plan_id, "planned": 3, "unchanged": 2},
            ensure_ascii=False,
        )
        job_id = job.id
    client = TestClient(
        create_app(Settings(_env_file=None, database_url=database_url)),
        base_url="http://localhost",
    )
    with client:
        page = client.get(f"/jobs/{job_id}")
        listing = client.get("/jobs")
    assert page.status_code == 200
    assert "批量 Tag 计划" in page.text
    assert f'href="/plans/{plan_id}"' in page.text
    assert "将修改" in page.text and ">3<" in page.text
    assert "查看脱敏原始结果" in page.text
    assert "批量 Tag 计划" in listing.text


def test_external_bangumi_links_use_allowed_domain_and_new_tab(tmp_path: Path) -> None:
    database_url = _database(tmp_path, "external.sqlite3")
    with session_scope(database_url) as session:
        pull_collections(session, [make_remote_collection(subject_id=101)])
        work_id = session.scalar(select(Work.id).where(Work.bgm_subject_id == 101))
    assert work_id is not None
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        bangumi_web_base_url="https://bangumi.tv",
    )
    client = TestClient(create_app(settings), base_url="http://localhost")
    with client:
        detail = client.get(f"/works/{work_id}")
        redirect = client.get("/out/bangumi/101", follow_redirects=False)
        invalid = client.get("/out/bangumi/0", follow_redirects=False)
        settings_page = client.get("/settings")
    assert 'href="/out/bangumi/101" target="_blank"' in detail.text
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "https://bangumi.tv/subject/101"
    assert invalid.status_code == 422
    assert 'target="_blank" rel="noopener noreferrer"' in settings_page.text


def test_dotenv_editor_preserves_unknown_lines_and_never_requires_secret_echo(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    old_secret = "".join(("old", "-", "runtime", "-", "credential"))
    new_secret = "".join(("new", "-", "saved", "-", "credential"))
    env_file.write_text(
        f"# keep this comment\nUNKNOWN_SETTING=preserved\nBANGUMI_ACCESS_TOKEN='{old_secret}'\n",
        encoding="utf-8",
    )
    update_dotenv(
        env_file,
        {
            "BANGUMI_ACCESS_TOKEN": new_secret,
            "BLD_STEAM_ROOT": r"D:\Program Files (x86)\Steam",
        },
    )
    saved = get_settings(env_file)
    text = env_file.read_text(encoding="utf-8")
    assert saved.bangumi_access_token is not None
    assert saved.bangumi_access_token.get_secret_value() == new_secret
    assert saved.steam_root == Path(r"D:\Program Files (x86)\Steam")
    assert "# keep this comment" in text and "UNKNOWN_SETTING=preserved" in text
    update_dotenv(env_file, {"BANGUMI_ACCESS_TOKEN": None})
    assert "BANGUMI_ACCESS_TOKEN" not in env_file.read_text(encoding="utf-8")
    try:
        update_dotenv(env_file, {"BANGUMI_USERNAME": "bad\nvalue"})
    except SettingsEditError as exc:
        assert "bad" not in str(exc)
    else:
        raise AssertionError("control characters must be rejected")


def test_settings_forms_save_locally_require_restart_and_do_not_echo_secrets(
    tmp_path: Path,
) -> None:
    database_url = _database(tmp_path, "settings.sqlite3")
    env_file = tmp_path / ".env"
    steam_config = tmp_path / "steam.toml"
    steam_config.write_text(
        "[steam]\nallow_network = false\n\n[[steam.status_rules]]\n"
        'match = "contains"\npattern = "完结"\nstatus = "done"\ncase_sensitive = true\n',
        encoding="utf-8",
    )
    old_secret = "".join(("old", "-", "ui", "-", "secret"))
    new_secret = "".join(("new", "-", "ui", "-", "secret"))
    env_file.write_text("UNKNOWN_SETTING=keep\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        steam_config=steam_config,
        bangumi_access_token=SecretStr(old_secret),
        bangumi_username="before",
        bangumi_user_agent="tester/bld-ui",
    )
    client = TestClient(
        create_app(settings, env_file=env_file), base_url="http://localhost"
    )
    with client:
        headers = _csrf(client)
        initial = client.get("/settings")
        response = client.post(
            "/settings/bangumi",
            headers=headers,
            data={
                "access_token": new_secret,
                "username": "after",
                "user_agent": "tester/bld-ui-next",
                "web_base_url": "https://chii.in",
            },
            follow_redirects=False,
        )
        notice = client.get(response.headers["location"])
    assert old_secret not in initial.text and new_secret not in response.text
    assert response.status_code == 303
    assert "重启 UI 后生效" in notice.text
    written = env_file.read_text(encoding="utf-8")
    assert "UNKNOWN_SETTING=keep" in written
    assert new_secret in written
    assert settings.bangumi_username == "before"


def test_process_environment_managed_setting_is_not_overwritten(
    tmp_path: Path, monkeypatch
) -> None:
    database_url = _database(tmp_path, "managed-settings.sqlite3")
    env_file = tmp_path / ".env"
    env_file.write_text("BANGUMI_USERNAME='file-value'\n", encoding="utf-8")
    monkeypatch.setenv("BANGUMI_USERNAME", "process-value")
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        bangumi_username="process-value",
    )
    client = TestClient(
        create_app(settings, env_file=env_file), base_url="http://localhost"
    )
    with client:
        page = client.get("/settings")
        response = client.post(
            "/settings/bangumi",
            headers=_csrf(client),
            data={
                "username": "attempted-overwrite",
                "web_base_url": "https://bgm.tv",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert "由进程环境管理" in page.text
    assert "attempted-overwrite" not in env_file.read_text(encoding="utf-8")
    assert "file-value" in env_file.read_text(encoding="utf-8")


def test_steam_rule_roundtrip_and_invalid_ui_rule_do_not_create_job(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "steam.toml"
    config_path.write_text(
        '[extra]\nkeep = "yes"\n[steam]\nunknown = "preserve"\n',
        encoding="utf-8",
    )
    configuration = save_steam_rule_configuration(
        config_path,
        {
            "rules": [
                {
                    "match": "regex",
                    "pattern": "^完结",
                    "status": "done",
                    "case_sensitive": True,
                }
            ],
            "remaining_status": "wish",
            "allow_network": True,
        },
    )
    saved_text = config_path.read_text(encoding="utf-8")
    assert 'keep = "yes"' in saved_text and 'unknown = "preserve"' in saved_text
    assert configuration == load_steam_rules(config_path)

    database_url = _database(tmp_path, "rules.sqlite3")
    settings = Settings(
        _env_file=None, database_url=database_url, steam_config=config_path
    )
    client = TestClient(create_app(settings), base_url="http://localhost")
    with client:
        headers = _csrf(client, "/steam/status-plan")
        invalid = client.post(
            "/steam/jobs/status-plan",
            headers=headers,
            data={
                "appids": "70",
                "rule_mode": "custom",
                "remaining_policy": "local",
                "rules_json": json.dumps(
                    [
                        {
                            "match": "regex",
                            "pattern": "[",
                            "status": "done",
                            "case_sensitive": True,
                        }
                    ]
                ),
            },
        )
        with session_scope(database_url) as session:
            invalid_count = session.scalar(select(func.count()).select_from(UiJob))
        valid = client.post(
            "/steam/jobs/status-plan",
            headers=headers,
            data={
                "appids": "70",
                "rule_mode": "custom",
                "remaining_policy": "doing",
                "rules_json": json.dumps(
                    [
                        {
                            "match": "contains",
                            "pattern": "在用",
                            "status": "doing",
                            "case_sensitive": True,
                        }
                    ]
                ),
            },
        )
        with session_scope(database_url) as session:
            job = session.scalar(select(UiJob).order_by(UiJob.created_at.desc()))
    assert invalid.status_code == 400 and invalid_count == 0
    assert valid.status_code == 202 and job is not None
    payload = json.loads(job.config_json)
    assert payload["rule_mode"] == "custom"
    assert payload["remaining_policy"] == "doing"
    assert payload["rules"][0]["pattern"] == "在用"


def test_code_fonts_are_readable_in_ui_styles() -> None:
    app_css = Path("src/bangumi_local/web/static/app.css").read_text(encoding="utf-8")
    actions_css = Path("src/bangumi_local/web/static/actions.css").read_text(encoding="utf-8")

    assert "code,kbd,samp{font-size:1em}" in app_css
    assert "pre{font-size:1rem;line-height:1.6}" in app_css
    assert ".mono{font-size:.95rem}" in app_css
    assert "font-size: 1rem;" in actions_css
    assert "font-size: 1em;" in actions_css
