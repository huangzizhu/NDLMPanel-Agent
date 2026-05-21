"""
工具注册中心（ToolRegistry）

职责：
1. 把所有工具函数的签名自动解析成 LLM 需要的 JSON Schema（tools 参数）
2. 根据 LLM 返回的函数名找到并执行对应工具
3. 把执行结果序列化为字符串，让 LLM 能够读懂

设计原则：
- 工具函数全部是同步函数，通过 run_in_executor 在线程池里执行，不阻塞 async 主循环
- 参数解析严格按照函数签名中的 camelCase 名称匹配（LLM 按 schema 生成，schema 由签名生成）
- 风险等级通过 RISK_LEVEL_MAP 集中管理，默认 WRITE（保守策略）
"""

import asyncio
import enum
import inspect
import json
import types
from typing import Any, Callable, get_args, get_origin

from pydantic import BaseModel

from ndlmpanel_agent.models.agent.tool_models import (
    ToolDefinition,
    ToolExecutionResult,
    ToolRiskLevel,
)

# ──────────────────────────────────────────────────────────────────────────────
# 风险等级映射表
# 每个工具函数的风险等级在这里集中声明，由开发者维护
# 未在此表中的工具，默认使用 WRITE（保守策略）
# ──────────────────────────────────────────────────────────────────────────────

RISK_LEVEL_MAP: dict[str, ToolRiskLevel] = {
    # ── Layer 1: 只读感知工具（READ_ONLY）──────────────────────────────────
    # 不修改系统状态，直接放行
    "getCpuInfo": ToolRiskLevel.READ_ONLY,
    "getMemoryInfo": ToolRiskLevel.READ_ONLY,
    "getDiskInfo": ToolRiskLevel.READ_ONLY,
    "getGpuInfo": ToolRiskLevel.READ_ONLY,
    "getNetworkInfo": ToolRiskLevel.READ_ONLY,
    "getFirewallStatus": ToolRiskLevel.READ_ONLY,
    "listFirewallPorts": ToolRiskLevel.READ_ONLY,
    "listDirectory": ToolRiskLevel.READ_ONLY,
    "listSingleFileOrDirectory": ToolRiskLevel.READ_ONLY,
    "grepFileOrDirectory": ToolRiskLevel.READ_ONLY,
    "listProcesses": ToolRiskLevel.READ_ONLY,
    "getProcessDetail": ToolRiskLevel.READ_ONLY,
    "getZombieOrphanProcesses": ToolRiskLevel.READ_ONLY,
    "querySystemLogs": ToolRiskLevel.READ_ONLY,
    "listUsers": ToolRiskLevel.READ_ONLY,
    "getLoginHistory": ToolRiskLevel.READ_ONLY,
    "pingHost": ToolRiskLevel.READ_ONLY,
    "checkPortConnectivity": ToolRiskLevel.READ_ONLY,
    "getEnvironmentVariables": ToolRiskLevel.READ_ONLY,
    "getSystemVersion": ToolRiskLevel.READ_ONLY,
    "getUptime": ToolRiskLevel.READ_ONLY,
    "checkDockerInstalled": ToolRiskLevel.READ_ONLY,
    "getDockerContainers": ToolRiskLevel.READ_ONLY,
    "checkNginxInstalled": ToolRiskLevel.READ_ONLY,
    "getNginxStatus": ToolRiskLevel.READ_ONLY,
    "generateStaticSiteConfig": ToolRiskLevel.READ_ONLY,
    "generateProxyConfig": ToolRiskLevel.READ_ONLY,
    "testNginxConfig": ToolRiskLevel.READ_ONLY,
    "getNginxSiteList": ToolRiskLevel.READ_ONLY,
    "checkDatabaseInstalled": ToolRiskLevel.READ_ONLY,
    "getDatabaseStatus": ToolRiskLevel.READ_ONLY,
    "getDirectoryTree": ToolRiskLevel.READ_ONLY,
    "isTextFile": ToolRiskLevel.READ_ONLY,
    "readTextFile": ToolRiskLevel.READ_ONLY,
    # ── Layer 2: 受控操作（WRITE）──────────────────────────────────────────
    # 有副作用但相对可逆，SafetyGuard 可放行但记录日志
    "createFile": ToolRiskLevel.WRITE,
    "createDirectory": ToolRiskLevel.WRITE,
    "renameFileOrDirectory": ToolRiskLevel.WRITE,
    "changePermissions": ToolRiskLevel.WRITE,
    "changeOwner": ToolRiskLevel.WRITE,
    "copyFile": ToolRiskLevel.WRITE,
    "compressPath": ToolRiskLevel.WRITE,
    "decompressArchive": ToolRiskLevel.WRITE,
    "writeTextFile": ToolRiskLevel.WRITE,
    # ── Layer 3: 高危操作（DANGEROUS）─────────────────────────────────────
    # 不可逆或影响系统安全，SafetyGuard 会要求人工确认
    "saveNginxConfig": ToolRiskLevel.DANGEROUS,
    "reloadNginx": ToolRiskLevel.DANGEROUS,
    "restartNginx": ToolRiskLevel.DANGEROUS,
    "createNginxSite": ToolRiskLevel.DANGEROUS,
    "deleteNginxSite": ToolRiskLevel.DANGEROUS,
    "applySslCertificate": ToolRiskLevel.DANGEROUS,
    "configSslForNginx": ToolRiskLevel.DANGEROUS,
    "renewSslCertificate": ToolRiskLevel.DANGEROUS,
    "killProcess": ToolRiskLevel.DANGEROUS,
    "deleteFile": ToolRiskLevel.DANGEROUS,
    "deleteDirectory": ToolRiskLevel.DANGEROUS,
    "addFirewallPort": ToolRiskLevel.DANGEROUS,
    "removeFirewallPort": ToolRiskLevel.DANGEROUS,
    "manageSystemService": ToolRiskLevel.DANGEROUS,
    "autoCleanProcesses": ToolRiskLevel.DANGEROUS,
    "batchKillProcesses": ToolRiskLevel.DANGEROUS,
}


# ──────────────────────────────────────────────────────────────────────────────
# 类型注解 → JSON Schema 映射
# ──────────────────────────────────────────────────────────────────────────────

# Python 基础类型 → JSON Schema 类型字符串
_PRIMITIVE_TYPE_MAP: dict[type, dict] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}


def _annotation_to_json_schema(annotation: Any) -> dict:
    """
    把单个 Python 类型注解转换为 JSON Schema 片段。

    支持的类型：
    - str / int / float / bool → 基础 JSON 类型
    - Enum 子类               → {"type": "string", "enum": [...枚举值...]}
    - list[X]                 → {"type": "array"}（元素类型暂不展开，LLM 通常不需要）
    - X | None（Optional）    → 递归处理 X，忽略 None 部分
    - 其他                    → 兜底 {"type": "string"}，并附注说明
    """
    # ── 基础类型 ─────────────────────────────────────────────────────────
    if annotation in _PRIMITIVE_TYPE_MAP:
        # 返回副本，避免共享同一个字典被后续修改
        return dict(_PRIMITIVE_TYPE_MAP[annotation])

    # ── 枚举类型 ─────────────────────────────────────────────────────────
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return {
            "type": "string",
            "enum": [member.value for member in annotation],
            "description": f"可选值: {', '.join(m.value for m in annotation)}",
        }

    # ── 泛型类型（list[X]、X | None 等）────────────────────────────────
    origin = get_origin(annotation)  # 获取泛型基类，如 list、Union
    args = get_args(annotation)  # 获取泛型参数，如 (str, NoneType)

    # list[X] → array，OpenAI function calling 需要显式 items
    if origin is list:
        item_schema: dict[str, Any] = {"type": "string"}
        if args:
            item_schema = _annotation_to_json_schema(args[0])
        return {
            "type": "array",
            "items": item_schema,
        }

    # Python 3.10+ 的 X | Y 语法（types.UnionType）
    # 以及 typing.Union[X, None]（Optional[X]）
    is_union = origin is types.UnionType if hasattr(types, "UnionType") else False
    # typing.Union 的 origin 是 typing.Union
    try:
        import typing
        is_union = is_union or (origin is typing.Union)
    except AttributeError:
        pass

    if is_union and args:
        # 过滤掉 NoneType，取剩余的第一个类型递归处理
        non_none_args = [a for a in args if a is not type(None)]
        if non_none_args:
            return _annotation_to_json_schema(non_none_args[0])

    # ── 兜底 ─────────────────────────────────────────────────────────────
    return {"type": "string"}


# ──────────────────────────────────────────────────────────────────────────────
# 结果序列化
# ──────────────────────────────────────────────────────────────────────────────


def _serialize_result(result: Any) -> str:
    """
    把工具函数的返回值序列化为字符串，供 LLM 读取。

    处理规则：
    - Pydantic Model → JSON 字符串（model_dump_json）
    - list[Pydantic] → JSON 数组字符串
    - list[基础类型] → JSON 数组字符串
    - None           → "(无返回值)"
    - 其他           → str()
    """
    if result is None:
        return "(无返回值)"

    if isinstance(result, BaseModel):
        return result.model_dump_json(indent=2)

    if isinstance(result, list):
        if result and isinstance(result[0], BaseModel):
            return json.dumps(
                [item.model_dump() for item in result],
                ensure_ascii=False,
                indent=2,
                default=str,  # datetime 等无法直接序列化的类型，转 str
            )
        # 基础类型列表
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)

    return str(result)


# ──────────────────────────────────────────────────────────────────────────────
# ToolRegistry 主体
# ──────────────────────────────────────────────────────────────────────────────


class ToolRegistry:
    """
    工具注册中心。

    初始化时传入工具函数列表（ALL_TOOL_FUNCTIONS），自动解析签名并缓存。
    此后在整个 Agent 生命周期内复用同一个实例，不重复解析。

    外部调用方：
    - AgentOrchestrator：调 get_tools_schema() 和 execute()
    - SafetyGuard：调 get_definition() 读取风险等级
    """

    def __init__(self, tool_functions: list[Callable]) -> None:
        """
        Args:
            tool_functions: 工具函数列表，通常传入 ALL_TOOL_FUNCTIONS
        """
        # 函数名 → 可调用对象
        self._registry: dict[str, Callable] = {}
        # 函数名 → 工具元信息（含 JSON Schema、风险等级）
        self._definitions: dict[str, ToolDefinition] = {}
        # 缓存 LLM tools 参数格式，避免重复构建
        self._tools_schema_cache: list[dict] | None = None

        for fn in tool_functions:
            self._registerFn(fn)

    # ── 注册阶段（构造时自动调用）────────────────────────────────────────

    def _registerFn(self, fn: Callable) -> None:
        """解析单个函数的元信息，存入 _registry 和 _definitions。"""
        name = fn.__name__
        self._registry[name] = fn

        # 读取 docstring 作为工具描述，供 LLM 判断何时调用
        description = inspect.getdoc(fn) or f"执行 {name} 操作"

        # 从 RISK_LEVEL_MAP 读取风险等级，未配置的函数用 WRITE 保守策略
        risk_level = RISK_LEVEL_MAP.get(name, ToolRiskLevel.WRITE)

        # 解析函数签名生成 JSON Schema（parameters 字段）
        parameters_schema = self._buildParametersSchema(fn)

        self._definitions[name] = ToolDefinition(
            name=name,
            description=description,
            risk_level=risk_level,
            parameters_schema=parameters_schema,
        )

    def _buildParametersSchema(self, fn: Callable) -> dict:
        """
        把函数签名转换为 OpenAI function calling 所需的 parameters 字段格式。

        返回示例（对应 killProcess(pid: int, signalNumber: int = SIGTERM)）：
        {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "signalNumber": {"type": "integer", "default": 15}
            },
            "required": ["pid"]
        }
        """
        sig = inspect.signature(fn)
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            annotation = param.annotation

            # 没有类型注解时兜底为 string
            if annotation is inspect.Parameter.empty:
                schema_fragment = {"type": "string"}
            else:
                schema_fragment = _annotation_to_json_schema(annotation)

            # 有默认值的参数：把默认值写进 schema（枚举默认值取 .value）
            if param.default is not inspect.Parameter.empty:
                default_val = param.default
                if isinstance(default_val, enum.Enum):
                    default_val = default_val.value
                schema_fragment = {**schema_fragment, "default": default_val}
            else:
                # 没有默认值 → required
                required.append(param_name)

            properties[param_name] = schema_fragment

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    # ── 对外接口 ─────────────────────────────────────────────────────────

    def getToolsSchema(self) -> list[dict]:
        """
        返回 OpenAI API 的 tools 参数格式列表。
        在 AgentOrchestrator 每次调用 LLM 时传入。

        结果在首次调用时生成并缓存，之后直接返回缓存。

        返回格式示例：
        [
          {
            "type": "function",
            "function": {
              "name": "killProcess",
              "description": "终止指定进程。",
              "parameters": {
                "type": "object",
                "properties": {"pid": {"type": "integer"}, ...},
                "required": ["pid"]
              }
            }
          },
          ...
        ]
        """
        if self._tools_schema_cache is None:
            self._tools_schema_cache = [
                {
                    "type": "function",
                    "function": {
                        "name": defn.name,
                        "description": defn.description,
                        "parameters": defn.parameters_schema,
                    },
                }
                for defn in self._definitions.values()
            ]
        return self._tools_schema_cache

    def getDefinition(self, name: str) -> ToolDefinition | None:
        """
        按函数名获取工具元信息（含风险等级）。
        主要供 SafetyGuard 使用。

        Returns:
            ToolDefinition，若函数名未注册则返回 None
        """
        return self._definitions.get(name)

    async def execute(
        self,
        toolName: str,
        argumentsJson: str,
    ) -> ToolExecutionResult:
        """
        根据 LLM 返回的函数名和参数（JSON 字符串），找到函数并执行。

        注意：工具函数全部是同步函数，这里通过 run_in_executor 在线程池里
        调用，避免阻塞 asyncio 事件循环（重要：getCpuInfo 等有 time.sleep）。

        Args:
            toolName:       LLM 返回的 function name（如 "killProcess"）
            argumentsJson:  LLM 返回的 arguments 字段（JSON 字符串）

        Returns:
            ToolExecutionResult，包含执行状态和输出字符串
        """
        # ── 查找函数 ──────────────────────────────────────────────────────
        fn = self._registry.get(toolName)
        if fn is None:
            return ToolExecutionResult(
                tool_name=toolName,
                success=False,
                output="",
                error_message=f"未知工具: '{toolName}'，该函数未在 ToolRegistry 中注册",
            )

        # ── 解析参数 ──────────────────────────────────────────────────────
        try:
            kwargs: dict = json.loads(argumentsJson) if argumentsJson.strip() else {}
        except json.JSONDecodeError as e:
            return ToolExecutionResult(
                tool_name=toolName,
                success=False,
                output="",
                error_message=f"参数 JSON 解析失败: {e}，原始参数: {argumentsJson!r}",
            )

        # ── 枚举参数反序列化 ──────────────────────────────────────────────
        kwargs = self._coerceEnumArgs(fn, kwargs)

        # ── 在线程池中执行同步函数 ─────────────────────────────────────────
        import functools

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                functools.partial(fn, **kwargs),
            )
        except Exception as e:
            return ToolExecutionResult(
                tool_name=toolName,
                success=False,
                output="",
                error_message=f"{type(e).__name__}: {e}",
            )

        # ── 序列化结果 ────────────────────────────────────────────────────
        return ToolExecutionResult(
            tool_name=toolName,
            success=True,
            output=_serialize_result(result),
        )

    # ── 内部辅助 ─────────────────────────────────────────────────────────

    def _coerceEnumArgs(self, fn: Callable, kwargs: dict) -> dict:
        """
        把 LLM 传来的字符串枚举值还原为 Python 枚举对象。

        例：LLM 传 {"sortBy": "cpu"}，函数签名是 sortBy: ProcessSortBy
        → 转换为 {"sortBy": ProcessSortBy("cpu")}

        无法转换时保留原始值，让函数自己决定是否报错。
        """
        sig = inspect.signature(fn)
        coerced = dict(kwargs)

        for param_name, param in sig.parameters.items():
            if param_name not in coerced:
                continue

            annotation = param.annotation
            if annotation is inspect.Parameter.empty:
                continue

            # 处理 Optional[Enum]（X | None）
            actual_type = _unwrap_optional(annotation)

            if (
                isinstance(actual_type, type)
                and issubclass(actual_type, enum.Enum)
                and isinstance(coerced[param_name], str)
            ):
                try:
                    coerced[param_name] = actual_type(coerced[param_name])
                except ValueError:
                    pass  # 值不合法，保留原始字符串，让函数报错

        return coerced

    def registeredToolNames(self) -> list[str]:
        """返回所有已注册的工具名，用于调试或展示。"""
        return list(self._registry.keys())


# ──────────────────────────────────────────────────────────────────────────────
# 模块级工具函数
# ──────────────────────────────────────────────────────────────────────────────


def _unwrap_optional(annotation: Any) -> Any:
    """
    从 Optional[X]（即 X | None）中提取出 X。
    如果不是 Optional，直接返回原注解。

    例：
      str | None  → str
      Optional[int] → int
      ProcessSortBy → ProcessSortBy（直接返回）
    """
    origin = get_origin(annotation)
    args = get_args(annotation)

    is_union = False
    if hasattr(types, "UnionType") and isinstance(annotation, types.UnionType):
        is_union = True
    try:
        import typing
        if origin is typing.Union:
            is_union = True
    except AttributeError:
        pass

    if is_union and args:
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return non_none[0]

    return annotation
