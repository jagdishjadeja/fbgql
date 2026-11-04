# Facebook Page Posts Scraper

Extract public Facebook posts from pages, profiles, groups, or a single post URL.
The Actor always runs in posts-only mode: it never fetches comments or replies.
No Facebook login is required. Apify platform usage applies.

## Input

```json
{"pageOrUrl": "ronaldo", "maxPosts": 5}
```

Use `afterDate` and optionally `beforeDate` (`YYYY-MM-DD`) to select a date window.
`dateTimezone` defaults to UTC. A date filter requires `afterDate` and overrides
`maxPosts`. Single post URLs ignore feed limits and date filters.

`proxyConfiguration` configures the proxy; residential proxy is enabled by default
in the input schema. `engine` accepts `async` (default) or `threads`.
Legacy comment-related inputs, including `postsOnly: false`, are ignored.

## Output

One dataset row per post, with `post` and `error` fields. The post contains its
`post_id`, `feedback_id`, `text`, `permalink`, `page_name`, `created_time`,
`reaction_count`, `reactions`, `share_count`, `comment_count`, and `media`.
`comment_count` is Facebook's reported count; no comment content is collected.
`error` is null on success. Unreachable targets produce an explanatory error row.

## Troubleshooting

If a public target returns no posts, try a residential proxy country matching the
target's audience or a numeric target ID. Private and login-gated targets are not
available anonymously. Start with `maxPosts: 1` before increasing the scope.

## Local development

From the repository root, install `pip install -e '.[apify]'` and run the Actor
with Apify's local runner. The underlying library and CLI retain their upstream
features; for posts-only CLI use `fbgql scrape --page ronaldo --posts-only`.

## Post images and videos

`post.media` is an array of attached photos and videos, including album items.
Each item has `type` (`photo` or `video`), `id`, `url`, `thumbnail_url`, `hd_url`,
and `sd_url`. The preferred video URL uses HD when available, otherwise SD.
Photos use `url` for the image. Missing fields are null; posts without returned
attachments have an empty array. A video thumbnail is not a playable video URL.

Media is extracted from data Facebook returns to the anonymous session. Files are
not downloaded, and Facebook CDN URLs may expire. Some media or video qualities
may be unavailable. Single-post URLs read embedded public post data; if Facebook
does not expose the matching post, the actor returns an explanatory error row.

## Only new posts

Set `onlyNewPosts: true` to remember exported post IDs in the named Apify
Key-Value Store `fbgql-post-checkpoints`. Override `checkpointStoreName` to keep
independent histories or start fresh. Use the exact same `pageOrUrl` on each run.

The first run uses `maxPosts`. Later runs scan from the latest saved timestamp
with a 24-hour overlap, ignore `maxPosts`, and export only unseen IDs. Explicit
date filters cannot be combined with this mode. Posts delayed beyond the overlap
may be missed; edits to existing posts are not exported again. If timestamps are
unavailable, discovery continues using `maxPosts` and ID deduplication.

Checkpoints are saved after dataset writes and successful completion. Interrupted
runs can repeat already-written rows on retry; dataset and checkpoint writes are
not transactional. Run sequentially per target/store to avoid concurrent updates.
No new posts produces an empty dataset. Named stores retain the exported IDs and
grow with use. Single-post URLs are deduplicated by their post ID.
