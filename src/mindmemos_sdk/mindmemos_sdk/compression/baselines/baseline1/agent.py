"""Portable extraction of the original host_observation worker's Agent hooks."""
import json
from pathlib import Path

from common import docker, save
from src.agents.openclaw.runner import OpenClawAgent
from .observer.metrics import summarize

HERE = Path(__file__).resolve().parent


class ObservedHost(OpenClawAgent):
    def _set_model(self, task_id, model):
        super()._set_model(task_id, model)
        docker('exec', task_id, 'mkdir', '-p', '/tmp/host_meter')
        for name in ('install.py', 'meter.mjs', 'model_registration.py'):
            docker('cp', str(HERE / 'observer' / name), f'{task_id}:/tmp/host_meter/{name}')
        docker('exec', task_id, 'python3', '/tmp/host_meter/install.py')
        native = json.loads((HERE / 'vision_registration.json').read_text())
        docker('exec', '-i', task_id, 'python3', '/tmp/host_meter/model_registration.py',
               input=json.dumps({'model': native, 'base_url': self.openrouter_base_url,
                                 'api_key': self.openrouter_api_key}))

    def prepare_grading_transcript(self, task_id):
        docker('exec', task_id, 'python3', '-c',
               "from pathlib import Path; Path('/tmp/host_meter/phase').write_text('scorer')")
        return self.transcript_container_path

    def collect_usage(self, task_id, output_dir, elapsed_time):
        legacy = super().collect_usage(task_id, output_dir, elapsed_time)
        copied = docker('cp', f'{task_id}:/tmp/host_meter', str(output_dir / 'meter'), check=False)
        path = output_dir / 'meter/events.jsonl'
        measurement = {'status': 'not_collected', 'coverage_verified': False}
        if copied.returncode == 0 and path.is_file():
            try:
                events = [json.loads(line) for line in path.read_text().split('\n') if line.strip()]
                measurement = summarize(events)
                transcript = output_dir / 'chat.jsonl'
                count = sum(e.get('type') == 'message' and e.get('message', {}).get('role') == 'assistant'
                            for e in (json.loads(line) for line in transcript.read_text().split('\n') if line.strip()))
                measurement.update(status='collected', transcript_assistant_count=count,
                    coverage_verified=measurement['log_pairing_complete'] and measurement['usage_totals_complete']
                                      and count == measurement['execution']['agent']['calls'])
            except (OSError, ValueError, KeyError, TypeError):
                measurement = {'status': 'invalid_or_incomplete_logs', 'coverage_verified': False}
        save(output_dir / 'measurement.json', measurement)
        return legacy


def make_agent(port, key, base_url):
    return ObservedHost(port, openrouter_api_key=key, openrouter_base_url=base_url,
                        image_model='openrouter-vision/openai/gpt-5.5')
