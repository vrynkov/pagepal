"""Which LLMs PagePal uses.

We use Groq's free tier. Each free model has a daily token limit, so we list
several models. If the first one fails (for example with a 429 "rate limit"
error), LangChain tries the next one.
"""

import os

from langchain_groq import ChatGroq

# Free Groq production models, best first. Check https://console.groq.com/docs/models
# if one disappears. You can override the list with GROQ_MODELS in .env
# (comma separated, e.g. "openai/gpt-oss-20b,llama-3.1-8b-instant").
DEFAULT_MODELS = [
    "openai/gpt-oss-120b",
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-20b",
]


def model_names() -> list[str]:
    raw = os.getenv("GROQ_MODELS", "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or DEFAULT_MODELS


def make_models() -> list[ChatGroq]:
    """One ChatGroq client per model name.

    max_retries=1: on a 429 we want to move to the next model quickly,
    not wait and retry the same model many times.
    """
    return [ChatGroq(model=name, temperature=0, max_retries=1) for name in model_names()]
