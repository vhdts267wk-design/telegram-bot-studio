param(
    [string]$TerminalPath = 'C:\Program Files\MetaTrader 5\terminal64.exe',
    [string]$Symbol,
    [string]$PythonPath,
    [string]$FeedUrl = 'https://telegram-bot-studio-i5nw-production.up.railway.app/api/market/feed',
    [string]$StateDirectory,
    [switch]$ReplaceBridgeKey
)

$ErrorActionPreference = 'Stop'
$bridgeExitCode = 1
$previousBridgeUrl = [Environment]::GetEnvironmentVariable('MARKET_BRIDGE_URL', 'Process')
$previousBridgeKey = [Environment]::GetEnvironmentVariable('MARKET_BRIDGE_KEY', 'Process')
$keyPointer = [IntPtr]::Zero
try {
    $TerminalPath = (Resolve-Path -LiteralPath $TerminalPath).Path
    $selectedTerminal = Get-Process -Name terminal64 -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $TerminalPath }
    if (-not $selectedTerminal) { throw 'Open and connect the selected MT5 DEMO terminal before starting this launcher.' }
    if (-not $PythonPath) { $PythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
    $PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
    if (-not $Symbol) { $Symbol = Read-Host 'Exact XAU/USD symbol shown in your broker Market Watch (including suffix)' }
    if (-not $Symbol -or $Symbol.Trim() -ne $Symbol -or $Symbol.Length -gt 64) { throw 'An exact broker symbol is required.' }
    if (-not $StateDirectory) {
        $workspaceWork = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
        $StateDirectory = Join-Path $workspaceWork 'mt5-private'
    }
    $StateDirectory = [IO.Path]::GetFullPath($StateDirectory)
    New-Item -ItemType Directory -Path $StateDirectory -Force | Out-Null
    # Current-user-only ACL and Windows DPAPI keep the ingest key out of source,
    # command arguments, transcripts, and ordinary plaintext configuration.
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $privateAcl = Get-Acl -LiteralPath $StateDirectory
    $privateAcl.SetAccessRuleProtection($true, $false)
    foreach ($existingRule in @($privateAcl.Access)) {
        $privateAcl.RemoveAccessRuleSpecific($existingRule)
    }
    $privateAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $currentSid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
    )))
    Set-Acl -LiteralPath $StateDirectory -AclObject $privateAcl
    $keyFile = Join-Path $StateDirectory 'bridge-key.dpapi'
    if ($ReplaceBridgeKey -or -not (Test-Path -LiteralPath $keyFile)) {
        $secureBridgeKey = Read-Host 'Private Railway market bridge key (hidden input; never send it in Telegram)' -AsSecureString
        ConvertFrom-SecureString -SecureString $secureBridgeKey | Set-Content -LiteralPath $keyFile -Encoding ASCII
    } else {
        $secureBridgeKey = Get-Content -LiteralPath $keyFile -Raw | ConvertTo-SecureString
    }
    Write-Host 'DEMO only. Fixed order size: 0.01 lots. A Telegram Accept may open a broker market order with the displayed stop and target.'
    Write-Host 'The helper never changes your account or enables MT5 trading settings. Keep this window open; Ctrl+C stops it.'
    $localConsent = Read-Host 'Type ENABLE DEMO ORDERS to start'
    if ($localConsent -cne 'ENABLE DEMO ORDERS') { throw 'Local order execution was not enabled.' }
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureBridgeKey)
    [Environment]::SetEnvironmentVariable('MARKET_BRIDGE_KEY', [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer), 'Process')
    [Environment]::SetEnvironmentVariable('MARKET_BRIDGE_URL', $FeedUrl, 'Process')
    & $PythonPath (Join-Path $PSScriptRoot 'mt5_trade_bridge.py') --terminal $TerminalPath --symbol $Symbol --account-mode demo --volume 0.01 --state-directory $StateDirectory --enable-orders
    $bridgeExitCode = $LASTEXITCODE
} catch {
    # Do not echo exception text: filesystem/provider errors can contain secrets.
    Write-Host 'Bridge setup stopped. Check the selected running demo terminal, Python path, symbol, private key, and local confirmation.'
} finally {
    [Environment]::SetEnvironmentVariable('MARKET_BRIDGE_KEY', $previousBridgeKey, 'Process')
    [Environment]::SetEnvironmentVariable('MARKET_BRIDGE_URL', $previousBridgeUrl, 'Process')
    if ($keyPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
    if ($secureBridgeKey) { $secureBridgeKey.Dispose() }
}
exit $bridgeExitCode
