"""Manual live-model smoke; synthetic task, temporary state, no Slack calls."""
import asyncio
import json
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.interview_questions import load_question_bank
from agent.interview_runtime import run_interview_turn
from gateway.interview_store import InterviewStore

async def main():
    with tempfile.TemporaryDirectory(prefix='interview-smoke-') as directory:
        store = InterviewStore(Path(directory) / 'interviews.sqlite3')
        record = store.create('smoke', 'synthetic-user', 'Clarify requirements for a personal reading-list application.',
            load_question_bank(home=directory), {'team':'synthetic', 'channel':'synthetic', 'thread':'synthetic', 'profile':''})
        first = await run_interview_turn(record, 'Ask one question about the intended audience; do not implement anything.')
        print(json.dumps({'stage':'question', 'kind':first['kind'], 'question':first.get('question'), 'choices':first.get('choices')}, ensure_ascii=False), flush=True)
        assert first['kind'] == 'question'
        record = store.update(record['key'], record['revision'], messages=first['messages'])
        # Reopen storage to exercise persisted provider messages, not just RAM.
        record = InterviewStore(Path(directory) / 'interviews.sqlite3').get(record['key'])
        second = await run_interview_turn(record, 'Audience is only me. Summarize known requirements and open questions now.', intent='summary')
        print(json.dumps({'stage':'summary', 'kind':second['kind'], 'text':second.get('text')}, ensure_ascii=False), flush=True)
        assert second['kind'] == 'summary'
        record = store.update(record['key'], record['revision'], messages=second['messages'])
        third = await run_interview_turn(record, 'The owner explicitly approved Create the plan. Produce a short proposal only.', intent='plan')
        print(json.dumps({'stage':'plan', 'kind':third['kind'], 'text':third.get('text')}, ensure_ascii=False), flush=True)
        assert third['kind'] == 'plan'
        print('PASS: live model question -> persisted summary -> explicitly authorized plan; no execution tools available.', flush=True)

if __name__ == '__main__':
    asyncio.run(main())
