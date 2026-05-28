"""
Agent 配置模块

支持两种配置方式：
1. 后端代码直接构造 AgentConfiguration 对象传入（生产环境）
2. 从 .env 文件 / 环境变量自动加载（本地调试）

环境变量前缀统一为 NDLM_，避免和其他项目冲突。
"""

from pydantic import BaseModel
from pydantic_settings import BaseSettings


# ──────────────────────────────────────────────────────────────────────────────
# 子配置（纯 BaseModel，不直接读环境变量）
# ──────────────────────────────────────────────────────────────────────────────


class LLMConfiguration(BaseModel):
    """大模型连接配置"""

    api_key: str = ""
    base_url: str = ""
    model_name: str = ""
    max_tokens: int = 655360
    temperature: float = 0.7


class SafetyConfiguration(BaseModel):
    """安全护栏配置"""

    enable_command_filter: bool = True
    enable_prompt_injection_detection: bool = True
    require_human_confirm_for_high_risk: bool = True


class ContextConfiguration(BaseModel):
    """对话上下文管理配置"""

    max_context_tokens: int = 62000
    session_ttl_seconds: int = 1800
    default_system_prompt: str = (
        "你是一个专业的 Linux 运维助手，名叫 NDLM。\n"
        "你可以调用工具来查询和管理 Linux 系统。\n"
        "规则：\n"
        "1. 只执行用户明确要求的操作，不要主动执行破坏性命令\n"
        "2. 对于高风险操作（如终止进程、删除文件），必须向用户确认后再执行\n"
        "3. 每次回复要简洁，先说结论，再说细节\n"
        "4. 如果工具执行失败，解释原因并提供替代方案"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 总配置（支持从环境变量 / .env 文件加载）
# ──────────────────────────────────────────────────────────────────────────────


class AgentConfiguration(BaseSettings):
    """
    Agent 总配置。

    使用方式一：后端直接构造（生产环境）
        config = AgentConfiguration(
            llm=LLMConfiguration(api_key="sk-xxx", base_url="https://..."),
        )

    使用方式二：从环境变量自动加载（本地调试）
        # .env 文件中设置 NDLM_LLM_API_KEY=sk-xxx
        config = AgentConfiguration()
    """

    llm: LLMConfiguration = LLMConfiguration()
    safety: SafetyConfiguration = SafetyConfiguration()
    context: ContextConfiguration = ContextConfiguration()
    max_tool_call_rounds: int = 10
    audit_log_directory: str = "./logs"

    model_config = {
        "env_prefix": "NDLM_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_nested_delimiter": "_",
        "env_nested_max_split": 1,
        "extra": "ignore",
    }


# ──────────────────────────────────────────────────────────────────────────────
# 便捷工厂函数
# ──────────────────────────────────────────────────────────────────────────────


def load_config_from_env() -> AgentConfiguration:
    """
    从环境变量 / .env 文件加载配置。
    本地调试时直接调用此函数即可。

    环境变量映射规则（前缀 NDLM_，嵌套用 _ 分隔）：
        NDLM_LLM_API_KEY       → config.llm.api_key
        NDLM_LLM_BASE_URL      → config.llm.base_url
        NDLM_LLM_MODEL_NAME    → config.llm.model_name
        NDLM_LLM_MAX_TOKENS    → config.llm.max_tokens
        NDLM_LLM_TEMPERATURE   → config.llm.temperature
        NDLM_MAX_TOOL_CALL_ROUNDS → config.max_tool_call_rounds
        ...
    """
    return AgentConfiguration()
