param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [string]$HookScript,

    [Parameter(Mandatory = $true)]
    [string]$HookName,

    [Parameter(Mandatory = $true)]
    [string]$LogPath,

    [string]$SocketEndpoint = "tcp://127.0.0.1:8876"
)

$ErrorActionPreference = "Continue"
$env:CODEX_BUDDY_SOCKET = $SocketEndpoint

try {
    $payload = [Console]::In.ReadToEnd()
} catch {
    $payload = ""
}

try {
    $logDir = Split-Path -Parent $LogPath
    if ($logDir) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }
    $timestamp = (Get-Date).ToString("o")
    $socket = if ($env:CODEX_BUDDY_SOCKET) { $env:CODEX_BUDDY_SOCKET } else { "" }
    $entry = @(
        "-----"
        "ts=$timestamp"
        "hook=$HookName"
        "socket=$socket"
        "script=$HookScript"
        "payload=$payload"
    ) -join "`r`n"
    Add-Content -Path $LogPath -Value ($entry + "`r`n") -Encoding UTF8
} catch {
}

$payload | & $PythonExe $HookScript
$exitCode = $LASTEXITCODE

try {
    Add-Content -Path $LogPath -Value ("exit=$exitCode`r`n") -Encoding UTF8
} catch {
}

exit $exitCode
