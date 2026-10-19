import json
from dataclasses import replace

from app.config import Settings
from app.core.vllm_launch import launch_args


def test_launch_uses_qwen_parsers_and_reserves_audio_memory_for_whisper():
    args = launch_args(replace(Settings(), kv_cache_memory_gb=4, max_num_seqs=16))
    assert args[args.index("--kv-cache-memory-bytes") + 1] == str(4 * 1024**3)
    assert json.loads(args[args.index("--limit-mm-per-prompt") + 1]) == {"image": 1, "video": 0}
    assert args[args.index("--tool-call-parser") + 1] == "qwen3_coder"
    assert args[args.index("--reasoning-parser") + 1] == "qwen3"
    assert json.loads(args[args.index("--default-chat-template-kwargs") + 1]) == {"enable_thinking": False}
    assert args[args.index("--max-num-seqs") + 1] == "16"


def test_operator_can_choose_fraction_based_cache_without_hidden_explicit_cap():
    args = launch_args(replace(Settings(), kv_cache_memory_gb=0, gpu_memory_utilization=0.6))
    assert "--kv-cache-memory-bytes" not in args
    assert args[args.index("--gpu-memory-utilization") + 1] == "0.6"
