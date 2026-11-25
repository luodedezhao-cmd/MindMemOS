"""Render the Host/P0/Ours comparison from per-run measured metrics (JSONL).

This is a table renderer, not a benchmark runner or a tokenizer. Missing values
remain unknown. Input/cost must include agent, compressor and host auxiliary
calls; peak/dependency must use the frozen measurement rules, not byte estimates.
"""
import argparse
import json
import math
from pathlib import Path
import statistics

METHODS = ('Host', 'P0', 'Ours')
INPUTS = ('agent_input_tokens', 'compressor_input_tokens', 'host_aux_input_tokens')
COUNTS = ('compression_calls', 'history_errors', 'http_409_count', 'passthrough_calls')
NUMERIC = ('score', 'steps', 'peak_input_tokens', 'dependency', 'input_tokens', 'api_cost', 'elapsed_seconds') + INPUTS + COUNTS


def metrics_from_events(events, *, complete):
    """Read ONLY the unified events.jsonl, never its duplicate usage files.

    The caller must attest log completeness; a truncated log cannot establish
    zero calls or complete token totals. Missing usage still remains unknown.
    Host has no proxy: collect equivalent native metrics separately.
    """
    if complete is not True:
        raise ValueError('Event metrics require an explicitly complete event log')
    buckets = {field: [] for field in INPUTS}
    result = dict.fromkeys(COUNTS, 0)
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('Each event must be a JSON object')
        name = event.get('event')
        if name == 'compressor_usage':
            field = 'compressor_input_tokens'
            result['compression_calls'] += 1
        elif name == 'main_usage':
            source = event.get('source')
            if source not in ('agent', 'host_auxiliary'):
                raise ValueError('main_usage requires agent/host_auxiliary source')
            field = 'agent_input_tokens' if source == 'agent' else 'host_aux_input_tokens'
        else:
            counter = {'history_error': 'history_errors', 'history_rejected': 'http_409_count',
                       'request_passthrough': 'passthrough_calls'}.get(name)
            if counter:
                result[counter] += 1
            continue
        usage = event.get('usage') or {}
        if not isinstance(usage, dict):
            raise ValueError('Event usage must be a JSON object or null')
        value = usage.get('prompt_tokens', usage.get('input_tokens'))
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('Input usage must be a nonnegative integer or null')
        buckets[field].append(value)
    for field, values in buckets.items():
        known = sum(v for v in values if v is not None)
        missing = sum(v is None for v in values)
        result[field] = None if missing else known
        result[field + '_known'] = known
        result[field + '_unknown_calls'] = missing
    result['input_tokens_known'] = sum(result[f + '_known'] for f in INPUTS)
    result['input_tokens_unknown_calls'] = sum(result[f + '_unknown_calls'] for f in INPUTS)
    result['input_tokens'] = (None if result['input_tokens_unknown_calls'] else result['input_tokens_known'])
    return result


def percentile(values, q):
    values = sorted(values)
    index = (len(values) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def render(rows):
    groups = {method: {} for method in METHODS}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Each run must be a JSON object')
        method = row.get('method')
        if method not in groups or not isinstance(row.get('task_id'), str) or not row['task_id']:
            raise ValueError('Every run needs a valid method and task_id')
        if type(row.get('repeat')) is not int or row['repeat'] < 1:
            raise ValueError('repeat must be a positive integer')
        key = (row['task_id'], row['repeat'])
        if key in groups[method]:
            raise ValueError('Duplicate task/repeat in one method')
        if row.get('success') is not None and type(row['success']) is not bool:
            raise ValueError('success must be Boolean or null')
        for field in NUMERIC + tuple(f + suffix for f in (*INPUTS, 'input_tokens') for suffix in ('_known', '_unknown_calls')):
            value = row.get(field)
            if value is not None and (type(value) not in (int, float) or value < 0 or not math.isfinite(value)):
                raise ValueError(f'{field} must be nonnegative finite or null')
        for field in COUNTS + tuple(f + '_unknown_calls' for f in (*INPUTS, 'input_tokens')):
            if row.get(field) is not None and type(row[field]) is not int:
                raise ValueError(f'{field} must be an integer count')
        if all(row.get(f) is not None for f in INPUTS):
            total = sum(row[f] for f in INPUTS)
            if row.get('input_tokens') is not None and row['input_tokens'] != total:
                raise ValueError('Input total disagrees with agent/compressor/host-aux split')
            row = dict(row, input_tokens=total)
        if row.get('score') is not None and row['score'] > 1:
            raise ValueError('score must be normalized to [0,1]')
        groups[method][key] = row
    keys = set(groups['Host'])
    if not keys or any(set(group) != keys for group in groups.values()):
        raise ValueError('All three methods must contain the same nonempty task/repeat set, including failures')

    def cell(method, field, fn=sum, percent=False):
        runs = list(groups[method].values())
        vals = [r[field] for r in runs if r.get(field) is not None]
        missing = len(runs) - len(vals)
        # Incomplete per-call usage can still contribute a known subtotal.
        if fn is sum:
            vals += [r[field + '_known'] for r in runs
                     if r.get(field) is None and r.get(field + '_known') is not None]
        if not vals:
            return f'未知（{missing} 次运行缺失）'
        value = fn(vals)
        text = f'{100 * value:.2f}%' if percent else f'{value:,.2f}'
        if missing:
            text += f'（已知部分；{missing} 次运行缺失）'
        unknown_calls = sum(r.get(field + '_unknown_calls') or 0 for r in runs)
        if unknown_calls:
            text += f'（{unknown_calls} 次调用缺 usage）'
        return text

    headers = ['方法', '成功率 / 得分', 'Steps', 'Peak input tokens', 'Dependency',
               'Agent 输入', 'Compressor 输入', 'Host-aux 输入', '累计输入 token',
               '输入节省率（vs Host；完整配对）', '压缩调用数', 'history_error / HTTP 409 / 透传次数',
               'API 费用', '耗时 p50 / p95（秒）']
    lines = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |']
    exclusions = []
    for method in METHODS:
        paired = [k for k in keys if groups['Host'][k].get('input_tokens') is not None
                  and groups[method][k].get('input_tokens') is not None]
        baseline = sum(groups['Host'][k]['input_tokens'] for k in paired)
        total = sum(groups[method][k]['input_tokens'] for k in paired)
        saving = '不可计算' if not baseline else f'{100 * (1 - total / baseline):.2f}%'
        saving += f'（{len(paired)}/{len(keys)} 配对）'
        host_missing = sum(groups['Host'][k].get('input_tokens') is None for k in keys)
        method_missing_only = sum(groups['Host'][k].get('input_tokens') is not None and
                                  groups[method][k].get('input_tokens') is None for k in keys)
        exclusions.append(f'{method} 节省率排除 {len(keys) - len(paired)} 对：Host 输入缺失 {host_missing}，仅本组输入缺失 {method_missing_only}。')
        cells = [method, cell(method, 'success', statistics.mean, True) + ' / ' + cell(method, 'score', statistics.mean)]
        cells += [cell(method, f, statistics.mean) for f in ('steps', 'peak_input_tokens', 'dependency')]
        cells += [cell(method, f) for f in INPUTS]
        cells += [cell(method, 'input_tokens'), saving, cell(method, 'compression_calls'),
                  ' / '.join(cell(method, f) for f in COUNTS[1:]), cell(method, 'api_cost'),
                  cell(method, 'elapsed_seconds', lambda v: percentile(v, .5)) + ' / ' +
                  cell(method, 'elapsed_seconds', lambda v: percentile(v, .95))]
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines += ['', f'每组 {len(keys)} 次运行；包含失败。费用须使用同一货币；未知用量不按零计算。']
    for method in METHODS:
        known = [r['success'] for r in groups[method].values() if r.get('success') is not None]
        lines.append(f'{method}：失败 {known.count(False)}，完成状态未知 {len(keys) - len(known)}。')
    lines += exclusions
    lines += ['缺失时合计仅为已知部分，均值/分位数仅基于已知运行。节省率仅基于完整配对子集，可能有选择偏差，不代表全体；不同方法可能使用不同子集。',
              '压缩调用数包含失败尝试；history_error 和 HTTP 409 是同一接入问题的不同观测，不相加。接入失败可提前终止任务，低用量不能据此解释为更高效率。',
              'P0 保留原 prompt 与 indent=2 history 格式；Ours 包含联合边界规则，固定指令和序列化开销不对称，已计入 compressor 输入。两组均省略 temperature，使用服务端默认；P0 不是旧采样参数的逐项复刻。']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, required=True, help='Per-run measured metrics JSONL')
    args = parser.parse_args()
    try:
        rows = [json.loads(line) for line in args.runs.read_text().split("\n") if line.strip()]
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Each run must be a JSON object')
            if row.get('events_path'):
                path = args.runs.parent / row['events_path']
                events = [json.loads(line) for line in path.read_text().split("\n") if line.strip()]
                metrics = metrics_from_events(events, complete=row.get('events_complete'))
                if any(key in row and row[key] != value for key, value in metrics.items()):
                    raise ValueError('Measured row conflicts with event-derived metrics')
                row.update(metrics)
        print(render(rows), end='')
    except (ValueError, OSError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
