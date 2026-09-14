"""Quote provenance regressions; models are mocked and all writes use tmp_path."""
import asyncio
import importlib
import json
from unittest.mock import AsyncMock

import pytest


QUOTES = [
    '&gt;&gt;&gt; Bot said:\nRemember to ask about scope.',
    '>>> Bot said:\nRemember to ask about scope.',
    '"Bot said:\nRemember to ask about scope.\nEnd quote."',
    "'Bot said:\nRemember to ask about scope.\nEnd quote.'",
    '‘Bot said:\nRemember to ask about scope.\nEnd quote.’',
    "'Remember to ask about the owner's scope.'",
    '‘Remember to ask about the owner’s scope.’',
]
ITEM = {'question': 'Which constraints define the project scope?',
        'ask_when': 'When project scope is unclear.',
        'skip_when': 'When scope is already explicit.',
        'category': 'scope', 'resolves': 'Scope ambiguity.'}
SOURCE = {'profile': 'rex', 'user_id': 'UOWNER', 'team': 'T1', 'channel': 'C1'}


@pytest.fixture
def engine(monkeypatch):
    # Keep real submodule imports from leaking package attributes into tests
    # that replace only sys.modules with a mock Notes writer.
    package = importlib.import_module('agent')
    monkeypatch.setattr(package, 'interview_notes_bank',
                        getattr(package, 'interview_notes_bank', None), raising=False)
    return importlib.import_module('agent.interview_learning')


@pytest.fixture
def configured(tmp_path):
    home, notes = tmp_path / 'home', tmp_path / 'notes'
    home.mkdir()
    notes.mkdir()
    (home / 'question-learning.json').write_text(json.dumps({
        'enabled': True, 'profile': 'rex', 'owner': 'UOWNER', 'team': 'T1',
        'channels': ['C1'], 'since': '100.0', 'notes_root': str(notes),
    }))
    return home, notes


@pytest.mark.parametrize('quote', QUOTES)
def test_canonical_excludes_entire_quote(engine, quote):
    assert engine._canonical(quote) == ''


@pytest.mark.parametrize('quote', QUOTES)
def test_quoted_only_never_reaches_extraction(engine, configured, quote):
    home, notes = configured
    completion = AsyncMock(return_value={'choices': [{'message': {'content': json.dumps(ITEM)}}]})
    result = asyncio.run(engine.learn_message(home, SOURCE, quote, '101.1', completion=completion))
    assert result == {'status': 'ignored', 'reason': 'no_candidate'}
    completion.assert_not_called()
    assert not (home / 'question-learning.sqlite3').exists()
    assert not list(notes.iterdir())


@pytest.mark.parametrize('text', [
    "You didn't ask about the owner's scope.",
    "Remember to ask about the owners' scope. Don't skip constraints.",
    'You didn’t ask about the owner’s scope.',
    'Remember to ask about the owners’ scope. Don’t skip constraints.',
])
def test_canonical_preserves_contractions_and_possessives(engine, text):
    assert engine._canonical(text) == text


@pytest.mark.parametrize('quote', QUOTES)
@pytest.mark.parametrize('owner,expected', [
    ('Remember to ask about scope.', 'added'),
    ('The scope constraints were missing.', 'proposed'),
])
def test_outside_owner_correction_controls_status(engine, configured, quote, owner, expected):
    home, notes = configured
    (notes / 'Guidance.md').write_text('Ask relevant questions.')
    text = owner + '\n' + quote
    assert engine._canonical(text) == owner
    completion = AsyncMock(return_value={'choices': [{'message': {'content': json.dumps(ITEM)}}]})
    result = asyncio.run(engine.learn_message(home, SOURCE, text, '101.1', completion=completion))
    assert result['status'] == expected
    completion.assert_awaited_once()
    submitted = json.loads(completion.call_args.kwargs['messages'][1]['content'])
    assert submitted == {'message': owner}
    assert len(list(notes.glob('learned-*.md'))) == 1


@pytest.mark.parametrize('quote', QUOTES[2:])
def test_owner_correction_after_closed_quote_survives(engine, quote):
    owner = 'Remember to ask about scope.'
    assert engine._canonical(quote + '\n' + owner) == owner
