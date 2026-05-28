import subprocess

import pytest

from ndlmpanel_agent.exceptions import ToolExecutionException
from ndlmpanel_agent.tools.ops.misc import database_tools


# ── _validateMysqlIdentifier ──────────────────────────────────────────

def testValidateMysqlIdentifierAcceptsValidNames():
    assert database_tools._validateMysqlIdentifier("mydb") == "mydb"
    assert database_tools._validateMysqlIdentifier("_test") == "_test"
    assert database_tools._validateMysqlIdentifier("db_001") == "db_001"


def testValidateMysqlIdentifierRejectsInvalidNames():
    with pytest.raises(ToolExecutionException):
        database_tools._validateMysqlIdentifier("1invalid")
    with pytest.raises(ToolExecutionException):
        database_tools._validateMysqlIdentifier("has space")
    with pytest.raises(ToolExecutionException):
        database_tools._validateMysqlIdentifier("drop;--")


# ── _escapeMysqlString ────────────────────────────────────────────────

def testEscapeMysqlStringHandlesBackslashAndSingleQuote():
    assert database_tools._escapeMysqlString(r"a\b") == r"a\\b"
    assert database_tools._escapeMysqlString("it's") == "it\\'s"


def testEscapeMysqlStringPassesPlainText():
    assert database_tools._escapeMysqlString("simple") == "simple"


# ── testMysqlConnection ───────────────────────────────────────────────

def testMysqlConnectionSucceedsWhenPingReturnsAlive(monkeypatch):
    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        return subprocess.CompletedProcess(
            command, 0, stdout="mysqld is alive\n", stderr=""
        )

    monkeypatch.setattr(database_tools, "runCommand", fakeRunCommand)

    result = database_tools.testMysqlConnection("127.0.0.1", 3306, "root", "secret")

    assert result == {
        "isConnectable": True,
        "host": "127.0.0.1",
        "port": 3306,
        "username": "root",
    }


def testMysqlConnectionFailsWhenPingRejected(monkeypatch):
    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="Access denied for user"
        )

    monkeypatch.setattr(database_tools, "runCommand", fakeRunCommand)

    result = database_tools.testMysqlConnection("127.0.0.1", 3306, "root", "bad")

    assert result["isConnectable"] is False
    assert result["host"] == "127.0.0.1"
    assert "Access denied" in result["errorMessage"]


def testMysqlConnectionFailsWhenHostUnreachable(monkeypatch):
    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        return subprocess.CompletedProcess(
            command, 2, stdout="", stderr="Can't connect to MySQL server"
        )

    monkeypatch.setattr(database_tools, "runCommand", fakeRunCommand)

    result = database_tools.testMysqlConnection("10.0.0.99", 3306, "root", "secret")

    assert result["isConnectable"] is False
    assert "Can't connect" in result["errorMessage"]


# ── createMysqlDatabase ───────────────────────────────────────────────

def testCreateMysqlDatabaseExecutesExpectedSql(monkeypatch):
    receivedSql = []

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        if command[0] == "mysql" and "-e" in command:
            receivedSql.append(command[command.index("-e") + 1])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(database_tools, "runCommand", fakeRunCommand)

    result = database_tools.createMysqlDatabase("mydb")

    assert result == {
        "dbName": "mydb",
        "charset": "utf8mb4",
        "collation": "utf8mb4_general_ci",
        "isCreated": True,
    }
    assert len(receivedSql) == 1
    assert "CREATE DATABASE IF NOT EXISTS `mydb`" in receivedSql[0]
    assert "utf8mb4" in receivedSql[0]


def testCreateMysqlDatabaseRejectsInvalidName():
    with pytest.raises(ToolExecutionException):
        database_tools.createMysqlDatabase("123invalid")


def testCreateMysqlDatabaseRaisesOnMysqlError(monkeypatch):
    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="ERROR: database already exists"
        )

    monkeypatch.setattr(database_tools, "runCommand", fakeRunCommand)

    with pytest.raises(ToolExecutionException, match="创建数据库失败"):
        database_tools.createMysqlDatabase("mydb")


# ── createMysqlUserAndGrant ───────────────────────────────────────────

def testCreateMysqlUserAndGrantExecutesExpectedSql(monkeypatch):
    receivedSql = []

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        if command[0] == "mysql" and "-e" in command:
            receivedSql.append(command[command.index("-e") + 1])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(database_tools, "runCommand", fakeRunCommand)

    result = database_tools.createMysqlUserAndGrant("mydb", "appuser", "p@ss")

    assert result == {
        "dbName": "mydb",
        "username": "appuser",
        "host": "localhost",
        "privileges": "ALL PRIVILEGES",
        "isGranted": True,
        "isCreated": True,
    }
    assert len(receivedSql) == 1
    sql = receivedSql[0]
    assert "CREATE USER IF NOT EXISTS 'appuser'@'localhost'" in sql
    assert "GRANT ALL PRIVILEGES ON `mydb`.*" in sql
    assert "FLUSH PRIVILEGES" in sql


def testCreateMysqlUserAndGrantEscapesPasswordWithBackslash(monkeypatch):
    """密码包含反斜杠时，SQL 字符串里应被正确转义"""
    receivedSql = []

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        if command[0] == "mysql" and "-e" in command:
            receivedSql.append(command[command.index("-e") + 1])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(database_tools, "runCommand", fakeRunCommand)

    database_tools.createMysqlUserAndGrant("mydb", "appuser", r"pass\word")

    sql = receivedSql[0]
    assert r"pass\\word" in sql


def testCreateMysqlUserAndGrantEscapesPasswordWithSingleQuote(monkeypatch):
    """密码包含单引号时，SQL 字符串里应被正确转义"""
    receivedSql = []

    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        if command[0] == "mysql" and "-e" in command:
            receivedSql.append(command[command.index("-e") + 1])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(database_tools, "runCommand", fakeRunCommand)

    database_tools.createMysqlUserAndGrant("mydb", "appuser", "pass'word")

    sql = receivedSql[0]
    assert "pass\\'word" in sql


def testCreateMysqlUserAndGrantRejectsInvalidDbName():
    with pytest.raises(ToolExecutionException):
        database_tools.createMysqlUserAndGrant("1bad", "appuser", "secret")


def testCreateMysqlUserAndGrantRejectsInvalidUsername():
    with pytest.raises(ToolExecutionException):
        database_tools.createMysqlUserAndGrant("mydb", "bad user", "secret")


def testCreateMysqlUserAndGrantRaisesOnMysqlError(monkeypatch):
    def fakeRunCommand(command, timeout=30, checkReturnCode=True, useSudo=False):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="ERROR: access denied"
        )

    monkeypatch.setattr(database_tools, "runCommand", fakeRunCommand)

    with pytest.raises(ToolExecutionException, match="创建用户或授权失败"):
        database_tools.createMysqlUserAndGrant("mydb", "appuser", "secret")
