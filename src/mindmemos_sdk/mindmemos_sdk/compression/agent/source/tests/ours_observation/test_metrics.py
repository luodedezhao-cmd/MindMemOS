from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from src.ours_observation.metrics import summarize, accounting_allows_queue
from src.context_compression.core import ContextManager
from src.context_compression.types import CompressionConfig, CompressionResult
from src.context_compression.proxy import EventLog
from tests.context_compression.test_core import payload, msg, IDENTITY


def e(kind, id='a', **kw):
    return {'schema_version':3,'event':kind,'call_id':id,'instance_id':IDENTITY,'phase':'execution',
            'source':'agent','model':'openai/gpt-5.5',**kw}


def sample():
    raw={'prompt_tokens':100,'completion_tokens':30,'total_tokens':130,'cost':.123,
         'completion_tokens_details':{'reasoning_tokens':20}}
    routing=[e('native_started','native'),e('native_finished','native',success=True)]
    proxy=[e('main_started',native_call_id='native'),e('main_usage',native_call_id='native',usage=raw,success=True)]
    return proxy,routing,[]


class MeterTests(unittest.TestCase):
    def cancelled_sample(self, dispatched=True, raw=None):
        p,r,n=sample()
        p += [e('compression_started','c'),e('compressor_started','c',source='compressor'),
              e('compressor_usage','c',source='compressor',cancelled=True,request_dispatched=dispatched,usage=raw,success=False),
              e('compression_failed','c',cause='cancelled')]
        return p,r,n

    def test_cancelled_sent_usage_unknown_but_queue_continues(self):
        m=summarize(*self.cancelled_sample(),1)
        self.assertTrue(m['coverage_verified'])
        self.assertFalse(m['billing_complete'])
        self.assertFalse(m['usage_totals_complete'])
        self.assertTrue(m['cancellation_only_incomplete'])
        self.assertTrue(accounting_allows_queue(m))
        self.assertIsNone(m['execution']['compressor']['billed_cost_usd']['total'])

    def test_unsent_cancelled_is_zero_not_fake_upstream_usage(self):
        m=summarize(*self.cancelled_sample(dispatched=False),1)
        self.assertEqual(m['execution']['compressor']['input_tokens']['total'],0)
        self.assertEqual(m['execution']['compressor']['billed_cost_usd']['total'],0)
        self.assertTrue(m['billing_complete'])
        self.assertTrue(accounting_allows_queue(m))
        self.assertEqual(m['calls'][-1]['usage_basis'],'not_dispatched')

    def test_known_cancelled_usage_still_counted(self):
        m=summarize(*self.cancelled_sample(raw={'prompt_tokens':10,'completion_tokens':5,'total_tokens':15,'cost':.2}),1)
        self.assertEqual(m['execution']['compressor']['billed_cost_usd']['total'],.2)
        self.assertTrue(accounting_allows_queue(m))

    def test_other_defects_not_excused_by_cancel(self):
        for defect in ('main_usage','audit','timeout','missing_terminal','missing_pair'):
            with self.subTest(defect=defect):
                p,r,n=self.cancelled_sample()
                if defect=='main_usage': p[1]['usage']=None
                if defect=='audit': p.append(e('compression_audit_error','c'))
                if defect=='timeout': p[-1]['cause']='timeout'
                if defect=='missing_terminal': p.pop()
                if defect=='missing_pair': r.pop()
                self.assertFalse(accounting_allows_queue(summarize(p,r,n,1)))

    def test_explicit_cancel_before_main_forward_is_not_unknown_call(self):
        p,r,n=self.cancelled_sample()
        r += [e('native_started','pending'),e('native_finished','pending',success=False)]
        p += [e('request_cancelled',native_call_id='pending',cause='task_ended')]
        m=summarize(p,r,n,2)
        self.assertEqual(m['execution']['agent']['calls'],1)
        self.assertEqual(m['cancelled_before_main_dispatch'],1)
        self.assertTrue(accounting_allows_queue(m))

    def test_audit_error_reported_without_changing_billing_ledger(self):
        p,r,n=sample()
        p.append(e('compression_audit_error','c',cause='compression_applied'))
        m=summarize(p,r,n,1)
        self.assertEqual(m['audit_write_errors'],1)
        self.assertTrue(m['billing_complete'])
        self.assertEqual(m['execution']['agent']['billed_cost_usd']['total'],.123)

    def test_unique_accounting_and_missing_cache(self):
        p,r,n=sample(); m=summarize(p,r,n,1)
        self.assertTrue(m['coverage_verified'])
        self.assertEqual(m['execution']['agent']['calls'],1)
        self.assertEqual(m['execution']['agent']['output_tokens']['total'],30)
        self.assertEqual(m['execution']['agent']['billed_cost_usd']['total'],.123)
        self.assertIsNone(m['execution']['agent']['cache_write_tokens']['total'])
        self.assertEqual(m['scorer_billing_status'],'unknown_python_grader_not_observed')

    def test_missing_start_finish_and_duplicate(self):
        p,r,n=sample()
        m=summarize(p[:1],r,n,1)
        self.assertFalse(m['log_pairing_complete']); self.assertFalse(m['billing_complete'])
        self.assertIsNone(m['execution']['agent']['billed_cost_usd']['total'])
        with self.assertRaises(ValueError): summarize(p+p[-1:],r,n,1)
        with self.assertRaises(ValueError): summarize(p[1:],r,n,1)

    def test_missing_native_mapping_is_not_zero(self):
        p,r,n=sample();m=summarize([],r,n,1)
        self.assertFalse(m['coverage_verified'])
        self.assertEqual(m['execution']['agent']['billed_cost_usd']['unknown_calls'],1)

    def test_zero_and_cache_write_whitelist(self):
        p,r,n=sample();p[-1]['usage']={'prompt_tokens':0,'completion_tokens':0,'total_tokens':0,'cost':0,
                                      'prompt_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}
        with tempfile.TemporaryDirectory() as tmp:
            log=EventLog(tmp,IDENTITY)
            for item in p: log(item)
            events=[json.loads(s) for s in Path(tmp,'events.jsonl').read_text().splitlines()]
            self.assertEqual(events[-1]['usage']['prompt_tokens_details']['cache_write_tokens'],0)
        m=summarize(p,r,n,1)
        self.assertEqual(m['execution']['agent']['billed_cost_usd']['total'],0)

    def test_full_decision_not_applied_is_still_counted(self):
        p,r,n=sample()
        p += [e('compression_started','c'), e('compressor_started','c',source='compressor'),
              e('compressor_usage','c',source='compressor',usage={'prompt_tokens':10,'completion_tokens':20,'cost':.1}),
              e('compression_decision','c',stage_end=True),e('compression_discarded','c',cause='not_smaller')]
        m=summarize(p,r,n,1)
        self.assertEqual(m['compression']['legal_true'],1)
        self.assertEqual(m['compression']['applied_full'],0)
        self.assertEqual(m['compression']['full_decision_ratio'],1)
        self.assertIsNone(m['compression']['full_application_ratio'])
        self.assertEqual(m['execution']['compressor']['billed_cost_usd']['total'],.1)

    def test_native_bypass_counted_once(self):
        p,r,n=sample()
        base={'schema_version':2,'phase':'execution','source':'host_auxiliary','provider':'openrouter',
              'model':'openai/gpt-4o-mini','api':'openai-completions','call_id':'image'}
        n=[dict(base,event='llm_started'),dict(base,event='llm_finished',raw_usage={'prompt_tokens':5,'completion_tokens':2,'cost':.01},
            usage={'input_tokens':5,'output_tokens':2},usage_source='raw_chat_completions',cost_source='openrouter_raw_usage_cost',billed_cost_usd=.01)]
        m=summarize(p,r,n,1)
        self.assertEqual(len(m['calls']),2)
        self.assertEqual(m['execution']['host_auxiliary']['billed_cost_usd']['total'],.01)


class DecisionTests(unittest.TestCase):
    def manager(self,compressor,events):
        return ContextManager(compressor,CompressionConfig(trigger_tokens=80),task='the task',instance_id=IDENTITY,event_sink=events.append)

    def test_true_not_smaller_retains_raw_and_records_decision(self):
        events=[]; manager=self.manager(Mock(return_value=CompressionResult(True,.9,'end','y'*4000)),events)
        original=payload([msg('x'*800),msg('keep')]);out=manager.transform(original)
        self.assertEqual(out['messages'],original['messages'])
        decision=next(e for e in events if e['event']=='compression_decision')
        discard=next(e for e in events if e['event']=='compression_discarded')
        self.assertEqual(decision['call_id'],discard['call_id']); self.assertTrue(decision['stage_end'])

    def test_late_valid_result_has_decision_but_not_application(self):
        events=[]; manager=self.manager(Mock(return_value=CompressionResult(False,.9,'active','M')),events)
        with patch('src.context_compression.core.time.monotonic',side_effect=[0,121,122]):
            manager.transform(payload([msg('x'*800),msg('keep')]))
        self.assertTrue(any(e['event']=='compression_decision' for e in events))
        self.assertFalse(any(e['event']=='compression_applied' for e in events))
        self.assertEqual(manager._memory,())

    def test_invalid_return_not_counted_as_false(self):
        events=[]; manager=self.manager(Mock(return_value='not json'),events)
        manager.transform(payload([msg('x'*800),msg('keep')]))
        self.assertFalse(any(e['event']=='compression_decision' for e in events))
        self.assertTrue(any(e['event']=='compression_failed' for e in events))


if __name__=='__main__': unittest.main()
