"""Structural validation of captured Anthropic Messages exchanges.

Reference: @anthropic-ai/sdk 0.110.0, resources/messages/messages.ts,
ToolResultBlockParam and ToolUseBlockParam. No behavioral labels are inferred.
"""
import json


CHECKS = ['tool_result_id', 'preceding_assistant_join', 'result_block_order',
          'next_user_answers', 'unique_tool_ids', 'cross_span_call_id',
          'cross_span_content', 'cross_span_timing', 'successful_request_valid']


def blocks(message):
    content = message.get('content', [])
    return content if isinstance(content, list) else []


def request_errors(request):
    errors, seen = [], set()
    messages = request.get('messages')
    if not isinstance(messages, list) or not messages:
        return ['request messages missing']
    for n, message in enumerate(messages):
        if not isinstance(message, dict):
            errors.append('invalid message'); continue
        uses = [b for b in blocks(message) if b.get('type') == 'tool_use']
        for b in uses:
            ident = b.get('id')
            if not isinstance(ident, str) or not ident:
                errors.append('tool_use missing id')
            elif ident in seen:
                errors.append('duplicate tool_use id')
            seen.add(ident)
        previous = messages[n-1] if n else {}
        prior = {b.get('id') for b in blocks(previous) if b.get('type') == 'tool_use'}
        result_ids, other_seen = [], False
        for b in blocks(message):
            if b.get('type') != 'tool_result':
                other_seen = True; continue
            ident = b.get('tool_use_id')
            if not isinstance(ident, str) or not ident:
                errors.append('tool_result missing tool_use_id')
            if message.get('role') != 'user' or previous.get('role') != 'assistant' or ident not in prior:
                errors.append('tool_result does not join immediately preceding assistant')
            if other_seen:
                errors.append('tool_result blocks must come first')
            result_ids.append(ident)
        if len(set(result_ids)) != len(result_ids):
            errors.append('duplicate tool_result id')
        if uses and message.get('role') != 'assistant':
            errors.append('tool_use outside assistant message')
        # Request MessageParam omits stop_reason. A transcript assistant tool_use
        # must therefore also be answered, even if stop_reason was not retained.
        if uses or message.get('stop_reason') == 'tool_use':
            following = messages[n+1] if n+1 < len(messages) else {}
            answered = {b.get('tool_use_id') for b in blocks(following) if b.get('type') == 'tool_result'}
            if following.get('role') != 'user' or {b.get('id') for b in uses} != answered:
                errors.append('tool_use not answered in next user message')
    return errors


def decode(raw):
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


def validate(llms, tools):
    errors, emitted, requests = [], {}, []
    for event in llms:
        ident, attrs = event['id'], event.get('attrs', {})
        try:
            request = decode(event.get('input'))
            if not isinstance(request, dict):
                raise ValueError('request is not an object')
            errors.extend(ident + ': ' + error for error in request_errors(request))
            requests.append((event, request))
        except (ValueError, TypeError):
            errors.append(ident + ': request not captured as valid JSON')
        try:
            response = decode(event.get('output'))
            if not isinstance(response, dict):
                raise ValueError('response is not an object')
        except (ValueError, TypeError):
            if attrs.get('status_code') == 'STATUS_CODE_OK' and not event.get('error'):
                errors.append(ident + ': successful response missing')
            continue
        for b in blocks(response):
            if b.get('type') == 'tool_use':
                call_id = b.get('id')
                if not call_id or call_id in emitted:
                    errors.append(ident + ': duplicate or missing emitted tool_use id')
                emitted[call_id] = (event, b)
        if response.get('stop_reason') == 'tool_use':
            ids = {b.get('id') for b in blocks(response) if b.get('type') == 'tool_use'}
            # Full cross-span history is checked below, after all requests load.
            if not ids:
                errors.append(ident + ': tool_use stop without tool_use block')
    for event in tools:
        attrs = event.get('attrs', {})
        call_id = attrs.get('gen_ai_tool_call_id') or attrs.get('gen_ai.tool.call.id')
        if call_id not in emitted:
            errors.append(event['id'] + ': TOOL id has no earlier emitted tool_use'); continue
        emitter, block = emitted[call_id]
        try:
            if int(attrs['chq_tsns']) < int(emitter['attrs']['end_tsns']):
                errors.append(event['id'] + ': TOOL starts before emitting LLM ends')
            consumers = []
            for consumer, request in requests:
                for message in request.get('messages', []):
                    for result in blocks(message):
                        if result.get('type') == 'tool_result' and result.get('tool_use_id') == call_id:
                            consumers.append(consumer)
                            if result.get('content') != event.get('output'):
                                errors.append(event['id'] + ': TOOL output differs from tool_result.content')
                            if int(attrs['end_tsns']) > int(consumer['attrs']['chq_tsns']):
                                errors.append(event['id'] + ': TOOL ends after consuming LLM starts')
            if not consumers:
                errors.append(event['id'] + ': TOOL result has no consuming LLM')
        except (KeyError, TypeError, ValueError):
            errors.append(event['id'] + ': timing missing or invalid')
    for call_id, (emitter, block) in emitted.items():
        later = sorted(((e, r) for e, r in requests if int(e['attrs']['chq_tsns']) >= int(emitter['attrs']['end_tsns'])), key=lambda x: int(x[0]['attrs']['chq_tsns']))
        if not later:
            errors.append(emitter['id'] + ': emitted tool_use not answered in next user message')
        else:
            req = later[0][1]
            pairs = [(i, m) for i, m in enumerate(req['messages']) if m.get('role') == 'assistant' and any(b.get('id') == call_id for b in blocks(m))]
            if not pairs:
                errors.append(emitter['id'] + ': emitted tool_use absent from next request')
            else:
                i, m = pairs[-1]
                nxt = req['messages'][i+1] if i+1 < len(req['messages']) else {}
                if nxt.get('role') != 'user' or not any(b.get('tool_use_id') == call_id for b in blocks(nxt)):
                    errors.append(emitter['id'] + ': emitted tool_use not answered in next user message')
    return sorted(set(errors))
