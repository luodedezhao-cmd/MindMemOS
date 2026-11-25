"""Original request classification and usage helpers; no provider relay."""
import json

SUMMARY_SYSTEM = ('You are a context summarization assistant. Your task is to read a conversation between a user '
                  'and an AI coding assistant, then produce a structured summary following the exact format specified.\n\n'
                  'Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary.')

def category(body):
    for m in body.get('messages', []):
        if m.get('role') not in ('system', 'developer'):
            continue
        content = m.get('content', '')
        if isinstance(content, list):
            content = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict))
        if content == SUMMARY_SYSTEM:
            return 'compressor'
    if body.get('model', '').removeprefix('openai/') == 'gpt-5.4':
        return 'grader'
    return 'agent' if body.get('stream') else 'agent_auxiliary'

def usage_from_response(data, stream):
    if not stream:
        try:
            return json.loads(data).get('usage')
        except (ValueError, AttributeError):
            return None
    usage = None
    for line in data.decode(errors='replace').splitlines():
        if line.startswith('data: ') and line[6:] != '[DONE]':
            try:
                usage = json.loads(line[6:]).get('usage') or usage
            except (ValueError, AttributeError):
                pass
    return usage

def summarize_usage(entries):
    result = {}
    for role in ('agent', 'compressor', 'agent_auxiliary', 'grader'):
        subset = [e for e in entries if e.get('category') == role]
        with_usage = [e for e in subset if isinstance(e.get('usage'), dict)]
        result[role] = {'requests': len(subset), 'finished': sum('finished_at' in e for e in subset),
                        'requests_with_usage': len(with_usage),
                        'missing_usage': len(subset) - len(with_usage),
                        'wall_seconds_sum': sum(e.get('wall_seconds', 0) for e in subset)}
        for dest, names in {'input_tokens': ('prompt_tokens', 'input_tokens'),
                            'output_tokens': ('completion_tokens', 'output_tokens'),
                            'total_tokens': ('total_tokens',)}.items():
            result[role][dest] = sum(next((e['usage'][n] for n in names if e['usage'].get(n) is not None), 0) for e in with_usage)
    return result
