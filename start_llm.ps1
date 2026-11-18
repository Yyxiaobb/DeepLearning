$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$llamaDir = Join-Path $projectRoot 'models\llama.cpp\runtime'
$serverExe = Join-Path $llamaDir 'llama-server.exe'
$modelPath = Join-Path $projectRoot 'models\llm\Qwen3.5-4B-Q4_K_M.gguf'
$logPath = Join-Path $projectRoot 'data\llm-server.log'
$errorPath = Join-Path $projectRoot 'data\llm-server-error.log'
$pidPath = Join-Path $projectRoot 'data\llm-server.pid'
$healthUrl = 'http://127.0.0.1:8081/health'
$modelAlias = 'Qwen3.5-4B-Q4_K_M'

if (-not (Test-Path -LiteralPath $serverExe)) { throw "找不到本地推理程序：$serverExe" }
if (-not (Test-Path -LiteralPath $modelPath)) { throw "找不到模型文件：$modelPath" }
New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'data') | Out-Null

try {
    $health = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
    if ($health.StatusCode -eq 200) {
        Write-Output '本地语言模型服务已经运行：http://127.0.0.1:8081'
        exit 0
    }
} catch { }

$arguments = @(
    '-m', ('"{0}"' -f $modelPath),
    '--host', '127.0.0.1',
    '--port', '8081',
    '--alias', $modelAlias,
    '--ctx-size', '8192',
    '--parallel', '1',
    '--n-gpu-layers', '99',
    '--jinja',
    '--reasoning', 'off'
) -join ' '

$process = Start-Process -FilePath $serverExe -ArgumentList $arguments -WorkingDirectory $llamaDir -WindowStyle Hidden -PassThru -RedirectStandardOutput $logPath -RedirectStandardError $errorPath
Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ascii

$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    if ($process.HasExited) {
        $details = if (Test-Path -LiteralPath $errorPath) { Get-Content -LiteralPath $errorPath -Tail 30 } else { @() }
        throw "本地语言模型服务启动失败。$([Environment]::NewLine)$($details -join [Environment]::NewLine)"
    }
    try {
        $health = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        if ($health.StatusCode -eq 200) {
            $warmupBody = @{
                model = $modelAlias
                messages = @(@{ role = 'user'; content = '只回复 OK。' })
                temperature = 0
                max_tokens = 2
            } | ConvertTo-Json -Depth 5 -Compress
            Write-Output '模型已加载，正在预热首次推理…'
            try {
                Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8081/v1/chat/completions' -ContentType 'application/json' -Body $warmupBody -TimeoutSec 90 | Out-Null
                Write-Output "本地语言模型服务已启动并预热（PID $($process.Id)）：http://127.0.0.1:8081"
            } catch {
                Write-Warning "模型服务已运行，但预热请求未完成：$($_.Exception.Message)"
                Write-Output "本地语言模型服务已启动（PID $($process.Id)）：http://127.0.0.1:8081"
            }
            exit 0
        }
    } catch { }
    Start-Sleep -Milliseconds 500
}
throw "等待本地模型加载超时；请查看 $errorPath"
