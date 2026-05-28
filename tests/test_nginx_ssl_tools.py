import subprocess

import pytest

from ndlmpanel_agent.exceptions import ServiceUnavailableException, ToolExecutionException
from ndlmpanel_agent.tools.ops.misc import nginx_tools


def testBuildCertbotCommandUsesWebrootAndNonInteractiveMode():
    command = nginx_tools._buildCertbotCommand(
        domain="example.com",
        email="admin@example.com",
        webroot="/var/www/example.com",
    )

    assert command == [
        "certbot",
        "certonly",
        "--webroot",
        "-w",
        "/var/www/example.com",
        "-d",
        "example.com",
        "--email",
        "admin@example.com",
        "--agree-tos",
        "--non-interactive",
    ]


def testApplySslCertificateReturnsCertPaths(monkeypatch):
    def fakeWhich(name):
        return "/usr/bin/certbot" if name == "certbot" else None

    commands = []

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        commands.append({
            "command": command,
            "timeout": timeout,
            "checkReturnCode": checkReturnCode,
            "useSudo": useSudo,
        })
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(nginx_tools.shutil, "which", fakeWhich)
    monkeypatch.setattr(nginx_tools, "_findSiteConfigPath", lambda domain: "/etc/nginx/sites-enabled/example.com.conf")
    monkeypatch.setattr(nginx_tools, "_resolveWebrootFromConfig", lambda configPath, domain: "/var/www/example.com")
    monkeypatch.setattr(nginx_tools, "runCommand", fakeRunCommand)

    result = nginx_tools.applySslCertificate("example.com", "admin@example.com")

    assert result["domain"] == "example.com"
    assert result["webroot"] == "/var/www/example.com"
    assert result["certPath"] == "/etc/letsencrypt/live/example.com/fullchain.pem"
    assert result["keyPath"] == "/etc/letsencrypt/live/example.com/privkey.pem"
    assert commands[0]["command"][0] == "certbot"
    assert commands[0]["useSudo"] is True
    assert commands[0]["checkReturnCode"] is False


def testApplySslCertificateRejectsMissingCertbot(monkeypatch):
    monkeypatch.setattr(nginx_tools.shutil, "which", lambda name: None)

    with pytest.raises(ServiceUnavailableException):
        nginx_tools.applySslCertificate("example.com", "admin@example.com")


def testApplySslCertificateRejectsMissingConfig(monkeypatch):
    monkeypatch.setattr(nginx_tools.shutil, "which", lambda name: "/usr/bin/certbot")
    monkeypatch.setattr(nginx_tools, "_findSiteConfigPath", lambda domain: None)

    with pytest.raises(ToolExecutionException):
        nginx_tools.applySslCertificate("example.com", "admin@example.com")


def testConfigSslForNginxWritesHttpsConfigAndReloads(monkeypatch, tmp_path):
    enabledDir = tmp_path / "sites-enabled"
    enabledDir.mkdir()
    monkeypatch.setattr(nginx_tools, "SITES_ENABLED_DIR", enabledDir)
    monkeypatch.setattr(nginx_tools, "SITES_AVAILABLE_DIR", tmp_path / "sites-available")

    existingConfig = enabledDir / "example.com.conf"
    existingConfig.write_text(
        """server {
    listen 80;
    server_name example.com;
    root /srv/example.com;
}
""",
        encoding="utf-8",
    )
    certPath = tmp_path / "fullchain.pem"
    keyPath = tmp_path / "privkey.pem"
    certPath.write_text("cert", encoding="utf-8")
    keyPath.write_text("key", encoding="utf-8")

    commands = []
    installedFiles = {}

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        commands.append({
            "command": command,
            "useSudo": useSudo,
        })
        if command[:4] == ["install", "-D", "-m", "644"]:
            with open(command[4], encoding="utf-8") as tmpFile:
                installedFiles[command[5]] = tmpFile.read()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(nginx_tools, "runCommand", fakeRunCommand)

    result = nginx_tools.configSslForNginx(
        "example.com",
        str(certPath),
        str(keyPath),
    )

    configPath = str(enabledDir / "example.com.conf")
    assert result == {
        "domain": "example.com",
        "configPath": configPath,
        "certPath": str(certPath),
        "keyPath": str(keyPath),
        "isSslConfigured": True,
        "isReloaded": True,
    }
    config = installedFiles[configPath]
    assert "listen 80;" in config
    assert "return 301 https://$host$request_uri;" in config
    assert "listen 443 ssl;" in config
    assert f"ssl_certificate {certPath};" in config
    assert f"ssl_certificate_key {keyPath};" in config
    assert "root /srv/example.com;" in config
    assert [item["command"] for item in commands] == [
        ["install", "-D", "-m", "644", commands[0]["command"][4], configPath],
        ["nginx", "-t"],
        ["systemctl", "reload", "nginx"],
    ]
    assert all(item["useSudo"] for item in commands)


def testConfigSslForNginxKeepsReverseProxyLocation(monkeypatch, tmp_path):
    enabledDir = tmp_path / "sites-enabled"
    enabledDir.mkdir()
    monkeypatch.setattr(nginx_tools, "SITES_ENABLED_DIR", enabledDir)
    monkeypatch.setattr(nginx_tools, "SITES_AVAILABLE_DIR", tmp_path / "sites-available")

    existingConfig = enabledDir / "api.example.com.conf"
    existingConfig.write_text(
        """server {
    listen 80;
    server_name api.example.com;
    location / {
        proxy_pass http://127.0.0.1:3000;
    }
}
""",
        encoding="utf-8",
    )
    certPath = tmp_path / "fullchain.pem"
    keyPath = tmp_path / "privkey.pem"
    certPath.write_text("cert", encoding="utf-8")
    keyPath.write_text("key", encoding="utf-8")

    installedFiles = {}

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        if command[:4] == ["install", "-D", "-m", "644"]:
            with open(command[4], encoding="utf-8") as tmpFile:
                installedFiles[command[5]] = tmpFile.read()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(nginx_tools, "runCommand", fakeRunCommand)

    nginx_tools.configSslForNginx(
        "api.example.com",
        str(certPath),
        str(keyPath),
    )

    config = installedFiles[str(enabledDir / "api.example.com.conf")]
    assert "listen 443 ssl;" in config
    assert "proxy_pass http://127.0.0.1:3000;" in config
    assert "proxy_set_header Host $host;" in config
    assert "root /var/www/api.example.com;" in config
    assert "try_files $uri $uri/ =404;" not in config


def testConfigSslForNginxRejectsMissingCertificate(monkeypatch, tmp_path):
    keyPath = tmp_path / "privkey.pem"
    keyPath.write_text("key", encoding="utf-8")

    with pytest.raises(ToolExecutionException):
        nginx_tools.configSslForNginx(
            "example.com",
            str(tmp_path / "missing.pem"),
            str(keyPath),
        )


def testRenewSslCertificateRunsCertbotRenewAndReloads(monkeypatch):
    monkeypatch.setattr(nginx_tools.shutil, "which", lambda name: "/usr/bin/certbot")

    commands = []

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        commands.append({
            "command": command,
            "useSudo": useSudo,
            "checkReturnCode": checkReturnCode,
        })
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(nginx_tools, "runCommand", fakeRunCommand)

    result = nginx_tools.renewSslCertificate("example.com")

    assert result == {
        "domain": "example.com",
        "isRenewed": True,
        "isReloaded": True,
    }
    assert [item["command"] for item in commands] == [
        ["certbot", "renew", "--cert-name", "example.com", "--non-interactive"],
        ["nginx", "-t"],
        ["systemctl", "reload", "nginx"],
    ]
    assert commands[0]["checkReturnCode"] is False
    assert all(item["useSudo"] for item in commands)


def testRenewSslCertificateRejectsCertbotFailure(monkeypatch):
    monkeypatch.setattr(nginx_tools.shutil, "which", lambda name: "/usr/bin/certbot")

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")

    monkeypatch.setattr(nginx_tools, "runCommand", fakeRunCommand)

    with pytest.raises(ToolExecutionException):
        nginx_tools.renewSslCertificate("example.com")
