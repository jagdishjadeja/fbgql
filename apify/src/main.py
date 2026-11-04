"""Apify actor entrypoint — a thin adapter over fbgql.Scraper.

Reads the actor input, builds a ScrapeJob, and streams post results to the default
Dataset. Free — $0 developer fee; only Apify platform usage.
All scraping logic lives in the fbgql core package.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from hashlib import sha256
from urllib.parse import urlsplit

from apify import Actor

# ``is_post_url`` — page vs. single post is decided by the core, so this actor and any
# other wrapper read a pasted link the same way.
from fbgql import Account, ScrapeJob, Scraper, SessionInvalid, is_post_url
from fbgql.dates import parse_time_bound


async def _send_webhook(url: str, target: str, posts: list[dict]) -> None:
    """Deliver once; a failed delivery must not advance the new-post checkpoint."""
    import httpx

    headers = {"Content-Type": "application/json"}
    token = os.environ.get("WEBHOOK_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "status": "SUCCEEDED",
        "count": len(posts),
        "posts": posts,
        "pageOrUrl": target,
        "runId": os.environ.get("APIFY_ACTOR_RUN_ID"),
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.RequestError:
        # Avoid exposing token-bearing URLs or response bodies in actor logs.
        raise RuntimeError("Webhook delivery failed due to a network error or timeout") from None
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Webhook delivery failed: HTTP {response.status_code}")
    Actor.log.info(f"Sent {len(posts)} posts to webhook")


async def _finish_unreachable(target: str, reason: str) -> None:
    """End the run as SUCCEEDED with an explanatory row instead of failing it.

    A private, login-gated, or IP-blocked target is a property of the input, not an
    actor bug — failing the run would count against the actor's Store quality score
    for every private link a user pastes. The row lands in the dataset's ``error``
    column so the user (and API/integration callers) still see why nothing came back.
    """
    Actor.log.warning(reason)
    await Actor.push_data({"post": {"url": target}, "error": reason})
    await Actor.set_status_message(reason, is_terminal=True)


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}

        page_or_url = inp.get("pageOrUrl")
        if not page_or_url:
            raise ValueError("Input 'pageOrUrl' is required")

        webhook_url = (inp.get("webhookUrl") or "").strip()
        if webhook_url:
            parsed_url = urlsplit(webhook_url)
            if (parsed_url.scheme not in ("https", "http") or not parsed_url.hostname
                    or parsed_url.username or parsed_url.password or parsed_url.fragment):
                raise ValueError("webhookUrl must be an HTTP(S) URL without credentials or a fragment")

        # This actor is anonymous-only: it scrapes logged-out public content as actor 0
        # and takes no Facebook credentials. A public actor must never ask users to paste
        # a session cookie jar into its input. Authenticated scraping still exists in the
        # library/CLI (``Account(cookies=…)`` / ``--cookies``) for gated content.
        # Resolve a proxy URL. On the platform a configured-but-failing proxy is a hard
        # error (falling back to the actor's datacenter IP would defeat the point). For
        # local dev (`apify run` without login) degrade to a direct connection.
        proxy_url = None
        proxy_input = inp.get("proxyConfiguration")
        if proxy_input:
            try:
                proxy_cfg = await Actor.create_proxy_configuration(actor_proxy_input=proxy_input)
                proxy_url = await proxy_cfg.new_url() if proxy_cfg else None
            except Exception as exc:  # noqa: BLE001
                if Actor.is_at_home():
                    raise
                Actor.log.warning(
                    f"Proxy unavailable locally ({exc}); continuing over the direct connection."
                )

        account = Account.anonymous_account(proxy=proxy_url)
        is_post = is_post_url(page_or_url)

        # Prefer calendar fields (afterDate/beforeDate). Fall back to legacy unix
        # afterTime/beforeTime so older API callers keep working. Calendar dates are
        # midnight in dateTimezone (default UTC); unix values are absolute.
        date_tz = inp.get("dateTimezone") or "UTC"
        try:
            after_time = parse_time_bound(
                inp.get("afterDate", inp.get("afterTime")), tz=date_tz
            )
            before_time = parse_time_bound(
                inp.get("beforeDate", inp.get("beforeTime")), tz=date_tz
            )
        except ValueError as exc:
            await Actor.fail(status_message=str(exc))
            return
        if (after_time is not None or before_time is not None) and after_time is None:
            await Actor.fail(
                status_message=(
                    "Date filter needs afterDate (lower bound) so pagination knows when "
                    "to stop. maxPosts is ignored while filtering by date."
                )
            )
            return

        only_new = bool(inp.get("onlyNewPosts", False))
        store = None
        state = {}
        seen = set()
        if only_new:
            if after_time is not None or before_time is not None:
                raise ValueError("onlyNewPosts cannot be combined with explicit date filters")
            store = await Actor.open_key_value_store(
                name=inp.get("checkpointStoreName") or "fbgql-post-checkpoints"
            )
            # Keep input identity explicit: use the same target string on every run.
            state_key = sha256(page_or_url.strip().encode()).hexdigest()
            state = await store.get_value(state_key) or {}
            seen = set(state.get("post_ids", []))
            if state.get("latest_timestamp") is not None and not is_post:
                after_time = max(0, state["latest_timestamp"] - 86400)
        latest = state.get("latest_timestamp")

        job = ScrapeJob(
            page=None if is_post else page_or_url,
            post_url=page_or_url if is_post else None,
            max_posts=int(inp.get("maxPosts", 1)),
            engine=inp.get("engine", "async"),
            accounts=[account],
            anonymous=True,
            after_time=after_time,
            before_time=before_time,
            posts_only=True,
            fail_on_partial=only_new or bool(webhook_url),
            on_progress=lambda msg: Actor.log.info(msg),
        )

        tz_note = f", dateTimezone={date_tz}" if after_time or before_time else ""
        Actor.log.info(
            f"Scraping {page_or_url} (posts only, engine={job.engine}, "
            f"access=anonymous{tz_note})"
        )

        scraped = 0
        discovered = 0
        webhook_posts = []
        try:
            async for post in Scraper().astream(job):
                discovered += 1
                post_id = post.post.post_id
                if only_new and post_id in seen:
                    continue
                if webhook_url and post.error:
                    raise RuntimeError("Post extraction failed; success webhook was not sent")
                post_data = asdict(post.post)
                await Actor.push_data({"post": post_data, "error": post.error})
                if webhook_url:
                    webhook_posts.append(post_data)
                scraped += 1
                if only_new and not post.error:
                    seen.add(post_id)
                    timestamp = post.post.created_time
                    if timestamp is not None:
                        latest = max(latest or 0, timestamp)
        except SessionInvalid as exc:
            # No credentials are involved, so this means Facebook served a login wall —
            # typically a blocked/flagged IP or a target that isn't publicly visible.
            await _finish_unreachable(
                page_or_url,
                "Facebook served a login wall for this target — it may be private, "
                f"or this IP is blocked (try a residential proxy). ({exc})",
            )
            return
        except ValueError as exc:
            # Raised when the id/post can't be resolved — private or gated targets.
            await _finish_unreachable(page_or_url, str(exc))
            return

        if discovered == 0 and not is_post and not state:
            await _finish_unreachable(
                page_or_url,
                f"No posts found for {page_or_url!r}. Common causes: (1) residential "
                "proxy country/IP blocked by Facebook — switch Apify proxy country to "
                "match the audience (or try another country); (2) private / "
                "login-gated profile or group; (3) pass the numeric id or a direct "
                "post URL. See the Actor README troubleshooting section.",
            )
            return

        if webhook_url:
            await _send_webhook(webhook_url, page_or_url, webhook_posts)

        if store is not None and discovered:
            await store.set_value(state_key, {
                "target": page_or_url.strip(),
                "latest_timestamp": latest,
                "post_ids": sorted(seen),
            })

        Actor.log.info(f"Done. Pushed {scraped} posts to the dataset.")
