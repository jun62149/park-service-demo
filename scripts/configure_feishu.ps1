# Run interactively on the local machine. Values never enter command history/output.
param([switch]$BindingsOnly)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
function Read-PrivateValue([string]$Prompt) {
    $secure=Read-Host $Prompt -AsSecureString
    $pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $value=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match '[\r\n"'']') { throw '输入为空或含不允许字符，请重新配置' }
    return $value.Trim()
}
New-Item -ItemType Directory -Path instance -Force | Out-Null
if (-not $BindingsOnly) {
    $appId=Read-PrivateValue 'App ID（隐藏输入）'
    $appSecret=Read-PrivateValue 'App Secret（隐藏输入，勿发聊天或截图）'
}
$engineer=Read-PrivateValue '当前应用下测试工程甲 open_id（隐藏输入）'
if ($engineer -notmatch '^ou_[A-Za-z0-9_-]{8,100}$') { throw '工程 open_id 格式不符' }
$reviewer=Read-Host '是否同时绑定企服测试账号？输入 yes，其他则跳过'
$bindings=@{'3'=@{open_id=$engineer;enabled=$true}}
if ($reviewer -eq 'yes') {
    $serviceId=Read-PrivateValue '当前应用下测试企服 open_id（隐藏输入）'
    if ($serviceId -notmatch '^ou_[A-Za-z0-9_-]{8,100}$') { throw '企服 open_id 格式不符' }
    $bindings['2']=@{open_id=$serviceId;enabled=$true}
}
# .env.feishu and instance/ are covered by .gitignore; do not print their contents.
if (-not $BindingsOnly) {
    $localConfig=@('FEISHU_ENABLED=true',"FEISHU_APP_ID=$appId","FEISHU_APP_SECRET=$appSecret",'FEISHU_TIMEOUT_SECONDS=5','FEISHU_BINDINGS_FILE=instance/feishu_bindings.json')
    [IO.File]::WriteAllLines((Join-Path (Get-Location) '.env.feishu'),$localConfig,[Text.UTF8Encoding]::new($false))
}
[IO.File]::WriteAllText((Join-Path (Get-Location) 'instance/feishu_bindings.json'),($bindings|ConvertTo-Json -Depth 3),[Text.UTF8Encoding]::new($false))
Remove-Variable appId,appSecret,engineer,serviceId,bindings,localConfig -ErrorAction SilentlyContinue
Write-Output '本机飞书配置已保存到受忽略文件，未发送任何消息。真实单次发送需另行明确授权；不要分享配置文件。'
