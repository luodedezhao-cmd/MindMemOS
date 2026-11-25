"""Portable extraction of the original ObservedOurs worker's hooks."""
import json
from pathlib import Path

from bootstrap import HERE, SOURCE
from common import docker, read, save
from src.agents.openclaw.runner import OpenClawAgent
from src.context_compression.types import CompressionConfig
from src.ours_observation.metrics import summarize


def read_events(path):
    return [json.loads(line) for line in path.read_text().split('\n') if line.strip()] if path.exists() else []


class ObservedOurs(OpenClawAgent):
    def _set_model(self, task_id, model):
        super()._set_model(task_id, model)
        docker('exec', task_id, 'mkdir', '-p', '/tmp/ours_meter/src/host_observation',
               '/tmp/ours_meter/src/ours_observation')
        for package in ('host_observation', 'ours_observation'):
            for name in ('install.py', 'meter.mjs'):
                docker('cp', str(SOURCE / 'src' / package / name),
                       f'{task_id}:/tmp/ours_meter/src/{package}/{name}')
        docker('exec', '-w', '/tmp/ours_meter', task_id, 'python3', '-m', 'src.ours_observation.install')
        native = json.loads(docker('exec', task_id, 'cat',
            '/tmp/openclaw/context_compression/native_model.json').stdout)
        if native != read(HERE / 'native_model.json'):
            raise RuntimeError('Native model defaults differ from the original experiment')

    def prepare_grading_transcript(self, task_id):
        docker('exec', task_id, 'python3', '-c',
               "from pathlib import Path; Path('/tmp/host_meter/phase').write_text('scorer')")
        return self.transcript_container_path

    def collect_usage(self, task_id, output_dir, elapsed_time):
        legacy = super().collect_usage(task_id, output_dir, elapsed_time)
        meter = docker('cp', f'{task_id}:/tmp/host_meter', str(output_dir / 'meter'), check=False)
        compression = docker('cp', f'{task_id}:/tmp/openclaw/context_compression',
                             str(output_dir / 'compression'), check=False)
        measurement = {'coverage_verified': False, 'status': 'not_collected'}
        if meter.returncode == 0 and compression.returncode == 0:
            try:
                transcript = read_events(output_dir / 'chat.jsonl')
                count = sum(e.get('type') == 'message' and e.get('message', {}).get('role') == 'assistant'
                            for e in transcript)
                measurement = summarize(read_events(output_dir / 'compression/events.jsonl'),
                    read_events(output_dir / 'meter/routing.jsonl'),
                    read_events(output_dir / 'meter/events.jsonl'), count)
                measurement['status'] = 'collected'
            except (ValueError, KeyError, TypeError, OSError):
                measurement = {'coverage_verified': False, 'status': 'invalid_or_incomplete_logs'}
        save(output_dir / 'measurement.json', measurement)
        return legacy


def make_agent(port, key, base_url):
    return ObservedOurs(port, openrouter_api_key=key, openrouter_base_url=base_url,
                        image_model='openrouter/openai/gpt-5.5',
                        compression_config=CompressionConfig.from_dict(read(HERE / 'config.json')))
