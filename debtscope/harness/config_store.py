"""Persistent model/endpoint configuration with provider presets.

Precedence (highest wins): environment variables > config file > presets.
The key is stored only in the user's home directory (~/.debtscope/config.json),
never inside a scanned repository.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".debtscope")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# OpenAI-compatible presets — only the base URL and a sensible default model.
# Model IDs reflect the common current releases as of 2026-09; every field is
# editable in the guide / wizard, so users can pin any newer or dated model.
# Order: Doubao first (the product default), then China providers, global
# providers, local runtimes, and the generic custom endpoint last.
PROVIDERS = {
    # ---- China: default & first-party clouds ----
    "doubao": {
        "label": "豆包 / 火山方舟 (Seed-Evolving · 默认)",
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        # Unified Model ID — always points to the latest Seed-Evolving release
        # (Agent & Coding optimized). Endpoint ids (ep-xxxx) also work here.
        "model": "doubao-seed-evolving",
        "key_url": "https://console.volcengine.com/ark",
    },
    "deepseek": {
        "label": "DeepSeek",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",  # rolling alias for the latest Chat model
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "qwen": {
        "label": "阿里通义千问 / 百炼 (Qwen)",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-coder-plus",
        "key_url": "https://bailian.console.aliyun.com/",
    },
    "zhipu": {
        "label": "智谱 GLM (BigModel)",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.3",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    "kimi": {
        "label": "Kimi / 月之暗面 (Moonshot)",
        "api_base": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.6",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
    },
    "hunyuan": {
        "label": "腾讯混元 (Hunyuan)",
        "api_base": "https://api.hunyuan.cloud.tencent.com/v1",
        "model": "hunyuan-turbo",
        "key_url": "https://console.cloud.tencent.com/hunyuan/api-key",
    },
    "qianfan": {
        "label": "百度千帆 / 文心 (ERNIE)",
        "api_base": "https://qianfan.baidubce.com/v2",
        "model": "ernie-4.5-turbo-128k",
        "key_url": "https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application",
    },
    "minimax": {
        "label": "MiniMax 海螺",
        "api_base": "https://api.minimax.chat/v1",
        "model": "MiniMax-M2.5",
        "key_url": "https://platform.minimaxi.com/user-center/basic-information/interface-key",
    },
    "mimo": {
        "label": "小米 MiMo",
        "api_base": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5-pro",
        "key_url": "https://www.xiaomimimo.com/",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow (聚合多模型)",
        "api_base": "https://api.siliconflow.cn/v1",
        # Aggregator: vendor-prefixed model names; swap to any model on the hub.
        "model": "Qwen/Qwen3-Coder-480B-A35B-Instruct",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
    },
    # ---- global providers ----
    "openai": {
        "label": "OpenAI (GPT)",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-5.5",
        "key_url": "https://platform.openai.com/api-keys",
    },
    "gemini": {
        "label": "Google Gemini",
        "api_base": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.5-flash",
        "key_url": "https://aistudio.google.com/apikey",
    },
    "xai": {
        "label": "xAI Grok",
        "api_base": "https://api.x.ai/v1",
        "model": "grok-4.6",
        "key_url": "https://console.x.ai/",
    },
    "mistral": {
        "label": "Mistral Le Chat",
        "api_base": "https://api.mistral.ai/v1",
        "model": "mistral-large-latest",
        "key_url": "https://console.mistral.ai/api-keys/",
    },
    # ---- local & generic ----
    "ollama": {
        "label": "Ollama / 本地模型 (无需 key)",
        "api_base": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5-coder:7b",
        "key_url": "",
    },
    "custom": {
        "label": "自定义 OpenAI 兼容端点（内网网关 / vLLM 等）",
        "api_base": "",
        "model": "",
        "key_url": "",
    },
}


@dataclass
class FileConfig:
    provider: str = "custom"
    api_base: str = ""
    api_key: str | None = None
    model: str = ""
    timeout: int = 45

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "api_base": self.api_base.rstrip("/"),
            "api_key": self.api_key,
            "model": self.model,
            "timeout": self.timeout,
        }


def load_config_file(path: str = CONFIG_PATH) -> FileConfig:
    if not os.path.isfile(path):
        return FileConfig()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return FileConfig()
    return FileConfig(
        provider=data.get("provider", "custom"),
        api_base=data.get("api_base", ""),
        api_key=data.get("api_key"),
        model=data.get("model", ""),
        timeout=int(data.get("timeout", 45) or 45),
    )


def save_config_file(cfg: FileConfig, path: str = CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, 0o600)  # owner-only; the file holds an API key
    except OSError:
        pass


def masked_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]
