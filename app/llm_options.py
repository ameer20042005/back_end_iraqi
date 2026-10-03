"""Request options shared by the project's OpenAI-compatible model clients."""


def normal_generation_options() -> dict:
    """Disable model-specific chain-of-thought modes where the server supports them."""
    return {
        "reasoning_effort": "none",
        "chat_template_kwargs": {"enable_thinking": False},
    }
