"""Success delivery and checkpoint ordering, with HTTPX's in-memory transport."""
import copy
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from fbgql.models import Post, PostResult


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.input = {'pageOrUrl': 'example', 'onlyNewPosts': True,
                      'webhookUrl': 'https://receiver.example/webhook'}
        self.state = {'post_ids': ['old'], 'latest_timestamp': 100000}
        self.rows = []
        self.events = []
        self.batch = [PostResult(Post('old', None, 'old', None, 0, created_time=100000)),
                      PostResult(Post('new', None, 'new', None, 0, created_time=100001))]
        self.scrape_failure = None
        self.write_failure = None
        harness = self

        class Store:
            async def get_value(self, key):
                return copy.deepcopy(harness.state)

            async def set_value(self, key, value):
                harness.events.append('checkpoint')
                harness.state = copy.deepcopy(value)

        class FakeActor:
            log = Mock()
            set_status_message = AsyncMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get_input(self):
                return harness.input

            async def open_key_value_store(self, **kwargs):
                return Store()

            async def push_data(self, row):
                if harness.write_failure:
                    raise harness.write_failure
                harness.events.append('dataset')
                harness.rows.append(row)

        path = Path(__file__).parents[1] / 'apify/src/main.py'
        spec = importlib.util.spec_from_file_location('webhook_actor', path)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'apify': SimpleNamespace(Actor=FakeActor())}):
            spec.loader.exec_module(self.module)

        async def stream(scraper, job):
            if harness.input.get('webhookUrl'):
                assert job.fail_on_partial
            for post in harness.batch:
                yield post
            if harness.scrape_failure:
                raise harness.scrape_failure

        self.scraper_patch = patch.object(self.module.Scraper, 'astream', stream)
        self.scraper_patch.start()
        self.addCleanup(self.scraper_patch.stop)

    async def test_delivery_auth_and_checkpoint_order(self):
        requests = []
        def handler(request):
            self.events.append('webhook')
            requests.append(request)
            return httpx.Response(200, json={'success': True})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {'WEBHOOK_TOKEN': 'test-secret', 'APIFY_ACTOR_RUN_ID': 'run-1'}):
            with patch.object(httpx, 'AsyncClient', return_value=client):
                await self.module.main()
        body = json.loads(requests[0].content)
        self.assertEqual(body['status'], 'SUCCEEDED')
        self.assertEqual(body['count'], 1)
        self.assertEqual([p['post_id'] for p in body['posts']], ['new'])
        self.assertIn('media', body['posts'][0])
        self.assertEqual(body['runId'], 'run-1')
        self.assertEqual(body['pageOrUrl'], 'example')
        self.assertEqual(requests[0].headers['authorization'], 'Bearer test-secret')
        self.assertEqual(self.events, ['dataset', 'webhook', 'checkpoint'])
        self.assertEqual(self.state['latest_timestamp'], 100001)

    async def test_http_failure_or_redirect_does_not_checkpoint(self):
        for status in [302, 401, 500]:
            with self.subTest(status=status):
                old_state = copy.deepcopy(self.state)
                client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, headers={'Location': 'https://other.example'})))
                with patch.object(httpx, 'AsyncClient', return_value=client):
                    with self.assertRaisesRegex(RuntimeError, f'HTTP {status}'):
                        await self.module.main()
                self.assertEqual(self.state, old_state)

    async def test_timeout_does_not_checkpoint(self):
        def handler(request):
            raise httpx.ReadTimeout('secret URL should not appear', request=request)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(httpx, 'AsyncClient', return_value=client):
            with self.assertRaisesRegex(RuntimeError, 'network error or timeout'):
                await self.module.main()
        self.assertEqual(self.state['post_ids'], ['old'])

    async def test_no_new_posts_sends_empty_batch_without_auth(self):
        self.batch = self.batch[:1]
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(204)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {'WEBHOOK_TOKEN': ''}):
            with patch.object(httpx, 'AsyncClient', return_value=client):
                await self.module.main()
        self.assertEqual(json.loads(requests[0].content)['posts'], [])
        self.assertEqual(json.loads(requests[0].content)['count'], 0)
        self.assertNotIn('authorization', requests[0].headers)

    async def test_scrape_and_dataset_failures_do_not_send(self):
        for failure_type in ['scrape_failure', 'write_failure']:
            with self.subTest(failure=failure_type):
                setattr(self, failure_type, RuntimeError('failed'))
                with patch.object(self.module, '_send_webhook', new_callable=AsyncMock) as send:
                    with self.assertRaises(RuntimeError):
                        await self.module.main()
                    send.assert_not_awaited()
                self.assertEqual(self.state['post_ids'], ['old'])
                setattr(self, failure_type, None)

    async def test_unreachable_target_does_not_send(self):
        self.batch = []
        self.state = {}
        with patch.object(self.module, '_send_webhook', new_callable=AsyncMock) as send:
            await self.module.main()
            send.assert_not_awaited()
        self.assertEqual(self.state, {})
        self.assertIn('error', self.rows[0])

    async def test_disabled_webhook_preserves_existing_flow(self):
        self.input.pop('webhookUrl')
        with patch.object(self.module, '_send_webhook', new_callable=AsyncMock) as send:
            await self.module.main()
            send.assert_not_awaited()
        self.assertEqual(self.state['post_ids'], ['new', 'old'])

    async def test_invalid_url_rejected_before_scraping(self):
        self.input['webhookUrl'] = 'file:///tmp/test'
        with self.assertRaisesRegex(ValueError, 'webhookUrl'):
            await self.module.main()
        self.assertEqual(self.rows, [])
