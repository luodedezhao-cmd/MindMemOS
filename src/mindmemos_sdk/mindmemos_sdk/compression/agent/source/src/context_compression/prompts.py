"""ADyn incremental and full prompts, using only the frozen TASK/M/R snapshot."""

import json
from pathlib import Path
import re

from .types import CompressionSnapshot


def build_messages(snapshot: CompressionSnapshot, method="dual_a") -> list[dict[str, str]]:
    """Only frozen TASK/M/R enter the model; no later messages or outcomes."""
    if method != "dual_a":
        raise ValueError("This ADyn package only includes dual_a prompts")
    if snapshot.compression_mode not in ("incremental", "full"):
        raise ValueError("dual_a requires incremental or full compression_mode")
    template_name = ("a_incremental_prompt.txt" if snapshot.compression_mode == "incremental"
                     else "a_full_prompt.txt")
    template = Path(__file__).with_name(template_name).read_text(encoding="utf-8")
    # Both modes see M; only the output/update policy differs. Substitute once
    # so template markers in the task, memory or raw history stay literal data.
    values = {"task": snapshot.task, "prev_summary": "\n\n".join(snapshot.memory),
              "history": json.dumps(list(snapshot.raw), ensure_ascii=False, indent=2)}
    text = re.sub(r"\{\{ (task|prev_summary|history) \}\}", lambda m: values[m[1]], template)
    return [{"role": "user", "content": text}]
