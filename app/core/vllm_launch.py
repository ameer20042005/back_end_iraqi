"""One testable source of vLLM flags; no credentials in the launch plan."""

from app.config import settings


def launch_args(config=settings) -> list[str]:
    args = [
        "--model", config.model_name, "--host", "127.0.0.1", "--port", str(config.vllm_port),
        "--max-model-len", str(config.max_model_len),
        "--gpu-memory-utilization", str(config.gpu_memory_utilization),
        "--max-num-seqs", str(config.max_num_seqs),
        "--max-num-batched-tokens", str(config.max_num_batched_tokens),
        "--async-scheduling", "--enable-prefix-caching",
        "--enable-auto-tool-choice", "--tool-call-parser", "gemma4",
        "--reasoning-parser", "gemma4",
        "--default-chat-template-kwargs", '{"enable_thinking":true}',
        "--limit-mm-per-prompt", '{"image":1,"audio":1}',
    ]
    if config.kv_cache_memory_gb > 0:
        args += ["--kv-cache-memory-bytes", str(int(config.kv_cache_memory_gb * 1024**3))]
    return args


if __name__ == "__main__":
    print("\n".join(launch_args()))
