$ErrorActionPreference = "Stop"

$BridgeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $BridgeRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$CodexDir = Join-Path $HOME ".codex"
$ConfigToml = Join-Path $CodexDir "config.toml"
$HooksJson = Join-Path $CodexDir "hooks.json"
$SocketEndpoint = if ($env:CODEX_BUDDY_SOCKET) { $env:CODEX_BUDDY_SOCKET } else { "tcp://127.0.0.1:8876" }

function Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Ensure-CodexHooksEnabled {
    New-Item -ItemType Directory -Force -Path $CodexDir | Out-Null
    if (-not (Test-Path $ConfigToml)) {
        Set-Content -Path $ConfigToml -Value "[features]`r`ncodex_hooks = true`r`n" -Encoding UTF8
        return
    }

    $content = Get-Content -Path $ConfigToml -Raw -Encoding UTF8
    if ($content -match "(?m)^\s*codex_hooks\s*=") {
        return
    }
    if ($content -match "(?m)^\[features\]\s*$") {
        $updated = [regex]::Replace($content, "(?m)^\[features\]\s*$", "[features]`r`ncodex_hooks = true", 1)
        Set-Content -Path $ConfigToml -Value $updated -Encoding UTF8
        return
    }

    $trimmed = $content.TrimEnd()
    $updated = if ($trimmed.Length -gt 0) {
        "$trimmed`r`n`r`n[features]`r`ncodex_hooks = true`r`n"
    } else {
        "[features]`r`ncodex_hooks = true`r`n"
    }
    Set-Content -Path $ConfigToml -Value $updated -Encoding UTF8
}

function New-HookCommand([string]$ScriptName) {
    $scriptPath = Join-Path $BridgeRoot "hooks\$ScriptName"
    return "powershell -NoProfile -ExecutionPolicy Bypass -Command `"$env:CODEX_BUDDY_SOCKET='$SocketEndpoint'; & '$VenvPython' '$scriptPath'`""
}

Step "Bridge root: $BridgeRoot"

Step "Creating Python venv and installing dependencies"
if (-not (Test-Path $VenvPython)) {
    python -m venv $VenvDir
}
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet -r (Join-Path $BridgeRoot "requirements.txt")

Step "Enabling codex_hooks in $ConfigToml"
Ensure-CodexHooksEnabled

Step "Writing $HooksJson"
if (Test-Path $HooksJson) {
    $backupPath = "$HooksJson.bak.$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
    Copy-Item -Path $HooksJson -Destination $backupPath -Force
    Write-Host "[!] Existing hooks.json backed up to $backupPath" -ForegroundColor Yellow
}

$hooks = @{
    hooks = @{
        PermissionRequest = @(
            @{
                matcher = ".*"
                hooks = @(
                    @{
                        type = "command"
                        command = (New-HookCommand "permission_request.py")
                        timeout = 115
                        statusMessage = "ClaudeCodeBuddy approval"
                    }
                )
            }
        )
        SessionStart = @(
            @{
                matcher = ".*"
                hooks = @(
                    @{
                        type = "command"
                        command = (New-HookCommand "session_start.py")
                        timeout = 3
                    }
                )
            }
        )
        UserPromptSubmit = @(
            @{
                matcher = ".*"
                hooks = @(
                    @{
                        type = "command"
                        command = (New-HookCommand "user_prompt_submit.py")
                        timeout = 3
                    }
                )
            }
        )
        Stop = @(
            @{
                matcher = ".*"
                hooks = @(
                    @{
                        type = "command"
                        command = (New-HookCommand "stop.py")
                        timeout = 3
                    }
                )
            }
        )
        InteractiveStart = @(
            @{
                matcher = ".*"
                hooks = @(
                    @{
                        type = "command"
                        command = (New-HookCommand "interactive_start.py")
                        timeout = 3
                    }
                )
            }
        )
        InteractiveEnd = @(
            @{
                matcher = ".*"
                hooks = @(
                    @{
                        type = "command"
                        command = (New-HookCommand "interactive_end.py")
                        timeout = 3
                    }
                )
            }
        )
    }
}
$hooks | ConvertTo-Json -Depth 8 | Set-Content -Path $HooksJson -Encoding UTF8

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "1. Restart Codex Desktop and any open Codex CLI sessions."
Write-Host "2. Start the daemon in a terminal before using the bridge:"
Write-Host "   `"$VenvPython`" -m codex_buddy_bridge --socket $SocketEndpoint --debug"
Write-Host "3. Keep that terminal open while using the bridge."
