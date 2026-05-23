import json
import subprocess

import pytest

from ndlmpanel_agent.exceptions import ToolExecutionException
from ndlmpanel_agent.tools.ops.misc import docker_tools


def _completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _patchDockerInstalled(monkeypatch):
    monkeypatch.setattr(
        docker_tools,
        "checkDockerInstalled",
        lambda: docker_tools.DockerInstallInfo(isInstalled=True, version="24.0.0"),
    )


def _sampleInspectData():
    return [
        {
            "Name": "/web",
            "Config": {
                "Image": "nginx:latest",
                "Env": [
                    "NODE_ENV=production",
                    "KEEP=value=with=equals",
                ],
            },
            "HostConfig": {
                "PortBindings": {
                    "80/tcp": [{"HostPort": "8080"}],
                    "443/tcp": [{"HostPort": "8443"}],
                }
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/srv/web",
                    "Destination": "/usr/share/nginx/html",
                },
                {
                    "Type": "volume",
                    "Source": "named-volume",
                    "Destination": "/data",
                },
            ],
        }
    ]


def testGetDockerImageListParsesJsonLines(monkeypatch):
    _patchDockerInstalled(monkeypatch)

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        assert command == ["docker", "images", "--format", "{{json .}}"]
        assert timeout == 30
        assert checkReturnCode is False
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "Repository": "nginx",
                        "Tag": "latest",
                        "ID": "sha256:abc",
                        "CreatedSince": "2 weeks ago",
                        "CreatedAt": "2026-05-01",
                        "Size": "192MB",
                    }
                ),
                "not json",
            ]
        )
        return _completed(command, stdout=stdout)

    monkeypatch.setattr(docker_tools, "runCommand", fakeRunCommand)

    assert docker_tools.getDockerImageList() == [
        {
            "repository": "nginx",
            "tag": "latest",
            "imageId": "sha256:abc",
            "createdSince": "2 weeks ago",
            "createdAt": "2026-05-01",
            "size": "192MB",
        }
    ]


def testGetDockerImageListRaisesOnDockerError(monkeypatch):
    _patchDockerInstalled(monkeypatch)

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        return _completed(command, returncode=1, stderr="docker daemon unavailable")

    monkeypatch.setattr(docker_tools, "runCommand", fakeRunCommand)

    with pytest.raises(ToolExecutionException, match="获取 Docker 镜像列表失败"):
        docker_tools.getDockerImageList()


def testCreateDockerContainerBuildsRunCommand(monkeypatch):
    _patchDockerInstalled(monkeypatch)
    commands = []

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        commands.append(
            {
                "command": command,
                "timeout": timeout,
                "checkReturnCode": checkReturnCode,
            }
        )
        return _completed(command, stdout="container123\n")

    monkeypatch.setattr(docker_tools, "runCommand", fakeRunCommand)

    result = docker_tools.createDockerContainer(
        imageName="nginx:latest",
        containerName="web",
        ports={"8080": "80"},
        envVars={"NODE_ENV": "production"},
        volumes={"/srv/web": "/usr/share/nginx/html"},
    )

    assert commands[0] == {
        "command": [
            "docker",
            "run",
            "-d",
            "--name",
            "web",
            "-p",
            "8080:80",
            "-e",
            "NODE_ENV=production",
            "-v",
            "/srv/web:/usr/share/nginx/html",
            "nginx:latest",
        ],
        "timeout": 30,
        "checkReturnCode": False,
    }
    assert result["containerId"] == "container123"
    assert result["isCreated"] is True


def testCreateDockerContainerRejectsInvalidPort(monkeypatch):
    _patchDockerInstalled(monkeypatch)

    with pytest.raises(ToolExecutionException, match="主机端口"):
        docker_tools.createDockerContainer(
            imageName="nginx:latest",
            containerName="web",
            ports={"99999": "80"},
        )


def testGetDockerContainerListIncludesStoppedContainers(monkeypatch):
    called = {}

    def fakeGetDockerContainers(includeStoppedContainers=False):
        called["includeStoppedContainers"] = includeStoppedContainers
        return []

    monkeypatch.setattr(docker_tools, "getDockerContainers", fakeGetDockerContainers)
    _patchDockerInstalled(monkeypatch)

    assert docker_tools.getDockerContainerList() == []
    assert called["includeStoppedContainers"] is True


def testGetDockerContainerLogsReturnsStdoutAndStderr(monkeypatch):
    _patchDockerInstalled(monkeypatch)

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        assert command == ["docker", "logs", "--tail", "50", "web"]
        return _completed(command, stdout="hello\n", stderr="warning\n")

    monkeypatch.setattr(docker_tools, "runCommand", fakeRunCommand)

    assert docker_tools.getDockerContainerLogs("web", tailLines=50) == {
        "containerId": "web",
        "logs": "hello",
        "errors": "warning",
    }


def testGetDockerContainerInfoParsesInspectJson(monkeypatch):
    _patchDockerInstalled(monkeypatch)

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        assert command == ["docker", "inspect", "web"]
        return _completed(command, stdout=json.dumps(_sampleInspectData()))

    monkeypatch.setattr(docker_tools, "runCommand", fakeRunCommand)

    result = docker_tools.getDockerContainerInfo("web")

    assert result["Name"] == "/web"
    assert result["Config"]["Image"] == "nginx:latest"


def testUpdateContainerEnvRecreatesContainerWithMergedConfig(monkeypatch):
    _patchDockerInstalled(monkeypatch)
    monkeypatch.setattr(docker_tools.time, "time", lambda: 1770000000)
    commands = []

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        commands.append(
            {
                "command": command,
                "checkReturnCode": checkReturnCode,
            }
        )
        if command == ["docker", "inspect", "old123"]:
            return _completed(command, stdout=json.dumps(_sampleInspectData()))
        if command[:2] == ["docker", "run"]:
            return _completed(command, stdout="new456\n")
        return _completed(command)

    monkeypatch.setattr(docker_tools, "runCommand", fakeRunCommand)

    result = docker_tools.updateContainerEnv("old123", {"DEBUG": "false"})

    assert result["oldContainerId"] == "old123"
    assert result["newContainerId"] == "new456"
    assert result["backupName"] == "web_backup_1770000000"
    commandList = [item["command"] for item in commands]
    assert ["docker", "stop", "old123"] in commandList
    assert ["docker", "rename", "old123", "web_backup_1770000000"] in commandList
    assert ["docker", "rm", "web_backup_1770000000"] in commandList

    runCommand = next(command for command in commandList if command[:2] == ["docker", "run"])
    assert "-e" in runCommand
    assert "NODE_ENV=production" in runCommand
    assert "KEEP=value=with=equals" in runCommand
    assert "DEBUG=false" in runCommand
    assert "8080:80" in runCommand
    assert "8443:443" in runCommand
    assert "/srv/web:/usr/share/nginx/html" in runCommand


def testReCreateDockerContainerRollsBackWhenCreateFails(monkeypatch):
    _patchDockerInstalled(monkeypatch)
    monkeypatch.setattr(docker_tools.time, "time", lambda: 1770000000)
    commands = []

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        commands.append(
            {
                "command": command,
                "checkReturnCode": checkReturnCode,
            }
        )
        if command == ["docker", "inspect", "old123"]:
            return _completed(command, stdout=json.dumps(_sampleInspectData()))
        if command[:2] == ["docker", "run"]:
            return _completed(command, returncode=1, stderr="name conflict")
        return _completed(command)

    monkeypatch.setattr(docker_tools, "runCommand", fakeRunCommand)

    with pytest.raises(ToolExecutionException, match="重新创建 Docker 容器失败"):
        docker_tools.reCreateDockerContainer("old123", envVars={"DEBUG": "false"})

    commandList = [item["command"] for item in commands]
    assert ["docker", "rm", "-f", "web"] in commandList
    assert ["docker", "rename", "web_backup_1770000000", "web"] in commandList
    assert ["docker", "start", "web"] in commandList
    assert all(
        item["checkReturnCode"] is False
        for item in commands
        if item["command"]
        in [
            ["docker", "rm", "-f", "web"],
            ["docker", "rename", "web_backup_1770000000", "web"],
            ["docker", "start", "web"],
        ]
    )
