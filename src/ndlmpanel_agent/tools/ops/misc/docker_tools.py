import json
import re
import time
from ndlmpanel_agent.exceptions import (
    ServiceUnavailableException,
    ToolExecutionException,
)
from ndlmpanel_agent.models.ops.misc.docker_models import DockerContainer, DockerInstallInfo
from ndlmpanel_agent.tools.ops._command_runner import runCommand


def checkDockerInstalled() -> DockerInstallInfo:
    try:
        result = runCommand(["docker", "--version"])
        versionStr = result.stdout.strip().split(",")[0].replace("Docker version ", "")
        return DockerInstallInfo(isInstalled=True, version=versionStr)
    except ToolExecutionException:
        return DockerInstallInfo(isInstalled=False)


def _parseMemoryValue(valueStr: str) -> float:
    """解析 '100MiB' / '1.5GiB' / '512KiB' → MB"""
    valueStr = valueStr.strip()
    multipliers = {
        "GiB": 1024,
        "MiB": 1,
        "KiB": 1 / 1024,
        "GB": 1000,
        "MB": 1,
        "KB": 0.001,
    }
    for suffix, factor in multipliers.items():
        if suffix in valueStr:
            try:
                return float(valueStr.replace(suffix, "").strip()) * factor
            except ValueError:
                return 0.0
    return 0.0


def getDockerContainers(
    includeStoppedContainers: bool = False,
) -> list[DockerContainer]:
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")

    cmd = ["docker", "ps", "--format", "{{json .}}", "--no-trunc"]
    if includeStoppedContainers:
        cmd.insert(2, "-a")

    result = runCommand(cmd)

    containers: list[DockerContainer] = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        container = DockerContainer(
            containerId=data.get("ID", ""),
            imageName=data.get("Image", ""),
            status=data.get("Status", ""),
            ports=data.get("Ports", ""),
        )

        # 对运行中的容器尝试获取资源占用
        if "Up" in container.status:
            try:
                statsResult = runCommand(
                    [
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{.CPUPerc}},{{.MemUsage}}",
                        container.containerId,
                    ],
                    timeout=10,
                )
                parts = statsResult.stdout.strip().split(",")
                if len(parts) >= 2:
                    container.cpuPercent = float(parts[0].strip().rstrip("%"))
                    memParts = parts[1].strip().split("/")
                    container.memoryUsageMB = _parseMemoryValue(memParts[0])
                    if len(memParts) > 1:
                        container.memoryLimitMB = _parseMemoryValue(memParts[1])
            except (ToolExecutionException, ValueError, IndexError):
                pass

        containers.append(container)

    return containers
# 连接本地 Docker 服务
def connectDocker():
    pass
# 拉取 Docker 镜像
def pullDockerImage(imageName : str, tag: str = "latest") -> dict:
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    fullImage = f"{imageName}:{tag}"
    runCommand(["docker", "pull", fullImage], timeout=300)

    return {
        "image": fullImage,
        "isPulled": True,
    }
# 获取本地所有镜像列表
def getDockerImageList() -> list[dict]:
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")

    result = runCommand(["docker", "images", "--format", "{{json .}}"], timeout=30, checkReturnCode=False)

    if result.returncode != 0:
        errorMessage = result.stderr.strip() or "未知错误"
        raise ToolExecutionException(f"获取 Docker 镜像列表失败: {errorMessage}")
    
    images: list[dict] = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        images.append({
            "repository": data.get("Repository", ""),
            "tag": data.get("Tag", ""),
            "imageId": data.get("ID", ""),
            "createdSince": data.get("CreatedSince", ""),
            "createdAt": data.get("CreatedAt", ""),
            "size": data.get("Size", ""),
        })

    return images

# 创建 Docker 容器（支持端口、环境变量、数据卷）
def _validateDockerPort(port,fieldName="端口"):
    try:
        portNumber = int(port)
    except (ValueError,TypeError):
        raise ToolExecutionException(f"{fieldName}必须是数字")

    if portNumber <= 0 or portNumber > 65535:
        raise ToolExecutionException(f"{fieldName}必须在1-65535之间")
    
    return str(portNumber)

def createDockerContainer(imageName, containerName, ports=None, envVars=None, volumes=None):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    
    imageName = imageName.strip()
    containerName = containerName.strip()
    if not imageName:
        raise ToolExecutionException("镜像名称不能为空")
    if not containerName:
        raise ToolExecutionException("容器名称不能为空")
    ports = ports or {}
    envVars = envVars or {}
    volumes = volumes or {}

    cmd = ["docker","run","-d","--name",containerName]

    for hostPort, containerPort in ports.items():
        hostPort = _validateDockerPort(hostPort,"主机端口")
        containerPort = _validateDockerPort(containerPort,"容器端口")
        cmd.extend(["-p", f"{hostPort}:{containerPort}"])

    for key, value in envVars.items():
        cmd.extend(["-e", f"{key}={value}"])

    for hostPath, containerPath in volumes.items():
        cmd.extend(["-v", f"{hostPath}:{containerPath}"])

    cmd.append(imageName)

    result = runCommand(cmd, timeout=30,checkReturnCode = False)

    if result.returncode != 0:
        errorMessage = result.stderr.strip() or "未知错误"
        raise ToolExecutionException(f"创建 Docker 容器失败: {errorMessage}")
    
    return {
        "containerId": result.stdout.strip(),
        "containerName": containerName,
        "imageName": imageName,
        "ports": ports,
        "envVars": envVars,
        "volumes": volumes,
        "isCreated": True,
    }
# 启动容器
def startDockerContainer(containerId):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    runCommand(["docker", "start", containerId], timeout=30)
# 停止容器
def stopDockerContainer(containerId):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    runCommand(["docker", "stop", containerId], timeout=30)
# 重启容器
def restartDockerContainer(containerId):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    runCommand(["docker", "restart", containerId], timeout=30)
# 删除容器
def deleteDockerContainer(containerId):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    runCommand(["docker", "rm", containerId], timeout=30)
# 获取所有容器列表（运行中+已停止）
def getDockerContainerList():
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    return getDockerContainers(includeStoppedContainers=True)
# 获取容器实时日志
def getDockerContainerLogs(containerId , tailLines: int = 200):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    result = runCommand(["docker", "logs", f"--tail", str(tailLines), containerId], timeout=30)
    return {
        "containerId": containerId,
        "logs": result.stdout.strip(),
        "errors": result.stderr.strip(),
    }
# 获取容器详细信息
def getDockerContainerInfo(containerId):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    result = runCommand(["docker", "inspect", containerId], timeout=30)
    data = json.loads(result.stdout.strip())
    if not data:
        raise ToolExecutionException("未找到容器信息")
    return data[0]
# 更新容器环境变量
def updateContainerEnv(containerId, newEnvVars):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    return reCreateDockerContainer(containerId,envVars=newEnvVars)
# 更新容器端口映射
def updateContainerPorts(containerId, newPorts):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    return reCreateDockerContainer(containerId, ports=newPorts)
# 更新容器数据卷挂载
def updateContainerVolumes(containerId, newVolumes):
    if not checkDockerInstalled().isInstalled:
        raise ServiceUnavailableException("Docker 未安装")
    return reCreateDockerContainer(containerId,volumes=newVolumes)

def reCreateDockerContainer(containerId, ports=None, envVars=None, volumes=None):
    containerInfo = getDockerContainerInfo(containerId)

    imageName = containerInfo["Config"]["Image"]
    containerName = containerInfo["Name"].lstrip("/")
    backupName = f"{containerName}_backup_{int(time.time())}"

    oldEnvVars = {}
    for item in containerInfo.get("Config", {}).get("Env", []) or []:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        oldEnvVars[key] = value

    oldPorts = {}
    portBindings = containerInfo.get("HostConfig", {}).get("PortBindings", {}) or {}
    for containerPortProto, bindings in portBindings.items():
        containerPort = containerPortProto.split("/", 1)[0]
        if not bindings:
            continue
        hostPort = bindings[0].get("HostPort")
        if hostPort:
            oldPorts[hostPort] = containerPort

    oldVolumes = {}
    for mount in containerInfo.get("Mounts", []) or []:
        if mount.get("Type") != "bind":
            continue
        source = mount.get("Source")
        destination = mount.get("Destination")
        if source and destination:
            oldVolumes[source] = destination
    

    mergedEnvVars = {**oldEnvVars, **(envVars or {})}
    mergedPorts = {**oldPorts, **(ports or {})}
    mergedVolumes = {**oldVolumes, **(volumes or {})}
    #停止旧容器
    stopDockerContainer(containerId)
    #旧容器改名为backup
    runCommand(["docker", "rename", containerId, backupName], timeout=30)
    #新建容器成功则删去旧容器,失败则将旧容器改回原名并启动
    try:
        createResult=createDockerContainer(imageName=imageName, containerName=containerName, ports=mergedPorts, envVars=mergedEnvVars, volumes=mergedVolumes)
        deleteDockerContainer(backupName)
        return {
            "oldContainerId": containerId,
            "newContainerId": createResult["containerId"],
            "containerName": containerName,
            "backupName": backupName,
            "isUpdated": True,
        }
    except ToolExecutionException as e:
        runCommand(["docker", "rm", "-f", containerName], timeout=30, checkReturnCode=False)
        runCommand(["docker", "rename", backupName, containerName], timeout=30, checkReturnCode=False)
        runCommand(["docker", "start", containerName], timeout=30, checkReturnCode=False)
        errorMessage = e.args[0] if e.args else "未知错误"
        raise ToolExecutionException(f"重新创建 Docker 容器失败: {errorMessage}")