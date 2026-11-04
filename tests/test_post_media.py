"""Attachment parsing contracts; synthetic payloads, no network needed."""
import json
import unittest

from fbgql.parsing import parse_permalink_posts, parse_posts
from fbgql.models import PostResult


def post_with(attachments, **extra):
    posts, _ = parse_posts(json.dumps({'post_id': '123', 'attachments': attachments, **extra}))
    return posts[0]


class PostMediaTests(unittest.TestCase):
    def test_album_and_deduplication(self):
        photo = {'__typename': 'Photo', 'id': 'a', 'image': {'uri': 'https://cdn/a.jpg'}}
        post = post_with([{'subattachments': {'nodes': [
            {'media': photo}, {'media': photo},
            {'media': {'__typename': 'Photo', 'id': 'b', 'image': {'uri': 'https://cdn/b.jpg'}}},
        ]}}])
        self.assertEqual([m.url for m in post.media], ['https://cdn/a.jpg', 'https://cdn/b.jpg'])
        self.assertEqual(PostResult(post).to_dict()['post']['media'][0]['type'], 'photo')

    def test_video_prefers_hd_and_preserves_thumbnail(self):
        post = post_with([{'media': {'__typename': 'Video', 'id': 'v',
            'preferred_thumbnail': {'image': {'uri': 'https://cdn/thumb.jpg'}},
            'videoDeliveryResponseFragment': {'videoDeliveryResponseResult': {
                'progressive_urls': [{'browser_native_hd_url': 'https://cdn/hd.mp4',
                                      'browser_native_sd_url': 'https://cdn/sd.mp4'}]}}}}])
        self.assertEqual(post.media[0].url, 'https://cdn/hd.mp4')
        self.assertEqual(post.media[0].sd_url, 'https://cdn/sd.mp4')
        self.assertEqual(post.media[0].thumbnail_url, 'https://cdn/thumb.jpg')

    def test_preview_is_not_playable_video(self):
        post = post_with([{'media': {'__typename': 'Video', 'id': 'v',
                                    'image': {'uri': 'https://cdn/preview.jpg'}}}])
        self.assertIsNone(post.media[0].url)
        self.assertEqual(post.media[0].thumbnail_url, 'https://cdn/preview.jpg')

    def test_excludes_comments_and_avatars(self):
        photo = {'attachments': [{'media': {'__typename': 'Photo', 'id': 'avatar',
                                            'image': {'uri': 'https://cdn/avatar.jpg'}}}]}
        post = post_with([], actors=[photo], feedback=photo)
        self.assertEqual(post.media, [])

    def test_merges_duplicate_video_quality(self):
        post = post_with([{'media': {'__typename': 'Video', 'id': 'v',
                                    'playable_url': 'https://cdn/sd.mp4'}},
                          {'media': {'__typename': 'Video', 'id': 'v',
                                    'playable_url_quality_hd': 'https://cdn/hd.mp4'}}])
        self.assertEqual(len(post.media), 1)
        self.assertEqual(post.media[0].url, 'https://cdn/hd.mp4')

    def test_permalink_json_and_missing_media(self):
        html = '<script type="application/json">' + json.dumps({
            'post_id': '123', 'message': {'text': 'caption'},
            'attachments': [{'media': {'__typename': 'Photo', 'id': 'p',
                                      'image': {'uri': 'https://cdn/p.jpg'}}}]
        }) + '</script><script>alert("ignored")</script>'
        posts = parse_permalink_posts(html)
        self.assertEqual(posts[0].text, 'caption')
        self.assertEqual(posts[0].media[0].url, 'https://cdn/p.jpg')
        self.assertEqual(post_with(None).media, [])
        self.assertEqual(parse_permalink_posts('<html>Login required</html>'), [])
