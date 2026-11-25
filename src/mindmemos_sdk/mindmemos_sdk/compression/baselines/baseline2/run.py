"""Run fixed-threshold synchronous full compression with Qwen122B."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import main

if __name__ == '__main__':
    main('baseline2')
