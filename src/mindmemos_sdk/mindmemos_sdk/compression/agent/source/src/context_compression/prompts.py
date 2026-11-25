"""Joint decision and compression using the archived original v1 prompt."""

import json
from pathlib import Path

from .types import CompressionSnapshot


JOINT_SYSTEM_PROMPT = Path(__file__).with_name("joint_prompt_v1.txt").read_text(encoding="utf-8")
V3_SYSTEM_PROMPT = Path(__file__).with_name("incremental_boundary_prompt_v3.txt").read_text(encoding="utf-8")
V4_SYSTEM_PROMPT = Path(__file__).with_name("incremental_boundary_prompt_v4.txt").read_text(encoding="utf-8")


def build_messages(snapshot: CompressionSnapshot, method="ours") -> list[dict[str, str]]:
    """Only frozen TASK/M/R enter the model; no later messages or outcomes."""
    if method in ("v3", "v4"):
        if snapshot.compression_mode == "full":
            return build_messages(snapshot, "p1" if method == "v4" else "p0")
        if snapshot.compression_mode != "boundary":
            raise ValueError(f"{method} requires boundary or full compression_mode")
        return [
            {"role": "system", "content": V4_SYSTEM_PROMPT if method == "v4" else V3_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"TASK": snapshot.task,
                "NEW_RAW_TRAJECTORY": list(snapshot.raw)}, ensure_ascii=False, allow_nan=False)},
        ]
    if method in ("p0", "p1", "dual_threshold", "dual_p1", "dual_mini", "dual_neutral", "dual_a"):
        if method in ("dual_threshold", "dual_p1", "dual_mini", "dual_neutral", "dual_a") and snapshot.compression_mode not in ("incremental", "full"):
            raise ValueError("dual_threshold requires a predetermined compression_mode")
        template_name = ("a_incremental_prompt.txt" if method == "dual_a" and snapshot.compression_mode == "incremental"
                         else "a_full_prompt.txt" if method == "dual_a"
                         else "p_neutral_prompt.txt" if method == "dual_neutral"
                         else "p_mini_prompt.txt" if method == "dual_mini"
                         else "p1_prompt.txt" if method in ("p1", "dual_p1") else "p0_prompt.txt")
        template = Path(__file__).with_name(template_name).read_text(encoding="utf-8")
        # Single substitution pass: raw content containing template markers is data.
        import re
        # dual_a keeps M visible at an incremental trigger: its Supersedes rule
        # needs the earlier claim in view. The other dual_* methods hide it.
        memory = () if method in ("dual_threshold", "dual_p1", "dual_mini", "dual_neutral") and snapshot.compression_mode == "incremental" else snapshot.memory
        values = {"task": snapshot.task, "prev_summary": "\n\n".join(memory),
                  "history": json.dumps(list(snapshot.raw), ensure_ascii=False, indent=2)}
        text = re.sub(r"\{\{ (task|prev_summary|history) \}\}", lambda m: values[m[1]], template)
        return [{"role": "user", "content": text}]
    inputs = {
        "TASK": snapshot.task,
        "EXISTING_COMPRESSED_MEMORY": list(snapshot.memory),
        "NEW_RAW_TRAJECTORY": list(snapshot.raw),
    }
    return [
        {"role": "system", "content": JOINT_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(inputs, ensure_ascii=False, allow_nan=False)},
    ]
