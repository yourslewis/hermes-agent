"""A real synthetic model response must not fail just for sentence case."""
import asyncio
import json

def test_natural_sentence_case_correction_extracts():
    from agent.interview_learning import _extract
    data = {'question':'How should late and out-of-order events be handled?',
        'ask_when':'Designing a streaming system with event-time processing',
        'skip_when':'Event ordering and lateness are irrelevant or already specified',
        'category':'stream-processing','resolves':'Event ordering and lateness handling requirements'}
    async def completion(**kw):
        return {'choices':[{'message':{'content':json.dumps(data)}}]}
    assert asyncio.run(_extract('You forgot to ask about late events.',completion)) == data
