# Starts the hourly GitHub Actions job from JoelHome. GitHub's own cron (17 * * * *) is delayed or dropped for hours
# at a time; the Windows task "CrackedBuckeye lake conditions hourly" runs this at :47. The fetch itself runs on GitHub.
# 2026-10-10: `gh workflow run` sometimes hung on a TLS handshake until the task's 5-minute limit killed it (result
# 267014, no log line), so about one hour in three never fired. Now each attempt gets 60 s, up to 3 attempts, --ref main
# skips gh's default-branch lookup, and every attempt (timeouts included) is logged.
$log = Join-Path $PSScriptRoot 'trigger.log'
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
$code = 1
for ($try = 1; $try -le 3; $try++) {
    $out = Join-Path $env:TEMP "lake-trigger-$PID.out"
    $p = Start-Process -FilePath $gh -ArgumentList 'workflow', 'run', 'hourly.yml', '--ref', 'main', '-R', 'jrcoop1985/ohio-lake-conditions' `
        -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError "$out.err"
    $null = $p.Handle   # without touching Handle, Windows PowerShell reports ExitCode as empty after WaitForExit
    if ($p.WaitForExit(60000)) {
        $code = $p.ExitCode
        $msg = ((Get-Content $out, "$out.err" -ErrorAction SilentlyContinue) -join ' ').Trim()
    } else {
        $p.Kill(); $code = 124; $msg = 'timed out after 60 s'
    }
    Remove-Item $out, "$out.err" -ErrorAction SilentlyContinue
    "$(Get-Date -Format s) try=$try exit=$code $msg" | Add-Content -Encoding utf8 $log
    if ($code -eq 0) { break }
    Start-Sleep -Seconds 20
}
exit $code
