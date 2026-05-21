import re

from ndlmpanel_agent.exceptions import ToolExecutionException
from ndlmpanel_agent.models.ops.misc.database_models import DatabaseInstallInfo, DatabaseStatus
from ndlmpanel_agent.tools.ops._command_runner import runCommand

# 数据库类型 → 版本检测命令
_VERSION_COMMANDS: dict[str, list[str]] = {
    "mysql": ["mysql", "--version"],
    "mariadb": ["mysql", "--version"],
    "postgresql": ["psql", "--version"],
    "postgres": ["psql", "--version"],
    "redis": ["redis-server", "--version"],
    "mongodb": ["mongod", "--version"],
}

# 数据库类型 → systemd 服务名候选
_SERVICE_NAMES: dict[str, list[str]] = {
    "mysql": ["mysql", "mysqld", "mariadb"],
    "mariadb": ["mariadb", "mysql", "mysqld"],
    "postgresql": ["postgresql", "postgres"],
    "postgres": ["postgresql", "postgres"],
    "redis": ["redis", "redis-server"],
    "mongodb": ["mongod", "mongodb"],
}


def checkDatabaseInstalled(databaseType: str = "mysql") -> DatabaseInstallInfo:
    dbType = databaseType.lower()
    cmd = _VERSION_COMMANDS.get(dbType)
    if not cmd:
        return DatabaseInstallInfo(isInstalled=False, databaseType=databaseType)

    try:
        result = runCommand(cmd, checkReturnCode=False)
        output = result.stdout.strip() or result.stderr.strip()
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return DatabaseInstallInfo(
            isInstalled=True,
            version=match.group(1) if match else output[:50],
            databaseType=databaseType,
        )
    except ToolExecutionException:
        return DatabaseInstallInfo(isInstalled=False, databaseType=databaseType)


def getDatabaseStatus(databaseType: str = "mysql") -> DatabaseStatus:
    dbType = databaseType.lower()
    serviceNames = _SERVICE_NAMES.get(dbType, [dbType])

    isRunning = False
    for name in serviceNames:
        try:
            result = runCommand(["systemctl", "is-active", name], checkReturnCode=False)
            if result.stdout.strip() == "active":
                isRunning = True
                break
        except ToolExecutionException:
            continue

    currentConnections = None
    slowQueryCount = None

    if isRunning and dbType in ("mysql", "mariadb"):
        try:
            result = runCommand(
                ["mysqladmin", "status"], checkReturnCode=False, timeout=5
            )
            if result.returncode == 0:
                tMatch = re.search(r"Threads:\s*(\d+)", result.stdout)
                sMatch = re.search(r"Slow queries:\s*(\d+)", result.stdout)
                if tMatch:
                    currentConnections = int(tMatch.group(1))
                if sMatch:
                    slowQueryCount = int(sMatch.group(1))
        except ToolExecutionException:
            pass

    return DatabaseStatus(
        isRunning=isRunning,
        databaseType=databaseType,
        currentConnections=currentConnections,
        slowQueryCount=slowQueryCount,
    )
# 测试 root 账户连接 MySQL
def testMysqlConnection(host, port, username, password):
    pass
# 创建 MySQL 数据库
def _validateMysqlIdentifier(name:str, fieldName:str = "名称") -> str:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
        raise ToolExecutionException(f"{fieldName} '{name}' 不合法。必须以字母或下划线开头，后续字符只能是字母、数字或下划线。")
    return name
def _escapeMysqlIdentifier(name:str)->str:
    return name.replace("\\", "\\\\").replace("'", "\\'")
def createMysqlDatabase(dbName):
    dbName = _validateMysqlIdentifier(dbName,"数据库名称")

    sql = (
        f"CREATE DATABASE IF NOT EXISTS'{dbName}'"
        " CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;"
    )
    result = runCommand(
        ["mysql","-e",sql],
        useSudo=True,
        checkReturnCode=False,
    )

    if result.returncode != 0:
        errorMessage = result.stderr.strip() or result.stdout.strip()
        raise ToolExecutionException(f"创建数据库失败: {errorMessage}")
    
    return {
        "daName":dbName,
        "charset":"utf8mb4",
        "collation":"utf8mb4_general_ci",
        "isCreated":True,
    }

# 创建 MySQL 用户并授权指定数据库
def createMysqlUserAndGrant(dbName, username, password):
    dbName = _validateMysqlIdentifier(dbName,"数据库名称")
    username = _validateMysqlIdentifier(username,"用户名")
    escapePassword = _escapeMysqlIdentifier(password)

    sql = (
        f"CREATE USER IF NOT EXISTS '{username}'@'localhost' "
        f"IDENTIFIED BY '{escapePassword}'; "
        f"ALTER USER '{username}'@'localhost' IDENTIFIED BY '{escapePassword}'; "
        f"GRANT ALL PRIVILEGES ON `{dbName}`.* TO '{username}'@'localhost'; "
        "FLUSH PRIVILEGES;"
    )
    result = runCommand(
        ["mysql","-e",sql],
        useSudo=True,
        checkReturnCode=False,
    )

    if result.returncode != 0:
        errorMessage = result.stderr.strip() or result.stdout.strip()
        raise ToolExecutionException(f"创建用户或授权失败: {errorMessage}")
    return {
        "dbName":dbName,
        "username":username,
        "host":'localhost',
        "privileges":"ALL PRIVILEGES",
        "isGranted":True,
        "isCreated":True,
    }
# 获取所有数据库列表
def getMysqlDatabaseList():
    pass