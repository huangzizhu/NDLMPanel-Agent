from pydantic import BaseModel
from enum import Enum  

class NginxInstallInfo(BaseModel):
    isInstalled: bool
    version: str | None = None
    configPath: str | None = None


class NginxStatus(BaseModel):
    isRunning: bool
    workerProcessCount: int
    activeConnections: int | None = None
    requestsPerSecond: float | None = None

class NginxSiteMode(str,Enum):
    STATIC = "static"
    REVERSE_PROXY = "reverse_proxy"

class NginxSiteCreateResult(BaseModel):
    domain: str
    mode: NginxSiteMode
    listenPort: int
    configPath: str
    enabledPath: str | None = None
    rootPath: str | None = None
    proxyPass: str | None = None
    isEnabled: bool 
    isReloaded: bool 

# class NginxSiteProxyResult(BaseModel):
#     domain: str
#     mode: NginxSiteMode
#     listenPort: int
#     configPath: str
#     enabledPath: str | None = None
#     rootPath: str 
#     proxyPass: str 
#     isEnabled: bool 
#     isReloaded: bool 

