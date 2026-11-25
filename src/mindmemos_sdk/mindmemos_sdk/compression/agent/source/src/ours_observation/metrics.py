"""Authoritative proxy usage + native bypass usage; never add duplicate views."""
from collections import Counter
from copy import deepcopy
import math
from src.host_observation.metrics import summarize as host_summary

FIELDS = ('input_tokens', 'output_tokens', 'total_tokens', 'cache_read_tokens',
          'cache_write_tokens', 'reasoning_tokens', 'billed_cost_usd')


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def usage(raw, openrouter=True):
    raw = raw or {}
    prompt = raw.get('prompt_tokens_details') or {}
    completion = raw.get('completion_tokens_details') or {}
    return {k: number(v) for k, v in dict(input_tokens=raw.get('prompt_tokens'),
        output_tokens=raw.get('completion_tokens'), total_tokens=raw.get('total_tokens'),
        cache_read_tokens=prompt.get('cached_tokens'), cache_write_tokens=prompt.get('cache_write_tokens'),
        reasoning_tokens=completion.get('reasoning_tokens'), billed_cost_usd=raw.get('cost') if openrouter else None).items()}


def index(events, kind):
    selected = [e for e in events if e['event'] == kind]
    result = {e['call_id']: e for e in selected}
    if len(result) != len(selected) or None in result:
        raise ValueError('Missing or duplicate call ID: ' + kind)
    return result


def timeout_only_incomplete(measurement, termination):
    """A hard timeout kill leaves the in-flight call without usage. Timeout is a
    task outcome the benchmark defines, not an instrumentation defect, so it must
    not halt the batch. Every other gap still stops the queue."""
    if (termination or {}).get('reason') != 'timeout':
        return False
    if measurement.get('audit_write_errors', 0):
        return False
    rows = measurement.get('calls') or []
    if not rows:
        return False
    missing = ('input_tokens', 'output_tokens', 'billed_cost_usd')
    defects = [r for r in rows
               if not r.get('complete') or any(r.get(f) is None for f in missing)]
    # Only two shapes are explainable by the kill: a call still in flight, and a
    # compression whose result the policy discarded. Anything else is a defect.
    return all((r.get('missing_usage_reason') == 'incomplete_call' and not r.get('complete'))
               or (r.get('source') == 'compressor'
                   and r.get('missing_usage_reason') == 'unused_compression_policy')
               for r in defects)


def accounting_allows_queue(measurement):
    """Explicit task-end cancellation/drain/timeout gaps may continue, not other defects."""
    if measurement.get('audit_write_errors', 0):
        return False
    if (measurement.get('shutdown_only_incomplete') is True
            or measurement.get('timeout_only_incomplete') is True
            or measurement.get('unused_policy_allows_queue') is True):
        return True
    return (measurement.get('coverage_verified') is True
            and ((measurement.get('usage_totals_complete') is True
                  and measurement.get('billing_complete') is True)
                 or measurement.get('cancellation_only_incomplete') is True))


def summarize(proxy, routing, native, transcript_count=None):
    if any(e.get('schema_version') != 3 for e in proxy + routing):
        raise ValueError('Ours requires schema 3 proxy/routing events')
    if len({e.get('instance_id') for e in proxy}) > 1:
        raise ValueError('Mixed proxy instances')
    ns, ne = index(routing, 'native_started'), index(routing, 'native_finished')
    if set(ne) - set(ns):
        raise ValueError('Orphan native completion')
    rows, issues = [], []
    references = set()
    for started, finished in (('main_started', 'main_usage'), ('compressor_started', 'compressor_usage')):
        starts, ends = index(proxy, started), index(proxy, finished)
        if set(ends) - set(starts):
            raise ValueError('Orphan proxy completion')
        for call_id, start in starts.items():
            end = ends.get(call_id)
            if end and any(start.get(k) != end.get(k) for k in
                           ('phase','source','model','native_call_id','compression_id','compression_mode')):
                raise ValueError('Proxy call identity changed')
            if started == 'main_started':
                ref = start.get('native_call_id')
                if ref not in ns or any(ns[ref].get(k) != start.get(k) for k in ('phase','source','model')):
                    issues.append('unmatched_proxy_call')
                references.add(ref)
            cancelled = started == 'compressor_started' and (end or {}).get('cancelled') is True
            not_sent = cancelled and (end or {}).get('request_dispatched') is False and not (end or {}).get('usage')
            measured = {f: 0 for f in FIELDS} if not_sent else usage((end or {}).get('usage'))
            rows.append({'call_id': call_id, 'source': start['source'], 'phase': start['phase'],
                         'model': start['model'], 'returned_model': (end or {}).get('returned_model'),
                         'complete': end is not None, 'success': (end or {}).get('success'),
                         'cancelled': cancelled, 'request_dispatched': (end or {}).get('request_dispatched'),
                         'usage_basis': 'not_dispatched' if not_sent else 'upstream_usage',
                         'latency_seconds': (end or {}).get('latency_seconds'),
                         **measured})
            if start.get('compression_id') is not None:
                rows[-1]['compression_id'] = start['compression_id']
    rejected = {e.get('native_call_id') for e in proxy if e['event'] == 'history_rejected'}
    cancelled_requests = {e.get('native_call_id') for e in proxy if e['event'] == 'request_cancelled'}
    if cancelled_requests - set(ns) or cancelled_requests & references:
        issues.append('invalid_cancelled_request')
    for call_id in set(ns) - references - rejected - cancelled_requests:
        # No upstream record cannot prove a zero-cost request. Keep an unknown row.
        start = ns[call_id]
        rows.append({'call_id': 'unmatched-' + call_id, 'phase': start['phase'], 'source': start['source'],
                     'model': start['model'], 'complete': False, 'success': None, 'latency_seconds': None, **usage(None)})
        issues.append('unmatched_native_call')
    bypass = host_summary(native)
    hs, he = index(native, 'llm_started'), index(native, 'llm_finished')
    for call_id, start in hs.items():
        if start.get('provider') == 'context-compression':
            raise ValueError('Proxied usage must not also enter the native bypass ledger')
        end = he.get(call_id, {})
        rows.append({'call_id': call_id, 'phase': start['phase'], 'source': start['source'],
                     'model': start['model'], 'complete': call_id in he, 'success': end.get('success'),
                     'latency_seconds': end.get('elapsed_seconds'),
                     **usage(end.get('raw_usage'), end.get('cost_source') == 'openrouter_raw_usage_cost')})
    if any(r['phase'] not in ('execution','scorer') or r['source'] not in ('agent','host_auxiliary','compressor') for r in rows):
        raise ValueError('Invalid phase/source')
    result = {'calls': rows, 'issues': issues, 'native_calls': len(ns),
              'native_unfinished': len(set(ns) - set(ne)), 'transcript_assistant_count': transcript_count,
              'native_auto_compaction_started': bypass['native_auto_compaction_started'],
              'native_auto_compaction_succeeded': bypass['native_auto_compaction_succeeded']}
    result['history_rejected'] = sum(e['event']=='history_rejected' for e in proxy)
    result['audit_write_errors'] = sum(e['event']=='compression_audit_error' for e in proxy)
    result['passthrough_calls'] = sum(e['event']=='request_passthrough' for e in proxy)
    result['managed_requests'] = sum(e['event']=='history_ingested' for e in proxy)
    result['cancelled_before_main_dispatch'] = len(cancelled_requests)
    for row in rows:
        row['missing_usage_reason'] = ('incomplete_call' if not row['complete'] else 'missing_raw_fields') if any(row[f] is None for f in ('input_tokens','output_tokens','billed_cost_usd')) else None
    for phase in ('execution','scorer'):
        result[phase] = {}
        for source in ('agent','compressor','host_auxiliary'):
            group = [r for r in rows if r['phase'] == phase and r['source'] == source]
            result[phase][source] = {'calls': len(group), **{f: {
                'known_sum': math.fsum(r[f] for r in group if r[f] is not None),
                'unknown_calls': sum(r[f] is None for r in group),
                'total': math.fsum(r[f] for r in group) if all(r[f] is not None for r in group) else None,
            } for f in FIELDS}}
    decisions = index(proxy, 'compression_decision')
    applied = index(proxy, 'compression_applied')
    discarded = index(proxy, 'compression_discarded')
    failed = index(proxy, 'compression_failed')
    scheduled = index(proxy, 'compression_started')
    terminal = list(applied) + list(discarded) + list(failed)
    if len(set(terminal)) != len(terminal) or (set(terminal) | set(decisions)) - set(scheduled):
        raise ValueError('Invalid compression lifecycle')
    compressor_starts = index(proxy, 'compressor_started')
    parents = {k: e.get('compression_id') or k for k, e in compressor_starts.items()}
    if set(scheduled) != set(parents.values()):
        issues.append('compressor_start_mismatch')
    for parent in scheduled:
        calls = [e for k, e in compressor_starts.items() if parents[k] == parent]
        if scheduled[parent].get('mode') in ('v3', 'v4'):
            modes = [e.get('compression_mode') for e in calls]
            expected_first = scheduled[parent].get('compression_mode')
            decision = decisions.get(parent, {})
            if (not calls or len(calls) > 2 or calls[0]['call_id'] != parent
                    or modes[0] != expected_first
                    or any(e.get('compression_id') != parent for e in calls)
                    or (len(calls) == 2 and (modes != ['boundary', 'full'] or decision.get('stage_end') is not True))
                    or (parent in applied and applied[parent]['stage_end'] is True
                        and expected_first == 'boundary' and len(calls) != 2)):
                issues.append(scheduled[parent]['mode'] + '_compressor_call_mismatch')
        elif len(calls) != 1 or calls[0]['call_id'] != parent:
            issues.append('compressor_start_mismatch')
    if set(scheduled) - set(terminal):
        issues.append('compressor_incomplete')
    true = sum(e['stage_end'] is True for e in decisions.values())
    false = sum(e['stage_end'] is False for e in decisions.values())
    full = sum(e['stage_end'] is True for e in applied.values())
    result['compression'] = {'calls': len(scheduled), 'legal_true': true, 'legal_false': false,
        'cancelled': sum(r.get('cancelled', False) for r in rows if r['source']=='compressor'),
        'without_legal_decision': len(scheduled)-len(decisions), 'applied_full': full,
        'applied_incremental': len(applied)-full, 'full_decision_ratio': true/len(decisions) if decisions else None,
        'full_application_ratio': full/len(applied) if applied else None,
        'discarded': dict(Counter(e.get('cause','unknown') for e in discarded.values())),
        'failed': dict(Counter(e.get('cause', e.get('error_type','unknown')) for e in failed.values()))}
    result['compression']['full_decision_ratio'] = true/(true+false) if true+false else None
    if any(e.get('mode') in ('v3', 'v4') for e in scheduled.values()):
        result['compression'].update(events=len(scheduled), model_calls=len(compressor_starts),
            capacity_full_decisions=sum(e.get('trigger_reason') == 'full_capacity' for e in decisions.values()),
            semantic_full_applied=sum(e.get('trigger_reason') == 'semantic_boundary' for e in applied.values()),
            capacity_full_applied=sum(e.get('trigger_reason') == 'full_capacity' for e in applied.values()))
    result['log_pairing_complete'] = not issues and not result['native_unfinished'] and all(r['complete'] for r in rows) and not bypass['meter_errors']
    result['usage_totals_complete'] = all(r[f] is not None for r in rows for f in ('input_tokens','output_tokens'))
    result['billing_complete'] = all(r['billed_cost_usd'] is not None for r in rows)
    result['transcript_count_matches'] = transcript_count is not None and transcript_count == sum(e['source']=='agent' and e['phase']=='execution' for e in ns.values()) + sum(e['source']=='agent' and e['phase']=='execution' for e in hs.values())
    result['coverage_verified'] = bool(ns) and result['log_pairing_complete'] and result['transcript_count_matches']
    unknown = [r for r in rows if any(r[f] is None for f in ('input_tokens','output_tokens','billed_cost_usd'))]
    result['cancellation_only_incomplete'] = (result['coverage_verified'] and not result['audit_write_errors']
        and bool(unknown) and all(r['source']=='compressor' and r.get('cancelled') is True
            and r['complete'] and r.get('compression_id', r['call_id']) in failed
            and failed[r.get('compression_id', r['call_id'])].get('cause')=='cancelled'
            for r in unknown))
    finalized = [e for e in proxy if e['event'] == 'proxy_finalized']
    abandoned = index(proxy, 'compressor_abandoned')
    unfinished = index(proxy, 'compression_shutdown_incomplete')
    if finalized or abandoned or unfinished:
        # Canonical ledger ends at a single cutoff. late_events.jsonl is evidence,
        # never concatenate it here: doing so would mutate a published accounting snapshot.
        if len(finalized) != 1 or proxy[-1] != finalized[0]:
            raise ValueError('Missing, duplicate or nonterminal ledger finalization')
        seal = finalized[0]
        missing_calls = set(compressor_starts) - set(index(proxy, 'compressor_usage'))
        missing_jobs = set(scheduled) - set(terminal)
        if (set(abandoned) != missing_calls or set(unfinished) != missing_jobs
                or seal.get('unfinished_compressor_calls') != len(missing_calls)
                or seal.get('unfinished_compression_jobs') != len(missing_jobs)
                or (seal.get('compression_drained') is True and (missing_calls or missing_jobs))
                or any(e.get('cause') != 'shutdown_drain_timeout' for e in [*abandoned.values(), *unfinished.values()])):
            raise ValueError('Invalid shutdown-incomplete evidence')
        for call_id, event in abandoned.items():
            if any(event.get(k) != compressor_starts[call_id].get(k)
                   for k in ('phase', 'source', 'model', 'compression_id', 'compression_mode')):
                raise ValueError('Abandoned call identity changed')
        result['ledger_finalized'] = True
        result['compression_drained'] = seal.get('compression_drained') is True
        result['compression']['unfinished_at_shutdown'] = len(missing_jobs)
        result['shutdown_only_incomplete'] = bool(missing_jobs or missing_calls) and (
            seal.get('compression_drained') is False and bool(ns)
            and not result['audit_write_errors'] and not result['native_unfinished']
            and not bypass['meter_errors'] and result['transcript_count_matches']
            and set(issues) <= {'compressor_incomplete'}
            and all(r['complete'] or (r['source'] == 'compressor' and r['call_id'] in abandoned) for r in rows)
            and all(r['source'] == 'compressor' and (r['call_id'] in abandoned or (
                r.get('cancelled') is True and r['complete']
                and (failed.get(r.get('compression_id', r['call_id']), {}).get('cause') == 'cancelled'
                     or r.get('compression_id', r['call_id']) in unfinished)))
                for r in unknown))
    result['scorer_billing_status'] = 'unknown_python_grader_not_observed'
    result['coverage_scope'] = 'native pi-ai + compression proxy; excludes Python grader and external tool-service billing'
    if finalized and finalized[0].get('unused_compression_usage') == 'unknown':
        apply_unused_policy(result, applied)
    return result


def apply_unused_policy(result, applied):
    """User-requested experiment view; never erase the actual billing evidence."""
    original = result['calls']
    unused = {r['call_id'] for r in original if r['source'] == 'compressor'
              and r.get('compression_id', r['call_id']) not in applied}
    result['usage_reporting_policy'] = 'unused_compression_unknown'
    result['compression']['unused_model_calls'] = len(unused)
    # Both calls of a two-stage job follow the parent's application status.
    # Known returned billing remains available even when the experiment view
    # deliberately labels that call unknown. Old ledgers have no policy marker.
    result['actual_usage'] = {key: deepcopy(result[key]) for key in
                             ('calls', 'execution', 'scorer', 'usage_totals_complete', 'billing_complete')}
    result['unused_policy_allows_queue'] = (result['coverage_verified'] and all(
        r['call_id'] in unused for r in original
        if any(r[f] is None for f in ('input_tokens', 'output_tokens', 'billed_cost_usd'))))
    result['calls'] = deepcopy(original)
    for row in result['calls']:
        if row['source'] != 'compressor':
            continue
        row['compression_applied'] = row['call_id'] not in unused
        if row['call_id'] in unused:
            row.update({f: None for f in FIELDS})
            row['usage_basis'] = 'unused_compression_unknown'
            row['missing_usage_reason'] = 'unused_compression_policy'
    for phase in ('execution', 'scorer'):
        group = [r for r in result['calls'] if r['phase'] == phase and r['source'] == 'compressor']
        result[phase]['compressor'] = {'calls': len(group), **{f: {
            'known_sum': math.fsum(r[f] for r in group if r[f] is not None),
            'unknown_calls': sum(r[f] is None for r in group),
            'total': math.fsum(r[f] for r in group) if all(r[f] is not None for r in group) else None,
        } for f in FIELDS}}
    result['usage_totals_complete'] = all(r[f] is not None for r in result['calls'] for f in ('input_tokens', 'output_tokens'))
    result['billing_complete'] = all(r['billed_cost_usd'] is not None for r in result['calls'])
