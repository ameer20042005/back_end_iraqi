"""Request options shared by the project's OpenAI-compatible model clients."""


def normal_generation_options() -> dict:
    """Sampling profile matching the requested Gemma 4 runner settings.

    التفكير (chain-of-thought) معطّل: مع reasoning_effort="high" كلّف رد من
    كلمتين ~794 توكن و17 ثانية على LM Studio، مقابل أقل من ثانيتين بدونه.
    """
    return {
        "top_p": 0.8,
        "top_k": 64,
        "min_p": 0.05,
        "repetition_penalty": 1.1,
        "reasoning_effort": "none",
        "chat_template_kwargs": {"enable_thinking": False},
    }
