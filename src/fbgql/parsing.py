"""Pure parsers for Facebook GraphQL responses.

Facebook returns anti-JSON-hijacking prefixes (``for (;;);``) and, on some surfaces,
several JSON objects concatenated line by line. These helpers normalize that and pull
the fields we need out of deeply-nested structures using generic recursive search, so
small shape changes don't break extraction.

All functions are pure (text/dict in, data out) and shared by both engines.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from . import config
from .models import Comment, Media, Post, PostMedia, ReactionTypeCount, Reply

# ---------------------------------------------------------------------------
# Low-level JSON normalization
# ---------------------------------------------------------------------------


def _strip_prefix(text: str) -> str:
    text = text.lstrip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);") :]
    elif text.startswith("for(;;);"):
        text = text[len("for(;;);") :]
    return text


def fb_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object from a Facebook GraphQL response body."""
    text = _strip_prefix(text)
    first_line = text.split("\n", 1)[0].strip()
    if not first_line:
        return {}
    try:
        return json.loads(first_line)
    except json.JSONDecodeError:
        # Fall back to the first successfully-parsing line (streamed multi-object).
        for obj in iter_json_objects(text):
            return obj
        return {}


def iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    """Yield each parseable JSON object from a (possibly multi-object) body."""
    for line in _strip_prefix(text).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


# ---------------------------------------------------------------------------
# Generic recursive search
# ---------------------------------------------------------------------------


def deep_find_first(obj: Any, key: str) -> Any:
    """Depth-first search for the first value at ``key`` anywhere in ``obj``."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = deep_find_first(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = deep_find_first(item, key)
            if found is not None:
                return found
    return None


def deep_find_all(obj: Any, key: str, out: list | None = None) -> list:
    """Collect every value stored at ``key`` anywhere in ``obj``."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            deep_find_all(v, key, out)
    elif isinstance(obj, list):
        for item in obj:
            deep_find_all(item, key, out)
    return out


def fb_request_error(data: dict[str, Any]) -> dict[str, Any] | None:
    """Detect Facebook's non-GraphQL rejection envelope.

    Shape: ``{"error": 1357054, "errorSummary": "...", "errorDescription": "..."}``.
    Returned when the whole request is refused (account restriction / rate limit /
    bad params) rather than answered with GraphQL data or a GraphQL ``errors`` array.
    """
    if isinstance(data, dict) and data.get("error") and "errorSummary" in data:
        return {
            "code": data.get("error"),
            "summary": data.get("errorSummary"),
            "description": data.get("errorDescription"),
        }
    return None


# Facebook "you must log in" error codes — a dead/expired session, not a restriction.
_LOGIN_REQUIRED_CODES = {1357001, 1357002, 1357004}


def raise_if_rejected(data: dict[str, Any], where: str) -> None:
    """Raise on a Facebook rejection envelope.

    A login-required code means the cookies are expired/checkpointed (re-mint) and
    raises :class:`SessionInvalid`; anything else raises :class:`RequestRejected`.
    """
    err = fb_request_error(data)
    if not err:
        return
    if err["code"] in _LOGIN_REQUIRED_CODES:
        from .errors import SessionInvalid

        raise SessionInvalid(
            f"Facebook reports the session is logged out on the {where} request "
            f"(error {err['code']}: {err['summary']}). The cookies expired or the account "
            "hit a checkpoint — re-mint them (./run.sh login  or  fbgql mint-session)."
        )

    from .errors import RequestRejected

    raise RequestRejected(
        f"Facebook rejected the {where} request (error {err['code']}: {err['summary']}). "
        "This usually means the account is restricted, flagged for automation, or hard "
        f"rate-limited — normal browsing can still work. Details: {err['description']}"
    )


# Message fragments that mean the *query itself* is wrong (stale doc_id / changed
# variable schema) — not a transient empty page. These must fail loudly, not retry.
_STALE_QUERY_MARKERS = (
    "missing_required_variable_value",
    "is not defined on",
    "unknown operation",
    "no query named",
    "persisted query",
    "doc_id",
)


def graphql_stale_query_error(data: dict[str, Any]) -> dict[str, Any] | None:
    """Detect a GraphQL error that means the doc_id/variable schema is stale.

    Distinct from the retryable empty-page 1675012 case: here Facebook accepted the
    request but rejected the *query* (e.g. ``missing_required_variable_value``),
    which means the shipped doc_id or its variables no longer match the server.
    """
    if not isinstance(data, dict):
        return None
    for err in data.get("errors", []) or []:
        msg = str(err.get("message") or "")
        if any(m in msg for m in _STALE_QUERY_MARKERS):
            return {"code": err.get("code"), "message": msg, "summary": err.get("summary")}
    return None


def raise_if_doc_id_stale(data: dict[str, Any], where: str, doc_id: str) -> None:
    """Raise :class:`DocIdStale` if the query was rejected as malformed/outdated."""
    err = graphql_stale_query_error(data)
    if err:
        from .errors import DocIdStale

        raise DocIdStale(
            f"Facebook rejected the {where} query as malformed (doc_id={doc_id}): "
            f"{err['message']}. The shipped doc_id or its variable set is out of date — "
            "Facebook rotated the query. Capture a fresh request from the browser and set "
            f"FBGQL_DOC_ID_{where.upper()} (and update the payload variables)."
        )


def graphql_error_codes(data: dict[str, Any]) -> tuple[list[int], list[int]]:
    """Split top-level GraphQL errors into (warning_codes, critical_codes).

    Facebook marks soft errors with severity "WARNING" (data usually still present);
    anything else is treated as critical.
    """
    warnings: list[int] = []
    critical: list[int] = []
    for err in data.get("errors", []) or []:
        code = err.get("code")
        if code is None:
            continue
        severity = (err.get("severity") or "").upper()
        if severity == "WARNING":
            warnings.append(code)
        else:
            critical.append(code)
    return warnings, critical


# ---------------------------------------------------------------------------
# Comments / replies
# ---------------------------------------------------------------------------


@dataclass
class PageInfo:
    end_cursor: str | None
    has_next_page: bool


@dataclass
class CommentsPage:
    comments: list[Comment]
    page_info: PageInfo
    critical_codes: list[int]
    # Internal per-comment tokens needed to fetch replies, index-aligned with comments.
    reply_tokens: list[tuple[str | None, str | None]]  # (comment_feedback_id, expansion_token)


def _extract_media(node: dict[str, Any]) -> Media | None:
    """Best-effort media for media-only comments (empty text)."""
    attachments = node.get("attachments") or []
    for att in attachments:
        target = att.get("target") or att.get("media") or att.get("style_infos") or {}
        typename = (att.get("__typename") or deep_find_first(att, "__typename") or "").lower()
        url = deep_find_first(target, "uri") or deep_find_first(att, "uri")
        if "photo" in typename or "image" in typename:
            return Media(type="photo", url=url)
        if "video" in typename:
            return Media(type="video", url=url)
        if "sticker" in typename:
            return Media(type="sticker", url=url)
        if url:
            return Media(type="media", url=url)
    return None


# Stable Facebook reaction-type node ids → English keys.
_REACTION_TYPE_BY_ID: dict[str, str] = {
    "1635855486666999": "like",
    "1678524932434102": "love",
    "613557422527858": "care",
    "115940531764070": "haha",
    "478547315650144": "wow",
    "908563459236177": "sad",
    "444813342392137": "angry",
}


def _as_int(val: Any) -> int | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return None
    return None


def _parse_top_reactions(top: Any) -> list[ReactionTypeCount]:
    """Parse a ``top_reactions`` connection into typed counts."""
    if not isinstance(top, dict):
        return []
    out: list[ReactionTypeCount] = []
    for edge in top.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node") or {}
        if not isinstance(node, dict):
            node = {}
        rid = str(node.get("id") or "")
        count = _as_int(edge.get("reaction_count")) or 0
        if count <= 0 and not rid:
            continue
        rtype = _REACTION_TYPE_BY_ID.get(rid, rid or "unknown")
        name = node.get("localized_name")
        if isinstance(name, str) and not name.strip():
            name = None
        out.append(ReactionTypeCount(type=rtype, count=count, name=name))
    return out


def _reaction_total_from_feedback(feedback: dict[str, Any]) -> int:
    """Best total reaction count from a comment/reply feedback object."""
    reactors = feedback.get("reactors") or {}
    for k in ("count", "count_reduced"):
        n = _as_int(reactors.get(k))
        if n is not None:
            return n
    return 0


def _reactions_from_feedback(feedback: dict[str, Any]) -> tuple[int, list[ReactionTypeCount]]:
    """Return (total, per-type breakdown) for a comment/reply feedback node."""
    reactions = _parse_top_reactions(feedback.get("top_reactions"))
    total = _reaction_total_from_feedback(feedback)
    if total <= 0 and reactions:
        total = sum(r.count for r in reactions)
    return total, reactions


def _post_reactions(story: dict[str, Any]) -> tuple[int, list[ReactionTypeCount], int]:
    """Return (reaction_count, reactions, share_count) for a feed story."""
    reactions: list[ReactionTypeCount] = []
    # Prefer UFI feedback section (avoids nested comment reaction noise).
    ufi = ((story.get("comet_sections") or {}).get("feedback") or {})
    search_roots: list[Any] = [ufi, story.get("feedback") or {}, story]

    for root in search_roots:
        if not isinstance(root, dict) or not root:
            continue
        top = root.get("top_reactions")
        if top is None:
            top = deep_find_first(root, "top_reactions")
        parsed = _parse_top_reactions(top)
        if parsed:
            reactions = parsed
            break
    if not reactions:
        for cand in deep_find_all(story, "top_reactions"):
            parsed = _parse_top_reactions(cand)
            if parsed:
                reactions = parsed
                break

    total = sum(r.count for r in reactions)
    for root in search_roots:
        if not isinstance(root, dict) or not root:
            continue
        found_total = False
        for cand in deep_find_all(root, "reaction_count"):
            if isinstance(cand, dict):
                n = _as_int(cand.get("count"))
                if n is not None and n >= total:
                    total = n
                    found_total = True
                    break
        if found_total:
            break

    share_count = 0
    for root in search_roots:
        if not isinstance(root, dict) or not root:
            continue
        for cand in deep_find_all(root, "share_count"):
            if isinstance(cand, dict):
                n = _as_int(cand.get("count"))
            else:
                n = _as_int(cand)
            if n is not None:
                share_count = n
                break
        if share_count:
            break

    return total, reactions, share_count


def _comment_from_node(node: dict[str, Any]) -> tuple[Comment, tuple[str | None, str | None]]:
    feedback = node.get("feedback") or {}
    body = node.get("body") or {}
    text = body.get("text", "") if isinstance(body, dict) else ""
    author = (node.get("author") or {}).get("name")
    created = node.get("created_time")
    comment_id = node.get("legacy_fbid") or node.get("id")
    media = _extract_media(node) if not text else None
    reaction_count, reactions = _reactions_from_feedback(feedback)

    comment = Comment(
        comment_id=str(comment_id) if comment_id is not None else None,
        author=author,
        text=text or "",
        reaction_count=reaction_count,
        created_time=int(created) if isinstance(created, (int, float)) else None,
        media=media,
        reactions=reactions,
    )

    fb_id = feedback.get("id")
    expansion = (feedback.get("expansion_info") or {}).get("expansion_token")
    return comment, (fb_id, expansion)


def _comments_connection(data: dict[str, Any]) -> dict[str, Any]:
    node = (data.get("data") or {}).get("node") or {}
    conn = node.get("comment_rendering_instance_for_feed_location") or {}
    return conn.get("comments") or {}


def parse_comments_page(data: dict[str, Any]) -> CommentsPage:
    """Parse one page of top-level comments."""
    _warn, critical = graphql_error_codes(data)
    comments_conn = _comments_connection(data)
    edges = comments_conn.get("edges") or []
    page = comments_conn.get("page_info") or {}
    page_info = PageInfo(
        end_cursor=page.get("end_cursor"),
        has_next_page=bool(page.get("has_next_page")),
    )

    comments: list[Comment] = []
    reply_tokens: list[tuple[str | None, str | None]] = []
    for edge in edges:
        node = edge.get("node") or {}
        comment, tokens = _comment_from_node(node)
        comments.append(comment)
        reply_tokens.append(tokens)

    return CommentsPage(
        comments=comments,
        page_info=page_info,
        critical_codes=critical,
        reply_tokens=reply_tokens,
    )


def parse_replies(data: dict[str, Any]) -> list[Reply]:
    """Parse the (single-page) replies for one top-level comment."""
    node = (data.get("data") or {}).get("node") or {}
    conn = node.get("replies_connection") or node.get("comment_rendering_instance_for_feed_location") or {}
    edges = conn.get("edges") or []
    replies: list[Reply] = []
    for edge in edges:
        rnode = edge.get("node") or {}
        comment, _tokens = _comment_from_node(rnode)
        replies.append(
            Reply(
                comment_id=comment.comment_id,
                author=comment.author,
                text=comment.text,
                reaction_count=comment.reaction_count,
                created_time=comment.created_time,
                media=comment.media,
                reactions=comment.reactions,
            )
        )
    return replies


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


def extract_comment_count(node: dict[str, Any]) -> int:
    """Total comment count for a post story, tolerant of several nested shapes."""
    for cri in deep_find_all(node, "comment_rendering_instance"):
        total = deep_find_first(cri, "total_count")
        if isinstance(total, int):
            return total
    for key in ("total_comment_count", "comment_count"):
        val = deep_find_first(node, key)
        if isinstance(val, int):
            return val
    total = deep_find_first(node, "total_count")
    return total if isinstance(total, int) else 0


def _story_created_time(story: dict[str, Any]) -> int | None:
    """Unix creation time for a feed story (FB field is ``creation_time``)."""
    raw = story.get("creation_time")
    if isinstance(raw, (int, float)):
        return int(raw)
    # Dedicated timestamp section — avoid deep search (nested attached stories).
    ts_story = ((story.get("comet_sections") or {}).get("timestamp") or {}).get("story") or {}
    nested = ts_story.get("creation_time")
    if isinstance(nested, (int, float)):
        return int(nested)
    return None


def _post_media(story: dict[str, Any]) -> list[PostMedia]:
    """Read typed attachment media, excluding author avatars and comment media.

    Attachment trees include album subattachments and video delivery wrappers.
    A video preview is never used as its playable URL.
    """
    result: list[PostMedia] = []
    seen: dict[tuple[str, str], PostMedia] = {}
    excluded = {"feedback", "comments", "actors", "author", "owner", "profile_picture"}

    def url(value: Any) -> str | None:
        if isinstance(value, dict):
            value = value.get("uri") or value.get("url")
        return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None

    def find_url(node: Any, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            for value in deep_find_all(node, key):
                found = url(value)
                if found:
                    return found
        return None

    def visit(node: Any, in_attachment: bool = False) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item, in_attachment)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("__typename")
        if in_attachment and kind in {"Photo", "Video"}:
            media_id = str(node["id"]) if node.get("id") is not None else None
            thumbnail = find_url(node, ("preferred_thumbnail", "thumbnailImage", "thumbnail", "image"))
            if kind == "Photo":
                source = find_url(node, ("photo_image", "full_image", "large_image", "image", "viewer_image"))
                item = PostMedia(type="photo", id=media_id, url=source, thumbnail_url=thumbnail)
            else:
                hd = find_url(node, ("browser_native_hd_url", "playable_url_quality_hd"))
                sd = find_url(node, ("browser_native_sd_url", "playable_url"))
                source = hd or sd or find_url(node, ("playable_url_dash",))
                item = PostMedia(type="video", id=media_id, url=source,
                                 thumbnail_url=thumbnail, hd_url=hd, sd_url=sd)
            identity = media_id or item.url or item.thumbnail_url
            if identity:
                key = (item.type, identity)
                previous = seen.get(key)
                if previous is None:
                    seen[key] = item
                    result.append(item)
                else:
                    for field in ("url", "thumbnail_url", "hd_url", "sd_url"):
                        if getattr(previous, field) is None:
                            setattr(previous, field, getattr(item, field))
                    if previous.type == "video":
                        previous.url = previous.hd_url or previous.sd_url or previous.url
            return
        for key, value in node.items():
            if key not in excluded:
                visit(value, in_attachment or key in {"attachments", "subattachments"})

    visit(story)
    return result


def _story_to_post(story: dict[str, Any], page_name: str | None) -> Post | None:
    post_id = story.get("post_id") or deep_find_first(story, "post_id")
    if not post_id:
        return None
    feedback_id = deep_find_first(story, "id") if "feedback" in story else None
    feedback = story.get("feedback") or deep_find_first(story, "feedback") or {}
    feedback_id = feedback.get("id") if isinstance(feedback, dict) else None
    message = deep_find_first(story, "message") or {}
    text = message.get("text") if isinstance(message, dict) else ""
    permalink = deep_find_first(story, "wwwURL") or deep_find_first(story, "url")
    reaction_count, reactions, share_count = _post_reactions(story)
    return Post(
        post_id=str(post_id),
        feedback_id=feedback_id,
        text=text or "",
        permalink=permalink,
        comment_count=extract_comment_count(story),
        page_name=page_name,
        created_time=_story_created_time(story),
        reaction_count=reaction_count,
        reactions=reactions,
        share_count=share_count,
        media=_post_media(story),
    )


def parse_posts(text: str, page_name: str | None = None) -> tuple[list[Post], str | None]:
    """Extract posts from a timeline response and the next cursor.

    Returns (posts, end_cursor). A story is any dict carrying a ``post_id``.
    """
    posts: list[Post] = []
    end_cursor: str | None = None
    seen: set[str] = set()

    for obj in iter_json_objects(text) or []:
        # Cursor for pagination.
        cursor = deep_find_first(obj, "end_cursor")
        if cursor:
            end_cursor = cursor
        # Stories: any dict carrying a post_id.
        for story in _iter_story_dicts(obj):
            post = _story_to_post(story, page_name)
            if post and post.post_id not in seen:
                seen.add(post.post_id)
                posts.append(post)

    return posts, end_cursor


def parse_permalink_posts(html: str) -> list[Post]:
    """Extract stories from JSON script blocks in a public permalink page."""
    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.chunks: list[str] = []
            self.posts: list[Post] = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.active = dict(attrs).get("type") == "application/json"
                self.chunks = []

        def handle_data(self, data):
            if self.active:
                self.chunks.append(data)

        def handle_endtag(self, tag):
            if tag == "script" and self.active:
                posts, _ = parse_posts("".join(self.chunks))
                self.posts.extend(posts)
                self.active = False

    parser = Scripts()
    parser.feed(html)
    return parser.posts


def _iter_story_dicts(obj: Any) -> Iterator[dict[str, Any]]:
    """Yield dicts that look like a post story (carry a ``post_id`` key)."""
    if isinstance(obj, dict):
        if "post_id" in obj:
            yield obj
        for v in obj.values():
            yield from _iter_story_dicts(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_story_dicts(item)


def is_hard_first_page_error(critical_codes: list[int]) -> bool:
    """True if a first-page critical error (other than the retryable empty code)."""
    return any(c != config.EMPTY_PAGE_RETRY_CODE for c in critical_codes)
