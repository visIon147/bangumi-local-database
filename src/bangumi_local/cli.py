from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time
import webbrowser

import typer
from sqlalchemy.exc import SQLAlchemyError

from bangumi_local.adapters.bangumi import BangumiAPIError, BangumiClient
from bangumi_local.adapters.steam import SteamDataError, detect_steam, read_steam_snapshot
from bangumi_local.config import ConfigurationError, Settings, get_settings
from bangumi_local.db.repositories import list_collections
from bangumi_local.db.session import session_scope
from bangumi_local.domain.models import (
    CollectionStatus,
    RemoteCollection,
    SubjectSearchCandidate,
    SubjectType,
)
from bangumi_local.domain.mutations import CollectionPatch, MutationValidationError
from bangumi_local.domain.plans import stable_json
from bangumi_local.domain.snapshots import CANONICAL_FIELDS
from bangumi_local.domain.tags import (
    DEFAULT_GALGAME_CLASSIFICATION_TAG,
    DEFAULT_GAME_CLASSIFICATION_TAG,
)
from bangumi_local.domain.steam import SteamConfigurationError, load_steam_rules
from bangumi_local.services.pull import pull_collections
from bangumi_local.services.pull_plans import (
    applied_pull_subject_ids,
    apply_pull_plan,
    create_pull_plan,
    preflight_pull_plan,
)
from bangumi_local.services.pull_media import materialize_pull_media
from bangumi_local.services.apply_plan import apply_reviewed_plan, preflight_plan
from bangumi_local.services.plans import (
    PlanError,
    StoredPlan,
    create_bulk_tag_plan,
    create_classification_plan,
    create_recovery_plan,
    create_sync_plan,
    export_plan,
    load_plan,
    plan_as_dict,
    review_plan,
)
from bangumi_local.services.shadow import bootstrap_shadows
from bangumi_local.services.status import build_status_report
from bangumi_local.services.edit_collection import edit_local_collection
from bangumi_local.services.migrations import MigrationSafetyError, upgrade_database_safely
from bangumi_local.services.manual_uncollect import reconcile_manual_uncollect
from bangumi_local.services.steam_import import apply_steam_import, preview_steam_import
from bangumi_local.services.steam_library import (
    SteamEntryListItem,
    SteamLibraryError,
    list_steam_collections,
    list_steam_entries,
)
from bangumi_local.services.steam_matching import (
    SteamMatchError,
    confirm_match,
    fetch_match_search,
    match_details,
    persist_match_search,
    prepare_match_search,
    set_match_disposition,
)
from bangumi_local.services.steam_match_apply import (
    apply_steam_match_plan,
    preflight_steam_match_plan,
)
from bangumi_local.services.steam_match_plans import (
    AutoMatchPolicy,
    fetch_steam_match_plan,
    persist_steam_match_plan,
    prepare_steam_match_plan,
    revise_steam_match_plan,
)
from bangumi_local.services.steam_match_media import register_match_candidate_media
from bangumi_local.services.steam_plans import create_steam_status_plan
from bangumi_local.services.steam_titles import (
    SteamTitleError,
    clear_manual_title,
    fetch_title_completion,
    persist_title_completion,
    prepare_title_completion,
    set_manual_title,
)
from bangumi_local.services.rating_queue import (
    RATING_ORDERS,
    RatingQueueError,
    RatingQueueStale,
    create_rating_queue,
    list_rating_queues,
    load_rating_queue,
    next_rating_item,
    prepare_rating_queue,
    rate_rating_item,
    rated_subject_ids,
    rating_queue_counts,
    reopen_rating_subject,
    set_rating_disposition,
)
from bangumi_local.services.discovery import (
    DISCOVERY_DECISIONS,
    DiscoveryError,
    DiscoveryIdentityConflict,
    bangumi_discovery_seeds,
    create_discovery_session,
    create_failed_discovery_session,
    decide_discovery_candidate,
    list_discovery_sessions,
    load_discovery_session,
    next_discovery_candidate,
    promotion_preview,
    reopen_discovery_candidate,
    steam_discovery_seeds,
)
from bangumi_local.domain.media import ImagePolicy, MediaReference
from bangumi_local.services.jobs import interrupt_running_jobs
from bangumi_local.services.initialization import (
    InitializationError,
    initialize_user_directory,
)
from bangumi_local.services.media import (
    MediaError,
    bind_media_source,
    cache_local_media,
    download_remote_media,
    mark_media_source_failure,
    media_status,
    prune_media_cache,
    register_media_references,
    scan_steam_librarycache,
    verify_media_cache,
)
from bangumi_local.db.models import LibraryEntry, MediaSource

app = typer.Typer(
    help="Auditable Bangumi collection mirror and safe plan/apply tools.",
    no_args_is_help=True,
)
shadow_app = typer.Typer(help="Inspect or bootstrap sync shadows.", no_args_is_help=True)
tags_app = typer.Typer(help="Generate immutable bulk personal-tag plans.", no_args_is_help=True)
plan_app = typer.Typer(help="Show, review, and explicitly apply immutable plans.", no_args_is_help=True)
collection_app = typer.Typer(help="Edit mirrored collection fields locally.", no_args_is_help=True)
sync_app = typer.Typer(help="Generate plans from safe local collection changes.", no_args_is_help=True)
db_app = typer.Typer(help="Safely migrate and inspect the local database.", no_args_is_help=True)
steam_app = typer.Typer(help="Import, inspect, match, and plan from a Steam library.", no_args_is_help=True)
steam_match_app = typer.Typer(help="Review Steam to Bangumi identity matches.", no_args_is_help=True)
steam_titles_app = typer.Typer(help="Complete or override imported Steam titles.", no_args_is_help=True)
rating_app = typer.Typer(help="Persistent local-first rating review workflow.", no_args_is_help=True)
rating_queue_app = typer.Typer(help="Create and resume fixed rating queues.", no_args_is_help=True)
discovery_app = typer.Typer(help="Bounded, persistent played-game discovery.", no_args_is_help=True)
discovery_session_app = typer.Typer(help="Create and resume discovery sessions.", no_args_is_help=True)
media_app = typer.Typer(help="Inspect and manage the portable local media cache.", no_args_is_help=True)
ui_app = typer.Typer(help="Run the loopback-only local Web interface.", no_args_is_help=True)
app.add_typer(shadow_app, name="shadow")
app.add_typer(tags_app, name="tags")
app.add_typer(plan_app, name="plan")
app.add_typer(collection_app, name="collection")
app.add_typer(sync_app, name="sync")
app.add_typer(db_app, name="db")
app.add_typer(steam_app, name="steam")
steam_app.add_typer(steam_match_app, name="match")
steam_app.add_typer(steam_titles_app, name="titles")
app.add_typer(rating_app, name="rating")
rating_app.add_typer(rating_queue_app, name="queue")
app.add_typer(discovery_app, name="discovery")
discovery_app.add_typer(discovery_session_app, name="session")
app.add_typer(media_app, name="media")
app.add_typer(ui_app, name="ui")


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


_configure_utf8_stdio()


@app.command("init")
def initialize_command(
    target_directory: Path = typer.Option(
        Path("."),
        "--target-directory",
        help="Directory in which to create .env and config/steam.toml.",
    ),
) -> None:
    """Create safe local configuration templates without overwriting files."""

    try:
        result = initialize_user_directory(target_directory)
    except (InitializationError, OSError) as exc:
        typer.echo(f"Initialization failed: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"Target: {result.target_directory}")
    for path in result.created:
        typer.echo(f"created: {path}")
    for path in result.skipped:
        typer.echo(f"skipped (already exists): {path}")
    typer.echo(f"Summary: created={len(result.created)} skipped={len(result.skipped)}")


def _settings_or_exit(env_file: str | Path | None = ".env") -> Settings:
    try:
        return get_settings(env_file)
    except Exception as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from None


def _status_label(value: int | None) -> str:
    if value is None:
        return "-"
    try:
        return CollectionStatus(value).label
    except ValueError:
        return str(value)


def _parse_subject_type(value: str | None) -> SubjectType | None:
    if value is None:
        return None
    try:
        return SubjectType.parse(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--subject-type") from exc


def _parse_collection_status(value: str) -> CollectionStatus:
    normalized = value.strip().lower().replace("_", "-")
    for status in CollectionStatus:
        if status.label == normalized:
            return status
    raise typer.BadParameter(
        "status must be wish, done, doing, on-hold, or dropped", param_hint="--status"
    )


def _parse_subject_types(value: str) -> tuple[SubjectType, ...]:
    raw = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not raw:
        raise typer.BadParameter("At least one subject type is required.")
    try:
        return tuple(SubjectType.parse(item) for item in raw)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--subject-type") from exc


def _parse_collection_statuses(value: str) -> tuple[CollectionStatus, ...]:
    raw = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not raw:
        raise typer.BadParameter("At least one collection status is required.")
    return tuple(_parse_collection_status(item) for item in raw)


def _parse_subject_ids(value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(
            dict.fromkeys(int(item.strip()) for item in value.split(",") if item.strip())
        )
    except ValueError as exc:
        raise typer.BadParameter("subject IDs must be comma-separated integers") from exc
    if not parsed or any(item < 1 for item in parsed):
        raise typer.BadParameter("subject IDs must be positive integers")
    return parsed


def _parse_app_ids(value: str) -> tuple[str, ...]:
    parsed = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not parsed or any(not item.isdigit() for item in parsed):
        raise typer.BadParameter("Steam AppIDs must be comma-separated positive integers.")
    return parsed


def _print_steam_entries(items: list[SteamEntryListItem]) -> None:
    typer.echo("appid  title  collections  scope  installed  playtime  match")
    for item in items:
        collections = ",".join(item.collections) or "-"
        title = item.title or "<unknown>"
        installed = "?" if item.installed is None else ("yes" if item.installed else "no")
        playtime = "-" if item.playtime_minutes is None else str(item.playtime_minutes)
        typer.echo(
            f"{item.app_id}  {title}  {collections}  {item.ownership_scope}  "
            f"{installed}  {playtime}  {item.match_status}"
        )


def _fetch_remote_collections(
    settings: Settings, subject_type: SubjectType | None = None
) -> list[RemoteCollection]:
    with BangumiClient(settings) as client:
        me = client.get_me()
        if me.username != client.username:
            raise BangumiAPIError(
                f"Authenticated user '{me.username}' does not match configured username."
            )
        return client.get_collections(subject_type=subject_type)


def _verify_client_user(client: BangumiClient) -> None:
    me = client.get_me()
    if me.username != client.username:
        raise BangumiAPIError(
            f"Authenticated user '{me.username}' does not match configured username."
        )


def _selector(ids: str | None, all_current: bool, public_tag: str | None) -> dict[str, object]:
    choices = sum((ids is not None, all_current, public_tag is not None))
    if choices != 1:
        raise typer.BadParameter("Choose exactly one of --ids, --all-current, or --public-tag.")
    if ids is not None:
        try:
            parsed = tuple(dict.fromkeys(int(value.strip()) for value in ids.split(",") if value.strip()))
        except ValueError as exc:
            raise typer.BadParameter("--ids must be a comma-separated list of integers.") from exc
        if not parsed or any(value < 1 for value in parsed):
            raise typer.BadParameter("--ids must contain positive subject IDs.")
        return {"mode": "ids", "ids": list(parsed)}
    if all_current:
        return {"mode": "all_current"}
    assert public_tag is not None
    return {"mode": "public_tag", "public_tag": public_tag}


_REASON_LABELS = {
    "will_modify": "将修改",
    "public_tag_matched": "公开标签命中",
    "no_op": "目标 Tag 已是预期状态",
    "pull_import": "将导入新的本地收藏镜像",
    "pull_remote_update": "远端安全字段将合并到 LOCAL",
    "pull_metadata_update": "作品公开元数据将更新",
    "pull_bootstrap": "LOCAL 与远端一致，将建立 shadow",
    "pull_bootstrap_mismatch": "缺少 shadow 且 LOCAL 与远端不同",
    "pull_conflict_record": "将记录字段冲突，不覆盖 LOCAL 值",
    "pull_converged": "LOCAL 与远端已收敛，将推进 shadow",
    "pull_local_preserved": "仅 LOCAL 有变化，本次保留",
    "pull_remote_missing": "远端缺失，本地记录保留",
    "pull_unchanged": "LOCAL、REMOTE 与 shadow 均无变化",
    "already_galgame": "已有 Galgame 分类",
    "already_game": "已有 Game 分类",
    "public_tag_not_matched": "公开标签未命中",
    "public_tag_not_matched_manual_review": "公开标签未命中，等待人工判断",
    "public_tag_read_failed": "公开标签读取失败",
    "local_tag_conflict": "本地 Tag 冲突",
    "local_tags_changed": "本地 Tag 相对 shadow 已修改",
    "missing_shadow": "缺少同步 shadow",
    "stale_remote": "远端已变化（stale）",
    "preflight_read_failed": "preflight 读取失败",
    "already_applied": "此前已应用",
    "previously_stale": "此前已标记 stale",
    "previously_failed": "此前执行失败，需新计划",
    "local_changed_remote_unchanged": "本地已修改且远端仍为基线",
    "remote_changed_pull_required": "远端已修改，需先 pull",
    "converged_pull_required": "两端已收敛，需先 pull 前移 shadow",
    "conflict": "本地与远端冲突",
    "remote_missing": "远端收藏缺失",
    "stale_local": "本地已变化（stale）",
    "local_work_missing": "本地 work/subject 缺失",
    "steam_unmatched": "Steam 条目尚未匹配",
    "steam_match_unconfirmed": "Steam 候选尚未人工确认",
    "steam_no_subject": "已人工确认 Bangumi 无对应条目",
    "steam_match_deferred": "Steam 匹配已延后",
    "steam_already_confirmed": "Steam 条目已有确认映射",
    "steam_match_auto_confirm": "高置信候选将自动确认",
    "steam_match_manual_review": "候选需要人工审核",
    "steam_match_no_candidates": "未找到候选，等待人工处理",
    "steam_match_title_unavailable": "无法取得可搜索标题，等待人工输入",
    "steam_match_mapping_collision": "候选已关联其他 Steam 条目，需人工审核",
    "steam_match_source_title_collision": "同账户存在同名/版本条目，需人工归并",
    "steam_match_batch_collision": "批内多个条目指向同一候选，需人工审核",
    "steam_match_manual_override": "人工指定候选将确认",
    "steam_match_mark_no_subject": "将记录人工确认无 Bangumi 条目",
    "steam_match_mark_deferred": "将暂缓该条目",
    "steam_match_subject_read_failed": "候选 fresh-read 失败",
    "steam_match_subject_not_game": "候选不是游戏条目",
    "steam_mapping_invalid": "Steam/Bangumi 映射无效",
    "steam_category_rule_conflict": "Steam 分类命中冲突状态",
    "steam_remaining_local_only": "未命中规则，默认仅保留本地",
    "steam_remote_missing_local_state_exists": "远端缺失但本地仍有收藏状态",
    "steam_status_already_desired": "Bangumi 已是目标状态",
    "steam_local_changed": "本地收藏状态已有未同步修改",
    "steam_create_collection": "将新增 Bangumi 收藏",
    "steam_patch_collection": "将更新 Bangumi 收藏状态",
    "reverse_verified_manual_uncollect": "已验证网页手动取消收藏，可恢复为原状态",
    "stale_source": "Steam 来源或人工映射已变化（stale）",
    "stale_remote_existence": "Bangumi 收藏存在性已变化（stale）",
}


def _print_candidates(title: str, candidates: tuple[object, ...]) -> None:
    typer.echo(f"\n{title} ({len(candidates)})")
    if not candidates:
        typer.echo("  -")
        return
    for raw in candidates:
        item = raw
        before = ", ".join(item.before_tags) or "-"  # type: ignore[attr-defined]
        after_tags = item.after_tags  # type: ignore[attr-defined]
        after = ", ".join(after_tags or ()) or "-"
        public = ", ".join(item.public_tags) or "-"  # type: ignore[attr-defined]
        reason = _REASON_LABELS.get(item.reason, item.reason)  # type: ignore[attr-defined]
        fields = ",".join(item.changed_fields) or "-"  # type: ignore[attr-defined]
        before_snapshot = item.before_snapshot  # type: ignore[attr-defined]
        intended_snapshot = item.intended_snapshot  # type: ignore[attr-defined]
        typer.echo(
            f"  [{item.subject_id or '-'}] {item.title}\n"  # type: ignore[attr-defined]
            f"    {item.bgm_url or '-'}\n"  # type: ignore[attr-defined]
            f"    fields=[{fields}] before=[{before}] after=[{after}] "
            f"public=[{public}] reason={reason}"
        )
        if fields != "-" and before_snapshot is not None:
            changes = []
            for field in item.changed_fields:  # type: ignore[attr-defined]
                old = before_snapshot.value_for(field)
                new = intended_snapshot.value_for(field) if intended_snapshot is not None else None
                changes.append(
                    f"{field}: {json.dumps(old, ensure_ascii=False)} -> "
                    f"{json.dumps(new, ensure_ascii=False)}"
                )
            typer.echo(f"    changes: {'; '.join(changes)}")
        field_statuses = item.selection_evidence.get("field_statuses", {})  # type: ignore[attr-defined]
        if field_statuses:
            typer.echo(
                "    field basis: "
                + ", ".join(f"{field}={status}" for field, status in field_statuses.items())
            )
        steam_app_id = item.selection_evidence.get("steam_app_id")  # type: ignore[attr-defined]
        if steam_app_id:
            collections = item.selection_evidence.get("steam_collections", [])  # type: ignore[attr-defined]
            operation = item.action.get("operation", "none")  # type: ignore[attr-defined]
            typer.echo(
                f"    steam_appid={steam_app_id} collections={collections} operation={operation}"
            )
        match_candidates = item.selection_evidence.get("match_candidates", [])  # type: ignore[attr-defined]
        if match_candidates:
            selected = item.selection_evidence.get("selected_subject_id")  # type: ignore[attr-defined]
            score = item.selection_evidence.get("selected_score")  # type: ignore[attr-defined]
            margin = item.selection_evidence.get("top_margin")  # type: ignore[attr-defined]
            mode = item.selection_evidence.get("review_mode")  # type: ignore[attr-defined]
            typer.echo(
                f"    match selected={selected or '-'} score={score if score is not None else '-'} "
                f"margin={margin if margin is not None else '-'} review={mode or '-'}"
            )
            for candidate in match_candidates:
                aliases = ", ".join(candidate.get("aliases", [])) or "-"
                reasons = ", ".join(candidate.get("reasons", [])) or "-"
                typer.echo(
                    f"      [{candidate.get('subject_id')}] {candidate.get('title')} / "
                    f"{candidate.get('title_original')} score={candidate.get('score')} "
                    f"date={candidate.get('release_date') or '-'} aliases=[{aliases}] "
                    f"reasons=[{reasons}]"
                )


def _print_stored_plan(stored: StoredPlan) -> None:
    typer.echo(
        f"Plan: {stored.plan.id}\nStatus: {stored.plan.status}\n"
        f"Kind: {stored.plan.kind}\nContent hash: {stored.plan.content_hash}"
    )
    if stored.plan.kind == "steam_match":
        selector = json.loads(stored.plan.selector_json)
        typer.echo(
            "Batch selection: "
            f"eligible={selector.get('eligible_count', len(stored.candidates))}, "
            f"offset={selector.get('batch_offset', 0)}, "
            f"evaluated={len(stored.candidates)}"
        )
    planned_title = "将建立本地匹配/处置" if stored.plan.kind == "steam_match" else "将修改"
    unchanged_title = (
        "需人工审核/本次不修改" if stored.plan.kind == "steam_match" else "本次不修改"
    )
    _print_candidates(planned_title, stored.planned)
    _print_candidates(unchanged_title, stored.unchanged)
    planned_label = "将本地处置" if stored.plan.kind == "steam_match" else "将修改"
    typer.echo(
        f"\n汇总: {planned_label}={len(stored.planned)}, "
        f"不修改={len(stored.unchanged)}, 总计={len(stored.candidates)}"
    )


def _confirm_exact_plan_id(
    plan_id: str,
    *,
    non_interactive: bool,
    confirm_plan_id: str | None,
    action: str,
) -> None:
    if non_interactive:
        if confirm_plan_id != plan_id:
            raise PlanError(
                f"Non-interactive {action} requires --confirm-plan-id with the exact plan ID."
            )
        return
    entered = typer.prompt(f"Type the full plan ID to {action}")
    if entered != plan_id:
        raise PlanError("Plan ID confirmation did not match; no state was changed.")


def _generate_bulk_plan(
    *,
    operation: str,
    tag: str | None,
    old_tag: str | None,
    new_tag: str | None,
    ids: str | None,
    all_current: bool,
    public_tag: str | None,
    subject_type: str | None,
) -> None:
    settings = _settings_or_exit()
    try:
        selector = _selector(ids, all_current, public_tag)
        parsed_type = _parse_subject_type(subject_type)
        if parsed_type is not None:
            selector["subject_type"] = int(parsed_type)
        with BangumiClient(settings) as client:
            _verify_client_user(client)
            collections = client.get_collections(subject_type=parsed_type)
            with session_scope(settings.database_url) as session:
                stored = create_bulk_tag_plan(
                    session,
                    collections,
                    operation=operation,
                    selector=selector,
                    detail_loader=client.get_subject_public_tags,
                    tag=tag,
                    old_tag=old_tag,
                    new_tag=new_tag,
                )
        paths = export_plan(stored, settings.plan_directory)
    except (ConfigurationError, BangumiAPIError, PlanError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error. Run 'uv run alembic upgrade head' first.", err=True)
        raise typer.Exit(code=1) from None
    _print_stored_plan(stored)
    typer.echo(f"Exports: {paths[0]} | {paths[1]}")
    typer.echo("Remote writes performed: 0")


@tags_app.command("bulk-add")
def tags_bulk_add(
    tag: str = typer.Option(..., "--tag"),
    ids: str | None = typer.Option(None, "--ids"),
    all_current: bool = typer.Option(False, "--all-current"),
    public_tag: str | None = typer.Option(None, "--public-tag"),
    subject_type: str | None = typer.Option(
        None, "--subject-type", help="book/anime/music/game/real or 1/2/3/4/6; default all."
    ),
    plan_only: bool = typer.Option(False, "--plan", help="Compatibility flag; this command always plans."),
) -> None:
    """Generate an add-tag plan; never writes Bangumi."""
    _generate_bulk_plan(
        operation="add", tag=tag, old_tag=None, new_tag=None,
        ids=ids, all_current=all_current, public_tag=public_tag, subject_type=subject_type,
    )


@tags_app.command("bulk-remove")
def tags_bulk_remove(
    tag: str = typer.Option(..., "--tag"),
    ids: str | None = typer.Option(None, "--ids"),
    all_current: bool = typer.Option(False, "--all-current"),
    public_tag: str | None = typer.Option(None, "--public-tag"),
    subject_type: str | None = typer.Option(
        None, "--subject-type", help="book/anime/music/game/real or 1/2/3/4/6; default all."
    ),
    plan_only: bool = typer.Option(False, "--plan", help="Compatibility flag; this command always plans."),
) -> None:
    """Generate a remove-tag plan; never writes Bangumi."""
    _generate_bulk_plan(
        operation="remove", tag=tag, old_tag=None, new_tag=None,
        ids=ids, all_current=all_current, public_tag=public_tag, subject_type=subject_type,
    )


@tags_app.command("rename")
def tags_rename(
    old_tag: str = typer.Option(..., "--old-tag"),
    new_tag: str = typer.Option(..., "--new-tag"),
    ids: str | None = typer.Option(None, "--ids"),
    all_current: bool = typer.Option(False, "--all-current"),
    public_tag: str | None = typer.Option(None, "--public-tag"),
    subject_type: str | None = typer.Option(
        None, "--subject-type", help="book/anime/music/game/real or 1/2/3/4/6; default all."
    ),
    plan_only: bool = typer.Option(False, "--plan", help="Compatibility flag; this command always plans."),
) -> None:
    """Generate a one-PATCH rename plan; never writes Bangumi."""
    _generate_bulk_plan(
        operation="rename", tag=None, old_tag=old_tag, new_tag=new_tag,
        ids=ids, all_current=all_current, public_tag=public_tag, subject_type=subject_type,
    )


@tags_app.command("classify-games")
def tags_classify_games(
    public_tag: str = typer.Option("Galgame", "--public-tag"),
    galgame_tag: str = typer.Option(DEFAULT_GALGAME_CLASSIFICATION_TAG, "--galgame-tag"),
    game_tag: str = typer.Option(DEFAULT_GAME_CLASSIFICATION_TAG, "--game-tag"),
) -> None:
    """Plan Galgame tags only for entries lacking either classification marker."""
    settings = _settings_or_exit()
    try:
        with BangumiClient(settings) as client:
            _verify_client_user(client)
            collections = client.get_collections(subject_type=SubjectType.GAME)
            with session_scope(settings.database_url) as session:
                stored = create_classification_plan(
                    session,
                    collections,
                    public_tag=public_tag,
                    galgame_tag=galgame_tag,
                    game_tag=game_tag,
                    detail_loader=client.get_subject_public_tags,
                )
        paths = export_plan(stored, settings.plan_directory)
    except (ConfigurationError, BangumiAPIError, PlanError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error. Run 'uv run alembic upgrade head' first.", err=True)
        raise typer.Exit(code=1) from None
    _print_stored_plan(stored)
    typer.echo(f"Exports: {paths[0]} | {paths[1]}")
    typer.echo("Remote writes performed: 0")


@plan_app.command("show")
def plan_show(
    plan_id: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify and show both actionable and unchanged plan entries."""
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            stored = load_plan(session, plan_id)
            payload = plan_as_dict(stored)
    except (PlanError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_stored_plan(stored)


@plan_app.command("review")
def plan_review(
    plan_id: str,
    non_interactive: bool = typer.Option(False, "--non-interactive"),
    confirm_plan_id: str | None = typer.Option(None, "--confirm-plan-id"),
) -> None:
    """Explicitly mark an immutable draft plan as reviewed."""
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            stored = load_plan(session, plan_id)
        _print_stored_plan(stored)
        _confirm_exact_plan_id(
            plan_id,
            non_interactive=non_interactive,
            confirm_plan_id=confirm_plan_id,
            action="review",
        )
        with session_scope(settings.database_url) as session:
            stored = review_plan(session, plan_id)
        paths = export_plan(stored, settings.plan_directory)
    except (PlanError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Plan {plan_id} reviewed. Exports refreshed: {paths[0]} | {paths[1]}")


@plan_app.command("recovery")
def plan_recovery(plan_id: str) -> None:
    """Generate, but never apply, a recovery draft for audited uncertain writes."""
    settings = _settings_or_exit()
    try:
        with BangumiClient(settings) as client:
            _verify_client_user(client)
            collections = client.get_collections()
            with session_scope(settings.database_url) as session:
                stored = create_recovery_plan(session, plan_id, collections)
        paths = export_plan(stored, settings.plan_directory)
    except (ConfigurationError, BangumiAPIError, PlanError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error while generating recovery plan.", err=True)
        raise typer.Exit(code=1) from None
    _print_stored_plan(stored)
    typer.echo(f"Exports: {paths[0]} | {paths[1]}")
    typer.echo("Remote writes performed: 0")


@plan_app.command("apply")
def plan_apply(
    plan_id: str,
    non_interactive: bool = typer.Option(False, "--non-interactive"),
    confirm_plan_id: str | None = typer.Option(None, "--confirm-plan-id"),
) -> None:
    """Preflight, explicitly confirm, serially write, verify, and audit a reviewed plan."""
    settings = _settings_or_exit()
    local_match_apply = False
    local_pull_apply = False
    pull_media_result = None
    try:
        with session_scope(settings.database_url) as session:
            inspected = load_plan(session, plan_id)
            local_match_apply = inspected.plan.kind == "steam_match"
            local_pull_apply = inspected.plan.kind == "pull" and inspected.plan.format_version == 5
            pull_selector = json.loads(inspected.plan.selector_json)
        with BangumiClient(settings) as client:
            if local_match_apply:
                preflight = preflight_steam_match_plan(settings.database_url, client, plan_id)
            elif local_pull_apply:
                _verify_client_user(client)
                raw_type = pull_selector.get("subject_type")
                parsed_type = SubjectType(int(raw_type)) if raw_type is not None else None
                collections = client.get_collections(subject_type=parsed_type)
                preflight = preflight_pull_plan(settings.database_url, plan_id, collections)
            else:
                _verify_client_user(client)
                preflight = preflight_plan(settings.database_url, client, plan_id)
            _print_candidates("本次将修改（fresh preflight）", preflight.will_modify)
            _print_candidates("本次不修改/已变 stale", preflight.unchanged)
            typer.echo(
                f"\nPreflight 汇总: 将修改={len(preflight.will_modify)}, "
                f"不修改={len(preflight.unchanged)}, "
                f"总计={len(preflight.will_modify) + len(preflight.unchanged)}"
            )
            if not preflight.will_modify:
                raise PlanError("Preflight found no items that are safe to modify; no confirmation or write occurred.")
            _confirm_exact_plan_id(
                plan_id,
                non_interactive=non_interactive,
                confirm_plan_id=confirm_plan_id,
                action="apply",
            )
            if local_match_apply:
                result = apply_steam_match_plan(
                    settings.database_url,
                    client,
                    plan_id,
                    preflight,
                    backup_directory=settings.backup_directory,
                )
            elif local_pull_apply:
                result = apply_pull_plan(
                    settings.database_url,
                    plan_id,
                    preflight,
                    backup_directory=settings.backup_directory,
                )
            else:
                result = apply_reviewed_plan(
                    settings.database_url,
                    client,
                    plan_id,
                    preflight,
                    backup_directory=settings.backup_directory,
                    write_delay_seconds=settings.bangumi_write_delay_ms / 1000,
                    max_retries=settings.bangumi_max_retries,
                    retry_base_seconds=settings.bangumi_retry_base_seconds,
                )
        if local_pull_apply:
            applied_subjects = applied_pull_subject_ids(settings.database_url, plan_id)
            pull_media_result = materialize_pull_media(
                settings.database_url,
                [item for item in collections if item.subject_id in applied_subjects],
                policy=str(pull_selector.get("image_policy", "metadata")),
                cache_directory=settings.media_cache_directory,
                max_bytes=settings.image_max_item_bytes,
                timeout_seconds=settings.bangumi_request_timeout_seconds,
            )
        reverse_exports: tuple[object, object] | None = None
        if result.reverse_plan_id is not None:
            with session_scope(settings.database_url) as session:
                reverse = load_plan(session, result.reverse_plan_id)
            reverse_exports = export_plan(reverse, settings.plan_directory)
        followup_exports: tuple[object, object] | None = None
        if result.followup_tag_plan_id is not None:
            with session_scope(settings.database_url) as session:
                followup = load_plan(session, result.followup_tag_plan_id)
            followup_exports = export_plan(followup, settings.plan_directory)
    except (ConfigurationError, BangumiAPIError, PlanError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error while applying plan.", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"Apply complete: status={result.status}, applied={result.applied}, "
        f"stale={result.stale}, failed={result.failed}, pending={result.pending}."
    )
    typer.echo(f"Backup: {result.backup_path}")
    if result.reverse_plan_id is not None:
        typer.echo(f"Reverse draft: {result.reverse_plan_id}")
        if reverse_exports is not None:
            typer.echo(f"Reverse exports: {reverse_exports[0]} | {reverse_exports[1]}")
    if result.followup_tag_plan_id is not None:
        typer.echo(f"Independent follow-up Tag draft: {result.followup_tag_plan_id}")
        if followup_exports is not None:
            typer.echo(f"Tag draft exports: {followup_exports[0]} | {followup_exports[1]}")
    if result.followup_tag_error is not None:
        typer.echo(f"Follow-up Tag draft was not created: {result.followup_tag_error}")
    if local_match_apply:
        typer.echo("Bangumi writes performed: 0 (local mapping changes only)")
    if local_pull_apply:
        typer.echo("Bangumi writes performed: 0 (verified local pull merge only)")
        if pull_media_result is not None:
            typer.echo(
                "Pull media: "
                f"registered={pull_media_result.registered}, "
                f"downloaded={pull_media_result.downloaded}, "
                f"reused={pull_media_result.reused}, failed={pull_media_result.failed}."
            )


@plan_app.command("reconcile-manual-uncollect")
def plan_reconcile_manual_uncollect(
    plan_id: str,
    non_interactive: bool = typer.Option(False, "--non-interactive"),
    confirm_plan_id: str | None = typer.Option(None, "--confirm-plan-id"),
) -> None:
    """Verify a website uncollect, back up SQLite, then align LOCAL and shadow."""
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            stored = load_plan(session, plan_id)
        _print_stored_plan(stored)
        _confirm_exact_plan_id(
            plan_id,
            non_interactive=non_interactive,
            confirm_plan_id=confirm_plan_id,
            action="reconcile a manual Bangumi uncollect",
        )
        with BangumiClient(settings) as client:
            _verify_client_user(client)
            result = reconcile_manual_uncollect(
                settings.database_url,
                client,
                plan_id,
                backup_directory=settings.backup_directory,
            )
        restore_exports: tuple[object, object] | None = None
        if result.restore_plan_id is not None:
            with session_scope(settings.database_url) as session:
                restore = load_plan(session, result.restore_plan_id)
            restore_exports = export_plan(restore, settings.plan_directory)
    except (ConfigurationError, BangumiAPIError, PlanError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error while reconciling manual uncollect.", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"Manual uncollect reconciled: plan={result.plan_id}, "
        f"items={result.reconciled}, run={result.run_id}."
    )
    typer.echo(f"Backup: {result.backup_path}")
    if result.restore_plan_id is not None:
        typer.echo(f"Independent POST restore draft: {result.restore_plan_id}")
        if restore_exports is not None:
            typer.echo(f"Restore exports: {restore_exports[0]} | {restore_exports[1]}")
    typer.echo("Bangumi writes performed by this command: 0")


@collection_app.command("edit")
def collection_edit(
    subject_id: int = typer.Argument(..., min=1),
    rating: int | None = typer.Option(None, "--rating", min=1, max=10),
    clear_rating: bool = typer.Option(False, "--clear-rating"),
    status: str | None = typer.Option(None, "--status"),
    comment: str | None = typer.Option(None, "--comment"),
    clear_comment: bool = typer.Option(False, "--clear-comment"),
    private: bool | None = typer.Option(None, "--private/--public"),
) -> None:
    """Edit LOCAL collection fields only; never contacts Bangumi."""
    if rating is not None and clear_rating:
        raise typer.BadParameter("Choose --rating or --clear-rating, not both.")
    if comment is not None and clear_comment:
        raise typer.BadParameter("Choose --comment or --clear-comment, not both.")

    values: dict[str, object] = {}
    if rating is not None:
        values["rate"] = rating
    elif clear_rating:
        values["rate"] = 0
    if status is not None:
        values["type"] = int(_parse_collection_status(status))
    if comment is not None:
        values["comment"] = comment
    elif clear_comment:
        values["comment"] = ""
    if private is not None:
        values["private"] = private

    settings = _settings_or_exit()
    try:
        patch = CollectionPatch(values)
        with session_scope(settings.database_url) as session:
            result = edit_local_collection(session, subject_id, patch)
    except (LookupError, MutationValidationError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error. Run 'uv run alembic upgrade head' first.", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"LOCAL edit complete for subject {subject_id}.")
    typer.echo(f"Changed fields: {','.join(result.changed_fields) or '- (no-op)'}")
    typer.echo(f"Before: {result.before.to_json()}")
    typer.echo(f"After:  {result.after.to_json()}")
    typer.echo("Bangumi requests performed: 0")


@sync_app.command("plan")
def sync_plan(
    subject_ids: str | None = typer.Option(
        None, "--subject-id", help="Comma-separated Bangumi subject IDs."
    ),
    all_local_changes: bool = typer.Option(False, "--all-local-changes"),
    fields: str = typer.Option(
        ",".join(CANONICAL_FIELDS),
        "--fields",
        help="Comma-separated subset of rate,type,comment,private,tags.",
    ),
) -> None:
    """Fresh-read remote state and generate an immutable v2 sync plan."""
    if (subject_ids is None) == (not all_local_changes):
        raise typer.BadParameter(
            "Choose exactly one of --subject-id or --all-local-changes."
        )
    selected_fields = tuple(
        dict.fromkeys(item.strip() for item in fields.split(",") if item.strip())
    )
    unknown = set(selected_fields) - set(CANONICAL_FIELDS)
    if not selected_fields or unknown:
        raise typer.BadParameter(
            "--fields must contain only rate,type,comment,private,tags"
        )
    selector: dict[str, object]
    if subject_ids is not None:
        selector = {"mode": "ids", "ids": list(_parse_subject_ids(subject_ids))}
    else:
        selector = {"mode": "all_local_changes"}

    settings = _settings_or_exit()
    try:
        collections = _fetch_remote_collections(settings)
        with session_scope(settings.database_url) as session:
            stored = create_sync_plan(
                session,
                collections,
                selector=selector,
                fields=selected_fields,
            )
        paths = export_plan(stored, settings.plan_directory)
    except (ConfigurationError, BangumiAPIError, PlanError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error while generating sync plan.", err=True)
        raise typer.Exit(code=1) from None

    _print_stored_plan(stored)
    typer.echo(f"Exports: {paths[0]} | {paths[1]}")
    typer.echo("Remote writes performed: 0")


@sync_app.command("pull-plan")
def sync_pull_plan(
    subject_type: str | None = typer.Option(
        None, "--subject-type", help="book/anime/music/game/real or 1/2/3/4/6; default all."
    ),
    image_policy: str = typer.Option(
        "metadata",
        "--image-policy",
        help="none, metadata, missing, or refresh. Images are never fetched while planning.",
    ),
) -> None:
    """Fresh-read Bangumi and generate an immutable local-only v5 pull plan."""

    parsed_type = _parse_subject_type(subject_type)
    settings = _settings_or_exit()
    try:
        collections = _fetch_remote_collections(settings, parsed_type)
        with session_scope(settings.database_url) as session:
            stored = create_pull_plan(
                session,
                collections,
                subject_type=parsed_type,
                image_policy=image_policy,
            )
        paths = export_plan(stored, settings.plan_directory)
    except (ConfigurationError, BangumiAPIError, PlanError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error while generating pull plan.", err=True)
        raise typer.Exit(code=1) from None
    _print_stored_plan(stored)
    typer.echo(f"Exports: {paths[0]} | {paths[1]}")
    typer.echo("LOCAL writes performed: immutable plan only; Bangumi writes performed: 0")


def _print_rating_item(item: object) -> None:
    snapshot = json.loads(getattr(item, "subject_snapshot_json"))
    typer.echo(
        f"[{getattr(item, 'subject_id')}] {snapshot.get('title') or '-'} "
        f"position={getattr(item, 'position')} status={getattr(item, 'item_status')} "
        f"outcome={getattr(item, 'outcome') or '-'}"
    )
    typer.echo(f"  {snapshot.get('bgm_url') or '-'}")
    typer.echo(
        f"  type={snapshot.get('subject_type')} date={snapshot.get('release_date') or '-'} "
        f"tags={snapshot.get('public_tags') or []} enrichment={getattr(item, 'enrichment_status')}"
    )


@rating_queue_app.command("create")
def rating_queue_create(
    subject_type: str = typer.Option(
        "book,anime,music,game,real", "--subject-type",
        help="Comma-separated subject types.",
    ),
    collection_status: str = typer.Option(
        "done,doing,on-hold,dropped", "--collection-status",
        help="Comma-separated collection statuses.",
    ),
    include_deferred: bool = typer.Option(False, "--include-deferred"),
    order_name: str = typer.Option("recently-updated", "--order"),
    seed: int | None = typer.Option(None, "--seed"),
    max_items: int | None = typer.Option(None, "--max-items", min=1),
    allow_network: bool = typer.Option(False, "--allow-network"),
) -> None:
    """Create an immutable-membership rating queue; never writes Bangumi."""
    if order_name not in RATING_ORDERS:
        raise typer.BadParameter(f"--order must be one of {sorted(RATING_ORDERS)}")
    types = _parse_subject_types(subject_type)
    statuses = _parse_collection_statuses(collection_status)
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            seeds = prepare_rating_queue(
                session,
                subject_types=tuple(int(item) for item in types),
                collection_statuses=tuple(int(item) for item in statuses),
                include_deferred=include_deferred,
                order_name=order_name,
                random_seed=seed,
                max_items=max_items,
            )
        if allow_network and len(seeds) > 200:
            raise RatingQueueError(
                "Network enrichment is limited to 200 items; use filters or --max-items."
            )
        enrichments: dict[int, SubjectSearchCandidate] = {}
        errors: dict[int, str] = {}
        if allow_network:
            with BangumiClient(settings) as client:
                _verify_client_user(client)
                for index, item in enumerate(seeds):
                    try:
                        enrichments[item.subject_id] = client.get_subject(item.subject_id)
                    except BangumiAPIError as exc:
                        errors[item.subject_id] = str(exc)
                    if index + 1 < len(seeds):
                        time.sleep(0.25)
        selector = {
            "subject_types": [item.kind for item in types],
            "collection_statuses": [item.label for item in statuses],
            "include_deferred": include_deferred,
            "max_items": max_items,
            "allow_network": allow_network,
        }
        with session_scope(settings.database_url) as session:
            view = create_rating_queue(
                session,
                seeds,
                selector=selector,
                order_name=order_name,
                random_seed=seed,
                enrichments=enrichments,
                enrichment_errors=errors,
            )
    except (RatingQueueError, ConfigurationError, BangumiAPIError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error while creating rating queue.", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Rating queue: {view.session.id}")
    typer.echo(f"Status: {view.session.status}")
    typer.echo(f"Items: {len(view.items)}")
    typer.echo(f"Order: {view.session.order_name} seed={view.session.random_seed or '-'}")
    typer.echo(f"Fresh enrichment: {sum(item.enrichment_status == 'fresh' for item in view.items)}")
    typer.echo("Bangumi writes performed: 0")


@rating_queue_app.command("list")
def rating_queue_list() -> None:
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            queues = list_rating_queues(session)
    except SQLAlchemyError:
        typer.echo("Database error while listing rating queues.", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("session_id\tstatus\titems\tcursor\torder")
    for queue in queues:
        typer.echo(
            f"{queue.id}\t{queue.status}\t{queue.item_count}\t"
            f"{queue.cursor_position}\t{queue.order_name}"
        )


@rating_queue_app.command("show")
def rating_queue_show(session_id: str) -> None:
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            view = load_rating_queue(session, session_id)
            counts = rating_queue_counts(session, session_id)
    except (RatingQueueError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Rating queue: {view.session.id} status={view.session.status}")
    typer.echo(" ".join(f"{key}={value}" for key, value in counts.items()))
    for item in view.items:
        _print_rating_item(item)


@rating_queue_app.command("next")
def rating_queue_next(session_id: str) -> None:
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            item = next_rating_item(session, session_id)
    except (RatingQueueError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    if item is None:
        typer.echo("Rating queue complete.")
        return
    _print_rating_item(item)


@rating_queue_app.command("rate")
def rating_queue_rate(
    session_id: str,
    subject_id: int = typer.Argument(..., min=1),
    score: int = typer.Option(..., "--score", min=1, max=10),
    reason: str | None = typer.Option(None, "--reason"),
    skip_reason: bool = typer.Option(False, "--skip-reason"),
    publish_reason: bool = typer.Option(False, "--publish-reason"),
    public_comment: str | None = typer.Option(None, "--public-comment"),
    replace_existing_comment: bool = typer.Option(False, "--replace-existing-comment"),
) -> None:
    """Save a rating locally and advance the queue; never contacts Bangumi."""
    settings = _settings_or_exit()
    stale_error: str | None = None
    try:
        with session_scope(settings.database_url) as session:
            try:
                rate_rating_item(
                    session, session_id, subject_id, score=score, reason=reason,
                    skip_reason=skip_reason, publish_reason=publish_reason,
                    public_comment=public_comment,
                    replace_existing_comment=replace_existing_comment,
                )
            except RatingQueueStale as exc:
                # The stale marker is useful durable state, so commit it before exiting.
                stale_error = str(exc)
    except (RatingQueueError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    if stale_error is not None:
        typer.echo(stale_error, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"LOCAL rating saved: subject={subject_id} score={score}")
    typer.echo("Bangumi requests performed: 0")


def _rating_disposition(session_id: str, subject_id: int, decision: str, reason: str | None) -> None:
    settings = _settings_or_exit()
    stale_error: str | None = None
    try:
        with session_scope(settings.database_url) as session:
            try:
                set_rating_disposition(
                    session, session_id, subject_id, decision=decision, reason=reason
                )
            except RatingQueueStale as exc:
                stale_error = str(exc)
    except (RatingQueueError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    if stale_error is not None:
        typer.echo(stale_error, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Rating review decision saved: subject={subject_id} decision={decision}")
    typer.echo("Bangumi requests performed: 0")


@rating_queue_app.command("skip")
def rating_queue_skip(
    session_id: str, subject_id: int = typer.Argument(..., min=1),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    _rating_disposition(session_id, subject_id, "skipped", reason)


@rating_queue_app.command("defer")
def rating_queue_defer(
    session_id: str, subject_id: int = typer.Argument(..., min=1),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    _rating_disposition(session_id, subject_id, "deferred", reason)


@rating_app.command("reopen")
def rating_reopen(
    subject_id: int = typer.Argument(..., min=1),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            state = reopen_rating_subject(session, subject_id, reason)
    except (RatingQueueError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Rating review reopened: subject={state.subject_id} state={state.state}")


@rating_app.command("sync-plan")
def rating_sync_plan(session_id: str) -> None:
    """Generate a v2 rate/comment plan for locally rated queue items."""
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            subject_ids = rated_subject_ids(session, session_id)
        if not subject_ids:
            raise RatingQueueError("Rating queue has no rated items to synchronize.")
        collections = _fetch_remote_collections(settings)
        with session_scope(settings.database_url) as session:
            stored = create_sync_plan(
                session,
                collections,
                selector={"mode": "ids", "ids": list(subject_ids), "rating_session_id": session_id},
                fields=("rate", "comment"),
            )
        paths = export_plan(stored, settings.plan_directory)
    except (RatingQueueError, ConfigurationError, BangumiAPIError, PlanError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error while generating rating sync plan.", err=True)
        raise typer.Exit(code=1) from None
    _print_stored_plan(stored)
    typer.echo(f"Exports: {paths[0]} | {paths[1]}")
    typer.echo("Remote writes performed: 0")


def _print_discovery_candidate(candidate: object) -> None:
    evidence = json.loads(getattr(candidate, "evidence_json"))
    typer.echo(
        f"[{getattr(candidate, 'id')}] {getattr(candidate, 'title')} "
        f"priority={getattr(candidate, 'priority_score')} "
        f"status={getattr(candidate, 'item_status')} "
        f"decision={getattr(candidate, 'decision') or '-'}"
    )
    typer.echo(
        f"  key={getattr(candidate, 'candidate_key')} "
        f"subject={getattr(candidate, 'subject_id') or '-'} "
        f"work={getattr(candidate, 'work_id') or '-'} "
        f"library_entry={getattr(candidate, 'library_entry_id') or '-'}"
    )
    typer.echo(f"  evidence={stable_json(evidence)}")


def _print_discovery_session(view: object) -> None:
    row = getattr(view, "session")
    candidates = getattr(view, "candidates")
    typer.echo(
        f"Discovery session: {row.id} provider={row.provider} "
        f"status={row.status} items={row.item_count} cursor={row.cursor_position}"
    )
    for candidate in candidates:
        _print_discovery_candidate(candidate)


@discovery_session_app.command("create-steam")
def discovery_create_steam(
    account_id: str | None = typer.Option(None, "--account-id"),
    include_owned_unplayed: bool = typer.Option(False, "--include-owned-unplayed"),
    include_decided: bool = typer.Option(False, "--include-decided", hidden=True),
    max_items: int = typer.Option(50, "--max-items", min=1, max=200),
) -> None:
    """Create a local-only discovery session from bounded Steam evidence."""
    settings = _settings_or_exit()
    filters = {
        "account_id": account_id,
        "include_owned_unplayed": include_owned_unplayed,
        "include_decided": include_decided,
        "max_items": max_items,
    }
    try:
        with session_scope(settings.database_url) as session:
            seeds = steam_discovery_seeds(
                session, account_id=account_id,
                include_owned_unplayed=include_owned_unplayed,
                include_decided=include_decided, max_items=max_items,
            )
            view = create_discovery_session(
                session, provider="steam", filters=filters, seeds=seeds
            )
    except (DiscoveryError, SteamLibraryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _print_discovery_session(view)
    typer.echo("Bangumi requests performed: 0; writes performed: 0")


def _failed_discovery(
    settings: Settings, provider: str, filters: dict[str, object], error: str
) -> str:
    with session_scope(settings.database_url) as session:
        row = create_failed_discovery_session(
            session, provider=provider, filters=filters, error=error
        )
        return row.id


@discovery_session_app.command("create-search")
def discovery_create_search(
    query: str = typer.Option(..., "--query"),
    public_tag: list[str] | None = typer.Option(None, "--public-tag"),
    year_from: int | None = typer.Option(None, "--year-from", min=1, max=9999),
    year_to: int | None = typer.Option(None, "--year-to", min=1, max=9999),
    min_rating_count: int | None = typer.Option(None, "--min-rating-count", min=0),
    sort: str = typer.Option("match", "--sort"),
    max_items: int = typer.Option(50, "--max-items", min=1, max=200),
    allow_network: bool = typer.Option(False, "--allow-network"),
    include_decided: bool = typer.Option(False, "--include-decided", hidden=True),
) -> None:
    if not allow_network:
        raise typer.BadParameter("Bangumi discovery search requires --allow-network.")
    if not query.strip():
        raise typer.BadParameter("--query cannot be blank.")
    if sort not in {"match", "heat", "rank", "score"}:
        raise typer.BadParameter("--sort must be match, heat, rank, or score.")
    if year_from is not None and year_to is not None and year_from > year_to:
        raise typer.BadParameter("--year-from cannot be greater than --year-to.")
    public_tags = tuple(item.strip() for item in (public_tag or ()) if item.strip())
    if public_tag and len(public_tags) != len(public_tag):
        raise typer.BadParameter("--public-tag cannot be blank.")
    settings = _settings_or_exit()
    filters: dict[str, object] = {
        "query": query.strip(), "public_tags": list(public_tags),
        "year_from": year_from, "year_to": year_to,
        "min_rating_count": min_rating_count, "sort": sort,
        "max_items": max_items, "include_nsfw": False,
        "include_decided": include_decided,
    }
    try:
        with BangumiClient(settings) as client:
            _verify_client_user(client)
            candidates = client.search_subjects_filtered(
                query, subject_type=SubjectType.GAME, limit=max_items, sort=sort,
                meta_tags=public_tags, year_from=year_from,
                year_to=year_to, min_rating_count=min_rating_count,
                include_nsfw=False,
            )
        with session_scope(settings.database_url) as session:
            seeds = bangumi_discovery_seeds(
                session, candidates, provider="bangumi_search",
                include_decided=include_decided,
            )
            view = create_discovery_session(
                session, provider="bangumi_search", filters=filters, seeds=seeds
            )
    except (ConfigurationError, BangumiAPIError, ValueError, SQLAlchemyError) as exc:
        try:
            failed_id = _failed_discovery(settings, "bangumi_search", filters, str(exc))
            typer.echo(f"Failed discovery session: {failed_id}", err=True)
        except SQLAlchemyError:
            pass
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _print_discovery_session(view)
    typer.echo("Bangumi writes performed: 0")


@discovery_session_app.command("create-browse")
def discovery_create_browse(
    year: int | None = typer.Option(None, "--year", min=1, max=9999),
    platform: str | None = typer.Option(None, "--platform"),
    sort: str = typer.Option("rank", "--sort"),
    max_items: int = typer.Option(50, "--max-items", min=1, max=200),
    allow_network: bool = typer.Option(False, "--allow-network"),
    include_decided: bool = typer.Option(False, "--include-decided", hidden=True),
) -> None:
    if not allow_network:
        raise typer.BadParameter("Bangumi discovery browse requires --allow-network.")
    if year is None and not (platform and platform.strip()):
        raise typer.BadParameter("Choose --year, --platform, or both.")
    if sort not in {"date", "rank"}:
        raise typer.BadParameter("--sort must be date or rank.")
    settings = _settings_or_exit()
    filters: dict[str, object] = {
        "year": year, "platform": platform.strip() if platform else None,
        "sort": sort, "max_items": max_items, "include_nsfw": False,
        "include_decided": include_decided,
    }
    try:
        with BangumiClient(settings) as client:
            _verify_client_user(client)
            candidates = client.browse_game_subjects(
                year=year, platform=platform, sort=sort, limit=max_items
            )
        with session_scope(settings.database_url) as session:
            seeds = bangumi_discovery_seeds(
                session, candidates, provider="bangumi_browse",
                include_decided=include_decided,
            )
            view = create_discovery_session(
                session, provider="bangumi_browse", filters=filters, seeds=seeds
            )
    except (ConfigurationError, BangumiAPIError, ValueError, SQLAlchemyError) as exc:
        try:
            failed_id = _failed_discovery(settings, "bangumi_browse", filters, str(exc))
            typer.echo(f"Failed discovery session: {failed_id}", err=True)
        except SQLAlchemyError:
            pass
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _print_discovery_session(view)
    typer.echo("Bangumi writes performed: 0")


@discovery_session_app.command("list")
def discovery_session_list() -> None:
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            rows = list_discovery_sessions(session)
    except SQLAlchemyError:
        typer.echo("Database error while listing discovery sessions.", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("session_id\tprovider\tstatus\titems\tcursor")
    for row in rows:
        typer.echo(f"{row.id}\t{row.provider}\t{row.status}\t{row.item_count}\t{row.cursor_position}")


@discovery_session_app.command("show")
def discovery_session_show(session_id: str) -> None:
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            view = load_discovery_session(session, session_id)
    except (DiscoveryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _print_discovery_session(view)


@discovery_session_app.command("next")
def discovery_session_next(session_id: str) -> None:
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            candidate = next_discovery_candidate(session, session_id)
    except (DiscoveryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    if candidate is None:
        typer.echo("Discovery session complete.")
        return
    _print_discovery_candidate(candidate)


@discovery_app.command("decide")
def discovery_decide(
    session_id: str,
    candidate_id: str,
    decision: str = typer.Option(..., "--decision"),
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    normalized_decision = decision.replace("-", "_")
    if normalized_decision not in DISCOVERY_DECISIONS:
        choices = [item.replace("_", "-") for item in sorted(DISCOVERY_DECISIONS)]
        raise typer.BadParameter(f"--decision must be one of {choices}")
    settings = _settings_or_exit()
    conflict_error: str | None = None
    candidate = None
    try:
        with session_scope(settings.database_url) as session:
            try:
                candidate = decide_discovery_candidate(
                    session, session_id, candidate_id,
                    decision=normalized_decision, reason=reason,
                )
            except DiscoveryIdentityConflict as exc:
                # Preserve the diagnostic lifecycle state for the next reviewer.
                conflict_error = str(exc)
    except (DiscoveryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    if conflict_error is not None:
        typer.echo(conflict_error, err=True)
        raise typer.Exit(code=1)
    assert candidate is not None
    typer.echo(
        f"Discovery decision saved: {candidate.candidate_key} -> "
        f"{normalized_decision.replace('_', '-')}"
    )
    typer.echo("Bangumi requests performed: 0; writes performed: 0")


@discovery_app.command("reopen")
def discovery_reopen(
    candidate_key: str,
    reason: str | None = typer.Option(None, "--reason"),
) -> None:
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            state = reopen_discovery_candidate(session, candidate_key, reason=reason)
    except (DiscoveryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Discovery decision reopened: {state.candidate_key}")


@discovery_app.command("promotion-preview")
def discovery_promotion_preview(candidate_id: str) -> None:
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            preview = promotion_preview(session, candidate_id)
    except (DiscoveryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Candidate: {preview.candidate_id}")
    typer.echo(f"Promotion status: {preview.status}")
    typer.echo(f"work={preview.work_id or '-'} subject={preview.subject_id or '-'} library_entry={preview.library_entry_id or '-'}")
    typer.echo(preview.detail)
    typer.echo("Mutations performed: 0")


@steam_app.command("detect")
def steam_detect() -> None:
    """Detect the Steam root, account, and available read-only sources."""
    settings = _settings_or_exit()
    try:
        detected = detect_steam(settings)
    except SteamDataError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Steam root: {detected.root}")
    typer.echo(f"Accounts detected: {len(detected.account_ids)}")
    typer.echo("Selected account: configured/unique")
    typer.echo(f"Category source: {detected.category_source}")
    typer.echo(f"Cloud collection cache: {detected.category_file_available}")
    typer.echo(f"Legacy sharedconfig: {detected.legacy_file_available}")
    typer.echo(f"Local config: {detected.local_config_available}")
    typer.echo(f"Installed manifests: {detected.installed_manifest_count}")


@steam_app.command("import")
def steam_import_command(
    apply_local: bool = typer.Option(False, "--apply-local"),
    allow_network: bool = typer.Option(
        False,
        "--allow-network",
        help="Allow configured network metadata providers; local import remains authoritative for categories.",
    ),
) -> None:
    """Preview or apply a local-only Steam library import."""
    settings = _settings_or_exit()
    if allow_network:
        typer.echo(
            "Network metadata is explicitly enabled; local sources remain authoritative "
            "for custom categories."
        )
    try:
        snapshot = read_steam_snapshot(settings, allow_network=allow_network)
        with session_scope(settings.database_url) as session:
            summary = (
                apply_steam_import(session, snapshot)
                if apply_local
                else preview_steam_import(session, snapshot)
            )
    except (SteamDataError, SteamLibraryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Source: {summary.source_kind}")
    typer.echo(
        f"Entries: seen={summary.entries_seen} categorized={summary.categorized_entries} "
        f"manual-categorized={summary.manual_categorized_entries} "
        f"new={summary.new_entries} updated={summary.updated_entries}"
    )
    typer.echo(
        f"Collections: seen={summary.collections_seen} manual={summary.manual_collections_seen} "
        f"new={summary.new_collections} updated={summary.updated_collections}"
    )
    typer.echo(f"Membership changes: {summary.membership_changes}")
    typer.echo(f"LOCAL writes performed: {'yes' if summary.applied else '0 (dry-run)'}")
    typer.echo("Bangumi requests performed: 0")


@steam_app.command("collections")
def steam_collections() -> None:
    """List mirrored Steam collections without exposing account identifiers."""
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            items = list_steam_collections(session, settings.steam_account_id)
    except (SteamLibraryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo("name  kind  active  entries")
    for item in items:
        typer.echo(f"{item.name}  {item.kind}  {item.active}  {item.active_entries}")
    typer.echo(f"Total: {len(items)}")


@steam_titles_app.command("complete")
def steam_titles_complete(
    app_ids: str | None = typer.Option(None, "--appids"),
    all_missing: bool = typer.Option(False, "--all-missing"),
    max_items: int = typer.Option(250, "--max-items", min=1, max=250),
    request_delay_ms: int = typer.Option(250, "--request-delay-ms", min=0),
    allow_network: bool = typer.Option(False, "--allow-network"),
) -> None:
    """Fill Steam titles from public Store metadata without changing matches."""
    if (app_ids is None) == (not all_missing):
        raise typer.BadParameter("Choose exactly one of --appids or --all-missing.")
    if not allow_network:
        raise typer.BadParameter("Steam Store title completion requires --allow-network.")
    parsed = _parse_app_ids(app_ids) if app_ids is not None else None
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            prepared = prepare_title_completion(
                session,
                account_id=settings.steam_account_id,
                app_ids=parsed,
                all_missing=all_missing,
                max_items=max_items,
            )
        fetched = fetch_title_completion(
            prepared,
            timeout_seconds=settings.bangumi_request_timeout_seconds,
            request_delay_seconds=request_delay_ms / 1000,
            sleep_fn=time.sleep,
        )
        with session_scope(settings.database_url) as session:
            result = persist_title_completion(session, fetched)
    except (SteamTitleError, SteamLibraryError, SteamDataError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"Steam titles: selected={result.selected} updated={result.updated} "
        f"manual-preserved={result.preserved_manual} unavailable={result.unavailable} "
        f"stale={result.stale}"
    )
    for item in result.entries:
        typer.echo(
            f"  [{item['app_id']}] {item.get('title', '-')} status={item['status']}"
        )
    typer.echo("Bangumi requests performed: 0; Steam Store reads only.")


@steam_titles_app.command("set")
def steam_titles_set(
    app_id: str,
    title: str = typer.Option(..., "--title"),
) -> None:
    """Set a LOCAL manual title which future imports preserve."""
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            entry = set_manual_title(
                session,
                account_id=settings.steam_account_id,
                app_id=app_id,
                title=title,
            )
            displayed = entry.title_observed
    except (SteamTitleError, SteamLibraryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Steam AppID {app_id} LOCAL manual title: {displayed}")
    typer.echo("Network requests performed: 0")


@steam_titles_app.command("clear")
def steam_titles_clear(app_id: str) -> None:
    """Clear manual-title priority; the observed title is retained until refreshed."""
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            clear_manual_title(
                session, account_id=settings.steam_account_id, app_id=app_id
            )
    except (SteamTitleError, SteamLibraryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Steam AppID {app_id} manual-title priority cleared.")
    typer.echo("Network requests performed: 0")


@steam_app.command("list")
def steam_list(
    collection: str | None = typer.Option(None, "--collection"),
    collection_regex: str | None = typer.Option(None, "--collection-regex"),
    match_status: str | None = typer.Option(None, "--match-status"),
) -> None:
    """List imported Steam entries with category and matching state."""
    if collection is not None and collection_regex is not None:
        raise typer.BadParameter("Choose at most one of --collection or --collection-regex.")
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            items = list_steam_entries(
                session,
                account_id=settings.steam_account_id,
                collection_name=collection,
                collection_regex=collection_regex,
                match_status=match_status,
            )
    except (SteamLibraryError, ValueError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _print_steam_entries(items)
    typer.echo(f"Total: {len(items)}")


@steam_app.command("unmatched")
def steam_unmatched() -> None:
    """List Steam entries not currently confirmed to a Bangumi work."""
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            items = [
                item
                for item in list_steam_entries(
                    session, account_id=settings.steam_account_id
                )
                if item.match_status != "confirmed"
            ]
    except (SteamLibraryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _print_steam_entries(items)
    typer.echo(f"Total: {len(items)}")


@steam_app.command("status-plan")
def steam_status_plan(
    app_ids: str | None = typer.Option(None, "--appids"),
    all_eligible: bool = typer.Option(False, "--all-eligible"),
    remaining_status: str | None = typer.Option(None, "--remaining-status"),
    add_tag: str | None = typer.Option(None, "--add-tag"),
) -> None:
    """Generate an immutable v3 Bangumi collection status plan from Steam evidence."""
    if (app_ids is None) == (not all_eligible):
        raise typer.BadParameter("Choose exactly one of --appids or --all-eligible.")
    parsed_app_ids = _parse_app_ids(app_ids) if app_ids is not None else None
    parsed_remaining = (
        _parse_collection_status(remaining_status) if remaining_status is not None else None
    )
    settings = _settings_or_exit()
    try:
        rules = load_steam_rules(settings.steam_config)
        collections = _fetch_remote_collections(settings, SubjectType.GAME)
        with session_scope(settings.database_url) as session:
            stored = create_steam_status_plan(
                session,
                collections,
                app_ids=parsed_app_ids,
                all_eligible=all_eligible,
                account_id=settings.steam_account_id,
                configuration=rules,
                remaining_status=parsed_remaining,
                followup_tag=add_tag,
            )
        paths = export_plan(stored, settings.plan_directory)
    except (
        ConfigurationError,
        SteamConfigurationError,
        SteamLibraryError,
        BangumiAPIError,
        PlanError,
        ValueError,
        SQLAlchemyError,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _print_stored_plan(stored)
    typer.echo(f"Exports: {paths[0]} | {paths[1]}")
    if add_tag:
        typer.echo(
            f"Follow-up personal Tag '{add_tag}' is recorded but will only become a separate "
            "draft after status verification."
        )
    typer.echo("Remote writes performed: 0")


def _print_match_candidates(candidates: tuple[object, ...]) -> None:
    if not candidates:
        typer.echo("Candidates: 0")
        return
    typer.echo(f"Candidates: {len(candidates)}")
    for item in candidates:
        aliases = ", ".join(item.aliases) or "-"  # type: ignore[attr-defined]
        reasons = ", ".join(item.reasons)  # type: ignore[attr-defined]
        typer.echo(
            f"  [{item.subject_id}] {item.title} / {item.title_original}\n"  # type: ignore[attr-defined]
            f"    {item.url}\n"  # type: ignore[attr-defined]
            f"    date={item.release_date or '-'} score={item.score} "  # type: ignore[attr-defined]
            f"aliases=[{aliases}] reasons=[{reasons}]"
        )


@steam_match_app.command("search")
def steam_match_search(
    app_id: str,
    query: str | None = typer.Option(None, "--query"),
    allow_network: bool = typer.Option(False, "--allow-network"),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
) -> None:
    """Search and persist Bangumi game candidates for one Steam entry."""
    if not app_id.isdigit():
        raise typer.BadParameter("Steam AppID must be numeric.")
    if not allow_network:
        raise typer.BadParameter("Candidate search requires explicit --allow-network.")
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            prepared = prepare_match_search(
                session,
                app_id=app_id,
                account_id=settings.steam_account_id,
                query=query,
            )
        with BangumiClient(settings) as client:
            fetched = fetch_match_search(
                prepared,
                client,
                include_store_titles=True,
                timeout_seconds=settings.bangumi_request_timeout_seconds,
                limit=limit,
            )
        with session_scope(settings.database_url) as session:
            result = persist_match_search(session, fetched)
    except (SteamMatchError, SteamDataError, BangumiAPIError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Steam [{result.app_id}] {result.title}")
    typer.echo(f"Queries: {', '.join(result.queries)}")
    _print_match_candidates(result.candidates)
    typer.echo("Mapping changes performed: 0; run 'steam match confirm' explicitly.")


@steam_match_app.command("plan")
def steam_match_plan(
    app_ids: str | None = typer.Option(None, "--appids"),
    collection: str | None = typer.Option(None, "--collection"),
    collection_regex: str | None = typer.Option(None, "--collection-regex"),
    all_unmatched: bool = typer.Option(False, "--all-unmatched"),
    include_no_subject: bool = typer.Option(False, "--include-no-subject"),
    include_deferred: bool = typer.Option(False, "--include-deferred"),
    auto_threshold: int = typer.Option(95, "--auto-threshold", min=0, max=100),
    min_margin: int = typer.Option(20, "--min-margin", min=0, max=100),
    allow_nonexact: bool = typer.Option(False, "--allow-nonexact-auto"),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
    offset: int = typer.Option(0, "--offset", min=0),
    max_items: int = typer.Option(250, "--max-items", min=1, max=250),
    candidate_images: str = typer.Option("metadata", "--candidate-images"),
    request_delay_ms: int = typer.Option(250, "--request-delay-ms", min=0),
    allow_network: bool = typer.Option(False, "--allow-network"),
) -> None:
    """Build a reviewable v4 batch match plan with controlled automatic items."""
    if not allow_network:
        raise typer.BadParameter("Batch candidate search requires explicit --allow-network.")
    if candidate_images not in {"none", "metadata", "cache"}:
        raise typer.BadParameter("--candidate-images must be none, metadata, or cache.")
    choices = sum(
        (
            app_ids is not None,
            collection is not None,
            collection_regex is not None,
            all_unmatched,
        )
    )
    if choices != 1:
        raise typer.BadParameter(
            "Choose exactly one of --appids, --collection, --collection-regex, or --all-unmatched."
        )
    parsed_app_ids = _parse_app_ids(app_ids) if app_ids is not None else None
    settings = _settings_or_exit()
    try:
        policy = AutoMatchPolicy(
            score_threshold=auto_threshold,
            minimum_margin=min_margin,
            require_exact=not allow_nonexact,
        )
        with session_scope(settings.database_url) as session:
            prepared = prepare_steam_match_plan(
                session,
                account_id=settings.steam_account_id,
                app_ids=parsed_app_ids,
                collection_name=collection,
                collection_regex=collection_regex,
                all_unmatched=all_unmatched,
                include_no_subject=include_no_subject,
                include_deferred=include_deferred,
                candidate_image_policy=candidate_images,
                policy=policy,
                candidate_limit=limit,
                batch_offset=offset,
                max_items=max_items,
            )
        with BangumiClient(settings) as client:
            fetched = fetch_steam_match_plan(
                prepared,
                client,
                include_store_titles=True,
                timeout_seconds=settings.bangumi_request_timeout_seconds,
                request_delay_seconds=request_delay_ms / 1000,
                sleep_fn=time.sleep,
            )
        with session_scope(settings.database_url) as session:
            stored = persist_steam_match_plan(session, fetched)
            media_selection = register_match_candidate_media(
                session, stored, policy=candidate_images
            )
        cached_candidate_images = 0
        if candidate_images == "cache":
            for reference in media_selection.missing:
                try:
                    materialized = download_remote_media(
                        reference,
                        settings.media_cache_directory,
                        max_bytes=settings.image_max_item_bytes,
                        timeout_seconds=settings.bangumi_request_timeout_seconds,
                    )
                except MediaError as exc:
                    code = str(exc) if str(exc).startswith("media_") else "media_download_failed"
                    with session_scope(settings.database_url) as session:
                        mark_media_source_failure(
                            session,
                            reference,
                            failure_code=code[:32],
                            missing=code == "media_remote_missing",
                        )
                    continue
                with session_scope(settings.database_url) as session:
                    register_media_references(
                        session,
                        (reference,),
                        policy="cache",
                        cached={reference.key: materialized},
                    )
                cached_candidate_images += 1
        paths = export_plan(stored, settings.plan_directory)
    except (
        ConfigurationError,
        SteamMatchError,
        SteamLibraryError,
        SteamDataError,
        BangumiAPIError,
        PlanError,
        MediaError,
        SQLAlchemyError,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _print_stored_plan(stored)
    typer.echo(
        f"Candidate images: observed={media_selection.observed} "
        f"missing-downloaded={cached_candidate_images}"
    )
    typer.echo(f"Exports: {paths[0]} | {paths[1]}")
    typer.echo("Mapping changes performed: 0; Bangumi writes performed: 0")


@steam_match_app.command("revise")
def steam_match_revise(
    plan_id: str,
    app_id: str,
    subject_id: int | None = typer.Option(None, "--subject-id", min=1),
    manual_review: bool = typer.Option(False, "--manual-review"),
    no_subject: bool = typer.Option(False, "--no-subject"),
    defer: bool = typer.Option(False, "--defer"),
    allow_network: bool = typer.Option(False, "--allow-network"),
) -> None:
    """Create a successor draft with one reviewed match decision changed."""
    if not app_id.isdigit():
        raise typer.BadParameter("Steam AppID must be numeric.")
    if sum((subject_id is not None, manual_review, no_subject, defer)) != 1:
        raise typer.BadParameter(
            "Choose exactly one of --subject-id, --manual-review, --no-subject, or --defer."
        )
    if subject_id is not None and not allow_network:
        raise typer.BadParameter("A manual subject ID requires explicit --allow-network validation.")
    decision = (
        "subject"
        if subject_id is not None
        else "manual_review"
        if manual_review
        else "no_subject"
        if no_subject
        else "deferred"
    )
    settings = _settings_or_exit()
    try:
        if subject_id is not None:
            with BangumiClient(settings) as client:
                with session_scope(settings.database_url) as session:
                    stored = revise_steam_match_plan(
                        session,
                        client,
                        plan_id=plan_id,
                        app_id=app_id,
                        decision=decision,  # type: ignore[arg-type]
                        subject_id=subject_id,
                    )
        else:
            with session_scope(settings.database_url) as session:
                stored = revise_steam_match_plan(
                    session,
                    None,
                    plan_id=plan_id,
                    app_id=app_id,
                    decision=decision,  # type: ignore[arg-type]
                )
        paths = export_plan(stored, settings.plan_directory)
    except (
        ConfigurationError,
        SteamMatchError,
        SteamLibraryError,
        BangumiAPIError,
        PlanError,
        SQLAlchemyError,
    ) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    _print_stored_plan(stored)
    typer.echo(f"Successor draft: {stored.plan.id}")
    typer.echo(f"Superseded draft cancelled: {plan_id}")
    typer.echo(f"Exports: {paths[0]} | {paths[1]}")
    typer.echo("Mapping changes performed: 0; Bangumi writes performed: 0")


@steam_match_app.command("show")
def steam_match_show(app_id: str) -> None:
    """Show current match state and stored candidates for one Steam entry."""
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            entry, candidates = match_details(
                session, app_id=app_id, account_id=settings.steam_account_id
            )
    except (SteamMatchError, SteamLibraryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"Steam [{entry.external_id}] {entry.title_observed or '<unknown>'}\n"
        f"Match status: {entry.match_status}\nWork ID: {entry.work_id or '-'}"
    )
    _print_match_candidates(candidates)


@steam_match_app.command("confirm")
def steam_match_confirm(app_id: str, subject_id: int = typer.Option(..., "--subject-id", min=1)) -> None:
    """Manually confirm one Steam AppID to one Bangumi game subject."""
    settings = _settings_or_exit()
    try:
        with BangumiClient(settings) as client:
            with session_scope(settings.database_url) as session:
                entry, work = confirm_match(
                    session,
                    client,
                    app_id=app_id,
                    subject_id=subject_id,
                    account_id=settings.steam_account_id,
                )
    except (SteamMatchError, BangumiAPIError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"Confirmed Steam AppID {entry.external_id} -> Bangumi subject {subject_id} "
        f"(work {work.id})."
    )
    typer.echo("Bangumi writes performed: 0")


def _steam_match_disposition(app_id: str, decision: str, reason: str | None) -> None:
    if not app_id.isdigit():
        raise typer.BadParameter("Steam AppID must be numeric.")
    settings = _settings_or_exit()
    try:
        with session_scope(settings.database_url) as session:
            entry = set_match_disposition(
                session,
                app_id=app_id,
                account_id=settings.steam_account_id,
                decision=decision,
                reason=reason,
            )
    except (SteamMatchError, SteamLibraryError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Steam AppID {entry.external_id} match status: {entry.match_status}")
    typer.echo("Bangumi requests performed: 0")


@steam_match_app.command("no-subject")
def steam_match_no_subject(
    app_id: str, reason: str | None = typer.Option(None, "--reason")
) -> None:
    """Record an explicit manual decision that Bangumi has no matching subject."""
    _steam_match_disposition(app_id, "no_subject", reason)


@steam_match_app.command("defer")
def steam_match_defer(
    app_id: str, reason: str | None = typer.Option(None, "--reason")
) -> None:
    """Defer matching without treating a search failure as no-subject."""
    _steam_match_disposition(app_id, "deferred", reason)


@steam_match_app.command("reopen")
def steam_match_reopen(
    app_id: str, reason: str | None = typer.Option(None, "--reason")
) -> None:
    """Reopen a prior no-subject or deferred decision."""
    _steam_match_disposition(app_id, "reopened", reason)


@db_app.command("upgrade")
def database_upgrade() -> None:
    """Back up, manifest, migrate, and verify the configured SQLite database."""
    settings = _settings_or_exit()
    try:
        result = upgrade_database_safely(
            settings.database_url,
            settings.backup_directory,
        )
    except (MigrationSafetyError, SQLAlchemyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Database upgraded: {result.from_revision or '(unversioned)'} -> {result.to_revision}")
    typer.echo(f"Backup: {result.backup_path}")
    typer.echo(f"Manifest: {result.manifest_path}")
    typer.echo("Foreign-key violations: 0")


@app.command("auth-check")
def auth_check() -> None:
    """Validate the configured token and expected Bangumi username."""
    settings = _settings_or_exit()
    try:
        with BangumiClient(settings) as client:
            me = client.get_me()
        expected = settings.require_bangumi()[1]
        if me.username != expected:
            typer.echo(
                f"Authentication mismatch: token belongs to '{me.username}', expected '{expected}'.",
                err=True,
            )
            raise typer.Exit(code=1)
    except (ConfigurationError, BangumiAPIError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Authenticated as {me.username} ({me.nickname}, uid={me.id}).")


@app.command()
def pull(
    subject_type: str | None = typer.Option(
        None, "--subject-type", help="book/anime/music/game/real or 1/2/3/4/6; default is all."
    ),
) -> None:
    """Safely mirror remote Bangumi collections into local SQLite."""
    settings = _settings_or_exit()
    parsed_type = _parse_subject_type(subject_type)
    try:
        collections = _fetch_remote_collections(settings, parsed_type)
        with session_scope(settings.database_url) as session:
            result = pull_collections(
                session, collections, scope_subject_type=parsed_type
            )
    except (ConfigurationError, BangumiAPIError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error. Run 'uv run alembic upgrade head' first.", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(
        "Pull complete: "
        f"remote={result.remote_count}, imported={result.imported}, "
        f"bootstrapped={result.bootstrapped}, bootstrap-mismatch={result.bootstrap_mismatches}, "
        f"remote-updated={result.remote_updates}, unchanged={result.unchanged}, "
        f"local-preserved={result.local_changes_preserved}, conflicts={result.conflicts}, "
        f"new-conflict-records={result.conflict_records_created}."
    )
    typer.echo(
        "By subject type: "
        + ", ".join(
            f"{SubjectType(type_id).kind}={count}"
            for type_id, count in sorted(result.by_subject_type.items())
        )
    )
    if result.missing_remote:
        typer.echo(
            f"Warning: {result.missing_remote} local mirrored collection(s) were absent remotely; "
            "they were retained because Phase 1 never deletes records.",
            err=True,
        )


@app.command()
def status(
    refresh_remote: bool = typer.Option(
        False, "--refresh-remote", help="Fresh-read Bangumi before comparing."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    subject_id: int | None = typer.Option(None, "--subject-id", min=1),
    subject_type: str | None = typer.Option(None, "--subject-type"),
) -> None:
    """Show field-level BASE/LOCAL/REMOTE sync status without applying changes."""
    settings = _settings_or_exit()
    parsed_type = _parse_subject_type(subject_type)
    try:
        remote = _fetch_remote_collections(settings, parsed_type) if refresh_remote else None
        with session_scope(settings.database_url) as session:
            report = build_status_report(
                session, remote, subject_id=subject_id, subject_type=parsed_type
            )
    except (ConfigurationError, BangumiAPIError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error. Run 'uv run alembic upgrade head' first.", err=True)
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return

    typer.echo(f"Remote source: {report.remote_source}")
    for name in (
        "clean",
        "remote_changed",
        "local_changed",
        "converged",
        "conflict",
        "bootstrap_missing",
        "remote_missing",
    ):
        typer.echo(f"{name}: {report.counts[name]}")
    for item in report.items:
        if item.diff.status.value == "clean":
            continue
        typer.echo(
            f"[{item.diff.status.value}] {item.title} "
            f"(subject={item.subject_id}, fields={','.join(item.diff.changed_fields) or '-'})"
        )
        typer.echo(f"  {item.bgm_url}")
        for field in item.diff.fields:
            if field.status.value == "clean":
                continue
            typer.echo(
                f"  {field.field}: {field.status.value} "
                f"BASE={json.dumps(field.base, ensure_ascii=False)} "
                f"LOCAL={json.dumps(field.local, ensure_ascii=False)} "
                f"REMOTE={json.dumps(field.remote, ensure_ascii=False)}"
            )


@shadow_app.command("bootstrap")
def shadow_bootstrap(
    apply: bool = typer.Option(False, "--apply", help="Create eligible local shadows."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only (the default)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    subject_id: int | None = typer.Option(None, "--subject-id", min=1),
) -> None:
    """Safely bootstrap missing shadows after a fresh remote read."""
    if apply and dry_run:
        typer.echo("Choose either --apply or --dry-run, not both.", err=True)
        raise typer.Exit(code=2)
    settings = _settings_or_exit()
    try:
        remote = _fetch_remote_collections(settings)
        with session_scope(settings.database_url) as session:
            result = bootstrap_shadows(
                session,
                remote,
                apply=apply,
                subject_id=subject_id,
            )
    except (ConfigurationError, BangumiAPIError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except SQLAlchemyError:
        typer.echo("Database error. Run 'uv run alembic upgrade head' first.", err=True)
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    typer.echo(f"Shadow bootstrap mode: {'apply' if apply else 'dry-run'}")
    for name, count in result.counts.items():
        typer.echo(f"{name}: {count}")
    for item in result.items:
        if item.outcome not in ("bootstrap_mismatch", "remote_missing"):
            continue
        typer.echo(
            f"[{item.outcome}] {item.title} (subject={item.subject_id}, "
            f"fields={','.join(item.changed_fields) or '-'})"
        )


@app.command("list")
def list_works(
    subject_type: str | None = typer.Option(None, "--subject-type"),
) -> None:
    """List locally mirrored Bangumi collections."""
    settings = _settings_or_exit()
    parsed_type = _parse_subject_type(subject_type)
    try:
        with session_scope(settings.database_url) as session:
            items = list_collections(session, parsed_type)
    except SQLAlchemyError:
        typer.echo("Database error. Run 'uv run alembic upgrade head' first.", err=True)
        raise typer.Exit(code=1) from None

    typer.echo("type\ttitle\tstatus\trating\ttags\tbgm_url")
    for item in items:
        typer.echo(
            f"{item.kind}\t{item.title}\t{_status_label(item.status)}\t{item.rating or '-'}\t"
            f"{', '.join(item.tags) or '-'}\t{item.bgm_url}"
        )


@media_app.command("status")
def media_status_command() -> None:
    """Show local media metadata and content-addressed cache usage."""
    settings = _settings_or_exit()
    with session_scope(settings.database_url) as session:
        result = media_status(session)
    typer.echo(f"Sources: {result.source_count}")
    typer.echo(f"Cached sources: {result.cached_source_count}")
    typer.echo(f"Failed sources: {result.failed_source_count}")
    typer.echo(f"Missing sources: {result.missing_source_count}")
    typer.echo(f"Blobs: {result.blob_count}")
    typer.echo(f"Bytes: {result.total_bytes}")


@media_app.command("scan-steam")
def media_scan_steam(
    appids: str | None = typer.Option(None, "--appids", help="Comma-separated Steam AppIDs."),
    image_policy: str | None = typer.Option(
        None, "--image-policy", help="none, metadata, or cache."
    ),
) -> None:
    """Scan Steam's local library cache; never contacts Steam or Bangumi."""
    settings = _settings_or_exit()
    if settings.steam_root is None:
        typer.echo("BLD_STEAM_ROOT is required for an explicit local scan.", err=True)
        raise typer.Exit(code=2)
    try:
        policy = ImagePolicy.parse(image_policy or settings.image_policy)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--image-policy") from exc
    selected = (
        tuple(dict.fromkeys(item.strip() for item in appids.split(",") if item.strip()))
        if appids
        else None
    )
    if policy is ImagePolicy.NONE:
        typer.echo("Image policy is none; Steam files were not scanned.")
        return
    candidates = scan_steam_librarycache(settings.steam_root, app_ids=selected)
    cached = {}
    if policy is ImagePolicy.CACHE:
        for candidate in candidates:
            cached[candidate.reference.key] = cache_local_media(
                candidate,
                settings.media_cache_directory,
                max_bytes=settings.image_max_item_bytes,
            )
    with session_scope(settings.database_url) as session:
        result = register_media_references(
            session,
            (candidate.reference for candidate in candidates),
            policy=policy,
            cached=cached,
        )
        for candidate in candidates:
            source = session.query(MediaSource).filter_by(
                provider=candidate.reference.provider,
                external_id=candidate.reference.external_id,
                variant=candidate.reference.variant,
                locale=candidate.reference.locale,
                origin=candidate.reference.origin,
            ).one_or_none()
            if source is None:
                continue
            for entry in session.query(LibraryEntry).filter_by(
                external_id=candidate.reference.external_id
            ).all():
                bind_media_source(
                    session,
                    source.id,
                    library_entry_id=entry.id,
                    role=(
                        "cover"
                        if candidate.reference.variant in {"library_portrait", "library_capsule"}
                        else candidate.reference.variant
                    ),
                    priority=100 if candidate.reference.variant == "library_portrait" else 50,
                )
    typer.echo(
        f"Examined={result.examined} created={result.sources_created} "
        f"updated={result.sources_updated} cached={result.cached} skipped={result.skipped}"
    )
    typer.echo("Network requests performed: 0")


@media_app.command("verify")
def media_verify() -> None:
    """Verify every registered blob against its file size and SHA-256."""
    settings = _settings_or_exit()
    with session_scope(settings.database_url) as session:
        issues = verify_media_cache(session, settings.media_cache_directory)
    typer.echo(f"Issues: {len(issues)}")
    for issue in issues:
        typer.echo(f"{issue.sha256}\t{issue.code}")


@media_app.command("fetch")
def media_fetch(
    source_ids: str | None = typer.Option(
        None, "--source-ids", help="Comma-separated registered media source UUIDs."
    ),
    allow_network: bool = typer.Option(False, "--allow-network"),
    include_cached: bool = typer.Option(False, "--include-cached"),
) -> None:
    """Fetch up to 200 registered remote images into the portable cache."""
    if not allow_network:
        typer.echo("Explicit --allow-network permission is required.", err=True)
        raise typer.Exit(code=2)
    settings = _settings_or_exit()
    selected_ids = (
        tuple(dict.fromkeys(item.strip() for item in source_ids.split(",") if item.strip()))
        if source_ids
        else ()
    )
    with session_scope(settings.database_url) as session:
        query = session.query(MediaSource).filter(MediaSource.origin == "remote")
        if selected_ids:
            query = query.filter(MediaSource.id.in_(selected_ids))
        if not include_cached:
            query = query.filter(MediaSource.current_blob_sha256.is_(None))
        rows = query.order_by(MediaSource.id).limit(201).all()
        references = tuple(
            MediaReference(
                provider=row.provider,
                external_id=row.external_id,
                variant=row.variant,
                locale=row.locale,
                origin=row.origin,
                remote_url=row.remote_url,
            )
            for row in rows
        )
    if len(references) > 200:
        typer.echo("Selection exceeds the 200-image limit; pass explicit --source-ids.", err=True)
        raise typer.Exit(code=2)

    cached = failed = 0
    for index, reference in enumerate(references, 1):
        try:
            materialized = download_remote_media(
                reference,
                settings.media_cache_directory,
                max_bytes=settings.image_max_item_bytes,
                timeout_seconds=settings.bangumi_request_timeout_seconds,
            )
        except MediaError as exc:
            failed += 1
            code = str(exc) if str(exc).startswith("media_") else "media_download_failed"
            with session_scope(settings.database_url) as session:
                mark_media_source_failure(
                    session,
                    reference,
                    failure_code=code[:32],
                    missing=code == "media_remote_missing",
                )
        else:
            with session_scope(settings.database_url) as session:
                register_media_references(
                    session,
                    (reference,),
                    policy=ImagePolicy.CACHE,
                    cached={reference.key: materialized},
                )
            cached += 1
        if index < len(references):
            time.sleep(0.25)
    typer.echo(f"Selected={len(references)} cached={cached} failed={failed}")


@media_app.command("prune")
def media_prune(
    max_bytes: int | None = typer.Option(None, "--max-bytes", min=0),
    apply_local: bool = typer.Option(False, "--apply-local"),
) -> None:
    """Preview or evict unpinned files from the project media cache."""
    settings = _settings_or_exit()
    target = settings.image_cache_max_bytes if max_bytes is None else max_bytes
    with session_scope(settings.database_url) as session:
        result = prune_media_cache(
            session,
            settings.media_cache_directory,
            max_bytes=target,
            apply=apply_local,
        )
    typer.echo(f"Mode: {'apply-local' if result.apply else 'dry-run'}")
    typer.echo(f"Blobs: {result.blob_count}")
    typer.echo(f"Bytes: {result.byte_count}")
    if result.apply:
        typer.echo("Only unpinned project-cache blobs were removed; Steam source files were untouched.")


@ui_app.command("serve")
def ui_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    open_browser: bool = typer.Option(True, "--open-browser/--no-open-browser"),
    env_file: Path = typer.Option(Path(".env"), "--env-file"),
) -> None:
    """Run the local UI. Non-loopback binding is intentionally unsupported."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        typer.echo(
            "The local UI only accepts loopback hosts; --host must be 127.0.0.1, ::1, or localhost.",
            err=True,
        )
        raise typer.Exit(code=2)
    settings = _settings_or_exit(env_file)
    try:
        with session_scope(settings.database_url) as session:
            interrupted = interrupt_running_jobs(session)
    except SQLAlchemyError:
        typer.echo("Database is not at the latest schema; run 'bld db upgrade'.", err=True)
        raise typer.Exit(code=1) from None
    from bangumi_local.web.app import create_app
    import uvicorn

    display_host = "[::1]" if host == "::1" else host
    url = f"http://{display_host}:{port}/"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    typer.echo(f"Bangumi Local Database UI: {url}")
    if interrupted:
        typer.echo(f"Marked {interrupted} interrupted job(s) for review.")
    uvicorn.run(
        create_app(settings, start_worker=True, env_file=env_file),
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    app()
