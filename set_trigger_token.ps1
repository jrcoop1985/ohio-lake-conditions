# One-time setup for the hourly trigger (trigger_hourly.ps1). Run it yourself in PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\Users\jrcoo\work\ohio-lake-conditions\set_trigger_token.ps1
# It opens GitHub's "new fine-grained token" page with the name, description, expiry and permission filled in.
# On that page: Repository access -> "Only select repositories" -> ohio-lake-conditions, then Generate token and copy it.
# Paste it at the hidden prompt here. The token is saved to a file only your Windows account can read, and is never
# printed, logged or committed.
$ErrorActionPreference = 'Stop'
$q = 'name=lake-trigger-JoelHome&description=Hourly+trigger+for+ohio-lake-conditions+from+JoelHome&target_name=jrcoop1985&expires_in=365&actions=write'
Start-Process "https://github.com/settings/personal-access-tokens/new?$q"
$sec = Read-Host -AsSecureString 'Paste the new token, then press Enter'
$plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if ($plain -notmatch '^github_pat_') { Write-Host 'That does not look like a fine-grained token (github_pat_...). Nothing saved.'; exit 1 }
$dir = Join-Path $env:USERPROFILE '.config\crackedbuckeye'
New-Item -ItemType Directory -Force $dir | Out-Null
$file = Join-Path $dir 'lake-trigger.token'
Set-Content -Path $file -Value $plain -NoNewline -Encoding ascii
$plain = $null
# Only this account may read the file.
icacls $file /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
Write-Host 'Saved. Testing one run of the trigger...'
& (Join-Path $PSScriptRoot 'trigger_hourly.ps1')
Write-Host "Trigger exit code: $LASTEXITCODE (0 = the hourly job started on GitHub). Last log line:"
Get-Content (Join-Path $PSScriptRoot 'trigger.log') -Tail 1
