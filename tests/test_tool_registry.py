"""
ToolRegistry 验证测试
运行方式：uv run python tests/test_tool_registry.py

分三个阶段测试：
  阶段 1: Schema 生成是否正确（不需要网络/LLM）
  阶段 2: 工具执行是否正常（调用真实系统函数）
  阶段 3: 异常处理是否健壮（错误参数、未知工具等）
"""

import asyncio
import json

from ndlmpanel_agent import ALL_TOOL_FUNCTIONS
from ndlmpanel_agent.tools.tool_registry import ToolRegistry


# ──────────────────────────────────────────────────────────────────────────────
# 测试工具函数
# ──────────────────────────────────────────────────────────────────────────────


def section(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}")


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def fail(msg: str) -> None:
    print(f"  ❌ {msg}")


def info(msg: str) -> None:
    print(f"  ℹ  {msg}")


# ──────────────────────────────────────────────────────────────────────────────
# 阶段 1：Schema 生成验证
# ──────────────────────────────────────────────────────────────────────────────


def test_schema_generation(registry: ToolRegistry) -> None:
    section("阶段 1：Schema 生成验证")

    schema_list = registry.getToolsSchema()

    # 1.1 数量检查
    tool_count = len(schema_list)
    info(f"注册工具数量: {tool_count}")
    if tool_count == len(ALL_TOOL_FUNCTIONS):
        ok(f"工具数量匹配 ({tool_count})")
    else:
        fail(f"工具数量不匹配: 注册了 {tool_count}，ALL_TOOL_FUNCTIONS 有 {len(ALL_TOOL_FUNCTIONS)}")

    # 1.2 检查几个关键工具的 schema
    schema_by_name = {s["function"]["name"]: s for s in schema_list}

    # listProcesses：有枚举参数 + Optional 参数
    print("\n  [listProcesses schema]")
    lp_schema = schema_by_name.get("listProcesses")
    if lp_schema is None:
        fail("listProcesses 未找到")
    else:
        params = lp_schema["function"]["parameters"]["properties"]
        print(f"    properties: {json.dumps(params, ensure_ascii=False, indent=4)}")

        sort_by = params.get("sortBy", {})
        if "enum" in sort_by and "cpu" in sort_by["enum"]:
            ok("sortBy 枚举值正确生成")
        else:
            fail(f"sortBy 枚举值缺失，实际: {sort_by}")

        keyword = params.get("keyword", {})
        if keyword.get("type") == "string":
            ok("keyword Optional[str] 正确解析为 string")
        else:
            fail(f"keyword 类型错误，实际: {keyword}")

    # killProcess：有必填 int 参数
    print("\n  [killProcess schema]")
    kp_schema = schema_by_name.get("killProcess")
    if kp_schema is None:
        fail("killProcess 未找到")
    else:
        params = kp_schema["function"]["parameters"]
        required = params.get("required", [])
        print(f"    required: {required}")
        if "pid" in required:
            ok("pid 是 required 参数")
        else:
            fail("pid 应该是 required 但不在列表中")

        if "signalNumber" not in required:
            ok("signalNumber 有默认值，不在 required 中")
        else:
            fail("signalNumber 有默认值，不应在 required 中")

    # getCpuInfo：无参数函数
    print("\n  [getCpuInfo schema]")
    cpu_schema = schema_by_name.get("getCpuInfo")
    if cpu_schema is None:
        fail("getCpuInfo 未找到")
    else:
        props = cpu_schema["function"]["parameters"]["properties"]
        required = cpu_schema["function"]["parameters"]["required"]
        if not props and not required:
            ok("getCpuInfo 无参数，properties 和 required 均为空")
        else:
            fail(f"getCpuInfo 应无参数，实际 props={props}, required={required}")

    # 1.3 风险等级检查
    print("\n  [风险等级验证]")
    from ndlmpanel_agent.models.agent.tool_models import ToolRiskLevel

    checks = [
        ("getCpuInfo", ToolRiskLevel.READ_ONLY),
        ("createFile", ToolRiskLevel.WRITE),
        ("killProcess", ToolRiskLevel.DANGEROUS),
        ("deleteFile", ToolRiskLevel.DANGEROUS),
    ]
    for name, expected_risk in checks:
        defn = registry.getDefinition(name)
        if defn is None:
            fail(f"{name} 的 definition 未找到")
        elif defn.risk_level == expected_risk:
            ok(f"{name}: {defn.risk_level.value} ✓")
        else:
            fail(f"{name}: 期望 {expected_risk.value}，实际 {defn.risk_level.value}")

    # 1.4 缓存验证（两次调用应返回同一个对象）
    schema1 = registry.getToolsSchema()
    schema2 = registry.getToolsSchema()
    if schema1 is schema2:
        ok("getToolsSchema() 结果已缓存（同一个对象）")
    else:
        fail("getToolsSchema() 未缓存，每次返回新对象（性能问题）")


# ──────────────────────────────────────────────────────────────────────────────
# 阶段 2：工具执行验证
# ──────────────────────────────────────────────────────────────────────────────


async def test_tool_execution(registry: ToolRegistry) -> None:
    section("阶段 2：工具执行验证")

    # 2.1 无参数工具
    print("\n  [getCpuInfo] 无参数调用")
    result = await registry.execute("getCpuInfo", "{}")
    if result.success:
        ok(f"执行成功，输出片段: {result.output[:80]}...")
    else:
        fail(f"执行失败: {result.error_message}")

    # 2.2 有枚举参数工具（LLM 传来的是字符串 "cpu"）
    print("\n  [listProcesses] 枚举参数 sortBy='cpu' 传入")
    result = await registry.execute("listProcesses", '{"sortBy": "cpu"}')
    if result.success:
        # 只展示前两个进程
        data = json.loads(result.output)
        info(f"共返回 {len(data)} 个进程，前两条:")
        for proc in data[:2]:
            info(f"  PID={proc['pid']} name={proc['processName']} cpu={proc['cpuPercent']}%")
        ok("listProcesses 枚举参数正确处理")
    else:
        fail(f"执行失败: {result.error_message}")

    # 2.3 有 Optional 参数工具（keyword 不传，使用默认值 None）
    print("\n  [listProcesses] 带 keyword 过滤")
    result = await registry.execute(
        "listProcesses", '{"sortBy": "pid", "keyword": "python"}'
    )
    if result.success:
        data = json.loads(result.output)
        info(f"关键词 'python' 过滤后: {len(data)} 个进程")
        ok("Optional 参数（keyword）正常工作")
    else:
        fail(f"执行失败: {result.error_message}")

    # 2.4 系统信息工具
    print("\n  [getSystemVersion]")
    result = await registry.execute("getSystemVersion", "{}")
    if result.success:
        info(f"系统版本: {result.output[:100]}")
        ok("getSystemVersion 正常执行")
    else:
        fail(f"执行失败: {result.error_message}")

    # 2.5 listDirectory
    print("\n  [listDirectory] 列出 /tmp")
    result = await registry.execute("listDirectory", '{"targetPath": "/tmp"}')
    if result.success:
        data = json.loads(result.output)
        info(f"/tmp 下有 {len(data)} 个条目")
        ok("listDirectory 正常执行")
    else:
        fail(f"执行失败: {result.error_message}")


# ──────────────────────────────────────────────────────────────────────────────
# 阶段 3：异常处理验证
# ──────────────────────────────────────────────────────────────────────────────


async def test_error_handling(registry: ToolRegistry) -> None:
    section("阶段 3：异常处理验证")

    # 3.1 未知工具名
    print("\n  [未知工具]")
    result = await registry.execute("nonExistentTool", "{}")
    if not result.success and "未知工具" in (result.error_message or ""):
        ok(f"未知工具返回正确错误: {result.error_message}")
    else:
        fail(f"未知工具处理异常，result: {result}")

    # 3.2 非法 JSON 参数
    print("\n  [非法 JSON 参数]")
    result = await registry.execute("listProcesses", "这不是JSON{{{")
    if not result.success and "JSON" in (result.error_message or ""):
        ok(f"JSON 解析失败正确返回错误")
    else:
        fail(f"非法 JSON 未被正确捕获，result: {result}")

    # 3.3 工具内部异常（访问不存在的路径）
    print("\n  [工具内部异常] listDirectory 访问不存在的路径")
    result = await registry.execute(
        "listDirectory", '{"targetPath": "/this/path/does/not/exist/xyz"}'
    )
    if not result.success:
        ok(f"工具内部异常被正确捕获: {result.error_message}")
    else:
        fail("应该失败但执行成功了")

    # 3.4 空参数字符串（合法情况，无参数工具）
    print("\n  [空参数字符串] getCpuInfo 传空字符串")
    result = await registry.execute("getCpuInfo", "")
    if result.success:
        ok("空参数字符串被正确处理（视为无参数）")
    else:
        fail(f"空参数字符串处理失败: {result.error_message}")

    # 3.5 getDefinition 查不存在的工具
    print("\n  [getDefinition 未知工具]")
    defn = registry.getDefinition("unknownTool")
    if defn is None:
        ok("getDefinition 未知工具正确返回 None")
    else:
        fail(f"getDefinition 应返回 None，实际返回: {defn}")


# ──────────────────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    print("\n🔧 ToolRegistry 验证测试")
    print(f"   工具函数总数: {len(ALL_TOOL_FUNCTIONS)}")

    # 初始化注册中心（模拟 AgentOrchestrator 初始化时的行为）
    registry = ToolRegistry(ALL_TOOL_FUNCTIONS)
    info(f"已注册工具: {registry.registeredToolNames()[:5]}...（共 {len(registry.registeredToolNames())} 个）")

    test_schema_generation(registry)
    await test_tool_execution(registry)
    await test_error_handling(registry)

    section("测试完成")


if __name__ == "__main__":
    asyncio.run(main())
