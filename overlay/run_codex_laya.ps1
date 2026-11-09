param(
    [string]$Scenario = '琥珀残帆_2025湖南省赛想定（人机版）',
    [string]$Side = '红方',
    [string[]]$Sides = @(),
    [int]$MaxWallMinutes = 45,
    [string]$CodexModel = '',
    [int]$CodexInterval = 600,
    [string]$MissionBrief = '',
    [string[]]$SideBrief = @(),
    [string]$KnowledgeRoot = '',
    [switch]$DisableReflection,
    [switch]$Interactive,
    [switch]$CompactOutput
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$layaRoot = Join-Path $root 'laya'
$layaPython = Join-Path $layaRoot '.venv\Scripts\python.exe'
$model = Join-Path $layaRoot 'models\laya-multilingual'
$enginePython = Join-Path $root 'LingYi\intelligent\new_env\ray210\python.exe'
$runner = Join-Path $root 'LingYi\intelligent\ai\headless_scenario_run.py'
$logRoot = Join-Path $root 'LingYi\intelligent\ai\headless_runs'

function Format-SimTime([double]$Seconds) {
    $span = [TimeSpan]::FromSeconds([Math]::Max(0, $Seconds))
    return ('{0:00}:{1:00}:{2:00}' -f [Math]::Floor($span.TotalHours), $span.Minutes, $span.Seconds)
}

function Format-Score($Value) {
    if ($null -eq $Value) { return '未知' }
    return ('{0:+0.##;-0.##;0}' -f [double]$Value)
}

function Show-ScoreReport($Report) {
    $label = if ($Report.final) { '终局得分' } else { '阶段得分（非终局结算）' }
    Write-Host "`n===== 本局$label ====="
    Write-Host "想定：$($Report.scenario)；推演时间 $(Format-SimTime $Report.simulation_seconds)"
    foreach ($sideScore in $Report.sides) {
        Write-Host ("`n[{0}] 总分：{1}" -f $sideScore.side, (Format-Score $sideScore.total))
        Write-Host ("初始分：{0}；加扣分明细合计：{1}" -f
                    (Format-Score $sideScore.initial), (Format-Score $sideScore.detail_total))
        foreach ($source in $sideScore.sources) {
            Write-Host ("  {0}：{1}（{2} 次）" -f $source.reason,
                        (Format-Score $source.points), $source.count)
        }
        if (@($sideScore.sources).Count -eq 0) { Write-Host '  未读取到得分来源明细。' }
        if ($null -eq $sideScore.unattributed -or $sideScore.unattributed -ne 0) {
            Write-Host "  未归因差额：$(Format-Score $sideScore.unattributed)；来源未确认。"
        } else {
            Write-Host '  明细与总分核对一致。'
        }
    }
    foreach ($note in $Report.notes) { Write-Host "计分说明：$note" }
    Write-Host "`n逐次加扣分明细：$($Report.markdown_path)"
    Write-Host "结构化比分：$($Report.json_path)"
}

function Show-RunEvent($item) {
    $sideLabel = if ($item.side) { "[$($item.side)] " } else { '' }
    switch ($item.event) {
        'start' {
            Write-Host "想定：$($item.scenario)；阵营：$($item.sides -join '、')"
            Write-Host "运行目录：$($item.run_dir)"
        }
        'reset_result' { Write-Host '推演已开始。' }
        'codex_workspace_ready' { Write-Host "${sideLabel}共享 workspace：$($item.workspace)" }
        'codex_request' {
            if ($item.attempt -eq 1) {
                Write-Host ("{0}第 {1} 轮规划中；推演时间 {2}" -f $sideLabel, $item.round,
                            (Format-SimTime $item.simulation_seconds))
            }
        }
        'codex_plan_ready' {
            Write-Host ("{0}时间 {1}；计划 {2}；任务 {3}；objective：{4}" -f $sideLabel,
                        (Format-SimTime $item.simulation_seconds), $item.plan.plan_id,
                        $item.plan.task.family, $item.plan.objective)
        }
        'codex_plan_failed' { Write-Host "${sideLabel}规划失败：$($item.reason)" }
        'codex_reflection_request' { Write-Host "${sideLabel}正在生成本局反思……" }
        'codex_reflection_saved' { Write-Host "${sideLabel}反思已保存：$($item.path)" }
        'codex_reflection_failed' { Write-Host "${sideLabel}反思生成失败：$($item.reason)" }
        'laya_count_choice' { Write-Host "${sideLabel}Laya 投入量：$($item.candidate)" }
        'laya_choice' { Write-Host "${sideLabel}Laya 选择：$($item.candidate)" }
        'laya_hold' { Write-Host "${sideLabel}本轮未下令：$($item.reason)" }
        'command_applied' {
            Write-Host "${sideLabel}已下达：$($item.mission)；单位 $($item.unit_ids.Count) 个"
        }
        'command_observed' {
            $status = if ($item.matched) { '已确认' } else { '未确认' }
            Write-Host "${sideLabel}任务归属：$status"
        }
        'launch_observed' {
            Write-Host "${sideLabel}起飞观察：$($item.airborne)/$($item.requested)；$($item.status)"
        }
        'unit_loss' {
            $roundLabel = if ($item.round) { "第 $($item.round) 轮；" } else { '' }
            $classLabel = if ($item.unit_class) { "（$($item.unit_class)）" } else { '' }
            $statusLabel = if ($item.status -eq 'confirmed') { '确认摧毁' } else { '疑似损失（离开本方态势）' }
            Write-Host ("{0}{1}：{2}{3}；{4}观察时间 {5}" -f $sideLabel, $statusLabel,
                        $item.unit_name, $classLabel, $roundLabel,
                        (Format-SimTime $item.simulation_seconds)) -ForegroundColor Yellow
        }
        'unit_loss_confirmed' {
            Write-Host ("{0}确认摧毁：{1}；观察时间 {2}" -f $sideLabel, $item.unit_name,
                        (Format-SimTime $item.simulation_seconds)) -ForegroundColor Yellow
        }
        'unit_loss_retracted' {
            Write-Host ("{0}单位重新出现，撤回疑似损失：{1}" -f $sideLabel, $item.unit_name)
        }
        'step_completed' {
            Write-Host ("引擎推进 {0} 步；墙钟 {1:n0} 秒" -f $item.step_calls, $item.wall_seconds)
        }
        'scenario_complete' {
            Write-Host ("想定自然结束；引擎推进 {0} 步；墙钟 {1:n0} 秒" -f $item.step_calls, $item.wall_seconds)
        }
        'wall_timeout' { Write-Host "达到墙钟上限 $($item.minutes) 分钟；推演已停止。" }
        'engine_stopped' { Write-Host 'Plat 已关闭。' }
        'error' { Write-Host "推演错误：$($item.type)：$($item.message)" }
    }
}

if ($Interactive) {
    if (-not $PSBoundParameters.ContainsKey('MaxWallMinutes')) { $MaxWallMinutes = 0 }
    $scenarioDir = Join-Path $root 'LingYi\Data\Scenarios'
    $scenarios = @(Get-ChildItem -LiteralPath $scenarioDir -Filter '*.scen' -File | Sort-Object BaseName)
    if ($scenarios.Count -eq 0) { throw "No .scen files found in $scenarioDir" }
    Write-Host '可选想定：'
    for ($index = 0; $index -lt $scenarios.Count; $index++) {
        Write-Host ('  {0}. {1}' -f ($index + 1), $scenarios[$index].BaseName)
    }
    $choice = Read-Host "想定编号或名称 [默认：$Scenario]"
    if ($choice) {
        $number = 0
        if ([int]::TryParse($choice, [ref]$number) -and $number -ge 1 -and $number -le $scenarios.Count) {
            $Scenario = $scenarios[$number - 1].BaseName
        } elseif ($scenarios.BaseName -contains $choice) {
            $Scenario = $choice
        } else {
            throw "无效的想定选择：$choice"
        }
    }
    $defaultSide = if ($Sides.Count -gt 1) { '3' } elseif ($Side -eq '蓝方') { '2' } else { '1' }
    $sideChoice = Read-Host "控制阵营：1 红方 / 2 蓝方 / 3 双方 [默认：$defaultSide]"
    if (-not $sideChoice) { $sideChoice = $defaultSide }
    switch ($sideChoice) {
        { $_ -in @('1', '红方') } { $Side = '红方'; $Sides = @(); break }
        { $_ -in @('2', '蓝方') } { $Side = '蓝方'; $Sides = @(); break }
        { $_ -in @('3', '双方') } { $Sides = @('红方', '蓝方'); break }
        default { throw "无效的阵营选择：$sideChoice" }
    }
    $wallChoice = Read-Host "最长墙钟分钟数，0 表示等到想定结束 [默认：$MaxWallMinutes]"
    if ($wallChoice) {
        $parsed = 0
        if (-not [int]::TryParse($wallChoice, [ref]$parsed) -or $parsed -lt 0) {
            throw "无效的墙钟上限：$wallChoice"
        }
        $MaxWallMinutes = $parsed
    }
    $intervalChoice = Read-Host "Codex 规划间隔，推演秒 [默认：$CodexInterval]"
    if ($intervalChoice) {
        $parsed = 0
        if (-not [int]::TryParse($intervalChoice, [ref]$parsed) -or $parsed -lt 1) {
            throw "无效的规划间隔：$intervalChoice"
        }
        $CodexInterval = $parsed
    }
    $modelChoice = Read-Host "Codex 模型 [回车使用当前默认：$CodexModel]"
    if ($modelChoice) { $CodexModel = $modelChoice }
    $briefChoice = Read-Host '共同简报文件路径 [回车使用想定描述]'
    if ($briefChoice) { $MissionBrief = $briefChoice }
    if ($Sides.Count -gt 1 -and $SideBrief.Count -eq 0) {
        foreach ($sideName in $Sides) {
            $path = Read-Host "$sideName 专属简报文件路径 [回车使用共同简报]"
            if ($path) { $SideBrief += "${sideName}=$path" }
        }
    }
}

if ($MaxWallMinutes -lt 0 -or $CodexInterval -lt 1) {
    throw 'MaxWallMinutes must be nonnegative and CodexInterval must be positive.'
}
$scenarioPath = Join-Path (Join-Path $root 'LingYi\Data\Scenarios') ($Scenario + '.scen')
if (-not (Test-Path -LiteralPath $scenarioPath -PathType Leaf)) { throw "Scenario missing: $scenarioPath" }
if ($MissionBrief -and -not (Test-Path -LiteralPath $MissionBrief -PathType Leaf)) {
    throw "Mission brief missing: $MissionBrief"
}
foreach ($item in $SideBrief) {
    $separatorAt = $item.IndexOf('=')
    $briefSide = if ($separatorAt -gt 0) { $item.Substring(0, $separatorAt) } else { '' }
    $briefPath = if ($separatorAt -gt 0) { $item.Substring($separatorAt + 1) } else { '' }
    $controlledSides = if ($Sides.Count -gt 0) { $Sides } else { @($Side) }
    if ($briefSide -notin $controlledSides -or -not $briefPath -or
        -not (Test-Path -LiteralPath $briefPath -PathType Leaf)) {
        throw "Invalid side brief: $item"
    }
}
$showCompact = $Interactive.IsPresent -or $CompactOutput.IsPresent

foreach ($file in @($layaPython, (Join-Path $model 'model.safetensors'), $enginePython, $runner)) {
    if (-not (Test-Path -LiteralPath $file)) { throw "Required file missing: $file" }
}
if (Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 18080 is already in use. Close the existing service before this run.'
}

$env:LAYA_HOST = '127.0.0.1'
$env:LAYA_PORT = '18080'
$env:LAYA_DEVICE = 'cuda'
$env:LAYA_PRELOAD = '1'
$env:LAYA_MODELS = 'multilingual'
$env:LAYA_MAX_LOADED = '1'
$env:LAYA_DEFAULT_MODEL = 'multilingual'
$env:LAYA_EXTRA_MODELS = @{ multilingual = $model } | ConvertTo-Json -Compress
$env:HF_HUB_OFFLINE = '1'
$env:HF_HUB_CACHE = Join-Path $layaRoot '.hf-cache'

New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$stdout = Join-Path $logRoot "laya_$stamp.stdout.log"
$stderr = Join-Path $logRoot "laya_$stamp.stderr.log"
$consoleLog = Join-Path $logRoot "console_$stamp.log"
$laya = $null
$ownedPlatPid = $null
$engineExe = $null
$consoleWriter = $null
$runExit = 1
$scoreReport = $null
$scoreUnavailable = $null
try {
    if ($showCompact) { Write-Host '正在启动 Laya，首次加载模型可能需要一段时间……' }
    $laya = Start-Process -FilePath $layaPython -ArgumentList @('-m', 'laya.serve') `
        -WorkingDirectory $layaRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $ready = $false
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        if ($laya.HasExited) { throw "Laya exited during startup; inspect $stderr" }
        try {
            $client = [System.Net.Sockets.TcpClient]::new()
            $client.Connect('127.0.0.1', 18080)
            $client.Dispose()
            $ready = $true
            break
        } catch {
            if ($client) { $client.Dispose() }
            if ($showCompact -and $attempt -gt 0 -and $attempt % 10 -eq 0) {
                Write-Host "Laya 加载中：$attempt 秒"
            }
            Start-Sleep -Seconds 1
        }
    }
    if (-not $ready) { throw "Laya did not start within 90 seconds; inspect $stderr" }
    if ($showCompact) { Write-Host 'Laya 已就绪，正在启动推演引擎……' }
    $runArgs = @($runner, '--scenario', $Scenario,
                 '--max-wall-minutes', [string]$MaxWallMinutes, '--codex-plan',
                 '--codex-interval', [string]$CodexInterval,
                 '--laya-url', 'http://127.0.0.1:18080')
    if ($Sides.Count -gt 0) {
        $runArgs += '--sides'
        $runArgs += $Sides
    } else {
        $runArgs += @('--side', $Side)
    }
    if ($CodexModel) { $runArgs += @('--codex-model', $CodexModel) }
    if ($MissionBrief) { $runArgs += @('--mission-brief', $MissionBrief) }
    if ($KnowledgeRoot) { $runArgs += @('--knowledge-root', $KnowledgeRoot) }
    if ($DisableReflection) { $runArgs += '--no-reflection' }
    foreach ($item in $SideBrief) { $runArgs += @('--side-brief', $item) }
    $consoleWriter = [System.IO.StreamWriter]::new(
        $consoleLog, $false, [System.Text.UTF8Encoding]::new($false))
    $consoleWriter.AutoFlush = $true
    & $enginePython @runArgs 2>&1 | ForEach-Object {
        $line = [string]$_
        $consoleWriter.WriteLine($line)
        if ($line -match 'game_process_id-(\d+)') {
            $ownedPlatPid = [int]$Matches[1]
        }
        if ($line.StartsWith('{')) {
            try {
                $eventItem = $line | ConvertFrom-Json -ErrorAction Stop
                if ($eventItem.event -eq 'configuration_ready') { $engineExe = $eventItem.engine }
                if ($eventItem.event -eq 'score_summary') {
                    $scoreReport = $eventItem.report
                    return
                }
                if ($eventItem.event -eq 'score_unavailable') {
                    $scoreUnavailable = $eventItem.reason
                    return
                }
                if ($showCompact -or $eventItem.event -in @('unit_loss', 'unit_loss_confirmed', 'unit_loss_retracted')) {
                    Show-RunEvent $eventItem
                } else {
                    Write-Host $line
                }
            } catch {
                if (-not $showCompact) { Write-Host $line }
            }
        } elseif (-not $showCompact) {
            Write-Host $line
        }
    }
    $runExit = $LASTEXITCODE
} finally {
    if ($consoleWriter) { $consoleWriter.Dispose() }
    if ($laya -and -not $laya.HasExited) {
        Stop-Process -Id $laya.Id -Force -ErrorAction SilentlyContinue
        Write-Host "Laya 已关闭（PID $($laya.Id)）。"
    }
    if ($ownedPlatPid -and $engineExe) {
        try {
            $plat = Get-Process -Id $ownedPlatPid -ErrorAction SilentlyContinue
            if ($plat -and $plat.Path -and
                [IO.Path]::GetFullPath($plat.Path) -ieq [IO.Path]::GetFullPath($engineExe)) {
                Stop-Process -Id $plat.Id -Force -ErrorAction SilentlyContinue
                Write-Host "Plat 已关闭（PID $($plat.Id)）。"
            }
        } catch {
            Write-Warning "无法完成 Plat 补充清理：$($_.Exception.Message)"
        }
    }
    if ($scoreReport) {
        Show-ScoreReport $scoreReport
    } elseif ($scoreUnavailable) {
        Write-Host "本局得分暂不可用：$scoreUnavailable"
    }
    if ($showCompact) { Write-Host "完整控制台日志：$consoleLog" }
}
exit $runExit
