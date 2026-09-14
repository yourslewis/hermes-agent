"""Manual live-provider canary: synthetic project, real public page, no Slack."""
import asyncio
import json
import tempfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.interview_runtime import run_interview_turn, LEGACY_SYSTEM_PROMPT


async def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        (root / 'schema.txt').write_text('Synthetic activity fields: event_id, timestamp, user_id')
        record = {'id': 'synthetic-research', 'owner': 'synthetic',
            'task': 'Clarify a synthetic event processing system', 'bank': {},
            'source': {}, 'read_roots': [str(root)], 'messages': [
                {'role': 'system', 'content': LEGACY_SYSTEM_PROMPT},
                {'role': 'user', 'content': 'Synthetic requirement: latency under five minutes.'},
                {'role': 'assistant', 'content': 'What fields are in the event?'}]}
        result = await run_interview_turn(record,
            f'Use read_file on {root}/schema.txt (this project is already approved), web_extract on https://example.com, and history_search '
            'for latency before asking the next requirement question. These are synthetic/public '
            'sources. Do not execute anything.')
        calls = [c['function']['name'] for m in result['messages'] for c in m.get('tool_calls', [])]
        assert {'read_file', 'web_extract', 'history_search'}.issubset(calls), calls
        outcomes = [json.loads(m['content']) for m in result['messages'] if m['role'] == 'tool']
        assert not any('error' in o for o in outcomes), outcomes
        assert result['kind'] == 'question', result['kind']
        assert result['messages'][:3] == record['messages']
        print(json.dumps({'kind': result['kind'], 'tools_called': calls,
                          'question': result['question'], 'legacy_prefix_preserved': True}))
        print('PASS: live model used project read, public extraction and interview history without execution.')


if __name__ == '__main__':
    asyncio.run(main())
