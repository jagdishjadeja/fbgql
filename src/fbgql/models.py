"""Data models: job config, sessions, and the frozen v1 output schema.

Plain dataclasses (no third-party dependency). If shared validation with the Apify
input schema becomes valuable, these can be swapped for pydantic without changing the
public import surface.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# Bump when the output shape changes in a breaking way. Downstream consumers
# (datasets, backend ingest) should key off this.
SCHEMA_VERSION = 3


class Profile(str, Enum):
    """Named policy presets resolved into (workers, reply_fb_cap)."""

    DEFAULT = "default"           # workers=3, reply_fb_cap=1500  (the proven best)
    TOPS_ONLY = "tops_only"       # reply_fb_cap=0
    FULL_REPLIES = "full_replies"  # reply_fb_cap=None (not recommended as default)


# Profile -> (workers, reply_fb_cap). reply_fb_cap semantics:
#   None -> always fetch replies; 0 -> tops only; int N -> replies iff fb_count < N.
PROFILE_PRESETS: dict[Profile, tuple[int, int | None]] = {
    Profile.DEFAULT: (3, 1500),
    Profile.TOPS_ONLY: (1, 0),
    Profile.FULL_REPLIES: (3, None),
}


@dataclass
class Account:
    """An injected Facebook session. The engine consumes this — it never logs in.

    ``fb_dtsg`` may be None; it is then derived from the cookies at runtime.
    ``proxy`` should be sticky and, ideally, the same IP the cookies were minted on.

    Set ``anonymous=True`` for logged-out access: no cookies are required and the
    request is made as actor ``0`` with no ``fb_dtsg``. This is deliberately explicit —
    an account whose cookies merely *lost* ``c_user`` is a dead session, not an
    anonymous one, and must keep failing loudly.
    """

    cookies: dict[str, str] = field(default_factory=dict)
    fb_dtsg: str | None = None
    proxy: str | None = None
    role: str = "primary"  # "primary" | "mega"
    anonymous: bool = False

    @property
    def c_user(self) -> str | None:
        return self.cookies.get("c_user")

    @classmethod
    def anonymous_account(cls, proxy: str | None = None,
                          cookies: dict[str, str] | None = None) -> Account:
        """A logged-out account. ``cookies`` is optional (anonymous datr/sb if you have them)."""
        return cls(cookies=cookies or {}, proxy=proxy, anonymous=True)


@dataclass
class Session:
    """A resolved, ready-to-use account (fb_dtsg guaranteed). Internal."""

    cookies: dict[str, str]
    fb_dtsg: str
    c_user: str
    proxy: str | None = None
    role: str = "primary"


@dataclass
class ScrapeJob:
    """Everything needed to run one scrape. Config ownership lives here (not in wrappers)."""

    page: str | None = None
    post_url: str | None = None
    max_posts: int = 20

    profile: Profile | str = Profile.DEFAULT
    engine: str = "threads"  # "threads" (default) | "async"

    # Explicit overrides; when None the profile preset is used.
    workers: int | None = None
    reply_fb_cap: int | None = -1  # sentinel -1 == "use profile preset"

    # Optional. With no accounts the job runs logged-out (the default); supply one to
    # scrape as a real session and reach login-gated content.
    accounts: list[Account] = field(default_factory=list)

    # Force logged-out mode even when ``accounts`` is populated (ignores them except for
    # a proxy). Leaving this False and passing no accounts is already anonymous, so this
    # is only needed to override a configured session. Public content only.
    anonymous: bool = False

    # Dual-account routing (advanced; single-account by default).
    mega_threshold: int | None = None  # pin heaviest post to a "mega" account if set

    # Anti-throttle.
    min_interval_sec: float = 1.0
    reply_concurrency: int = 2

    # Feed date window (unix seconds). Passed to the timeline query as afterTime/beforeTime
    # and enforced client-side (inclusive start, exclusive end). When set, ``max_posts`` is
    # ignored and pagination stops at the window (needs ``after_time`` as the lower bound).
    # Group feeds only get the client-side filter.
    after_time: int | None = None
    before_time: int | None = None

    # Discover posts only — skip comment/reply pagination. Useful for date-range probes.
    posts_only: bool = False

    # Cap top-level comments scraped per post (pagination stops at this many tops).
    # None = no limit. Replies (if enabled) are only fetched for the kept tops.
    max_comments: int | None = None

    # Optional progress callback (CLI/Apify). Not part of the output schema.
    on_progress: Callable[[str], None] | None = field(default=None, repr=False, compare=False)

    def resolved_policy(self) -> tuple[int, int | None]:
        """Return (workers, reply_fb_cap) after applying profile + overrides."""
        prof = self.profile if isinstance(self.profile, Profile) else Profile(self.profile)
        w, cap = PROFILE_PRESETS[prof]
        if self.workers is not None:
            w = self.workers
        if self.reply_fb_cap != -1:  # explicit override provided
            cap = self.reply_fb_cap
        return w, cap


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


@dataclass
class Media:
    type: str            # "photo" | "video" | "sticker" | "gif" | ...
    url: str | None = None


@dataclass
class ReactionTypeCount:
    """One reaction type and how many times it was used."""

    type: str            # like | love | care | haha | wow | sad | angry | or raw FB id
    count: int
    name: str | None = None  # localized label from Facebook when present


@dataclass
class Reply:
    comment_id: str | None
    author: str | None
    text: str
    reaction_count: int
    created_time: int | None = None
    media: Media | None = None
    reactions: list[ReactionTypeCount] = field(default_factory=list)


@dataclass
class Comment:
    comment_id: str | None
    author: str | None
    text: str
    reaction_count: int
    created_time: int | None = None
    media: Media | None = None
    reply_count: int = 0
    replies: list[Reply] = field(default_factory=list)
    reactions: list[ReactionTypeCount] = field(default_factory=list)


@dataclass
class PostMedia:
    """An attached photo or video; URLs may expire or be unavailable."""

    type: str
    id: str | None = None
    url: str | None = None
    thumbnail_url: str | None = None
    hd_url: str | None = None
    sd_url: str | None = None


@dataclass
class Post:
    post_id: str
    feedback_id: str | None
    text: str
    permalink: str | None
    comment_count: int
    page_name: str | None = None
    created_time: int | None = None  # unix seconds; story.creation_time from FB
    reaction_count: int = 0
    reactions: list[ReactionTypeCount] = field(default_factory=list)
    share_count: int = 0
    media: list[PostMedia] = field(default_factory=list)


@dataclass
class PostResult:
    """Result for a single post — the unit of streaming."""

    post: Post
    comments: list[Comment] = field(default_factory=list)
    tops: int = 0
    replies: int = 0
    total_scraped: int = 0
    coverage: float = 0.0
    nested_replies_on: bool = False  # True when reply threads were fetched for this post
    elapsed_sec: float = 0.0
    worker: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = SCHEMA_VERSION
        return d


@dataclass
class Result:
    """Full run result."""

    page: str | None
    posts: list[PostResult] = field(default_factory=list)
    weighted_coverage: float = 0.0
    median_coverage: float = 0.0
    total_scraped: int = 0
    total_fb_comments: int = 0
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "page": self.page,
            "weighted_coverage": self.weighted_coverage,
            "median_coverage": self.median_coverage,
            "total_scraped": self.total_scraped,
            "total_fb_comments": self.total_fb_comments,
            "elapsed_sec": self.elapsed_sec,
            "posts": [p.to_dict() for p in self.posts],
        }

    def to_json(self, path: str | None = None, *, indent: int = 2) -> str:
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return text
