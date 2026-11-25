"""Conservative Host metrics from observation events; no missing-to-zero coercion."""
import math


def summarize(events):
    starts, ends = {}, {}
    meter_errors = 0
    for event in events:
        if event.get('schema_version') != 2:
            raise ValueError('Legacy SDK-derived usage cannot be used as raw API accounting; recapture required')
        if event['event'] == 'llm_started':
            target = starts
        elif event['event'] == 'llm_finished':
            target = ends
        else:
            meter_errors += event['event'] == 'meter_error'
            continue
        call = event['call_id']
        if event.get('phase') not in ('execution', 'scorer') or event.get('source') not in ('agent', 'host_auxiliary'):
            raise ValueError('Invalid call classification')
        if call in target:
            raise ValueError('Duplicate call event; do not combine duplicate logs')
        target[call] = event
    if set(ends) - set(starts):
        raise ValueError('Completion without matching call start')
    for call, end in ends.items():
        if any(end.get(k) != starts[call].get(k) for k in ('source', 'phase', 'model', 'provider', 'api')):
            raise ValueError('Call identity changed between start and completion')
    result = {'meter_errors': meter_errors, 'observed_calls': len(starts),
              'unfinished_calls': len(set(starts) - set(ends)),
              'native_auto_compaction_started': sum(e['event'] == 'auto_compaction_start' for e in events),
              'native_auto_compaction_succeeded': sum(e['event'] == 'auto_compaction_end' and e.get('success') is True for e in events)}
    for phase in ('execution', 'scorer'):
        groups = {}
        for source in ('agent', 'host_auxiliary'):
            calls = [key for key, start in starts.items() if start['phase'] == phase and start['source'] == source]
            group = {'calls': len(calls)}
            for field in ('input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens', 'estimated_cost_usd', 'billed_cost_usd'):
                values = []
                for call in calls:
                    end = ends.get(call, {})
                    usage = end.get('usage') or {}
                    if field in ('estimated_cost_usd', 'billed_cost_usd'):
                        value = end.get(field)
                    else:
                        # API totals already include reasoning/cache components.
                        # Missing cache details do not invalidate known input total.
                        value = usage.get(field) if end.get('usage_source') == 'raw_chat_completions' else None
                    if value is not None and (type(value) not in (int, float) or value < 0 or not math.isfinite(value)):
                        raise ValueError('Invalid usage value')
                    values.append(value)
                missing = sum(v is None for v in values)
                subtotal = sum(v for v in values if v is not None)
                group[field] = {'known_sum': subtotal, 'unknown_calls': missing,
                                'total': None if missing else subtotal}
            groups[source] = group
        result[phase] = groups
    # No events cannot prove zero actual usage. A side-channel must verify coverage.
    result['log_pairing_complete'] = bool(starts) and not result['unfinished_calls'] and not meter_errors
    result['usage_totals_complete'] = bool(starts) and all(
        result[phase][source][field]['unknown_calls'] == 0
        for phase in ('execution', 'scorer') for source in ('agent', 'host_auxiliary')
        for field in ('input_tokens', 'output_tokens'))
    result['coverage_verified'] = False
    return result
