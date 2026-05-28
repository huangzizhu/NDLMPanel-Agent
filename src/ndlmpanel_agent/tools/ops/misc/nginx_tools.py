import os
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path

from ndlmpanel_agent.exceptions import (
    ServiceUnavailableException,
    ToolExecutionException,
)
from ndlmpanel_agent.models.ops.misc.nginx_models import NginxInstallInfo, NginxStatus,NginxSiteCreateResult
from ndlmpanel_agent.tools.ops._command_runner import runCommand

SITES_ENABLED_DIR = Path("/etc/nginx/sites-enabled")
SITES_AVAILABLE_DIR = Path("/etc/nginx/sites-available")
LETSENCRYPT_LIVE_DIR = Path("/etc/letsencrypt/live")
DEFAULT_WEBROOT_BASE = Path("/var/www")

def checkNginxInstalled() -> NginxInstallInfo:
    try:
        result = runCommand(["nginx", "-v"], checkReturnCode=False)
        output = result.stderr.strip() or result.stdout.strip()

        version = None
        vMatch = re.search(r"nginx/([\d.]+)", output)
        if vMatch:
            version = vMatch.group(1)

        configPath = None
        testResult = runCommand(["nginx", "-t"], checkReturnCode=False)
        cMatch = re.search(r"configuration file (\S+)", testResult.stderr)
        if cMatch:
            configPath = cMatch.group(1)

        return NginxInstallInfo(
            isInstalled=True, version=version, configPath=configPath
        )
    except ToolExecutionException:
        return NginxInstallInfo(isInstalled=False)


def getNginxStatus() -> NginxStatus:
    if not checkNginxInstalled().isInstalled:
        raise ServiceUnavailableException("Nginx 未安装")

    isRunning = False
    workerCount = 0

    try:
        result = runCommand(["systemctl", "is-active", "nginx"], checkReturnCode=False)
        isRunning = result.stdout.strip() == "active"
    except ToolExecutionException:
        pass

    if isRunning:
        try:
            result = runCommand(
                ["pgrep", "-c", "-f", "nginx: worker"], checkReturnCode=False
            )
            workerCount = int(result.stdout.strip())
        except (ToolExecutionException, ValueError):
            pass

    # 尝试读取 stub_status 获取连接数
    activeConnections = None
    try:
        resp = urllib.request.urlopen("http://127.0.0.1/nginx_status", timeout=2)
        content = resp.read().decode()
        connMatch = re.search(r"Active connections:\s*(\d+)", content)
        if connMatch:
            activeConnections = int(connMatch.group(1))
    except Exception:
        pass

    return NginxStatus(
        isRunning=isRunning,
        workerProcessCount=workerCount,
        activeConnections=activeConnections,
        requestsPerSecond=None,
    )

def generateStaticSiteConfig(domain: str, rootPath: str, listenPort: int = 80) -> str:
    return f"""server {{
    listen {listenPort};
    server_name {domain};
    root {rootPath};
    index index.html;
    location / {{
        try_files $uri $uri/ =404;
    }}
}}"""

def generateProxyConfig(domain: str, proxyPass: str, listenPort: int = 80) -> str:
    return f"""server {{
    listen {listenPort};
    server_name {domain};
    location /.well-known/acme-challenge/ {{
        root {DEFAULT_WEBROOT_BASE / domain};
    }}
    location / {{
        proxy_pass {proxyPass};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}"""

def createNginxSite(
        domain: str,
        mode: str,
        listenPort: int,
        rootPath: str | None = None,
        proxyPass: str | None = None
)-> NginxSiteCreateResult:
    mode = mode.strip().lower()
    if mode == "static":
        if not rootPath:
            raise ToolExecutionException("静态站点必须提供 rootPath")
        configContent = generateStaticSiteConfig(domain, rootPath, listenPort)
    elif mode == "reverse_proxy":
        if not proxyPass:
            raise ToolExecutionException("反向代理必须提供 proxyPass")
        configContent = generateProxyConfig(domain, proxyPass, listenPort)
    else:
        raise ToolExecutionException("不支持的模式")
    
    configPath = saveNginxConfig(domain, configContent)
    try:
        testNginxConfig()
    except ToolExecutionException:
        runCommand(["rm", "-f", configPath], useSudo=True, checkReturnCode=False)
        raise

    reloadNginx()

    return NginxSiteCreateResult(
        domain=domain,
        mode=mode,
        listenPort=listenPort,
        configPath=configPath,
        enabledPath=configPath,
        rootPath=rootPath if mode == "static" else None,
        proxyPass=proxyPass if mode == "reverse_proxy" else None,
        isEnabled=True,
        isReloaded=True,
    )   


def createNginxReverseProxySite(
    domain:str,
    proxyPort:str,
    proxyPass:str,
    listenPort:int,
    proxyProtocol:str="http"
)-> NginxSiteCreateResult:
    proxyPass = f"{proxyProtocol}://{proxyPass}:{proxyPort}"
    return createNginxSite(
        domain=domain,
        mode="reverse_proxy",
        listenPort=listenPort,
        proxyPass=proxyPass
)

# 保存 Nginx 配置文件到系统
def saveNginxConfig(configName, configContent):
    siteName = configName.replace("*.", "").replace("/", "_")
    if siteName.endswith(".conf"):
        siteName = siteName[:-5]
    configPath = str(SITES_ENABLED_DIR / f"{siteName}.conf")

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        suffix=".conf",
    ) as tmpFile:
        tmpFile.write(configContent)
        tmpPath = tmpFile.name

    try:
        runCommand(["install", "-D", "-m", "644", tmpPath, configPath], useSudo=True)
    finally:
        try:
            os.unlink(tmpPath)
        except OSError:
            pass

    return configPath


# 测试 Nginx 配置是否合法
def testNginxConfig():
    runCommand(["nginx", "-t"], useSudo=True)


# 重载 Nginx 使配置生效
def reloadNginx():
    runCommand(["systemctl", "reload", "nginx"], useSudo=True)


# 重启 Nginx
def restartNginx():
    runCommand(["systemctl", "restart", "nginx"], useSudo=True)


def _normalizeSiteName(configName: str) -> str:
    """把域名或配置名统一成 nginx 配置文件名。"""
    siteName = configName.replace("*.", "").replace("/", "_")
    if siteName.endswith(".conf"):
        siteName = siteName[:-5]
    return siteName


def _findSiteConfigPath(domain: str) -> str | None:
    """
    在 sites-enabled / sites-available 中查找和域名匹配的配置。

    学习点：
    - 先找 `sites-enabled`，因为它表示当前生效的配置。
    - 找不到时再退回 `sites-available`，这样可以兼容“未启用但已写入”的场景。
    - 这里先用文件名匹配，后续如果你要做得更强，可以再读配置内容匹配 `server_name`。
    """
    siteName = _normalizeSiteName(domain)
    candidates = [
        SITES_ENABLED_DIR / f"{siteName}.conf",
        SITES_AVAILABLE_DIR / f"{siteName}.conf",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _resolveWebrootFromConfig(configPath: str, domain: str) -> str:
    """
    从 nginx 配置中推断 webroot。

    学习点：
    - Let's Encrypt 的 HTTP-01 验证需要一个“网站根目录”，certbot 会把临时验证文件放到这里。
    - 如果配置里写了 `root /path;`，通常这就是正确的 webroot。
    - 如果解析不到，就先用一个约定目录 `/var/www/{domain}`，让你后面可以手工校正。
    """
    try:
        content = Path(configPath).read_text(encoding="utf-8")
        rootMatch = re.search(r"^\s*root\s+([^;]+);", content, re.MULTILINE)
        if rootMatch:
            return rootMatch.group(1).strip()
    except OSError:
        pass

    return str(DEFAULT_WEBROOT_BASE / domain)


def _readSiteConfig(configPath: str | None) -> str:
    if not configPath:
        return ""
    try:
        return Path(configPath).read_text(encoding="utf-8")
    except OSError:
        return ""


def _extractNginxDirective(configContent: str, directiveName: str) -> str | None:
    match = re.search(
        rf"^\s*{re.escape(directiveName)}\s+([^;]+);",
        configContent,
        re.MULTILINE,
    )
    return match.group(1).strip() if match else None


def _buildCertbotCommand(domain: str, email: str, webroot: str) -> list[str]:
    """
    只负责“拼命令”，不负责执行。

    学习点：把“生成命令”和“执行命令”拆开，测试会更容易写。
    """
    return [
        "certbot",
        "certonly",
        "--webroot",
        "-w",
        webroot,
        "-d",
        domain,
        "--email",
        email,
        "--agree-tos",
        "--non-interactive",
    ]


def applySslCertificate(domain, email):
    """
    自动申请 Let's Encrypt 免费证书。

    学习路径：
    1. 先确认系统里有 `certbot`。
    2. 找到这个域名对应的 nginx 配置，推断 `webroot`。
    3. 拼出 certbot 命令。
    4. 用 root 权限执行。
    5. 返回证书路径，后续再交给 Nginx 去挂载。
    """
    if not shutil.which("certbot"):
        raise ServiceUnavailableException("certbot 未安装，请先安装 certbot")

    configPath = _findSiteConfigPath(domain)
    if not configPath:
        raise ToolExecutionException(f"找不到域名 {domain} 对应的 nginx 配置")

    webroot = _resolveWebrootFromConfig(configPath, domain)
    if not webroot:
        raise ToolExecutionException(f"无法为 {domain} 推断 webroot")

    # 这里把“怎么申请证书”完全交给 certbot。
    # 我们的工作是：准备参数、执行命令、捕获失败。
    command = _buildCertbotCommand(domain, email, webroot)
    result = runCommand(command, useSudo=True, checkReturnCode=False)

    if result.returncode != 0:
        errorMessage = (result.stderr or result.stdout or "").strip()
        raise ToolExecutionException(
            f"申请证书失败: {' '.join(command)}\n{errorMessage}"
        )

    liveDir = LETSENCRYPT_LIVE_DIR / domain
    return {
        "domain": domain,
        "webroot": webroot,
        "certPath": str(liveDir / "fullchain.pem"),
        "keyPath": str(liveDir / "privkey.pem"),
    }


# 获取所有已创建的站点列表
def getNginxSiteList():
    """获取 /etc/nginx/sites-enabled 下所有已启用站点配置。"""
    sites = []

    if not SITES_ENABLED_DIR.exists():
        return sites

    for configPath in sorted(SITES_ENABLED_DIR.glob("*.conf")):
        content = configPath.read_text(encoding="utf-8", errors="ignore")

        serverNameMatch = re.search(r"server_name\s+([^;]+);", content)
        listenMatch = re.search(r"listen\s+([^;]+);", content)
        rootMatch = re.search(r"root\s+([^;]+);", content)
        proxyMatch = re.search(r"proxy_pass\s+([^;]+);", content)

        sites.append({
            "configName": configPath.name,
            "configPath": str(configPath),
            "domain": serverNameMatch.group(1).strip() if serverNameMatch else None,
            "listen": listenMatch.group(1).strip() if listenMatch else None,
            "mode": "reverse_proxy" if proxyMatch else "static" if rootMatch else "unknown",
            "rootPath": rootMatch.group(1).strip() if rootMatch else None,
            "proxyPass": proxyMatch.group(1).strip() if proxyMatch else None,
            "isEnabled": True,
        })

    return sites

# 删除指定站点配置
def deleteNginxSite(configName):
    """删除指定站点配置，并在 nginx -t 成功后重载 Nginx。"""
    siteName = _normalizeSiteName(configName)
    configPath = SITES_ENABLED_DIR / f"{siteName}.conf"

    if not configPath.exists():
        raise ToolExecutionException(f"站点配置不存在: {configPath}")

    runCommand(["rm", "-f", str(configPath)], useSudo=True)
    try:
        testNginxConfig()
    except ToolExecutionException:
        raise
    reloadNginx()

    return {
        "configName": configName,
        "configPath": str(configPath),
        "isDeleted": True,
        "isReloaded": True,
    }
# 自动配置 SSL 到 Nginx
def configSslForNginx(domain, certPath, keyPath):
    """生成 HTTPS 配置，先测试配置合法性，再重载 Nginx。"""
    certFile = Path(certPath)
    keyFile = Path(keyPath)
    if not certFile.exists():
        raise ToolExecutionException(f"证书文件不存在: {certPath}")
    if not keyFile.exists():
        raise ToolExecutionException(f"私钥文件不存在: {keyPath}")

    existingConfigPath = _findSiteConfigPath(domain)
    existingConfig = _readSiteConfig(existingConfigPath)
    proxyPass = _extractNginxDirective(existingConfig, "proxy_pass")
    webroot = _extractNginxDirective(existingConfig, "root") or str(
        DEFAULT_WEBROOT_BASE / domain
    )

    if proxyPass:
        siteLocation = f"""location / {{
        proxy_pass {proxyPass};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}"""
    else:
        siteLocation = """root {webroot};
    index index.html;

    location / {{
        try_files $uri $uri/ =404;
    }}""".format(webroot=webroot)

    configContent = f"""server {{
    listen 80;
    server_name {domain};

    location /.well-known/acme-challenge/ {{
        root {webroot};
    }}

    location / {{
        return 301 https://$host$request_uri;
    }}
}}

server {{
    listen 443 ssl;
    server_name {domain};

    ssl_certificate {certPath};
    ssl_certificate_key {keyPath};

    {siteLocation}
}}"""

    configPath = saveNginxConfig(domain, configContent)
    testNginxConfig()
    reloadNginx()

    return {
        "domain": domain,
        "configPath": configPath,
        "certPath": certPath,
        "keyPath": keyPath,
        "isSslConfigured": True,
        "isReloaded": True,
    }
# 自动续期 SSL 证书
def renewSslCertificate(domain):
    """使用 certbot 续期指定域名证书，并在成功后重载 Nginx。"""
    if not shutil.which("certbot"):
        raise ServiceUnavailableException("certbot 未安装，请先安装 certbot")

    result = runCommand(
        ["certbot", "renew", "--cert-name", domain, "--non-interactive"],
        useSudo=True,
        checkReturnCode=False,
    )

    if result.returncode != 0:
        errorMessage = (result.stderr or result.stdout or "").strip()
        raise ToolExecutionException(f"续期证书失败: {errorMessage}")

    testNginxConfig()
    reloadNginx()

    return {
        "domain": domain,
        "isRenewed": True,
        "isReloaded": True,
    }
