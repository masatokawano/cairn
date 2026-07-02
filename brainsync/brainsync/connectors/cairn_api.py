"""Cairn HTTP API connector（read-only）。

cairn.db は直接開かない。アクセスは HTTP API のみ（責務分界の不変条件 2）。
updated_after による増分取得への移行は T4（それまでは全件取得 + クライアント側フィルタ）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brainsync import httpjson

LOOKBACK_DAYS = 7
MIN_MESSAGES = 4

# タイトルによる除外はハードコードの暫定措置。設定ファイルへの外出しは T4-6。
EXCLUDED_TITLE_PREFIXES = (
    "Review this change for security vulnerabilities.",
    "You are a security expert reviewing",
)

EXCLUDED_EXACT_TITLES = {
    "New chat",
    "User Request: Help Needed",
    "Untitled",
}


def is_available(base_url: str, timeout: float = 5) -> bool:
    """Cairn API が応答するか（/api/stats）。落ちていれば前回出力を温存する。"""
    return httpjson.ping(f"{base_url.rstrip('/')}/api/stats", timeout=timeout)


def fetch_conversations(
    base_url: str,
    *,
    limit: int = 500,
    timeout: float = 15,
    get=httpjson.get_json,
) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/conversations?limit={int(limit)}"
    payload, _ = get(url, timeout=timeout)
    return payload.get("results", [])


def is_review_candidate(item: dict) -> bool:
    title = (item.get("title") or "").strip()
    message_count = int(item.get("message_count", 0))

    if not title:
        return False
    if title in EXCLUDED_EXACT_TITLES:
        return False
    if title.startswith(EXCLUDED_TITLE_PREFIXES):
        return False
    # 単発質問や自動処理ログを週次レビューから除外する。
    if message_count < MIN_MESSAGES:
        return False
    return True


def select_recent_reviews(
    conversations: list[dict],
    *,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
) -> list[dict]:
    """直近 lookback_days 日に更新されたレビュー候補を新しい順で返す。"""
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)

    recent = [
        item
        for item in conversations
        if httpjson.parse_timestamp(item["updated_at"]) >= cutoff
        and is_review_candidate(item)
    ]
    recent.sort(
        key=lambda item: httpjson.parse_timestamp(item["updated_at"]),
        reverse=True,
    )
    return recent


class CheckError(Exception):
    """接続確認でレスポンス形状が契約に合わなかった。"""


def check(base_url: str, *, get=httpjson.get_json) -> list[dict]:
    """接続確認: 3 件取得してレスポンス形状を検証し、結果を返す。"""
    results = fetch_conversations(base_url, limit=3, get=get)
    if not isinstance(results, list):
        raise CheckError("results が配列ではありません")
    for item in results:
        if not (
            isinstance(item.get("id"), int)
            and isinstance(item.get("source"), str)
            and isinstance(item.get("title"), str)
            and isinstance(item.get("updated_at"), str)
            and isinstance(item.get("message_count"), int)
        ):
            raise CheckError(f"conversation の形が契約に合いません: {item.get('id')!r}")
    return results
