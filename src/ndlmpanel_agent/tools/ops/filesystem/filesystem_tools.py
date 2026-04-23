"""
文件系统操作工具
使用 pathlib/os/shutil，不依赖外部命令
"""

import grp
import os
import pwd
import shutil
import stat
from datetime import datetime
from pathlib import Path

from ndlmpanel_agent.exceptions import (
    PermissionDeniedException,
    ResourceNotFoundException,
    ToolExecutionException,
)
from ndlmpanel_agent.models.ops.filesystem.filesystem_models import (
    CompressResult,
    DecompressResult,
    DirectoryTreeNode,
    DirectoryTreeResult,
    FileInfo,
    FileOperationResult,
    FileType,
    GrepMatch,
    GrepResult,
    OwnerChangeResult,
    PermissionChangeResult,
    TextFileCheckResult,
    TextFileReadResult,
    TextFileWriteResult,
)


# ────────────────── 内部辅助 ──────────────────


def _resolveFileType(path: Path) -> FileType:
    if path.is_symlink():
        return FileType.SYMLINK
    if path.is_dir():
        return FileType.DIRECTORY
    if path.is_file():
        return FileType.FILE
    return FileType.OTHER


def _formatPermissions(mode: int) -> str:
    """将 st_mode 中的权限位转为 rwx 字符串，如 'rwxr-xr-x'"""
    octalMode = stat.S_IMODE(mode)
    result = ""
    for shift in (6, 3, 0):
        bits = (octalMode >> shift) & 0o7
        result += "r" if bits & 4 else "-"
        result += "w" if bits & 2 else "-"
        result += "x" if bits & 1 else "-"
    return result


def _requireExists(path: Path, label: str = "路径") -> None:
    if not path.exists():
        raise ResourceNotFoundException(f"{label}不存在: {path}")


# ────────────────── 公开接口 ──────────────────


def listDirectory(targetPath: str) -> list[FileInfo]:
    path = Path(targetPath)
    _requireExists(path, "目录")
    if not path.is_dir():
        raise ToolExecutionException(f"目标不是目录: {targetPath}")

    results: list[FileInfo] = []
    try:
        entries = sorted(
            (e for e in path.iterdir() if e.is_dir()),
            key=lambda x: x.name.lower(),
        )
    except PermissionError:
        raise PermissionDeniedException(f"无权访问目录: {targetPath}")

    for entry in entries:
        try:
            st = entry.lstat()
            results.append(
                FileInfo(
                    fileName=entry.name,
                    fileType=_resolveFileType(entry),
                    createdTime=datetime.fromtimestamp(st.st_ctime),
                    owner=pwd.getpwuid(st.st_uid).pw_name if hasattr(pwd, "getpwuid") else None,
                    group=grp.getgrgid(st.st_gid).gr_name if hasattr(grp, "getgrgid") else None,
                    sizeBytes=st.st_size,
                    permissions=_formatPermissions(st.st_mode),
                    modifiedTime=datetime.fromtimestamp(st.st_mtime),
                    absolutePath=str(entry.resolve())
                    if not entry.is_symlink()
                    else str(entry.absolute()),
                )
            )
        except (PermissionError, OSError):
            continue

    return results

def listSingleFileOrDirectory(targetPath: str) -> FileInfo:
    path = Path(targetPath)
    _requireExists(path, "路径")

    try:
        st = path.lstat()
        return FileInfo(
            fileName=path.name,
            fileType=_resolveFileType(path),
            createdTime=datetime.fromtimestamp(st.st_ctime),
            owner=pwd.getpwuid(st.st_uid).pw_name if hasattr(pwd, "getpwuid") else None,
            group=grp.getgrgid(st.st_gid).gr_name if hasattr(grp, "getgrgid") else None,
            sizeBytes=st.st_size,
            permissions=_formatPermissions(st.st_mode),
            modifiedTime=datetime.fromtimestamp(st.st_mtime),
            absolutePath=str(path.resolve()) if not path.is_symlink() else str(path.absolute()),
        )
    except PermissionError:
        raise PermissionDeniedException(f"无权访问: {targetPath}")

def grepFileOrDirectory(
    targetPath: str, 
    regExpr: str,
    recursive: bool = True,
    ignoreCase: bool = False,
    invertMatch: bool = False,
    searchFileNames: bool = False,
    timeout: int = 30,
) -> GrepResult:
    """
    搜索文件名或文件内容
    
    Args:
        targetPath:         目标文件或目录路径
        regExpr:            正则表达式模式
        recursive:          是否递归搜索目录（仅当targetPath是目录时有效）
        ignoreCase:         是否忽略大小写
        invertMatch:        是否反向匹配（仅用于搜索内容时）
        searchFileNames:    True=搜索文件名, False=搜索文件内容
        timeout:            命令超时秒数
    
    Returns:
        GrepResult:         包含所有匹配结果的包装类
    """
    path = Path(targetPath)
    _requireExists(path, "目标路径")
    
    try:
        if searchFileNames:
            return _grepFileNames(path, regExpr, recursive, ignoreCase, timeout)
        else:
            return _grepFileContent(path, regExpr, recursive, ignoreCase, invertMatch, timeout)
    
    except PermissionDeniedException:
        raise
    except ToolExecutionException:
        raise
    except Exception as e:
        raise ToolExecutionException(
            innerMessage=f"搜索失败: {str(e)}",
            cause=e,
        )


def _grepFileNames(
    path: Path,
    regExpr: str,
    recursive: bool,
    ignoreCase: bool,
    timeout: int,
) -> GrepResult:
    """使用 find 命令搜索文件名"""
    from ndlmpanel_agent.tools.ops._command_runner import runCommand
    
    # 构建find命令
    findCmd = ["find", str(path)]
    
    # 添加选项
    if recursive:
        pass
    else:
        findCmd.append("-maxdepth")
        findCmd.append("1")

    findCmd.extend(["-type", "f"])

    if ignoreCase:
        findCmd.append("-iregex")
    else:
        findCmd.append("-regex")
    
    # find 的正则匹配是全路径匹配，需要加上通配符前缀
    findCmd.append(f".*{regExpr}.*")
    
    # 执行 find 命令（不检查返回码，因为find找不到匹配时返回1）
    result = runCommand(findCmd, timeout=timeout, checkReturnCode=False)
    
    # 解析输出
    matches: list[GrepMatch] = []
    
    if result.stdout:
        # find 输出格式: 一行一个完整文件路径
        for lineNum, filePath in enumerate(result.stdout.strip().split("\n"), 1):
            if not filePath:
                continue
            
            try:
                file_path = Path(filePath)
                st = file_path.lstat()
                fileInfo = FileInfo(
                    fileName=file_path.name,
                    fileType=_resolveFileType(file_path),
                    createdTime=datetime.fromtimestamp(st.st_ctime),
                    owner=pwd.getpwuid(st.st_uid).pw_name if hasattr(pwd, "getpwuid") else None,
                    group=grp.getgrgid(st.st_gid).gr_name if hasattr(grp, "getgrgid") else None,
                    sizeBytes=st.st_size,
                    permissions=_formatPermissions(st.st_mode),
                    modifiedTime=datetime.fromtimestamp(st.st_mtime),
                    absolutePath=str(file_path.resolve()),
                )
                
                matches.append(
                    GrepMatch(
                        fileInfo=fileInfo,
                        lineNumber=lineNum,
                        lineContent=filePath,  # 对于文件名搜索，内容就是文件名
                    )
                )
            except (OSError, PermissionError):
                # 跳过无法读取的文件信息
                continue
    
    return GrepResult(
        success=True,
        pattern=regExpr,
        targetPath=str(path.resolve()),
        matches=matches,
        totalMatches=len(matches),
        errorMessage=None,
    )



def _grepFileContent(
    path: Path,
    regExpr: str,
    recursive: bool,
    ignoreCase: bool,
    invertMatch: bool,
    timeout: int,
) -> GrepResult:
    """使用 grep 命令搜索文件内容"""
    from ndlmpanel_agent.tools.ops._command_runner import runCommand
    
    # 构建grep命令
    grepCmd = ["grep"]
    
    # 添加选项
    if ignoreCase:
        grepCmd.append("-i")
    if invertMatch:
        grepCmd.append("-v")
    if recursive and path.is_dir():
        grepCmd.append("-r")
    
    # 添加行号输出（便于解析）
    grepCmd.append("-n")
    
    # 添加正则表达式和目标路径
    grepCmd.append(regExpr)
    grepCmd.append(str(path))
    
    # 执行grep命令（不检查返回码，因为grep找不到匹配时返回1）
    result = runCommand(grepCmd, timeout=timeout, checkReturnCode=False)
    
    # 解析输出
    matches: list[GrepMatch] = []
    fileInfoCache: dict[str, FileInfo] = {}  # 缓存文件信息以提高性能
    
    if result.returncode == 0 and result.stdout:
        # grep输出格式: filename:lineNumber:content
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
                
            # 分割文件路径、行号和内容（最多分割2次）
            parts = line.split(":", 2)
            if len(parts) >= 3:
                try:
                    filePath = parts[0]
                    lineNumber = int(parts[1])
                    lineContent = parts[2]
                    
                    # 检查缓存中是否已经有该文件的 FileInfo
                    if filePath not in fileInfoCache:
                        try:
                            file_path = Path(filePath)
                            st = file_path.lstat()
                            fileInfoCache[filePath] = FileInfo(
                                fileName=file_path.name,
                                fileType=_resolveFileType(file_path),
                                createdTime=datetime.fromtimestamp(st.st_ctime),
                                owner=pwd.getpwuid(st.st_uid).pw_name if hasattr(pwd, "getpwuid") else None,
                                group=grp.getgrgid(st.st_gid).gr_name if hasattr(grp, "getgrgid") else None,
                                sizeBytes=st.st_size,
                                permissions=_formatPermissions(st.st_mode),
                                modifiedTime=datetime.fromtimestamp(st.st_mtime),
                                absolutePath=str(file_path.resolve()),
                            )
                        except (OSError, PermissionError):
                            # 跳过无法读取的文件信息
                            continue
                    
                    matches.append(
                        GrepMatch(
                            fileInfo=fileInfoCache[filePath],
                            lineNumber=lineNumber,
                            lineContent=lineContent,
                        )
                    )
                except (ValueError, IndexError):
                    # 跳过解析错误的行
                    continue
    
    return GrepResult(
        success=True,
        pattern=regExpr,
        targetPath=str(path.resolve()),
        matches=matches,
        totalMatches=len(matches),
        errorMessage=None,
    )

def createFile(targetPath: str) -> FileOperationResult:
    path = Path(targetPath)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=False)
        return FileOperationResult(success=True, absolutePath=str(path.resolve()))
    except FileExistsError:
        raise ToolExecutionException(f"文件已存在: {targetPath}")
    except PermissionError:
        raise PermissionDeniedException(f"无权创建文件: {targetPath}")


def createDirectory(targetPath: str) -> FileOperationResult:
    path = Path(targetPath)
    try:
        path.mkdir(parents=True, exist_ok=True)
        return FileOperationResult(success=True, absolutePath=str(path.resolve()))
    except PermissionError:
        raise PermissionDeniedException(f"无权创建目录: {targetPath}")


def deleteFile(targetPath: str) -> FileOperationResult:
    path = Path(targetPath)
    _requireExists(path, "文件")
    if not path.is_file() and not path.is_symlink():
        raise ToolExecutionException(f"目标不是文件: {targetPath}")
    try:
        path.unlink()
        return FileOperationResult(success=True, absolutePath=targetPath)
    except PermissionError:
        raise PermissionDeniedException(f"无权删除文件: {targetPath}")


def deleteDirectory(targetPath: str, force: bool = False) -> FileOperationResult:
    path = Path(targetPath)
    _requireExists(path, "目录")
    if not path.is_dir():
        raise ToolExecutionException(f"目标不是目录: {targetPath}")

    try:
        if force:
            shutil.rmtree(path)
        else:
            path.rmdir()
        return FileOperationResult(success=True, absolutePath=targetPath)
    except OSError as e:
        if "not empty" in str(e).lower():
            raise ToolExecutionException(
                f"目录非空，请设置 force=True 以强制删除: {targetPath}"
            )
        raise PermissionDeniedException(f"删除目录失败: {e}")


def renameFileOrDirectory(sourcePath: str, destinationPath: str) -> FileOperationResult:
    src = Path(sourcePath)
    _requireExists(src, "源路径")

    dst = Path(destinationPath)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return FileOperationResult(success=True, absolutePath=str(dst.resolve()))
    except PermissionError:
        raise PermissionDeniedException(
            f"无权执行重命名/移动: {sourcePath} → {destinationPath}"
        )
    except OSError as e:
        raise ToolExecutionException(f"重命名/移动失败: {e}")


def changePermissions(
    targetPath: str,
    permissionMode: str,
    recursive: bool = False,
) -> PermissionChangeResult:
    path = Path(targetPath)
    _requireExists(path)

    # 解析八进制权限（如 "755"）
    try:
        mode = int(permissionMode, 8)
    except ValueError:
        raise ToolExecutionException(
            f"权限格式错误，请使用八进制格式如 '755': {permissionMode}"
        )

    try:
        if recursive and path.is_dir():
            for root, dirs, files in os.walk(path):
                os.chmod(root, mode)
                for f in files:
                    os.chmod(os.path.join(root, f), mode)
        else:
            os.chmod(path, mode)

        newMode = stat.S_IMODE(path.stat().st_mode)
        return PermissionChangeResult(success=True, newPermissions=oct(newMode)[2:])
    except PermissionError:
        raise PermissionDeniedException(f"无权修改权限: {targetPath}")


def changeOwner(
    targetPath: str,
    owner: str,
    group: str,
    recursive: bool = False,
) -> OwnerChangeResult:
    path = Path(targetPath)
    _requireExists(path)

    try:
        uid = pwd.getpwnam(owner).pw_uid
    except KeyError:
        raise ToolExecutionException(f"用户不存在: {owner}")

    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        raise ToolExecutionException(f"用户组不存在: {group}")

    try:
        if recursive and path.is_dir():
            for root, dirs, files in os.walk(path):
                os.chown(root, uid, gid)
                for f in files:
                    os.chown(os.path.join(root, f), uid, gid)
        else:
            os.chown(str(path), uid, gid)

        return OwnerChangeResult(success=True, newOwner=owner, newGroup=group)
    except PermissionError:
        raise PermissionDeniedException(f"无权修改所有者(通常需要root): {targetPath}")


# ────────────────── 新增工具函数 ──────────────────


def _buildDirectoryTree(path: Path, currentDepth: int, maxDepth: int) -> DirectoryTreeNode:
    node = DirectoryTreeNode(
        fileName=path.name,
        fileType=_resolveFileType(path),
        absolutePath=str(path.resolve()),
    )

    if currentDepth >= maxDepth or not path.is_dir():
        return node

    try:
        entries = sorted(
            (e for e in path.iterdir() if e.is_dir()),
            key=lambda x: x.name.lower(),
        )
    except PermissionError:
        return node

    for entry in entries:
        try:
            child = _buildDirectoryTree(entry, currentDepth + 1, maxDepth)
            node.children.append(child)
        except (PermissionError, OSError):
            continue

    return node


def getDirectoryTree(targetPath: str, maxDepth: int = 1) -> DirectoryTreeResult:
    path = Path(targetPath)
    _requireExists(path, "目录")
    if not path.is_dir():
        raise ToolExecutionException(f"目标不是目录: {targetPath}")
    if maxDepth < 1:
        raise ToolExecutionException(f"递归深度必须大于0: {maxDepth}")

    try:
        tree = _buildDirectoryTree(path, 0, maxDepth)
        return DirectoryTreeResult(
            success=True,
            rootPath=str(path.resolve()),
            maxDepth=maxDepth,
            tree=tree,
        )
    except PermissionError:
        raise PermissionDeniedException(f"无权访问目录: {targetPath}")


def copyFile(sourcePath: str, destinationPath: str) -> FileOperationResult:
    src = Path(sourcePath)
    _requireExists(src, "源文件")
    if not src.is_file():
        raise ToolExecutionException(f"源路径不是文件: {sourcePath}")

    dst = Path(destinationPath)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return FileOperationResult(success=True, absolutePath=str(dst.resolve()))
    except PermissionError:
        raise PermissionDeniedException(f"无权拷贝文件: {sourcePath} → {destinationPath}")
    except OSError as e:
        raise ToolExecutionException(f"拷贝文件失败: {e}")


def isTextFile(targetPath: str) -> TextFileCheckResult:
    path = Path(targetPath)
    _requireExists(path, "文件")
    if not path.is_file():
        return TextFileCheckResult(
            isTextFile=False,
            targetPath=targetPath,
            detectedEncoding=None,
        )

    try:
        chunk = path.read_bytes()
    except PermissionError:
        raise PermissionDeniedException(f"无权读取文件: {targetPath}")

    if b"\x00" in chunk:
        return TextFileCheckResult(
            isTextFile=False,
            targetPath=targetPath,
            detectedEncoding=None,
        )

    encoding = None
    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
        try:
            chunk.decode(enc)
            encoding = enc
            break
        except (UnicodeDecodeError, LookupError):
            continue

    return TextFileCheckResult(
        isTextFile=encoding is not None,
        targetPath=targetPath,
        detectedEncoding=encoding,
    )


def compressPath(targetPath: str) -> CompressResult:
    path = Path(targetPath)
    _requireExists(path, "目标路径")

    if path.is_file():
        archiveStem = path.stem
    elif path.is_dir():
        archiveStem = path.name
    else:
        raise ToolExecutionException(f"不支持的文件类型: {targetPath}")

    archiveBasePath = path.parent / archiveStem
    archivePath = path.parent / (archiveStem + ".tar.gz")

    try:
        if path.is_dir():
            shutil.make_archive(
                base_name=str(archiveBasePath),
                format="gztar",
                root_dir=str(path.parent),
                base_dir=path.name,
            )
        else:
            import tarfile
            with tarfile.open(str(archivePath), "w:gz") as tar:
                tar.add(str(path), arcname=path.name)

        st = archivePath.stat()
        return CompressResult(
            success=True,
            sourcePath=str(path.resolve()),
            archivePath=str(archivePath.resolve()),
            archiveSizeBytes=st.st_size,
        )
    except PermissionError:
        raise PermissionDeniedException(f"无权创建压缩文件: {archivePath}")
    except OSError as e:
        raise ToolExecutionException(f"压缩失败: {e}")


def decompressArchive(archivePath: str, targetPath: str | None = None) -> DecompressResult:
    path = Path(archivePath)
    _requireExists(path, "压缩文件")
    if not path.is_file():
        raise ToolExecutionException(f"目标不是文件: {archivePath}")

    suffixes = path.suffixes
    isZip = suffixes[-1] == ".zip"
    isTarGz = len(suffixes) >= 2 and suffixes[-2] == ".tar" and suffixes[-1] == ".gz"
    isTar = suffixes[-1] == ".tar" and not isTarGz

    if not (isZip or isTarGz or isTar):
        raise ToolExecutionException(
            f"不支持的压缩格式，仅支持 .tar.gz / .tar / .zip: {archivePath}"
        )

    if targetPath is None:
        targetPath = str(path.parent)

    dest = Path(targetPath)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise PermissionDeniedException(f"无权创建目标目录: {targetPath}")

    try:
        if isZip:
            shutil.unpack_archive(str(path), str(dest), format="zip")
        elif isTarGz:
            shutil.unpack_archive(str(path), str(dest), format="gztar")
        elif isTar:
            shutil.unpack_archive(str(path), str(dest), format="tar")

        return DecompressResult(
            success=True,
            archivePath=str(path.resolve()),
            targetPath=str(dest.resolve()),
        )
    except PermissionError:
        raise PermissionDeniedException(f"无权解压文件: {archivePath}")
    except OSError as e:
        raise ToolExecutionException(f"解压失败: {e}")


def readTextFile(targetPath: str) -> TextFileReadResult:
    path = Path(targetPath)
    _requireExists(path, "文件")
    if not path.is_file():
        raise ToolExecutionException(f"目标不是文件: {targetPath}")

    check = isTextFile(targetPath)
    if not check.isTextFile:
        raise ToolExecutionException(f"目标不是文本文件: {targetPath}")

    try:
        content = path.read_text(encoding=check.detectedEncoding or "utf-8")
        st = path.stat()
        return TextFileReadResult(
            success=True,
            targetPath=targetPath,
            content=content,
            encoding=check.detectedEncoding,
            sizeBytes=st.st_size,
        )
    except PermissionError:
        raise PermissionDeniedException(f"无权读取文件: {targetPath}")
    except OSError as e:
        raise ToolExecutionException(f"读取文件失败: {e}")


def writeTextFile(targetPath: str, content: str) -> TextFileWriteResult:
    path = Path(targetPath)
    _requireExists(path, "文件")
    if not path.is_file():
        raise ToolExecutionException(f"目标文件不存在，无法写入: {targetPath}")

    try:
        path.write_text(content, encoding="utf-8")
        st = path.stat()
        return TextFileWriteResult(
            success=True,
            targetPath=targetPath,
            sizeBytes=st.st_size,
        )
    except PermissionError:
        raise PermissionDeniedException(f"无权写入文件: {targetPath}")
    except OSError as e:
        raise ToolExecutionException(f"写入文件失败: {e}")
