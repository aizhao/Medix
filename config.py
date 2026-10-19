"""Runtime configuration loaded from environment variables.

Secrets must never be committed to the repository. Copy ``.env.example`` to
``.env`` (which is ignored by git) or export the variables in the shell.
"""
import os


LLM_CONFIG = {
    "api_key": os.getenv("MEDIX_LLM_API_KEY", ""),
    "model_name": os.getenv("MEDIX_LLM_MODEL", "qwen3.8-max"),
    "base_url": os.getenv(
        "MEDIX_LLM_BASE_URL",
        "https://llm-2puaenj268eae9bv.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    ),
    "temperature": float(os.getenv("MEDIX_LLM_TEMPERATURE", "0.7")),
    "max_tokens": int(os.getenv("MEDIX_LLM_MAX_TOKENS", "8192")),
}

MEM0_CONFIG = {"api_key": os.getenv("MEM0_API_KEY", "")}

