"""Cross-module contract: correction -> Notes -> new bank snapshot; edits win."""
import asyncio
import json
from pathlib import Path


def test_learning_notes_bank_roundtrip(tmp_path):
    from agent.interview_notes_bank import initialize_notes_bank, load_notes_bank, parse_question, render_question
    from agent.interview_questions import load_question_bank
    from agent.interview_learning import learn_message
    home = tmp_path / 'rex'
    home.mkdir()
    root = tmp_path / 'notes' / '03-Question-Bank'
    root.parent.mkdir()
    shipped = load_question_bank(tmp_path / 'unconfigured')
    initialize_notes_bank(root, shipped)
    (home / 'question-learning.json').write_text(json.dumps({'enabled': True, 'profile':'rex',
        'owner':'U', 'team':'T', 'channels':['C'], 'since':'1.0','notes_root':str(root)}))
    source = {'profile':'rex','user_id':'U','team':'T','channel':'C','thread':'th'}
    item = {'question':'How should late events be handled?', 'category':'streaming',
        'ask_when':'designing event processing', 'skip_when':'already specified',
        'resolves':'late event policy'}
    async def completion(**kwargs):
        return {'choices':[{'message':{'content':json.dumps(item)}}]}
    before = load_question_bank(home)
    outcome = asyncio.run(learn_message(home,source,'You forgot to ask how late events should be handled.',
        '2.0',completion=completion))
    assert outcome['status'] == 'added', outcome
    after = load_question_bank(home)
    added = [q for q in after['questions'] if q['question'] == item['question']]
    assert len(added) == 1
    assert len(after['questions']) == len(before['questions']) + 1
    paths = [p for p in root.glob('*.md') if p.name != 'Guidance.md' and item['question'] in p.read_text()]
    assert len(paths) == 1
    q = parse_question(paths[0].read_text())
    assert 'auto-generated' in q['tags']
    q['question'] = 'What is the policy for late and out-of-order events?'
    paths[0].write_text(render_question(q))
    manual = paths[0].read_bytes()
    replay = asyncio.run(learn_message(home,source,'You forgot to ask how late events should be handled.',
        '2.0',completion=completion))
    assert paths[0].read_bytes() == manual
    assert any(q['question'].startswith('What is the policy') for q in load_question_bank(home)['questions'])
    assert not any(q['question'].startswith('What is the policy') for q in before['questions'])
