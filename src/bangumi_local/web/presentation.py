from __future__ import annotations


_STATUS_LABELS = {
    "pending": "待处理", "rated": "已评分", "skipped": "已跳过", "deferred": "暂缓",
    "stale": "已过期", "suppressed": "已抑制", "draft": "草稿", "reviewed": "已审阅",
    "applying": "执行中", "applied": "已执行", "partial": "部分完成", "failed": "失败",
    "cancelled": "已取消", "cancel_requested": "等待取消", "queued": "排队中", "running": "运行中",
    "succeeded": "成功", "interrupted": "已中断", "confirmed": "已确认", "candidates": "有候选",
    "unmatched": "未匹配", "no_subject": "确认无条目", "manual_review": "人工审核",
    "played": "玩过", "not_played": "未玩过", "unsure": "不确定", "active": "进行中",
    "completed": "已完成", "planned": "将修改", "unchanged": "不修改",
    "wish": "想收藏", "done": "已完成", "doing": "进行中", "on-hold": "搁置", "dropped": "抛弃",
}

_COLLECTION_LABELS = {
    "book": {1: "想读", 2: "读过", 3: "在读", 4: "搁置", 5: "抛弃"},
    "anime": {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"},
    "music": {1: "想听", 2: "听过", 3: "在听", 4: "搁置", 5: "抛弃"},
    "game": {1: "想玩", 2: "玩过", 3: "在玩", 4: "搁置", 5: "抛弃"},
    "real": {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"},
}
_COLLECTION_CODES = {1: "wish", 2: "done", 3: "doing", 4: "on-hold", 5: "dropped"}
_GENERIC_COLLECTION_LABELS = {1: "想收藏", 2: "已完成", 3: "进行中", 4: "搁置", 5: "抛弃"}

_PLAN_REASON_LABELS = {
    "steam_match_auto_confirm": "高置信候选，计划自动确认",
    "steam_match_manual_review": "候选需要人工核对",
    "steam_match_search_failed": "联网搜索失败",
    "steam_match_title_unavailable": "Steam 标题缺失，无法搜索",
    "steam_match_no_candidates": "搜索完成但没有候选",
    "steam_match_source_title_collision": "多个 Steam 条目标题冲突",
    "steam_match_mark_no_subject": "人工确认 Bangumi 暂无条目",
    "steam_match_mark_deferred": "人工决定暂缓处理",
    "steam_match_confirm_subject": "人工确认指定 Bangumi 条目",
    "steam_match_auth_unavailable": "Bangumi 认证连续失败，已熔断",
}


def status_label(value: object, kind: str | None = None) -> str:
    """Render a Chinese explanation while preserving the stored status code."""

    if isinstance(value, int) and value in _COLLECTION_CODES:
        label = (
            _COLLECTION_LABELS.get(kind, {}).get(value)
            if kind is not None
            else _GENERIC_COLLECTION_LABELS.get(value)
        ) or "收藏状态"
        return f"{label}（{_COLLECTION_CODES[value]}）"
    code = str(value)
    label = _STATUS_LABELS.get(code)
    return f"{label}（{code}）" if label else code


def plan_reason_label(value: object) -> str:
    code = str(value)
    label = _PLAN_REASON_LABELS.get(code)
    if label:
        return f"{label}（{code}）"
    return code.replace("_", " ")
