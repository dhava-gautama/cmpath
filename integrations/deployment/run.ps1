<# Run the offline CMP contract from a source checkout on Windows PowerShell. #>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Get-Command python -ErrorAction SilentlyContinue
if ($null -eq $Python) {
    throw "Python 3.10+ is required"
}

$OldPath = $env:PYTHONPATH
try {
    if ([string]::IsNullOrWhiteSpace($OldPath)) {
        $env:PYTHONPATH = Join-Path $Root "src"
    } else {
        $env:PYTHONPATH = (Join-Path $Root "src") + [IO.Path]::PathSeparator + $OldPath
    }
    Push-Location $Root
    & $Python.Source (Join-Path $Root "conformance\run.py") @Arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
    $env:PYTHONPATH = $OldPath
}

