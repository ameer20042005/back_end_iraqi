import json
from dataclasses import replace

from app.config import Settings
from app.core.vllm_launch import launch_args


def test_launch_allows_native_audio_and_caps_kv_independently():
    args = launch_args(replace(Settings(), kv_cache_memory_gb=4, max_num_seqs=16))
    assert args[args.index("--kv-cache-memory-bytes") + 1] == str(4 * 1024**3)
    assert json.loads(args[args.index("--limit-mm-per-prompt") + 1]) == {"image": 1, "audio": 1}
    assert args[args.index("--max-num-seqs") + 1] == "16"


def test_operator_can_choose_fraction_based_cache_without_hidden_explicit_cap():
    args = launch_args(replace(Settings(), kv_cache_memory_gb=0, gpu_memory_utilization=0.6))
    assert "--kv-cache-memory-bytes" not in args
    assert args[args.index("--gpu-memory-utilization") + 1] == "0.6"
