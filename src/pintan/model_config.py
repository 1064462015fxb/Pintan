"""统一模型连接配置；凭证和服务端点始终作为一组选择。

Qwen Token Plan 与普通百炼、DeepSeek 的密钥不能混用。为避免误发凭证，
本模块不会在 Qwen 请求失败时自动切换到其他服务商。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit


TOKEN_PLAN_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
AgentRole = Literal["stable", "trending"]


@dataclass(frozen=True)
class ModelConfig:
    # repr=False 防止调试时打印配置对象而把密钥暴露到日志。
    api_key: str = field(repr=False)
    base_url: str
    model: str


def load_model_config(role: AgentRole = "stable", *, require_key: bool = True) -> ModelConfig:
    """按 Agent 职责选择模型，兼容明确选择的旧 DeepSeek 配置。

    默认使用用户提供的 Token Plan 端点。稳定评分由计算工具负责，模型只
    汇报，因此默认采用 qwen3.6-flash；爆款语义匹配采用 qwen3.7-plus。
    这两个默认值是任务分配策略，不表示已经在线验证了账户模型权限。
    """
    if role not in ("stable", "trending"):
        raise ValueError("未知 Agent 职责")
    provider = os.getenv("PINTAN_MODEL_PROVIDER", "qwen").strip().lower()
    if provider == "qwen":
        key = os.getenv("QWEN_API_KEY", "").strip()
        url = os.getenv("QWEN_BASE_URL", TOKEN_PLAN_URL).strip().rstrip("/")
        default_model = "qwen3.6-flash" if role == "stable" else "qwen3.7-plus"
        model = os.getenv(f"PINTAN_{role.upper()}_MODEL", default_model).strip()
        key_name = "QWEN_API_KEY"
    elif provider == "deepseek":
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
        key_name = "DEEPSEEK_API_KEY"
    else:
        raise ValueError("PINTAN_MODEL_PROVIDER 只支持 qwen 或 deepseek")

    # 拒绝 Markdown 链接、明文 HTTP 和带账号/参数的地址，避免凭证误传。
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in url)):
        raise ValueError("模型 Base URL 必须是纯 HTTPS 地址，不能包含账号、查询参数或 Markdown")
    if not model:
        raise ValueError("模型名称不能为空")
    if require_key and not key:
        raise RuntimeError(f"未设置 {key_name}")
    return ModelConfig(api_key=key, base_url=url, model=model)
