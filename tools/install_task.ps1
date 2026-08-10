# ---------------------------------------------------------------
# install_task.ps1 — 작업 스케줄러에 등록/해제
#
#   등록:  powershell -ExecutionPolicy Bypass -File tools\install_task.ps1
#   확인:  powershell -ExecutionPolicy Bypass -File tools\install_task.ps1 -Status
#   해제:  powershell -ExecutionPolicy Bypass -File tools\install_task.ps1 -Remove
#
# ⚠️ 왜 "사용자가 로그온한 경우에만 실행" 인가
#
#   pywinauto 가 실제 카톡 창에 키를 보내고, playwright 는 headless=False 로
#   눈에 보이는 브라우저를 띄운다(CLAUDE.md 제약 5). 둘 다 대화형 데스크톱이
#   있어야 동작한다.
#
#   "로그온 여부에 관계없이 실행" 으로 등록하면 화면이 없는 세션에서 돌면서
#   **조용히 계속 실패한다.** 무인 운영에서 최악의 형태다.
#   그래서 -LogonType Interactive 를 쓴다. 바꾸지 말 것.
#
#   같은 이유로 PC 를 로그인 상태로 켜 두어야 하고, 절전에 들어가면 안 된다.
#   절전은 이 스크립트가 못 막는다. 설정에서 직접 꺼야 한다.
#     설정 > 시스템 > 전원 및 배터리 > 화면 및 절전 → 전부 '안 함'
# ---------------------------------------------------------------

param(
    [switch]$Remove,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

# 작업 두 개를 등록한다. 한쪽이 죽어도 다른 쪽은 계속 돈다.
#   파이프라인 : 카톡 -> 링크 -> 텔레그램 전달 (주기 실행)
#   리스너     : 사장님이 봇에 보낸 것을 문구로 만들어 회신 (상시 대기)
$Root  = Split-Path -Parent $PSScriptRoot
$TASKS = @(
    @{ Name = "PartnersMoneyBot"
       Cmd  = Join-Path $Root "tools\run_bot.cmd"
       Desc = "카톡 딜방 -> 쿠팡/토스 제휴링크 -> 텔레그램 전달 (Partners_money)" },
    @{ Name = "PartnersMoneyListener"
       Cmd  = Join-Path $Root "tools\run_listener.cmd"
       Desc = "텔레그램으로 받은 링크/딜방 글을 발행 문구로 변환 (Partners_money)" }
)

function Show-Status {
    foreach ($spec in $TASKS) {
        $n = $spec.Name
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if (-not $t) {
            Write-Host "[$n] 등록되어 있지 않습니다"
            continue
        }
        $i = Get-ScheduledTaskInfo -TaskName $n
        Write-Host "[$n]"
        Write-Host "  상태        : $($t.State)"
        Write-Host "  로그온 유형 : $($t.Principal.LogonType)   (Interactive 여야 정상)"
        Write-Host "  마지막 실행 : $($i.LastRunTime)"
        Write-Host "  마지막 결과 : $($i.LastTaskResult)   (0 이면 정상)"
        Write-Host ""
    }
}

if ($Status) { Show-Status; exit 0 }

if ($Remove) {
    foreach ($spec in $TASKS) {
        if (Get-ScheduledTask -TaskName $spec.Name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $spec.Name -Confirm:$false
            Write-Host "해제했습니다: $($spec.Name)"
        } else {
            Write-Host "등록되어 있지 않습니다: $($spec.Name)"
        }
    }
    exit 0
}

foreach ($spec in $TASKS) {
    if (-not (Test-Path $spec.Cmd)) {
        Write-Error "진입점을 찾을 수 없습니다: $($spec.Cmd)"
        exit 1
    }
}

# .env 가 없으면 등록해봐야 첫 실행부터 멈춘다. 먼저 잡는다.
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Error "$EnvFile 이 없습니다. .env.example 을 복사해서 채우세요."
    exit 1
}

# 로그온 시 시작한다. 재부팅해도 로그인만 하면 살아난다.
#
# 다만 로그온 **직후** 는 아니다. 그때는 카카오톡이 아직 안 떴고 네트워크도
# 덜 올라와 있다. 그 상태로 첫 주기가 돌면 카톡 창을 못 찾아 실패하고,
# 무인 운영에서는 그 실패가 왜 났는지 보이지 않는다.
# 2분 늦춘다. 첫 주기 한 번을 헛돌리는 것보다 낫다.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT2M"

# Interactive: 로그온한 데스크톱에서만 실행한다. 위 주석 참고. 바꾸지 말 것.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

foreach ($spec in $TASKS) {
    $action = New-ScheduledTaskAction -Execute $spec.Cmd `
        -WorkingDirectory (Join-Path $Root "src")

    if (Get-ScheduledTask -TaskName $spec.Name -ErrorAction SilentlyContinue) {
        Write-Host "기존 작업을 지우고 다시 등록합니다: $($spec.Name)"
        Unregister-ScheduledTask -TaskName $spec.Name -Confirm:$false
    }

    Register-ScheduledTask -TaskName $spec.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description $spec.Desc | Out-Null
    Write-Host "등록 완료: $($spec.Name)"
}

Write-Host ""
Show-Status
Write-Host "지금 바로 시작하려면:"
foreach ($spec in $TASKS) {
    Write-Host "    Start-ScheduledTask -TaskName $($spec.Name)"
}
Write-Host ""
Write-Host "확인할 것:"
Write-Host "  - PC 를 로그인 상태로 켜 둘 것"
Write-Host "  - 절전 끄기 (설정 > 시스템 > 전원 > 화면 및 절전 → 전부 '안 함')"
Write-Host "  - 카톡 딜방 창을 열어 둘 것 (방 창 자동 열기는 아직 미검증)"
