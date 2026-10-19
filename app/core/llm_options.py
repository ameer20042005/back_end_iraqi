"""Request options shared by the project's OpenAI-compatible model clients."""


def normal_generation_options() -> dict:
    """Qwen3.5 direct-response profile shared by text clients.

    التفكير معطّل عبر chat_template_kwargs لتقليل زمن الرد.
    """
    return {
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
