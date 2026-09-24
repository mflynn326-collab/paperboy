$ErrorActionPreference = 'Stop'

Set-Location (Join-Path $PSScriptRoot '..')

function Invoke-Paperboy {
    param([string[]]$Arguments)

    & python -m paperboy @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Paperboy command failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Resumable {
    param([string[]]$Arguments, [int]$Attempts = 8)

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            Invoke-Paperboy $Arguments
            return
        } catch {
            if ($attempt -eq $Attempts) {
                throw
            }
            Start-Sleep -Seconds (30 * $attempt)
        }
    }
}

Invoke-Resumable @('harvest-ssrn', '--from-date', '2020-01-01', '--until-date', '2026-06-19', '--per-page', '100')
Invoke-Resumable @('enrich-unpaywall', '--limit', '500', '--all')
Invoke-Paperboy @('drive-sync')
