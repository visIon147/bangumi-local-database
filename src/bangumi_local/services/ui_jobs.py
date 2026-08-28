from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace
import json
import time

from bangumi_local.adapters.bangumi import BangumiAPIError, BangumiClient
from bangumi_local.config import Settings
from bangumi_local.db.models import (
    BangumiSubject,
    DiscoveryCandidate,
    LibraryEntry,
    MediaSource,
    RatingQueueItem,
    SourceAccount,
)
from bangumi_local.db.session import session_scope
from bangumi_local.domain.media import MediaReference
from bangumi_local.domain.models import CollectionStatus, SubjectSearchCandidate, SubjectType
from bangumi_local.domain.plans import stable_json
from bangumi_local.services.apply_plan import apply_reviewed_plan, preflight_plan
from bangumi_local.services.discovery import (
    bangumi_discovery_seeds,
    create_discovery_session,
)
from bangumi_local.services.discovery_promotion import (
    create_discovery_status_plan,
    promote_bangumi_identity,
)
from bangumi_local.services.jobs import JobContext, JobRunner
from bangumi_local.services.media import (
    MediaError,
    bind_media_source,
    cache_local_media,
    download_remote_media,
    mark_media_source_failure,
    prune_media_cache,
    register_media_references,
    scan_steam_librarycache,
    verify_media_cache,
)
from bangumi_local.services.plans import create_sync_plan, export_plan, load_plan
from bangumi_local.services.plans import (
    create_bulk_tag_plan,
    create_classification_plan,
    create_recovery_plan,
)
from bangumi_local.services.pull import pull_collections
from bangumi_local.services.pull_plans import (
    applied_pull_subject_ids,
    apply_pull_plan,
    create_pull_plan,
    preflight_pull_plan,
)
from bangumi_local.services.pull_media import materialize_pull_media
from bangumi_local.services.shadow import bootstrap_shadows
from bangumi_local.services.status import build_status_report
from bangumi_local.services.manual_uncollect import (
    preflight_manual_uncollect,
    reconcile_manual_uncollect,
)
from bangumi_local.services.rating_queue import (
    create_rating_queue,
    prepare_rating_queue,
    rated_subject_ids,
)
from bangumi_local.services.steam_matching import (
    confirm_match_subject,
    fetch_match_search,
    persist_match_search,
    prepare_match_search,
)
from bangumi_local.services.steam_import import apply_steam_import, preview_steam_import
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
    fetch_title_completion,
    persist_title_completion,
    prepare_title_completion,
)
from bangumi_local.adapters.steam import read_steam_snapshot
from bangumi_local.domain.steam import (
    load_steam_rules,
    steam_rule_configuration_from_payload,
)


def _verified_collections(settings: Settings, subject_type: SubjectType | None):
    with BangumiClient(settings) as client:
        me = client.get_me()
        if me.username != client.username:
            raise BangumiAPIError("Authenticated Bangumi user does not match configuration.")
        return client.get_collections(subject_type=subject_type)


def build_ui_job_runner(settings: Settings) -> JobRunner:
    runner = JobRunner(settings.database_url)

    def cache_queue_cover(
        reference: MediaReference,
        *,
        rating_queue_item_id: str | None = None,
        discovery_candidate_id: str | None = None,
    ) -> bool:
        """Cache a missing queue cover and bind it without holding a DB transaction online."""

        with session_scope(settings.database_url) as session:
            source = session.query(MediaSource).filter_by(
                provider=reference.provider,
                external_id=reference.external_id,
                variant=reference.variant,
                locale=reference.locale,
                origin=reference.origin,
            ).one_or_none()
            if source is not None and source.current_blob_sha256 is not None:
                bind_media_source(
                    session,
                    source.id,
                    rating_queue_item_id=rating_queue_item_id,
                    discovery_candidate_id=discovery_candidate_id,
                    role="cover",
                    priority=100,
                )
                return False
        materialized = download_remote_media(
            reference,
            settings.media_cache_directory,
            max_bytes=settings.image_max_item_bytes,
            timeout_seconds=settings.bangumi_request_timeout_seconds,
        )
        with session_scope(settings.database_url) as session:
            register_media_references(
                session,
                (reference,),
                policy="cache",
                cached={reference.key: materialized},
            )
            source = session.query(MediaSource).filter_by(
                provider=reference.provider,
                external_id=reference.external_id,
                variant=reference.variant,
                locale=reference.locale,
                origin=reference.origin,
            ).one()
            bind_media_source(
                session,
                source.id,
                rating_queue_item_id=rating_queue_item_id,
                discovery_candidate_id=discovery_candidate_id,
                role="cover",
                priority=100,
            )
        return True

    def auth_check_handler(context: JobContext, config: Mapping[str, object]):
        context.update(
            phase="remote_read",
            current=0,
            total=1,
            message="Validating the configured Bangumi account.",
        )
        with BangumiClient(settings) as client:
            me = client.get_me()
            if me.username != client.username:
                raise BangumiAPIError(
                    "Authenticated Bangumi user does not match configuration."
                )
        return {"authenticated": True, "username_matches": True}

    def pull_handler(context: JobContext, config: Mapping[str, object]):
        raw_type = config.get("subject_type")
        subject_type = SubjectType.parse(str(raw_type)) if raw_type else None
        context.update(phase="remote_read", current=0, total=None, message="Reading Bangumi collections.")
        collections = _verified_collections(settings, subject_type)
        context.update(
            phase="persist",
            current=0,
            total=len(collections),
            message="Remote read complete; applying three-way pull locally.",
        )
        image_policy = str(config.get("image_policy", "metadata"))
        references: list[MediaReference] = []
        cached_media = {}
        if image_policy != "none":
            from bangumi_local.services.media import normalize_remote_image_url

            for item in collections:
                if not item.subject.cover_url:
                    continue
                try:
                    reference = MediaReference(
                        provider="bangumi",
                        external_id=str(item.subject_id),
                        variant="preferred",
                        origin="remote",
                        remote_url=normalize_remote_image_url(
                            item.subject.cover_url, "bangumi"
                        ),
                    )
                    references.append(reference)
                    if image_policy == "cache":
                        cached_media[reference.key] = download_remote_media(
                            reference,
                            settings.media_cache_directory,
                            max_bytes=settings.image_max_item_bytes,
                            timeout_seconds=settings.bangumi_request_timeout_seconds,
                        )
                except MediaError:
                    # Cover failure is non-fatal to the collection pull.
                    continue
        with session_scope(settings.database_url) as session:
            result = pull_collections(
                session, collections, scope_subject_type=subject_type
            )
            if references:
                register_media_references(
                    session,
                    references,
                    policy=image_policy,
                    cached=cached_media,
                )
                for reference in references:
                    source = session.query(MediaSource).filter_by(
                        provider=reference.provider,
                        external_id=reference.external_id,
                        variant=reference.variant,
                        locale=reference.locale,
                        origin=reference.origin,
                    ).one_or_none()
                    identity = session.get(BangumiSubject, int(reference.external_id))
                    if source is not None and identity is not None:
                        bind_media_source(
                            session,
                            source.id,
                            work_id=identity.work_id,
                            role="cover",
                            priority=100,
                        )
        context.update(
            phase="persist",
            current=len(collections),
            total=len(collections),
            message="Local mirror updated.",
        )
        return {
            "remote_count": result.remote_count,
            "imported": result.imported,
            "remote_updates": result.remote_updates,
            "conflicts": result.conflicts,
            "missing_remote": result.missing_remote,
        }

    def pull_plan_handler(context: JobContext, config: Mapping[str, object]):
        raw_type = config.get("subject_type")
        subject_type = SubjectType.parse(str(raw_type)) if raw_type else None
        image_policy = str(config.get("image_policy", "metadata"))
        context.update(
            phase="remote_read",
            current=0,
            total=None,
            message="Reading Bangumi collections for an immutable local pull plan.",
        )
        collections = _verified_collections(settings, subject_type)
        with session_scope(settings.database_url) as session:
            stored = create_pull_plan(
                session,
                collections,
                subject_type=subject_type,
                image_policy=image_policy,
            )
            export_plan(stored, settings.plan_directory)
        context.update(
            phase="plan",
            current=len(collections),
            total=len(collections),
            message="Local pull plan created; no collection state was changed.",
        )
        return {
            "plan_id": stored.plan.id,
            "planned": len(stored.planned),
            "unchanged": len(stored.unchanged),
            "remote_count": len(collections),
            "bangumi_writes": 0,
            "local_collection_writes": 0,
        }

    def apply_handler(context: JobContext, config: Mapping[str, object]):
        plan_id = str(config.get("plan_id", ""))
        if not plan_id:
            raise ValueError("plan_id is required")
        context.update(
            phase="preflight",
            current=0,
            total=None,
            message="Running fresh plan preflight.",
        )
        with session_scope(settings.database_url) as session:
            inspected = load_plan(session, plan_id)
            is_pull = inspected.plan.kind == "pull" and inspected.plan.format_version == 5
            selector = json.loads(inspected.plan.selector_json)
        with BangumiClient(settings) as client:
            if is_pull:
                me = client.get_me()
                if me.username != client.username:
                    raise BangumiAPIError("Authenticated Bangumi user does not match configuration.")
                raw_type = selector.get("subject_type")
                subject_type = SubjectType(int(raw_type)) if raw_type is not None else None
                collections = client.get_collections(subject_type=subject_type)
                fresh = preflight_pull_plan(settings.database_url, plan_id, collections)
            else:
                fresh = preflight_plan(settings.database_url, client, plan_id)
            context.update(
                phase="apply",
                current=0,
                total=len(fresh.will_modify),
                message=(
                    "Preflight passed; starting verified local merges."
                    if is_pull
                    else "Preflight passed; starting serialized verified writes."
                ),
            )
            if is_pull:
                result = apply_pull_plan(
                    settings.database_url,
                    plan_id,
                    fresh,
                    backup_directory=settings.backup_directory,
                )
            else:
                result = apply_reviewed_plan(
                    settings.database_url,
                    client,
                    plan_id,
                    fresh,
                    backup_directory=settings.backup_directory,
                    write_delay_seconds=settings.bangumi_write_delay_ms / 1000,
                    max_retries=settings.bangumi_max_retries,
                    retry_base_seconds=settings.bangumi_retry_base_seconds,
                )
        media_result = None
        if is_pull:
            image_policy = str(selector.get("image_policy", "metadata"))
            applied_subjects = applied_pull_subject_ids(settings.database_url, plan_id)
            media_collections = [
                item for item in collections if item.subject_id in applied_subjects
            ]
            context.update(
                phase="media",
                current=0,
                total=len(media_collections),
                message="Registering cover metadata and applying the explicit cache policy.",
            )
            media_result = materialize_pull_media(
                settings.database_url,
                media_collections,
                policy=image_policy,
                cache_directory=settings.media_cache_directory,
                max_bytes=settings.image_max_item_bytes,
                timeout_seconds=settings.bangumi_request_timeout_seconds,
                progress=lambda current, total: context.update(
                    phase="media",
                    current=current,
                    total=total,
                    message="Processing Bangumi cover cache without duplicate downloads.",
                ),
            )
        context.update(
            phase="apply",
            current=len(fresh.will_modify),
            total=len(fresh.will_modify),
            message="Verified apply completed.",
        )
        payload = {
            "plan_id": plan_id,
            "run_id": result.run_id,
            "status": result.status,
            "applied": result.applied,
            "stale": result.stale,
            "failed": result.failed,
            "reverse_plan_id": result.reverse_plan_id,
        }
        if media_result is not None:
            payload["media"] = asdict(media_result)
        return payload

    def preflight_handler(context: JobContext, config: Mapping[str, object]):
        plan_id = str(config.get("plan_id", ""))
        if not plan_id:
            raise ValueError("plan_id is required")
        context.update(
            phase="preflight",
            current=0,
            total=None,
            message="Fresh-reading every immutable plan target.",
        )
        with session_scope(settings.database_url) as session:
            inspected = load_plan(session, plan_id)
            is_pull = inspected.plan.kind == "pull" and inspected.plan.format_version == 5
            selector = json.loads(inspected.plan.selector_json)
        with BangumiClient(settings) as client:
            if is_pull:
                me = client.get_me()
                if me.username != client.username:
                    raise BangumiAPIError("Authenticated Bangumi user does not match configuration.")
                raw_type = selector.get("subject_type")
                subject_type = SubjectType(int(raw_type)) if raw_type is not None else None
                collections = client.get_collections(subject_type=subject_type)
                result = preflight_pull_plan(settings.database_url, plan_id, collections)
            else:
                result = preflight_plan(settings.database_url, client, plan_id)
        return {
            "plan_id": plan_id,
            "will_modify": [
                {"subject_id": item.subject_id, "title": item.title}
                for item in result.will_modify
            ],
            "unchanged": [
                {
                    "subject_id": item.subject_id,
                    "title": item.title,
                    "reason": item.reason,
                }
                for item in result.unchanged
            ],
            "will_modify_count": len(result.will_modify),
            "unchanged_count": len(result.unchanged),
        }

    def remote_media_handler(context: JobContext, config: Mapping[str, object]):
        source_ids = tuple(str(item) for item in config.get("source_ids", []) if str(item))
        with session_scope(settings.database_url) as session:
            statement = session.query(MediaSource).filter(MediaSource.origin == "remote")
            if source_ids:
                statement = statement.filter(MediaSource.id.in_(source_ids))
            else:
                statement = statement.filter(MediaSource.current_blob_sha256.is_(None))
            rows = statement.order_by(MediaSource.id).limit(200).all()
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
        cached = 0
        failures = 0
        for index, reference in enumerate(references, 1):
            context.update(
                phase="media_fetch",
                current=index - 1,
                total=len(references),
                message=f"Fetching image {index} of {len(references)}.",
            )
            try:
                materialized = download_remote_media(
                    reference,
                    settings.media_cache_directory,
                    max_bytes=settings.image_max_item_bytes,
                    timeout_seconds=settings.bangumi_request_timeout_seconds,
                )
            except MediaError as exc:
                failures += 1
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
                        policy="cache",
                        cached={reference.key: materialized},
                    )
                cached += 1
        return {"selected": len(references), "cached": cached, "failed": failures}

    def steam_media_scan_handler(context: JobContext, config: Mapping[str, object]):
        if settings.steam_root is None:
            raise ValueError("Steam root is not configured.")
        policy = str(config.get("policy", "metadata"))
        app_ids = tuple(str(item) for item in config.get("app_ids", []) if str(item)) or None
        if policy == "none":
            return {"examined": 0, "created": 0, "updated": 0, "cached": 0}
        context.update(
            phase="steam_media_scan",
            current=0,
            total=None,
            message="Scanning local Steam image metadata.",
        )
        candidates = scan_steam_librarycache(settings.steam_root, app_ids=app_ids)
        cached_media = {}
        if policy == "cache":
            for index, candidate in enumerate(candidates, 1):
                context.update(
                    phase="steam_media_cache",
                    current=index - 1,
                    total=len(candidates),
                    message=f"Caching Steam image {index} of {len(candidates)}.",
                )
                cached_media[candidate.reference.key] = cache_local_media(
                    candidate,
                    settings.media_cache_directory,
                    max_bytes=settings.image_max_item_bytes,
                )
        with session_scope(settings.database_url) as session:
            summary = register_media_references(
                session,
                (candidate.reference for candidate in candidates),
                policy=policy,
                cached=cached_media,
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
                ):
                    bind_media_source(
                        session,
                        source.id,
                        library_entry_id=entry.id,
                        role=(
                            "cover"
                            if candidate.reference.variant
                            in {"library_portrait", "library_capsule"}
                            else candidate.reference.variant
                        ),
                        priority=(
                            100
                            if candidate.reference.variant == "library_portrait"
                            else 50
                        ),
                    )
        return {
            "examined": summary.examined,
            "created": summary.sources_created,
            "updated": summary.sources_updated,
            "cached": summary.cached,
            "skipped": summary.skipped,
            "network_requests": 0,
        }

    def media_verify_handler(context: JobContext, config: Mapping[str, object]):
        context.update(
            phase="media_verify",
            current=0,
            total=None,
            message="Verifying registered cache files.",
        )
        with session_scope(settings.database_url) as session:
            issues = verify_media_cache(session, settings.media_cache_directory)
        return {
            "issue_count": len(issues),
            "issues": [
                {"sha256": issue.sha256, "code": issue.code} for issue in issues
            ],
        }

    def media_prune_handler(context: JobContext, config: Mapping[str, object]):
        context.update(
            phase="media_prune",
            current=0,
            total=None,
            message="Pruning unpinned cache entries within the configured root.",
        )
        maximum = (
            int(config["max_bytes"])
            if config.get("max_bytes") is not None
            else settings.image_cache_max_bytes
        )
        with session_scope(settings.database_url) as session:
            result = prune_media_cache(
                session,
                settings.media_cache_directory,
                max_bytes=maximum,
                apply=True,
            )
        return {
            "removed_blobs": result.blob_count,
            "removed_bytes": result.byte_count,
        }

    def rating_create_handler(context: JobContext, config: Mapping[str, object]):
        subject_types = tuple(int(item) for item in config.get("subject_types", [1, 2, 3, 4, 6]))
        statuses = tuple(int(item) for item in config.get("collection_statuses", [2, 3, 4, 5]))
        order = str(config.get("order", "recently-updated"))
        seed = int(config["seed"]) if config.get("seed") is not None else None
        max_items = int(config["max_items"]) if config.get("max_items") is not None else None
        with session_scope(settings.database_url) as session:
            seeds = prepare_rating_queue(
                session,
                subject_types=subject_types,
                collection_statuses=statuses,
                include_deferred=bool(config.get("include_deferred", False)),
                order_name=order,
                random_seed=seed,
                max_items=max_items,
            )
        if len(seeds) > 200:
            raise ValueError("Network enrichment is limited to 200 items.")
        enrichments: dict[int, SubjectSearchCandidate] = {}
        errors: dict[int, str] = {}
        with BangumiClient(settings) as client:
            for index, item in enumerate(seeds, 1):
                context.update(
                    phase="rating_enrichment",
                    current=index - 1,
                    total=len(seeds),
                    message=f"Reading subject {index} of {len(seeds)}.",
                )
                try:
                    enrichments[item.subject_id] = client.get_subject(item.subject_id)
                except BangumiAPIError as exc:
                    errors[item.subject_id] = str(exc)
                if index < len(seeds):
                    time.sleep(0.25)
        selector = {
            "subject_types": list(subject_types),
            "collection_statuses": list(statuses),
            "include_deferred": bool(config.get("include_deferred", False)),
            "max_items": max_items,
            "allow_network": True,
        }
        with session_scope(settings.database_url) as session:
            view = create_rating_queue(
                session,
                seeds,
                selector=selector,
                order_name=order,
                random_seed=seed,
                enrichments=enrichments,
                enrichment_errors=errors,
            )
        return {
            "session_id": view.session.id,
            "items": len(view.items),
            "enriched": len(enrichments),
            "failed_enrichment": len(errors),
        }

    def rating_enrich_handler(context: JobContext, config: Mapping[str, object]):
        session_id = str(config.get("session_id", ""))
        subject_ids = tuple(int(item) for item in config.get("subject_ids", []))
        if not subject_ids or len(subject_ids) > 200:
            raise ValueError("Rating enrichment requires between 1 and 200 subjects.")
        with session_scope(settings.database_url) as session:
            rows = session.query(RatingQueueItem).filter(
                RatingQueueItem.session_id == session_id,
                RatingQueueItem.subject_id.in_(subject_ids),
            ).all()
            if len(rows) != len(set(subject_ids)):
                raise ValueError("One or more subjects are not part of the rating queue.")
            item_ids = {row.subject_id: row.id for row in rows}
        enrichments: dict[int, SubjectSearchCandidate] = {}
        errors: dict[int, str] = {}
        with BangumiClient(settings) as client:
            for index, subject_id in enumerate(subject_ids, 1):
                context.update(
                    phase="rating_enrichment",
                    current=index - 1,
                    total=len(subject_ids),
                    message=f"Reading subject {index} of {len(subject_ids)}.",
                )
                try:
                    enrichments[subject_id] = client.get_subject(subject_id)
                except BangumiAPIError as exc:
                    errors[subject_id] = str(exc)
                if index < len(subject_ids):
                    time.sleep(0.25)
        with session_scope(settings.database_url) as session:
            for subject_id in subject_ids:
                row = session.query(RatingQueueItem).filter_by(
                    session_id=session_id, subject_id=subject_id
                ).one()
                detail = enrichments.get(subject_id)
                if detail is None:
                    row.enrichment_status = "failed"
                    row.error = errors.get(subject_id, "subject_enrichment_failed")
                    continue
                snapshot = json.loads(row.subject_snapshot_json)
                snapshot.update(
                    title=detail.display_title,
                    title_original=detail.title_original,
                    summary=detail.summary,
                    release_date=detail.release_date,
                    cover_url=detail.cover_url,
                    public_tags=list(detail.public_tags),
                    rank=detail.rank,
                    score=detail.score,
                    rating_count=detail.rating_count,
                    bgm_url=f"https://bgm.tv/subject/{subject_id}",
                )
                row.subject_snapshot_json = stable_json(snapshot)
                row.enrichment_status = "fresh"
                row.error = None
        cached = 0
        image_errors = 0
        if bool(config.get("cache_image", True)):
            for subject_id, detail in enrichments.items():
                if not detail.cover_url:
                    continue
                reference = MediaReference(
                    provider="bangumi",
                    external_id=str(subject_id),
                    variant="preferred",
                    origin="remote",
                    remote_url=detail.cover_url,
                )
                try:
                    cached += int(
                        cache_queue_cover(
                            reference,
                            rating_queue_item_id=item_ids[subject_id],
                        )
                    )
                except MediaError:
                    image_errors += 1
        return {
            "session_id": session_id,
            "items": len(subject_ids),
            "enriched": len(enrichments),
            "failed_enrichment": len(errors),
            "images_cached": cached,
            "image_failures": image_errors,
        }

    def discovery_cache_images_handler(
        context: JobContext, config: Mapping[str, object]
    ):
        session_id = str(config.get("session_id", ""))
        candidate_ids = tuple(str(item) for item in config.get("candidate_ids", []))
        if not candidate_ids or len(candidate_ids) > 200:
            raise ValueError("Discovery image caching requires between 1 and 200 candidates.")
        with session_scope(settings.database_url) as session:
            rows = session.query(DiscoveryCandidate).filter(
                DiscoveryCandidate.session_id == session_id,
                DiscoveryCandidate.id.in_(candidate_ids),
            ).all()
            if len(rows) != len(set(candidate_ids)):
                raise ValueError("One or more candidates are not part of the discovery session.")
            covers = [(row.id, row.subject_id, row.cover_url) for row in rows if row.cover_url]
        cached = 0
        failed = 0
        for index, (candidate_id, subject_id, cover_url) in enumerate(covers, 1):
            context.update(
                phase="discovery_images",
                current=index - 1,
                total=len(covers),
                message=f"Caching candidate image {index} of {len(covers)}.",
            )
            if subject_id is None:
                failed += 1
                continue
            try:
                cached += int(
                    cache_queue_cover(
                        MediaReference(
                            provider="bangumi",
                            external_id=str(subject_id),
                            variant="preferred",
                            origin="remote",
                            remote_url=str(cover_url),
                        ),
                        discovery_candidate_id=candidate_id,
                    )
                )
            except MediaError:
                failed += 1
        return {
            "session_id": session_id,
            "selected": len(candidate_ids),
            "with_cover": len(covers),
            "images_cached": cached,
            "already_cached": len(covers) - cached - failed,
            "failed": failed,
        }

    def rating_sync_handler(context: JobContext, config: Mapping[str, object]):
        session_id = str(config.get("session_id", ""))
        with session_scope(settings.database_url) as session:
            subject_ids = rated_subject_ids(session, session_id)
        context.update(
            phase="remote_read", current=0, total=None, message="Reading fresh collections."
        )
        collections = _verified_collections(settings, None)
        with session_scope(settings.database_url) as session:
            stored = create_sync_plan(
                session,
                collections,
                selector={
                    "mode": "ids",
                    "ids": list(subject_ids),
                    "rating_session_id": session_id,
                },
                fields=("rate", "comment"),
            )
            plan_id = stored.plan.id
        with session_scope(settings.database_url) as session:
            # Re-load before export so no ORM state escapes its transaction.
            from bangumi_local.services.plans import load_plan

            export_plan(load_plan(session, plan_id), settings.plan_directory)
        return {"plan_id": plan_id, "subject_count": len(subject_ids)}

    def discovery_search_handler(context: JobContext, config: Mapping[str, object]):
        query = str(config.get("query", "")).strip()
        context.update(phase="search", current=0, total=None, message="Searching Bangumi games.")
        with BangumiClient(settings) as client:
            candidates = client.search_subjects_filtered(
                query,
                subject_type=SubjectType.GAME,
                limit=int(config.get("max_items", 50)),
                sort=str(config.get("sort", "match")),
                meta_tags=tuple(str(item) for item in config.get("public_tags", [])),
                year_from=int(config["year_from"]) if config.get("year_from") is not None else None,
                year_to=int(config["year_to"]) if config.get("year_to") is not None else None,
                min_rating_count=(
                    int(config["min_rating_count"])
                    if config.get("min_rating_count") is not None
                    else None
                ),
            )
        with session_scope(settings.database_url) as session:
            seeds = bangumi_discovery_seeds(
                session, candidates, provider="bangumi_search", include_decided=False
            )
            view = create_discovery_session(
                session, provider="bangumi_search", filters=dict(config), seeds=seeds
            )
            image_targets = [
                (item.id, item.subject_id, item.cover_url)
                for item in view.candidates
                if item.subject_id is not None and item.cover_url
            ]
        cached = 0
        failed = 0
        if bool(config.get("cache_images", False)):
            for candidate_id, subject_id, cover_url in image_targets:
                try:
                    cached += int(cache_queue_cover(
                        MediaReference(
                            provider="bangumi", external_id=str(subject_id),
                            variant="preferred", origin="remote", remote_url=str(cover_url),
                        ),
                        discovery_candidate_id=candidate_id,
                    ))
                except MediaError:
                    failed += 1
        return {
            "session_id": view.session.id,
            "candidate_count": len(view.candidates),
            "images_cached": cached,
            "image_failures": failed,
        }

    def discovery_browse_handler(context: JobContext, config: Mapping[str, object]):
        context.update(phase="browse", current=0, total=None, message="Browsing bounded Bangumi games.")
        with BangumiClient(settings) as client:
            candidates = client.browse_game_subjects(
                year=int(config["year"]) if config.get("year") is not None else None,
                platform=str(config.get("platform") or "") or None,
                sort=str(config.get("sort", "rank")),
                limit=int(config.get("max_items", 50)),
            )
        with session_scope(settings.database_url) as session:
            seeds = bangumi_discovery_seeds(
                session, candidates, provider="bangumi_browse", include_decided=False
            )
            view = create_discovery_session(
                session, provider="bangumi_browse", filters=dict(config), seeds=seeds
            )
            image_targets = [
                (item.id, item.subject_id, item.cover_url)
                for item in view.candidates
                if item.subject_id is not None and item.cover_url
            ]
        cached = 0
        failed = 0
        if bool(config.get("cache_images", False)):
            for candidate_id, subject_id, cover_url in image_targets:
                try:
                    cached += int(cache_queue_cover(
                        MediaReference(
                            provider="bangumi", external_id=str(subject_id),
                            variant="preferred", origin="remote", remote_url=str(cover_url),
                        ),
                        discovery_candidate_id=candidate_id,
                    ))
                except MediaError:
                    failed += 1
        return {
            "session_id": view.session.id,
            "candidate_count": len(view.candidates),
            "images_cached": cached,
            "image_failures": failed,
        }

    def discovery_identity_handler(context: JobContext, config: Mapping[str, object]):
        candidate_id = str(config.get("candidate_id", ""))
        with session_scope(settings.database_url) as session:
            row = session.get(DiscoveryCandidate, candidate_id)
            if row is None:
                raise ValueError("Discovery candidate is missing.")
            subject_id = int(config.get("subject_id") or row.subject_id or 0)
            library_entry_id = row.library_entry_id
        if subject_id < 1:
            raise ValueError("A Bangumi subject ID is required.")
        context.update(phase="verify_identity", current=0, total=1, message="Verifying Bangumi subject.")
        with BangumiClient(settings) as client:
            subject = client.get_subject(subject_id)
        with session_scope(settings.database_url) as session:
            row = session.get(DiscoveryCandidate, candidate_id)
            assert row is not None
            if row.subject_id is not None:
                result = promote_bangumi_identity(
                    session, candidate_id, verified_subject=subject
                )
                work_id = result.work_id
            elif library_entry_id is not None:
                from bangumi_local.db.models import LibraryEntry, DiscoveryReviewState

                entry = session.get(LibraryEntry, library_entry_id)
                if entry is None:
                    raise ValueError("Steam entry is missing.")
                account = session.get(SourceAccount, entry.source_account_id)
                if account is None:
                    raise ValueError("Steam account is missing.")
                _, work = confirm_match_subject(
                    session,
                    candidate=subject,
                    app_id=entry.external_id,
                    account_id=account.external_account_id,
                    match_source="discovery_promotion",
                    review_reason="discovery_identity_confirmation",
                )
                row.work_id = work.id
                row.subject_id = subject.subject_id
                review = (
                    session.get(DiscoveryReviewState, row.review_state_id)
                    if row.review_state_id
                    else None
                )
                if review is not None:
                    review.work_id = work.id
                    review.subject_id = subject.subject_id
                work_id = work.id
            else:
                raise ValueError("Candidate has no promotable identity source.")
        return {"candidate_id": candidate_id, "work_id": work_id, "subject_id": subject_id}

    def discovery_status_handler(context: JobContext, config: Mapping[str, object]):
        candidate_id = str(config.get("candidate_id", ""))
        labels = {item.label: item for item in CollectionStatus}
        status = labels.get(str(config.get("status", "")))
        if status is None:
            raise ValueError("Unsupported collection status.")
        with session_scope(settings.database_url) as session:
            row = session.get(DiscoveryCandidate, candidate_id)
            if row is None or row.subject_id is None:
                raise ValueError("Candidate identity is missing.")
            subject_id = row.subject_id
        context.update(phase="remote_read", current=0, total=1, message="Reading fresh collection state.")
        with BangumiClient(settings) as client:
            try:
                remote = client.get_collection(subject_id)
            except BangumiAPIError as exc:
                if exc.status_code != 404:
                    raise
                remote = None
        with session_scope(settings.database_url) as session:
            stored = create_discovery_status_plan(
                session, candidate_id, status=status, remote=remote
            )
            plan_id = stored.plan.id
        with session_scope(settings.database_url) as session:
            from bangumi_local.services.plans import load_plan

            export_plan(load_plan(session, plan_id), settings.plan_directory)
        return {"candidate_id": candidate_id, "plan_id": plan_id, "status": status.label}

    def status_refresh_handler(context: JobContext, config: Mapping[str, object]):
        raw_type = config.get("subject_type")
        subject_type = SubjectType.parse(str(raw_type)) if raw_type else None
        collections = _verified_collections(settings, subject_type)
        with session_scope(settings.database_url) as session:
            report = build_status_report(
                session,
                collections,
                subject_id=int(config["subject_id"]) if config.get("subject_id") else None,
                subject_type=subject_type,
            )
        return report.to_dict()

    def shadow_bootstrap_handler(context: JobContext, config: Mapping[str, object]):
        collections = _verified_collections(settings, None)
        with session_scope(settings.database_url) as session:
            result = bootstrap_shadows(
                session,
                collections,
                apply=bool(config.get("apply", False)),
                subject_id=int(config["subject_id"]) if config.get("subject_id") else None,
            )
        return result.to_dict()

    def sync_plan_handler(context: JobContext, config: Mapping[str, object]):
        collections = _verified_collections(settings, None)
        raw_selector = config.get("selector")
        if not isinstance(raw_selector, Mapping):
            raise ValueError("selector is required")
        fields = tuple(str(item) for item in config.get("fields", []))
        with session_scope(settings.database_url) as session:
            stored = create_sync_plan(
                session, collections, selector=dict(raw_selector), fields=fields
            )
            plan_id = stored.plan.id
        with session_scope(settings.database_url) as session:
            from bangumi_local.services.plans import load_plan

            export_plan(load_plan(session, plan_id), settings.plan_directory)
        return {"plan_id": plan_id, "planned": len(stored.planned), "unchanged": len(stored.unchanged)}

    def _detail_tags(
        context: JobContext,
        collections,
        public_tag: str | None,
    ) -> dict[int, tuple[str, ...]]:
        details = {item.subject_id: item.subject.public_tags for item in collections}
        if public_tag is None:
            return details
        target = public_tag.strip().casefold()
        missing = [
            item
            for item in collections
            if target not in {tag.strip().casefold() for tag in item.subject.public_tags}
        ]
        if not missing:
            return details
        with BangumiClient(settings) as client:
            for index, item in enumerate(missing, 1):
                context.update(
                    phase="public_tags",
                    current=index - 1,
                    total=len(missing),
                    message=f"Reading full public tags {index} of {len(missing)}.",
                )
                details[item.subject_id] = client.get_subject_public_tags(item.subject_id)
                if index < len(missing):
                    time.sleep(0.25)
        return details

    def bulk_tag_handler(context: JobContext, config: Mapping[str, object]):
        selector = config.get("selector")
        if not isinstance(selector, Mapping):
            raise ValueError("selector is required")
        raw_type = selector.get("subject_type")
        subject_type = (
            SubjectType.parse(str(raw_type)) if raw_type not in (None, "") else None
        )
        collections = _verified_collections(settings, subject_type)
        public_tag = str(selector.get("public_tag")) if selector.get("mode") == "public_tag" else None
        details = _detail_tags(context, collections, public_tag)
        with session_scope(settings.database_url) as session:
            stored = create_bulk_tag_plan(
                session,
                collections,
                operation=str(config.get("operation")),
                selector=dict(selector),
                detail_loader=lambda subject_id: details[subject_id],
                tag=str(config["tag"]) if config.get("tag") is not None else None,
                old_tag=str(config["old_tag"]) if config.get("old_tag") is not None else None,
                new_tag=str(config["new_tag"]) if config.get("new_tag") is not None else None,
            )
            plan_id = stored.plan.id
        with session_scope(settings.database_url) as session:
            from bangumi_local.services.plans import load_plan

            export_plan(load_plan(session, plan_id), settings.plan_directory)
        return {"plan_id": plan_id, "planned": len(stored.planned), "unchanged": len(stored.unchanged)}

    def classify_handler(context: JobContext, config: Mapping[str, object]):
        collections = _verified_collections(settings, SubjectType.GAME)
        public_tag = str(config.get("public_tag", "Galgame"))
        details = _detail_tags(context, collections, public_tag)
        with session_scope(settings.database_url) as session:
            stored = create_classification_plan(
                session,
                collections,
                public_tag=public_tag,
                galgame_tag=str(config.get("galgame_tag", "Galgame分类")),
                game_tag=str(config.get("game_tag", "普通Game分类")),
                detail_loader=lambda subject_id: details[subject_id],
            )
            plan_id = stored.plan.id
        with session_scope(settings.database_url) as session:
            from bangumi_local.services.plans import load_plan

            export_plan(load_plan(session, plan_id), settings.plan_directory)
        return {"plan_id": plan_id, "planned": len(stored.planned), "unchanged": len(stored.unchanged)}

    def recovery_handler(context: JobContext, config: Mapping[str, object]):
        source_plan_id = str(config.get("plan_id", ""))
        collections = _verified_collections(settings, None)
        with session_scope(settings.database_url) as session:
            stored = create_recovery_plan(session, source_plan_id, collections)
            plan_id = stored.plan.id
        with session_scope(settings.database_url) as session:
            from bangumi_local.services.plans import load_plan

            export_plan(load_plan(session, plan_id), settings.plan_directory)
        return {"source_plan_id": source_plan_id, "plan_id": plan_id}

    def manual_uncollect_handler(context: JobContext, config: Mapping[str, object]):
        plan_id = str(config.get("plan_id", ""))
        with BangumiClient(settings) as client:
            result = reconcile_manual_uncollect(
                settings.database_url,
                client,
                plan_id,
                backup_directory=settings.backup_directory,
            )
        return {
            "plan_id": plan_id,
            "run_id": result.run_id,
            "reconciled": result.reconciled,
            "restore_plan_id": result.restore_plan_id,
        }

    def manual_uncollect_preflight_handler(
        context: JobContext, config: Mapping[str, object]
    ):
        plan_id = str(config.get("plan_id", ""))
        context.update(
            phase="preflight",
            current=0,
            total=None,
            message="Verifying that the collection is absent on Bangumi.",
        )
        with BangumiClient(settings) as client:
            result = preflight_manual_uncollect(settings.database_url, client, plan_id)
        return {
            "plan_id": result.plan_id,
            "verified_absent_count": len(result.subjects),
            "subjects": [
                {"subject_id": subject_id, "title": title}
                for subject_id, title in result.subjects
            ],
        }

    def steam_import_preview_handler(context: JobContext, config: Mapping[str, object]):
        context.update(phase="steam_read", current=0, total=None, message="Reading Steam library sources.")
        snapshot = read_steam_snapshot(settings, allow_network=bool(config.get("allow_network", False)))
        with session_scope(settings.database_url) as session:
            summary = preview_steam_import(session, snapshot)
        return asdict(summary)

    def steam_import_apply_handler(context: JobContext, config: Mapping[str, object]):
        context.update(phase="steam_read", current=0, total=None, message="Reading Steam library sources.")
        snapshot = read_steam_snapshot(
            settings, allow_network=bool(config.get("allow_network", False))
        )
        with session_scope(settings.database_url) as session:
            summary = apply_steam_import(session, snapshot)
        return asdict(summary)

    def steam_titles_complete_handler(context: JobContext, config: Mapping[str, object]):
        raw_appids = str(config.get("appids") or "")
        app_ids = tuple(
            dict.fromkeys(item.strip() for item in raw_appids.split(",") if item.strip())
        ) or None
        with session_scope(settings.database_url) as session:
            prepared = prepare_title_completion(
                session,
                account_id=settings.steam_account_id,
                app_ids=app_ids,
                all_missing=bool(config.get("all_missing", False)),
                max_items=int(config.get("max_items", 250)),
            )
        context.update(
            phase="steam_store_titles",
            current=0,
            total=len(prepared),
            message="Reading public Steam Store titles for the frozen selection.",
        )
        fetched = fetch_title_completion(
            prepared,
            timeout_seconds=settings.bangumi_request_timeout_seconds,
            request_delay_seconds=int(config.get("request_delay_ms", 250)) / 1000,
            sleep_fn=time.sleep,
        )
        with session_scope(settings.database_url) as session:
            result = persist_title_completion(session, fetched)
        context.update(
            phase="persist",
            current=len(prepared),
            total=len(prepared),
            message="Steam title completion finished.",
        )
        return asdict(result)

    def steam_match_search_handler(context: JobContext, config: Mapping[str, object]):
        app_id = str(config.get("app_id", ""))
        with session_scope(settings.database_url) as session:
            prepared = prepare_match_search(
                session,
                app_id=app_id,
                account_id=settings.steam_account_id,
                query=str(config.get("query") or "") or None,
            )
        context.update(
            phase="remote_read",
            current=0,
            total=None,
            message=f"Searching Bangumi candidates for Steam AppID {app_id}.",
        )
        with BangumiClient(settings) as client:
            fetched = fetch_match_search(
                prepared,
                client,
                include_store_titles=True,
                timeout_seconds=settings.bangumi_request_timeout_seconds,
                limit=int(config.get("limit", 10)),
            )
        with session_scope(settings.database_url) as session:
            result = persist_match_search(session, fetched)
        return {
            "app_id": app_id,
            "title": result.title,
            "queries": list(result.queries),
            "candidate_count": len(result.candidates),
            "best_subject_id": result.candidates[0].subject_id if result.candidates else None,
        }

    def steam_match_plan_handler(context: JobContext, config: Mapping[str, object]):
        raw_appids = str(config.get("appids") or "")
        app_ids = tuple(dict.fromkeys(item.strip() for item in raw_appids.split(",") if item.strip())) or None
        policy = AutoMatchPolicy(
            score_threshold=int(config.get("auto_threshold", 95)),
            minimum_margin=int(config.get("min_margin", 20)),
            require_exact=not bool(config.get("allow_nonexact_auto", False)),
        )
        with session_scope(settings.database_url) as session:
            prepared = prepare_steam_match_plan(
                session,
                account_id=settings.steam_account_id,
                app_ids=app_ids,
                collection_name=str(config.get("collection") or "") or None,
                collection_regex=str(config.get("collection_regex") or "") or None,
                all_unmatched=bool(config.get("all_unmatched", False)),
                include_no_subject=bool(config.get("include_no_subject", False)),
                include_deferred=bool(config.get("include_deferred", False)),
                candidate_image_policy=str(config.get("candidate_image_policy", "metadata")),
                policy=policy,
                candidate_limit=int(config.get("limit", 10)),
                batch_offset=int(config.get("offset", 0)),
                max_items=int(config.get("max_items", 250)),
            )
        context.update(
            phase="remote_read",
            current=0,
            total=len(prepared.entries),
            message="Searching Bangumi candidates for the frozen Steam selection.",
        )
        with BangumiClient(settings) as client:
            fetched = fetch_steam_match_plan(
                prepared,
                client,
                include_store_titles=True,
                timeout_seconds=settings.bangumi_request_timeout_seconds,
                request_delay_seconds=int(config.get("request_delay_ms", 250)) / 1000,
                sleep_fn=time.sleep,
            )
        with session_scope(settings.database_url) as session:
            stored = persist_steam_match_plan(session, fetched)
            plan_id = stored.plan.id
            planned_count = len(stored.planned)
            unchanged_count = len(stored.unchanged)
            media_selection = register_match_candidate_media(
                session,
                stored,
                policy=str(config.get("candidate_image_policy", "metadata")),
            )
        candidate_images_cached = 0
        for index, reference in enumerate(media_selection.missing, 1):
            context.update(
                phase="candidate_media",
                current=index - 1,
                total=len(media_selection.missing),
                message=f"Caching missing candidate image {index} of {len(media_selection.missing)}.",
            )
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
            candidate_images_cached += 1
        with session_scope(settings.database_url) as session:
            from bangumi_local.services.plans import load_plan

            export_plan(load_plan(session, plan_id), settings.plan_directory)
        return {
            "plan_id": plan_id,
            "planned": planned_count,
            "unchanged": unchanged_count,
            "candidate_images_observed": media_selection.observed,
            "candidate_images_cached": candidate_images_cached,
        }

    def steam_confirm_handler(context: JobContext, config: Mapping[str, object]):
        app_id = str(config.get("app_id", ""))
        subject_id = int(config.get("subject_id", 0))
        with BangumiClient(settings) as client:
            subject = client.get_subject(subject_id)
        with session_scope(settings.database_url) as session:
            entry, work = confirm_match_subject(
                session,
                candidate=subject,
                app_id=app_id,
                account_id=settings.steam_account_id,
            )
        return {"app_id": entry.external_id, "work_id": work.id, "subject_id": subject_id}

    def steam_revise_handler(context: JobContext, config: Mapping[str, object]):
        subject_id = int(config.get("subject_id", 0))
        with BangumiClient(settings) as client:
            subject = client.get_subject(subject_id)

        class _VerifiedSubjectClient:
            def get_subject(self, requested: int):
                if requested != subject.subject_id:
                    raise ValueError("Verified subject mismatch")
                return subject

        with session_scope(settings.database_url) as session:
            stored = revise_steam_match_plan(
                session,
                _VerifiedSubjectClient(),  # type: ignore[arg-type]
                plan_id=str(config.get("plan_id", "")),
                app_id=str(config.get("app_id", "")),
                decision="subject",
                subject_id=subject_id,
            )
            plan_id = stored.plan.id
        with session_scope(settings.database_url) as session:
            from bangumi_local.services.plans import load_plan

            export_plan(load_plan(session, plan_id), settings.plan_directory)
        return {"plan_id": plan_id, "supersedes": str(config.get("plan_id", ""))}

    def steam_status_plan_handler(context: JobContext, config: Mapping[str, object]):
        collections = _verified_collections(settings, SubjectType.GAME)
        labels = {item.label: item for item in CollectionStatus}
        raw_appids = str(config.get("appids") or "")
        app_ids = tuple(dict.fromkeys(item.strip() for item in raw_appids.split(",") if item.strip())) or None
        rules = load_steam_rules(settings.steam_config)
        rule_mode = str(config.get("rule_mode") or "saved")
        if rule_mode == "custom":
            raw_rules = config.get("rules")
            rules = steam_rule_configuration_from_payload(
                {
                    "rules": raw_rules,
                    "remaining_status": (
                        rules.remaining_status.label
                        if rules.remaining_status is not None
                        else None
                    ),
                    "allow_network": rules.allow_network,
                }
            )
        elif rule_mode != "saved":
            raise ValueError("Unsupported Steam rule mode.")
        legacy_remaining = str(config.get("remaining_status") or "")
        remaining_policy = str(config.get("remaining_policy") or legacy_remaining or "default")
        if remaining_policy == "local":
            rules = replace(rules, remaining_status=None)
        elif remaining_policy not in {"default", ""}:
            remaining = labels.get(remaining_policy)
            if remaining is None:
                raise ValueError("Unsupported remaining status.")
            rules = replace(rules, remaining_status=remaining)
        with session_scope(settings.database_url) as session:
            stored = create_steam_status_plan(
                session,
                collections,
                app_ids=app_ids,
                all_eligible=bool(config.get("all_eligible", False)),
                account_id=settings.steam_account_id,
                configuration=rules,
                remaining_status=None,
                followup_tag=str(config.get("add_tag") or "") or None,
                rules_source=(
                    "plan_override" if rule_mode == "custom" else "saved_defaults"
                ),
            )
            plan_id = stored.plan.id
        with session_scope(settings.database_url) as session:
            from bangumi_local.services.plans import load_plan

            export_plan(load_plan(session, plan_id), settings.plan_directory)
        return {"plan_id": plan_id, "planned": len(stored.planned), "unchanged": len(stored.unchanged)}

    runner.register("auth_check", auth_check_handler)
    runner.register("bangumi_pull", pull_handler)
    runner.register("bangumi_pull_plan", pull_plan_handler)
    runner.register("plan_preflight", preflight_handler)
    runner.register("plan_apply", apply_handler)
    runner.register("remote_media_fetch", remote_media_handler)
    runner.register("steam_media_scan", steam_media_scan_handler)
    runner.register("media_verify", media_verify_handler)
    runner.register("media_prune", media_prune_handler)
    runner.register("rating_queue_create_enriched", rating_create_handler)
    runner.register("rating_queue_enrich", rating_enrich_handler)
    runner.register("rating_sync_plan", rating_sync_handler)
    runner.register("discovery_create_search", discovery_search_handler)
    runner.register("discovery_create_browse", discovery_browse_handler)
    runner.register("discovery_cache_images", discovery_cache_images_handler)
    runner.register("discovery_promote_identity", discovery_identity_handler)
    runner.register("discovery_status_draft", discovery_status_handler)
    runner.register("status_refresh", status_refresh_handler)
    runner.register("shadow_bootstrap", shadow_bootstrap_handler)
    runner.register("sync_plan", sync_plan_handler)
    runner.register("bulk_tag_plan", bulk_tag_handler)
    runner.register("classify_games_plan", classify_handler)
    runner.register("plan_recovery", recovery_handler)
    runner.register("manual_uncollect_reconcile", manual_uncollect_handler)
    runner.register("manual_uncollect_preflight", manual_uncollect_preflight_handler)
    runner.register("steam_import_preview", steam_import_preview_handler)
    runner.register("steam_import_apply", steam_import_apply_handler)
    runner.register("steam_titles_complete", steam_titles_complete_handler)
    runner.register("steam_match_search", steam_match_search_handler)
    runner.register("steam_match_plan", steam_match_plan_handler)
    runner.register("steam_match_confirm", steam_confirm_handler)
    runner.register("steam_match_revise_subject", steam_revise_handler)
    runner.register("steam_status_plan", steam_status_plan_handler)
    return runner
