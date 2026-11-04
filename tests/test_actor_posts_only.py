"""The hosted actor must never invoke comment scraping, even for legacy inputs."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from fbgql.models import Post, PostResult


@pytest.mark.parametrize('legacy', [{}, {'postsOnly': False, 'profile': 'full_replies',
                                       'maxComments': 100, 'replyFbCap': 999}])
async def test_actor_always_extracts_posts_only(monkeypatch, legacy):
    rows = []

    class FakeActor:
        log = Mock()
        get_input = AsyncMock(return_value={'pageOrUrl': 'example', **legacy})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def push_data(self, row):
            rows.append(row)

    monkeypatch.setitem(sys.modules, 'apify', SimpleNamespace(Actor=FakeActor()))
    path = Path(__file__).parents[1] / 'apify/src/main.py'
    spec = importlib.util.spec_from_file_location('actor_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    post = Post('123', None, 'Post text', None, 42)

    async def stream(self, job):
        assert job.posts_only is True
        yield PostResult(post=post)

    monkeypatch.setattr(module.Scraper, 'astream', stream)
    await module.main()
    assert len(rows) == 1
    assert set(rows[0]) == {'post', 'error'}
    assert rows[0]['post']['comment_count'] == 42
    assert rows[0]['post']['text'] == 'Post text'
    assert rows[0]['error'] is None
