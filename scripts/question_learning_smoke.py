"""Live configured-model canary using synthetic corrections and temporary state only."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.interview_questions import load_question_bank
from agent.interview_notes_bank import initialize_notes_bank
from agent.interview_learning import learn_message

async def main():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        home = base / 'rex'
        home.mkdir()
        root = base / '03-Question-Bank'
        initialize_notes_bank(root, load_question_bank(base / 'default'))
        (home / 'question-learning.json').write_text(json.dumps({'enabled':True,'profile':'rex',
            'owner':'synthetic','team':'synthetic','channels':['synthetic'],'since':'1.0','notes_root':str(root)}))
        source = {'profile':'rex','user_id':'synthetic','team':'synthetic','channel':'synthetic','thread':'synthetic'}
        result = await learn_message(home,source,'You forgot to ask how late and out-of-order events should be handled in a streaming system.', '2.0')
        print(json.dumps(result))
        assert result['status'] == 'added', result
        bank = load_question_bank(home)
        questions = [q for q in bank['questions'] if q['id'] == result['question_id']]
        assert len(questions) == 1
        print(json.dumps({'question':questions[0]['question'],'loaded_in_bank':True}))

if __name__ == '__main__':
    asyncio.run(main())
