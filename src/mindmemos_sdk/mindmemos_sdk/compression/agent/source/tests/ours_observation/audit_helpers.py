"""Original snapshot audit functions, extracted without batch dependencies."""
from src.context_compression.core import estimated_tokens, memory_messages
from src.context_compression.types import CompressionConfig

def audit_capacity_snapshot(snapshot):
    config = CompressionConfig.from_dict(snapshot['config'])
    assert config.method in ('dual_threshold', 'dual_p1', 'dual_mini', 'dual_neutral', 'dual_a', 'v3', 'v4') and snapshot['raw'], 'Wrong capacity snapshot'
    incremental_bar, full_bar = config.trigger_tokens, config.full_trigger_tokens
    if config.dynamic_thresholds:
        # Recompute the sizing rule from its recorded inputs, then require the
        # run to have used exactly that: the bars are evidence, not an input.
        incremental_bar = max(config.trigger_tokens, 2 * snapshot['max_prior_increment'])
        full_bar = 2 * max(incremental_bar, snapshot['memory_after_last_full'])
    if 'effective_trigger_tokens' in snapshot:
        assert snapshot['effective_trigger_tokens'] == incremental_bar, 'Incremental bar mismatch'
        assert snapshot['effective_full_trigger_tokens'] == full_bar, 'Full bar mismatch'
    combined = memory_messages(snapshot['memory']) + snapshot['raw']
    if estimated_tokens(combined) >= full_bar:
        mode = 'full'
    else:
        assert estimated_tokens(snapshot['raw']) >= incremental_bar, 'Below incremental threshold'
        mode = 'boundary' if config.method in ('v3', 'v4') else 'incremental'
    assert snapshot.get('compression_mode') == mode, 'Capacity decision mismatch'
    return mode


def audit_memory_update(snapshot, result, applied):
    assert type(result['stage_end']) is bool and result['stage_end'] == applied['stage_end']
    assert isinstance(result['C'], str) and result['C'].strip(), 'Missing summary text'
    expected = [result['C']] if result['stage_end'] else snapshot['memory'] + [result['C']]
    assert applied['memory_after'] == expected, 'Append/replacement differs from compressor output'
