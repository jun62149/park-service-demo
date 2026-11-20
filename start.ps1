param([switch]$Install, [switch]$LoadLocalEnv, [switch]$FeishuOnce,
      [ValidateSet('assigned','completed')][string]$FeishuEvent='assigned', [int]$TicketId=0, [int]$Port=5000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($Install) {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) { python -m venv .venv; if ($LASTEXITCODE -ne 0) { throw '虚拟环境创建失败' } }
    & '.venv/Scripts/python.exe' -m pip install --index-url https://pypi.org/simple -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
}
if ($LoadLocalEnv) {
    $allowedNames=@('AI_PROVIDER','DEEPSEEK_API_KEY','DEEPSEEK_BASE_URL','DEEPSEEK_MODEL','OLLAMA_BASE_URL','OLLAMA_MODEL','AI_TIMEOUT_SECONDS','DEMO_MODE','FEISHU_ENABLED','FEISHU_APP_ID','FEISHU_APP_SECRET','FEISHU_TIMEOUT_SECONDS','FEISHU_BINDINGS_FILE')
    foreach ($envPath in @('.env','.env.feishu')) {
      if (-not (Test-Path -LiteralPath $envPath)) { continue }
      foreach ($configLine in (Get-Content -LiteralPath $envPath -Encoding UTF8)) {
        if ($configLine -match '^\s*([A-Z_]+)\s*=(.*)$' -and $allowedNames -contains $Matches[1]) {
            [Environment]::SetEnvironmentVariable($Matches[1],$Matches[2].Trim().Trim('"').Trim("'"),'Process')
        }
      }
    }
}
$env:PARK_PORT="$Port"
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    throw '请先按 README 创建虚拟环境并安装 requirements.txt'
}
if ($FeishuOnce) {
    # Explicit single-send mode enables only this backend process; local files remain unchanged.
    $env:FEISHU_ENABLED='true'
    & '.venv/Scripts/python.exe' 'scripts/feishu_once.py' '--send-one' '--event' $FeishuEvent '--ticket-id' "$TicketId"
    exit $LASTEXITCODE
} else {
    & '.venv/Scripts/python.exe' 'run.py'
}
