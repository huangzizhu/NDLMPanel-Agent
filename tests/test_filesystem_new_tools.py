#!/usr/bin/env python3
"""
新增文件系统工具函数测试
运行: uv run python tests/test_filesystem_new_tools.py

测试策略:
- 在 /tmp 下创建临时目录操作，测试完清理
- 覆盖正常流程和异常流程
"""

import os
import tempfile
import traceback
from pathlib import Path

from ndlmpanel_agent.exceptions import (
    GatewayAbstractException,
    PermissionDeniedException,
    ResourceNotFoundException,
    ToolExecutionException,
)
from ndlmpanel_agent.tools.ops.filesystem.filesystem_tools import (
    compressPath,
    copyFile,
    decompressArchive,
    getDirectoryTree,
    isTextFile,
    readTextFile,
    writeTextFile,
)

_results: list[tuple[str, str, str]] = []


def _run(module: str, name: str, func):
    try:
        result = func()
        _results.append((module, name, "PASS"))
        return result
    except PermissionDeniedException as e:
        _results.append((module, name, f"SKIP(权限不足: {e.innerMessage})"))
    except GatewayAbstractException as e:
        _results.append((module, name, f"FAIL: {e.innerMessage}"))
    except Exception as e:
        _results.append((module, name, f"ERROR: {type(e).__name__}: {e}"))
        traceback.print_exc()
    return None


def _expectFail(module: str, name: str, func, expectedException=ToolExecutionException):
    try:
        func()
        _results.append((module, name, "FAIL: 未抛出预期异常"))
    except expectedException:
        _results.append((module, name, "PASS"))
    except GatewayAbstractException as e:
        _results.append((module, name, f"PASS(抛出其他Gateway异常: {type(e).__name__})"))
    except Exception as e:
        _results.append((module, name, f"ERROR: {type(e).__name__}: {e}"))


def _printReport():
    print("\n" + "=" * 80)
    print("  新增文件系统工具 - 测试报告")
    print("=" * 80)

    currentModule = ""
    passCount = failCount = skipCount = errorCount = 0

    for module, name, status in _results:
        if module != currentModule:
            currentModule = module
            print(f"\n  [{module}]")

        if status == "PASS" or status.startswith("PASS("):
            icon = "✅"
            passCount += 1
        elif status.startswith("SKIP"):
            icon = "⏭️ "
            skipCount += 1
        elif status.startswith("FAIL"):
            icon = "❌"
            failCount += 1
        else:
            icon = "💥"
            errorCount += 1

        print(f"    {icon} {name:50s} {status}")

    total = len(_results)
    print(f"\n{'=' * 80}")
    print(
        f"  合计: {total} | ✅ {passCount} | ⏭️  {skipCount} | ❌ {failCount} | 💥 {errorCount}"
    )
    print(f"{'=' * 80}")

    return failCount + errorCount


def _setupTestDir() -> Path:
    testRoot = Path(tempfile.mkdtemp(prefix="ndlm_fs_test_"))

    (testRoot / "a.txt").write_text("hello world\n", encoding="utf-8")
    (testRoot / "b.py").write_text("print('hi')\n", encoding="utf-8")
    (testRoot / "binary.bin").write_bytes(bytes(range(256)))

    subDir = testRoot / "subdir"
    subDir.mkdir()
    (subDir / "c.txt").write_text("sub content\n", encoding="utf-8")

    deepDir = subDir / "deep"
    deepDir.mkdir()
    (deepDir / "d.txt").write_text("deep content\n", encoding="utf-8")

    emptyDir = testRoot / "emptydir"
    emptyDir.mkdir()

    return testRoot


def testGetDirectoryTree(testRoot: Path):
    M = "getDirectoryTree"

    result = _run(M, "深度1", lambda: getDirectoryTree(str(testRoot), maxDepth=1))
    if result:
        assert result.success, "应成功"
        assert result.tree is not None, "树不应为空"
        assert result.tree.fileName == testRoot.name, f"根节点名应为 {testRoot.name}"
        childNames = [c.fileName for c in result.tree.children]
        print(f"      → 深度1子项: {childNames}")
        assert "subdir" in childNames, "应包含 subdir"
        # 检查文件也都被包含
        assert "a.txt" in childNames, "应包含 a.txt"
        assert "b.py" in childNames, "应包含 b.py"
        assert "binary.bin" in childNames, "应包含 binary.bin"
        assert "emptydir" in childNames, "应包含 emptydir"
        subdirNode = next(c for c in result.tree.children if c.fileName == "subdir")
        assert len(subdirNode.children) == 0, "深度1时subdir不应有子节点"

    result = _run(M, "深度2", lambda: getDirectoryTree(str(testRoot), maxDepth=2))
    if result:
        subdirNode = next(c for c in result.tree.children if c.fileName == "subdir")
        childNames = [c.fileName for c in subdirNode.children]
        print(f"      → 深度2 subdir子项: {childNames}")
        assert "deep" in childNames, "深度2时应包含 deep"
        assert "c.txt" in childNames, "深度2时应包含 c.txt"
        deepNode = next(c for c in subdirNode.children if c.fileName == "deep")
        assert len(deepNode.children) == 0, "深度2时deep不应有子节点"

    result = _run(M, "深度3", lambda: getDirectoryTree(str(testRoot), maxDepth=3))
    if result:
        subdirNode = next(c for c in result.tree.children if c.fileName == "subdir")
        deepNode = next(c for c in subdirNode.children if c.fileName == "deep")
        childNames = [c.fileName for c in deepNode.children]
        print(f"      → 深度3 deep子项: {childNames}")
        assert "d.txt" in childNames, "深度3时应包含 d.txt"

    result = _run(M, "深度0应使用1", lambda: getDirectoryTree(str(testRoot), maxDepth=0))
    if result:
        assert result.maxDepth == 1, "深度0应使用1"
    
    result = _run(M, "深度6应使用5", lambda: getDirectoryTree(str(testRoot), maxDepth=6))
    if result:
        assert result.maxDepth == 5, "深度6应使用5"
    
    _expectFail(M, "不存在的目录", lambda: getDirectoryTree("/nonexistent_path_xyz", maxDepth=1), ResourceNotFoundException)
    _expectFail(M, "传入文件路径应报错", lambda: getDirectoryTree(str(testRoot / "a.txt"), maxDepth=1))


def testCopyFile(testRoot: Path):
    M = "copyFile"

    srcFile = str(testRoot / "a.txt")
    dstFile = str(testRoot / "a_copy.txt")

    result = _run(M, "正常拷贝", lambda: copyFile(srcFile, dstFile))
    if result:
        assert result.success, "应成功"
        assert Path(dstFile).exists(), "目标文件应存在"
        content = Path(dstFile).read_text(encoding="utf-8")
        assert content == "hello world\n", "内容应一致"
        print(f"      → 拷贝到: {result.absolutePath}")

    dstDir = testRoot / "copy_target"
    dstDir.mkdir()
    dstFile2 = str(dstDir / "a.txt")
    result = _run(M, "拷贝到新目录", lambda: copyFile(srcFile, dstFile2))
    if result:
        assert Path(dstFile2).exists(), "目标文件应存在"

    _expectFail(M, "源文件不存在", lambda: copyFile("/nonexistent_xyz.txt", "/tmp/dst.txt"), ResourceNotFoundException)
    _expectFail(M, "源路径是目录应报错", lambda: copyFile(str(testRoot), str(testRoot / "dir_copy")))


def testIsTextFile(testRoot: Path):
    M = "isTextFile"

    result = _run(M, "文本文件(.txt)", lambda: isTextFile(str(testRoot / "a.txt")))
    if result:
        assert result.isTextFile, ".txt 应被识别为文本文件"
        print(f"      → .txt: isText={result.isTextFile}, encoding={result.detectedEncoding}")

    result = _run(M, "文本文件(.py)", lambda: isTextFile(str(testRoot / "b.py")))
    if result:
        assert result.isTextFile, ".py 应被识别为文本文件"

    result = _run(M, "二进制文件(.bin)", lambda: isTextFile(str(testRoot / "binary.bin")))
    if result:
        assert not result.isTextFile, ".bin 不应被识别为文本文件"
        print(f"      → .bin: isText={result.isTextFile}")

    result = _run(M, "目录应返回False", lambda: isTextFile(str(testRoot)))
    if result:
        assert not result.isTextFile, "目录不应被识别为文本文件"

    _expectFail(M, "不存在的文件", lambda: isTextFile("/nonexistent_xyz.txt"), ResourceNotFoundException)


def testCompressPath(testRoot: Path):
    M = "compressPath"

    result = _run(M, "压缩目录", lambda: compressPath(str(testRoot / "subdir")))
    if result:
        assert result.success, "应成功"
        assert result.archivePath is not None, "压缩文件路径不应为空"
        assert Path(result.archivePath).exists(), "压缩文件应存在"
        assert result.archiveSizeBytes is not None and result.archiveSizeBytes > 0, "压缩文件大小应大于0"
        print(f"      → 压缩目录: {result.archivePath} ({result.archiveSizeBytes} bytes)")
        Path(result.archivePath).unlink(missing_ok=True)

    result = _run(M, "压缩单个文件", lambda: compressPath(str(testRoot / "a.txt")))
    if result:
        assert result.success, "应成功"
        assert result.archivePath is not None, "压缩文件路径不应为空"
        assert Path(result.archivePath).exists(), "压缩文件应存在"
        print(f"      → 压缩文件: {result.archivePath} ({result.archiveSizeBytes} bytes)")
        Path(result.archivePath).unlink(missing_ok=True)

    _expectFail(M, "不存在的路径", lambda: compressPath("/nonexistent_xyz"), ResourceNotFoundException)


def testDecompressArchive(testRoot: Path):
    M = "decompressArchive"

    archiveResult = compressPath(str(testRoot / "subdir"))
    assert archiveResult.success, "前置压缩应成功"
    archivePath = archiveResult.archivePath

    extractDir = testRoot / "extract_output"
    extractDir.mkdir()

    result = _run(M, "解压到指定目录", lambda: decompressArchive(archivePath, str(extractDir)))
    if result:
        assert result.success, "应成功"
        assert result.targetPath is not None, "目标路径不应为空"
        extractedSubdir = extractDir / "subdir"
        assert extractedSubdir.exists(), "解压后应存在 subdir 目录"
        assert (extractedSubdir / "c.txt").exists(), "解压后应存在 c.txt"
        print(f"      → 解压到: {result.targetPath}")

    Path(archivePath).unlink(missing_ok=True)

    archiveResult2 = compressPath(str(testRoot / "a.txt"))
    archivePath2 = archiveResult2.archivePath

    result = _run(M, "解压到默认目录(同目录)", lambda: decompressArchive(archivePath2))
    if result:
        assert result.success, "应成功"
        print(f"      → 默认解压到: {result.targetPath}")

    Path(archivePath2).unlink(missing_ok=True)

    _expectFail(M, "不存在的压缩文件", lambda: decompressArchive("/nonexistent.tar.gz"), ResourceNotFoundException)

    fakeArchive = testRoot / "fake.xyz"
    fakeArchive.write_text("not an archive", encoding="utf-8")
    _expectFail(M, "不支持的压缩格式", lambda: decompressArchive(str(fakeArchive)))


def testReadTextFile(testRoot: Path):
    M = "readTextFile"

    result = _run(M, "读取文本文件", lambda: readTextFile(str(testRoot / "a.txt")))
    if result:
        assert result.success, "应成功"
        assert result.content == "hello world\n", "内容应一致"
        assert result.encoding is not None, "编码不应为空"
        print(f"      → 内容: {repr(result.content[:50])}, 编码: {result.encoding}")

    result = _run(M, "读取.py文件", lambda: readTextFile(str(testRoot / "b.py")))
    if result:
        assert result.success, "应成功"
        assert "print" in result.content, "应包含 print"

    _expectFail(M, "读取二进制文件应报错", lambda: readTextFile(str(testRoot / "binary.bin")))
    _expectFail(M, "读取目录应报错", lambda: readTextFile(str(testRoot)))
    _expectFail(M, "读取不存在的文件", lambda: readTextFile("/nonexistent_xyz.txt"), ResourceNotFoundException)


def testWriteTextFile(testRoot: Path):
    M = "writeTextFile"

    targetFile = testRoot / "writable.txt"
    targetFile.write_text("original content\n", encoding="utf-8")

    result = _run(M, "覆写已存在文件", lambda: writeTextFile(str(targetFile), "new content\n"))
    if result:
        assert result.success, "应成功"
        actual = targetFile.read_text(encoding="utf-8")
        assert actual == "new content\n", "内容应被更新"
        print(f"      → 写入后大小: {result.sizeBytes} bytes")

    _expectFail(M, "写入不存在的文件应报错", lambda: writeTextFile("/nonexistent_xyz.txt", "data"))
    _expectFail(M, "写入目录应报错", lambda: writeTextFile(str(testRoot), "data"))


def testEndToEndWorkflow(testRoot: Path):
    M = "端到端工作流"

    workflowDir = testRoot / "workflow"
    workflowDir.mkdir()

    (workflowDir / "config.yaml").write_text("key: value\n", encoding="utf-8")
    (workflowDir / "data").mkdir()
    (workflowDir / "data" / "log.txt").write_text("line1\nline2\n", encoding="utf-8")

    result = _run(M, "1.获取目录树", lambda: getDirectoryTree(str(workflowDir), maxDepth=3))
    if result:
        assert result.success
        print(f"      → 目录树根: {result.tree.fileName}, 子项数: {len(result.tree.children)}")

    result = _run(M, "2.拷贝文件", lambda: copyFile(str(workflowDir / "config.yaml"), str(workflowDir / "config_backup.yaml")))
    if result:
        assert result.success

    result = _run(M, "3.判断文本文件", lambda: isTextFile(str(workflowDir / "config.yaml")))
    if result:
        assert result.isTextFile

    result = _run(M, "4.读取文本文件", lambda: readTextFile(str(workflowDir / "config.yaml")))
    if result:
        assert result.content == "key: value\n"

    result = _run(M, "5.修改文本文件", lambda: writeTextFile(str(workflowDir / "config.yaml"), "key: updated\n"))
    if result:
        assert result.success
        actual = (workflowDir / "config.yaml").read_text(encoding="utf-8")
        assert actual == "key: updated\n"

    result = _run(M, "6.压缩目录", lambda: compressPath(str(workflowDir)))
    if result:
        assert result.success
        archivePath = result.archivePath

        extractDir = testRoot / "workflow_extracted"
        result2 = _run(M, "7.解压目录", lambda: decompressArchive(archivePath, str(extractDir)))
        if result2:
            assert result2.success
            assert (extractDir / "workflow" / "config.yaml").exists(), "解压后应包含 config.yaml"

        Path(archivePath).unlink(missing_ok=True)


def main():
    testRoot = _setupTestDir()
    print(f"测试临时目录: {testRoot}")

    testGetDirectoryTree(testRoot)
    testCopyFile(testRoot)
    testIsTextFile(testRoot)
    testCompressPath(testRoot)
    testDecompressArchive(testRoot)
    testReadTextFile(testRoot)
    testWriteTextFile(testRoot)
    testEndToEndWorkflow(testRoot)

    errors = _printReport()

    import shutil
    shutil.rmtree(testRoot, ignore_errors=True)
    print(f"\n已清理临时目录: {testRoot}")

    return errors


if __name__ == "__main__":
    exit(main())
