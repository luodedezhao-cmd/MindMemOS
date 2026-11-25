"""Load the frozen ADyn overlay alongside the fixed, external benchmark."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'source'
sys.path.insert(0, str(HERE.parent / 'baselines'))
from common import digest, read, validate_benchmark


def validate_source():
    for entry in read(HERE / 'source_manifest.json')['source_files']:
        path = HERE / entry['path']
        if not path.is_file() or digest(path) != entry['packaged_sha256']:
            raise ValueError('ADyn source mismatch: ' + entry['path'])


def load_runtime(benchmark):
    """Preload the overlay before eval.run_batch adds its own root to sys.path."""
    benchmark = Path(benchmark).resolve()
    validate_benchmark(benchmark, [])
    validate_source()
    sys.path[:0] = [str(SOURCE), str(benchmark)]
    from src.agents.openclaw import runner
    from src.context_compression import integration
    if Path(runner.__file__).resolve() != SOURCE / 'src/agents/openclaw/runner.py':
        raise RuntimeError('A different OpenClaw runner was already imported')
    if Path(integration.__file__).resolve() != SOURCE / 'src/context_compression/integration.py':
        raise RuntimeError('A different compression runtime was already imported')
    from eval import run_batch
    if Path(run_batch.__file__).resolve() != benchmark / 'eval/run_batch.py':
        raise RuntimeError('Wrong benchmark evaluator imported')
    return run_batch
