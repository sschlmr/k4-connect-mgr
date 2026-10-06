<#
    K4 Connect Manager - server watchdog.

    Run every few minutes by Task Scheduler. Probes the server's /health
    endpoint and relaunches it if it is not answering. The certificate is
    self-signed, so validation is skipped for this local loopback probe only.
#>

param(
    [string]$InstallDir = "$env:ProgramFiles\K4 Connect Manager",
    [string]$DataDir    = "$env:ProgramData\k4cm",
    [int]   $TimeoutSec = 10
)

$ErrorActionPreference = 'Stop'
$logFile = Join-Path $DataDir 'logs\watchdog.log'

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    $logDir = Split-Path $logFile -Parent
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    Add-Content -Path $logFile -Value $line
}

function Get-ServerPort {
    $configPath = Join-Path $DataDir 'server.toml'
    if (Test-Path $configPath) {
        $match = Select-String -Path $configPath -Pattern '^\s*port\s*=\s*(\d+)' | Select-Object -First 1
        if ($match) { return [int]$match.Matches[0].Groups[1].Value }
    }
    return 8443
}

function Test-ServerHealthy([int]$Port) {
    $url = "https://127.0.0.1:$Port/health"
    try {
        if ($PSVersionTable.PSVersion.Major -ge 6) {
            $response = Invoke-WebRequest -Uri $url -TimeoutSec $TimeoutSec -SkipCertificateCheck -UseBasicParsing
        } else {
            Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class K4cmCertPolicy : ICertificatePolicy {
    public bool CheckValidationResult(ServicePoint sp, X509Certificate cert, WebRequest req, int problem) { return true; }
}
"@ -ErrorAction SilentlyContinue
            [System.Net.ServicePointManager]::CertificatePolicy = New-Object K4cmCertPolicy
            [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
            $response = Invoke-WebRequest -Uri $url -TimeoutSec $TimeoutSec -UseBasicParsing
        }
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Start-Server {
    $exe = Join-Path $InstallDir 'k4cm-server.exe'
    if (-not (Test-Path $exe)) {
        Write-Log "ERROR: server executable not found at $exe"
        return
    }
    Start-Process -FilePath $exe -ArgumentList 'run' -WindowStyle Hidden
    Write-Log "started $exe run"
}

$port = Get-ServerPort
if (Test-ServerHealthy -Port $port) {
    exit 0
}

Write-Log "health check on port $port failed - restarting the server"
Get-Process -Name 'k4cm-server' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-Server
