param(
    [string]$Profile = "memoryos"
)

$ErrorActionPreference = "Stop"
$dsh = Get-Command dsh -ErrorAction Stop
& $dsh.Source plugin --profile $Profile remove dsh-memoryos
if ($LASTEXITCODE -ne 0) {
    throw "DeepSeek Harness plugin removal failed"
}
