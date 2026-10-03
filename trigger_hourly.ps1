# Starts the hourly GitHub Actions job from JoelHome. GitHub's own cron (17 * * * *) is delayed or dropped for hours
# at a time; the Windows task "CrackedBuckeye lake conditions hourly" runs this at :47. The fetch itself runs on GitHub.
$log = Join-Path $PSScriptRoot 'trigger.log'
$out = & 'C:\Program Files\GitHub CLI\gh.exe' workflow run hourly.yml -R jrcoop1985/ohio-lake-conditions 2>&1
"$(Get-Date -Format s) exit=$LASTEXITCODE $out" | Add-Content -Encoding utf8 $log
exit $LASTEXITCODE
