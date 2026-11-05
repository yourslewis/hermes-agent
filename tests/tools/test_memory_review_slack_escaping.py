"""Slack plain-text transport escaping must not break exact card binding."""
import copy
import pytest
from tests.tools.test_memory_review import Slack, home, stage
from tools.memory_tool import load_on_disk_store
from tools.memory_review import MemoryReview


class EscapingSlack(Slack):
    async def conversations_history(self, **kw):
        response = copy.deepcopy(await super().conversations_history(**kw))
        def escape(node):
            if isinstance(node, dict):
                if node.get('type') == 'plain_text':
                    node['text'] = node['text'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                for value in node.values():
                    escape(value)
            elif isinstance(node, list):
                for value in node:
                    escape(value)
        escape(response)
        return response


@pytest.mark.asyncio
async def test_slack_escaped_review_delivery_and_callback(home):
    rec = stage(load_on_disk_store(), action='add', content='A & B < C > D; literal &amp; stays literal.')
    slack = EscapingSlack()
    service = MemoryReview(slack)
    await service.flush()
    from tools.memory_review import read
    current = read(rec['id'])
    assert current.get('card_dirty') is False
    assert await service.verify_card(current)
    button = slack.messages['1.1']['blocks'][-1]['elements'][0]
    result = await service.callback({'user': {'id': 'OWNER'}, 'team': {'id': 'T1'},
        'channel': {'id': 'D1'}, 'message': {'ts': '1.1'}, 'actions': [button]})
    assert result == 'Applied'
    assert load_on_disk_store().memory_entries == rec['review']['after_entries']


@pytest.mark.asyncio
@pytest.mark.parametrize('entity,char', [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>')])
async def test_literal_entity_cannot_be_replaced_by_decoded_character(home, entity, char):
    rec = stage(load_on_disk_store(), action='add', content=f'Literal {entity} stays literal.')
    slack = EscapingSlack()
    service = MemoryReview(slack)
    await service.flush()
    from tools.memory_review import read
    current = read(rec['id'])
    assert await service.verify_card(current)
    slack.messages['1.1']['blocks'][0]['text']['text'] = slack.messages['1.1']['blocks'][0]['text']['text'].replace(entity, char)
    assert not await service.verify_card(current)


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['text', 'value'])
async def test_escaped_card_still_rejects_changed_content(home, tamper):
    rec = stage(load_on_disk_store(), action='add', content='A & B')
    slack = EscapingSlack()
    service = MemoryReview(slack)
    await service.flush()
    from tools.memory_review import read
    current = read(rec['id'])
    blocks = slack.messages['1.1']['blocks']
    if tamper == 'text':
        blocks[0]['text']['text'] += ' changed'
    else:
        blocks[-1]['elements'][0]['value'] += 'changed'
    assert not await service.verify_card(current)
