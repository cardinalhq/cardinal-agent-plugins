#requires -Version 5.1
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5) {
    throw 'This test requires the inbox Windows PowerShell 5.1.'
}
$PSVersionTable.PSVersion.ToString()

$skill = Join-Path $PSScriptRoot '../../.github/skills/cardinal-query'
$tempDir = Join-Path ([IO.Path]::GetTempPath()) ('Cardinal query ' + [Guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $tempDir
$server = $null
try {
    # Install the same folder layout distributed to customers, including a path with spaces.
    $zip = Join-Path $tempDir 'cardinal-query.zip'
    Compress-Archive -Path $skill -DestinationPath $zip
    $install = Join-Path $tempDir '.github/skills'
    Expand-Archive -Path $zip -DestinationPath $install
    $installedScript = Join-Path $install 'cardinal-query/scripts/cardinal_query.ps1'
    $portProbe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $portProbe.Start()
    $port = $portProbe.LocalEndpoint.Port
    $portProbe.Stop()
    $ready = Join-Path $tempDir 'ready'
    $server = Start-Job -ArgumentList $port,$ready -ScriptBlock {
        param($Port, $Ready)
        $ErrorActionPreference = 'Stop'
        $listener = [Net.HttpListener]::new()
        $listener.Prefixes.Add("http://127.0.0.1:$Port/")
        $listener.Start()
        Set-Content -LiteralPath $Ready -Value 'ready'
        $pattern = '-9223372036854775807'
        $bucket = [DateTimeOffset]::Parse('2026-06-01T12:01:00Z').ToUnixTimeMilliseconds()
        $warning = @{service_name='Checkout'; level='WARNING'; chq_fingerprint=[long]$pattern}
        $failure = @{service_name='checkout'; level='ERROR'; chq_fingerprint=$pattern}
        $row = @{timestamp=$bucket; timestamp_ns=1780315260000000001L; tags=@{
            service_name='checkout'; level='ERROR'; chq_fingerprint=$pattern; message='Timeouts [db] :: retry'
        }}
        try {
            while ($true) {
                $context = $listener.GetContext()
                $response = $context.Response
                try {
                    if ($context.Request.Headers['X-CardinalHQ-API-Key'] -ne 'fixture-only') {
                        $response.StatusCode = 401
                        continue
                    }
                    if ($context.Request.HttpMethod -ne 'POST' -or $context.Request.Url.AbsolutePath -ne '/gateway/api/v1/logs/query') {
                        $response.StatusCode = 404
                        continue
                    }
                    $id = $context.Request.Headers['X-CardinalHQ-Query-Id']
                    $response.Headers.Add('X-CardinalHQ-Query-Id', $id)
                    $reader = [IO.StreamReader]::new($context.Request.InputStream)
                    $payload = ConvertFrom-Json -InputObject $reader.ReadToEnd()
                    $reader.Dispose()
                    if ($payload.s -ne '2026-06-01T12:00:00.000Z' -or $payload.e -ne '2026-06-01T12:05:00.000Z') {
                        $response.StatusCode = 400
                        continue
                    }
                    $writer = [IO.StreamWriter]::new($response.OutputStream, [Text.UTF8Encoding]::new($false))
                    $response.ContentType = 'text/event-stream'
                    $writer.WriteLine('data: {"type":"heartbeat","data":{"status":"waiting"}}')
                    $writer.WriteLine()
                    $writer.Flush()
                    $records = @()
                    if ($payload.q.Contains('count_over_time')) {
                        if ($payload.step -ne 60) { throw 'Incorrect Issues bucket size' }
                        if ($payload.q.Contains('chq_fingerprint=')) {
                            if ($payload.q.Contains($pattern) -and $payload.q.Contains('(?is:.*TIMEOUT.*)')) {
                                $records = @(@{timestamp=$bucket; tags=$failure; value=3})
                            }
                        }
                        elseif ($payload.q.Contains('(?s:.*)')) {
                            $records = @(
                                @{timestamp=$bucket; tags=$failure; value=3}
                                @{timestamp=$bucket; tags=$warning; value=2}
                                @{timestamp=$bucket; tags=@{service_name='checkout';level='ERROR'}; value=1}
                                @{timestamp=($bucket-60000); tags=$failure; value=99}
                            )
                        }
                    }
                    elseif ($payload.reverse -and $payload.limit -ge 1) {
                        if ($payload.q.Contains('service_name="checkout"') -and (
                            $payload.q.Contains($pattern) -or
                            ($payload.q.Contains('(?s:.*Timeout.*)') -and $payload.q.Contains('::') -and
                             $payload.q.Contains('(?s:.*health.*)') -and $payload.q.Contains('deployment_environment="test"') -and
                             $payload.q.Contains('db|cache') -and $payload.q.Contains('debug$')))) {
                            $records = @($row)
                        }
                    }
                    foreach ($record in $records) {
                        $writer.WriteLine('data: ' + (ConvertTo-Json -InputObject @{type='result'; data=$record} -Depth 8 -Compress))
                        $writer.WriteLine()
                        $writer.Flush()
                    }
                    if ($payload.q.Contains('service_name="slow"')) {
                        Start-Sleep -Seconds 4
                        continue
                    }
                    if (-not $payload.q.Contains('service_name="cut"')) {
                        $status = if ($payload.q.Contains('service_name="partial"')) { 'error' } else { 'ok' }
                        $writer.WriteLine('data: ' + (ConvertTo-Json -InputObject @{type='done'; data=@{status=$status}} -Compress))
                        $writer.WriteLine()
                    }
                    $writer.Dispose()
                }
                finally { $response.Close() }
            }
        }
        finally { $listener.Close() }
    }
    for ($attempt = 0; $attempt -lt 60 -and -not (Test-Path -LiteralPath $ready); $attempt++) {
        if ($server.State -eq 'Failed') { Receive-Job $server }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-Path -LiteralPath $ready)) { throw 'Fixture server did not start.' }

    $env:PATH = "$env:SystemRoot\System32\WindowsPowerShell\v1.0"
    if (Get-Command python.exe,py.exe,pwsh.exe -ErrorAction SilentlyContinue) {
        throw 'Python or PowerShell 7 is on the test PATH.'
    }
    $env:CARDINAL_QUERY_URL = "http://127.0.0.1:$port/gateway/api/v1"
    $env:CARDINAL_QUERY_API_KEY = 'fixture-only'
    $base = "-Start '2026-06-01T12:00:00Z' -End '2026-06-01T12:05:00Z'"
    $quotedScript = "'" + ($installedScript.Replace("'", "''")) + "'"
    function Invoke-InstalledSkill([string]$Arguments, [string]$ExpectedError) {
        # Invoke an independent Windows PowerShell process with the same command
        # syntax an agent uses. Encode the command to preserve Windows quoting.
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("& $quotedScript $Arguments"))
        $info = [Diagnostics.ProcessStartInfo]::new((Join-Path $PSHOME 'powershell.exe'),
            "-NoProfile -NonInteractive -EncodedCommand $encoded")
        $info.UseShellExecute = $false
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $info.StandardOutputEncoding = [Text.Encoding]::UTF8
        $process = [Diagnostics.Process]::Start($info)
        try {
            $stdout = $process.StandardOutput.ReadToEndAsync()
            $stderr = $process.StandardError.ReadToEndAsync()
            if (-not $process.WaitForExit(20000)) {
                $process.Kill()
                throw 'Skill subprocess did not exit.'
            }
            $output = $stdout.Result
            $errors = $stderr.Result
            if ($ExpectedError) {
                if ($process.ExitCode -eq 0 -or $errors -notmatch $ExpectedError -or $output -or
                    $errors.Contains($env:CARDINAL_QUERY_API_KEY)) { throw "Expected $ExpectedError with no results or key. Got $errors" }
                return
            }
            if ($process.ExitCode -ne 0) { throw "Skill failed: $errors" }
            ConvertFrom-Json -InputObject $output
        }
        finally { $process.Dispose() }
    }
    $logs = Invoke-InstalledSkill "logs $base -Service checkout -Level error -Label 'deployment_environment=test' -Contains @('Timeout','::') -NotContains 'health' -Grep 'db|cache' -NotGrep 'debug$' -Limit 1"
    if ($logs.returned -ne 1 -or -not $logs.limit_reached -or $logs.logs[0].timestamp_ns -ne 1780315260000000001L -or
        $logs.logs[0].tags.message -ne 'Timeouts [db] :: retry' -or $logs.query_ids.Count -ne 1) { throw 'Unexpected log results.' }

    $issues = Invoke-InstalledSkill "issues $base -Limit 1 -Samples 1"
    $fingerprint = '-9223372036854775807'
    if ($issues.total -ne 6 -or $issues.matched_patterns -ne 2 -or $issues.trend.Count -ne 1 -or
        $issues.service_counts.Count -ne 2 -or $issues.trend[0].warn -ne 2 -or $issues.trend[0].error -ne 4 -or
        $issues.patterns[0].fingerprint -cne $fingerprint -or $issues.patterns[0].count -ne 3 -or
        $issues.patterns[0].sample.message -ne $logs.logs[0].tags.message -or $issues.warnings.Count -ne 0) {
        throw ('Unexpected Issues results: ' + (ConvertTo-Json $issues -Depth 10 -Compress))
    }
    $focused = Invoke-InstalledSkill "issues $base -Service checkout -Severity error -Fingerprint '$fingerprint' -Contains TIMEOUT -Samples 0"
    if ($focused.total -ne 3 -or $focused.matched_patterns -ne 1 -or $focused.patterns[0].fingerprint -cne $fingerprint) {
        throw 'Unexpected focused Issues result.'
    }
    $drill = Invoke-InstalledSkill "logs $base -Service checkout -Level error -Label 'chq_fingerprint=$fingerprint' -Field @('message','level')"
    if ($drill.returned -ne 1 -or $drill.logs[0].tags.chq_fingerprint -cne $fingerprint) { throw 'Unexpected fingerprint drill-down.' }

    $empty = Invoke-InstalledSkill "logs $base -Service missing"
    if ($empty.returned -ne 0 -or $empty.logs.Count -ne 0) { throw 'Unexpected empty result.' }
    Invoke-InstalledSkill "logs $base -Service partial" 'Results may be incomplete'
    Invoke-InstalledSkill "logs $base -Service cut" 'ended before completion'
    Invoke-InstalledSkill "issues -Since 10s -End '2026-06-01T12:00:30Z'" 'no completed bucket'
    $env:CARDINAL_QUERY_API_KEY = 'invalid-key'
    Invoke-InstalledSkill "logs $base" 'HTTP 401'
    $env:CARDINAL_QUERY_API_KEY = 'fixture-only'
    Invoke-InstalledSkill "logs $base -Service slow -Timeout 1" 'timed out'
    'PASS: installed skill ran log filters, Issues, fingerprint drill-down, and API error handling with Windows PowerShell 5.1 and no Python or pwsh on PATH.'

    if ($env:CARDINAL_QUERY_HTTPS_SMOKE_URL) {
        $env:CARDINAL_QUERY_URL = $env:CARDINAL_QUERY_HTTPS_SMOKE_URL
        $env:CARDINAL_QUERY_API_KEY = 'invalid-windows-smoke-key'
        Invoke-InstalledSkill "logs $base -Timeout 10" 'HTTP 401'
        'PASS: Windows PowerShell 5.1 reached the hosted Query API over HTTPS.'
    }
}
finally {
    if ($server) { Stop-Job -Job $server; Remove-Job -Job $server -Force }
    Remove-Item -LiteralPath $tempDir -Recurse -Force
}
