import copy
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fbgql.dates  # Load before replacing the package with the actor test double.
from fbgql.models import Post, PostResult, ScrapeJob


class IncrementalTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeat_run_overlap_and_failed_write(self):
        state = {}
        rows = []
        jobs = []
        batch = [PostResult(Post('a', None, 'first', None, 0, created_time=200000))]
        fail = False

        class Store:
            async def get_value(self, key):
                return copy.deepcopy(state.get(key))

            async def set_value(self, key, value):
                state[key] = copy.deepcopy(value)

        class Actor:
            log = Mock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get_input(self):
                return {'pageOrUrl': 'example', 'onlyNewPosts': True, 'maxPosts': 2}

            async def open_key_value_store(self, **kwargs):
                assert kwargs['name'] == 'fbgql-post-checkpoints'
                return Store()

            async def push_data(self, row):
                if fail:
                    raise RuntimeError('dataset failed')
                rows.append(row)

        class Scraper:
            async def astream(self, job):
                jobs.append(job)
                for post in batch:
                    yield post

        api = SimpleNamespace(Account=SimpleNamespace(anonymous_account=lambda **kw: None),
                              ScrapeJob=ScrapeJob, Scraper=Scraper,
                              SessionInvalid=type('SessionInvalid', (Exception,), {}),
                              is_post_url=lambda value: False)
        path = Path(__file__).parents[1] / 'apify/src/main.py'
        spec = importlib.util.spec_from_file_location('incremental_actor', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'apify': SimpleNamespace(Actor=Actor()), 'fbgql': api}):
            spec.loader.exec_module(module)
            await module.main()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(jobs[-1].after_time)
            await module.main()
            self.assertEqual(len(rows), 1)
            self.assertEqual(jobs[-1].after_time, 200000 - 86400)
            self.assertTrue(jobs[-1].fail_on_partial)
            batch.append(PostResult(Post('b', None, 'same timestamp', None, 0, created_time=200000)))
            await module.main()
            self.assertEqual(len(rows), 2)
            old_state = copy.deepcopy(state)
            batch.append(PostResult(Post('c', None, 'new', None, 0, created_time=210000)))
            fail = True
            with self.assertRaises(RuntimeError):
                await module.main()
            self.assertEqual(state, old_state)
            fail = False
            await module.main()
            self.assertEqual(len(rows), 3)
            self.assertEqual(next(iter(state.values()))['latest_timestamp'], 210000)
            batch.clear()
            await module.main()
            self.assertEqual(len(rows), 3)
