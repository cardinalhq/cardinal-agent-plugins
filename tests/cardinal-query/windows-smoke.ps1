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
        # Keep this file ASCII: Windows PowerShell 5.1 reads it as ANSI.
        $emoji = [char]::ConvertFromUtf32(0x1F600)
        # Two distinct long values share their first 256 characters; one repeats.
        $longValues = @(('y' * 300), ('y' * 300), (('y' * 299) + 'z'), ('a' + ($emoji * 300)),
            '2026-11-16T10:00:00Z', '2026-11-16T10:00:00.000+00:00')
        $attempts = @{}
        try {
            while ($true) {
                $context = $listener.GetContext()
                $response = $context.Response
                try {
                    if ($context.Request.Headers['X-CardinalHQ-API-Key'] -ne 'fixture-only') {
                        $response.StatusCode = 401
                        continue
                    }
                    $path = $context.Request.Url.AbsolutePath
                    # /empty/ serves an empty tag rollup, which the server encodes as null.
                    if ($context.Request.HttpMethod -ne 'POST' -or
                        $path -cnotmatch '^/(gateway|empty)/api/v1/logs/(query|tags|tagvalues)\z') {
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
                    # Retry scenarios, selected by service name.
                    $scenario = if ($payload.q -cmatch 'service_name="(flaky|busy|down|broken|drop)"') { $Matches[1] }
                    if ($scenario) { $attempts[$scenario] = 1 + [int]$attempts[$scenario] }
                    $first = $scenario -and $attempts[$scenario] -eq 1
                    $writer = [IO.StreamWriter]::new($response.OutputStream, [Text.UTF8Encoding]::new($false))
                    if (($scenario -ceq 'flaky' -and $first) -or $scenario -ceq 'down') {
                        $response.StatusCode = 503
                        $response.ContentType = 'text/plain'
                        $writer.Write('upstream unavailable')
                        $writer.Dispose()
                        continue
                    }
                    if ($scenario -ceq 'busy' -and $first) {
                        $response.StatusCode = 429
                        $response.Headers.Add('Retry-After', '1')
                        $writer.Dispose()
                        continue
                    }
                    if ($scenario -ceq 'broken') {
                        $response.StatusCode = 500
                        $response.ContentType = 'application/json'
                        $writer.Write('{"status":500,"code":"QUERY_FAILED","message":"worker failed"}')
                        $writer.Dispose()
                        continue
                    }
                    if ($path.EndsWith('/tags')) {
                        if ($payload.q -and $payload.q.Contains('echo')) {
                            # An error body that echoes the key, which the client must redact.
                            $response.StatusCode = 400
                            $response.ContentType = 'text/plain'
                            $writer.Write('rejected key ' + $context.Request.Headers['X-CardinalHQ-API-Key'])
                        }
                        elseif ($path.StartsWith('/empty/')) {
                            $response.ContentType = 'application/json'
                            $writer.Write('{"tags":null}')
                        }
                        elseif ($payload.q -and $payload.q.Contains('bad')) {
                            # Tag endpoints report parse errors as plain text.
                            $response.StatusCode = 400
                            $response.ContentType = 'text/plain'
                            $writer.Write('invalid query expression: parse error at 1:5')
                        }
                        else {
                            $names = if ($payload.q -ceq '{service_name="checkout"}') { @('service_name','level','message','chq_fingerprint','http_route') }
                                     elseif (-not $payload.q) { @('service_name','k8s_namespace_name','message','chq_fingerprint') }
                                     else { @() }
                            $response.ContentType = 'application/json'
                            $writer.Write((ConvertTo-Json -InputObject @{tags=$names} -Compress))
                        }
                        $writer.Dispose()
                        continue
                    }
                    $response.ContentType = 'text/event-stream'
                    $writer.WriteLine('data: {"type":"heartbeat","data":{"status":"waiting"}}')
                    $writer.WriteLine()
                    $writer.Flush()
                    $records = @()
                    if ($path.EndsWith('/tagvalues')) {
                        if ($context.Request.QueryString['tagName'] -ceq 'failing') {
                            $writer.WriteLine('data: ' + (ConvertTo-Json -InputObject @{type='error'; data=@{
                                code='QUERY_TOO_LARGE'; message=('query limit exceeded for ' + $context.Request.Headers['X-CardinalHQ-API-Key'])}} -Compress))
                            $writer.WriteLine()
                            $writer.Dispose()
                            continue
                        }
                        if ($context.Request.QueryString['tagName'] -ceq 'long') {
                            $records = @($longValues | Select-Object -First $payload.limit | ForEach-Object { @{value=$_} })
                        }
                        elseif ($context.Request.QueryString['tagName'] -ceq 'k8s_namespace_name' -and $payload.q -ceq '{service_name="checkout"}') {
                            $records = @(@('checkout-prod','checkout-staging','payments') |
                                Where-Object { -not $payload.contains -or $_.Contains($payload.contains) } |
                                Select-Object -First $payload.limit | ForEach-Object { @{value=$_} })
                        }
                    }
                    elseif ($payload.q.Contains('count_over_time')) {
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
                    elseif ($scenario) {
                        # The message reports which attempt succeeded.
                        $records = @(@{timestamp=$bucket; tags=@{service_name=$scenario; message="attempt $($attempts[$scenario])"}})
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
                    if ($scenario -ceq 'drop') {
                        # Drop the connection after one row, before the done event.
                        $writer.Dispose()
                        continue
                    }
                    # q is absent for unscoped tag values.
                    if (([string]$payload.q).Contains('service_name="slow"')) {
                        Start-Sleep -Seconds 4
                        continue
                    }
                    if (-not ([string]$payload.q).Contains('service_name="cut"')) {
                        $status = if (([string]$payload.q).Contains('service_name="partial"')) { 'error' } else { 'ok' }
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
    # Undocumented hook that shortens retry backoff for the test.
    $env:CARDINAL_QUERY_RETRY_BASE_MS = '20'
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
            $script:lastErrors = $errors
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

    $tags = Invoke-InstalledSkill "tags $base"
    if ($tags.source -ne 'org_daily_rollup' -or $null -ne $tags.scope -or $tags.matched -ne 4 -or
        @($tags.core_fields_not_listed) -join ',' -ne 'level' -or -not $tags.note) {
        throw ('Unexpected tag names: ' + (ConvertTo-Json $tags -Compress))
    }
    $scopedTags = Invoke-InstalledSkill "tags $base -Service checkout -Match LEVEL"
    if ($scopedTags.source -ne 'matching_rows' -or @($scopedTags.tags) -join ',' -ne 'level' -or @($scopedTags.core_fields_not_listed).Count -ne 0) {
        throw ('Unexpected scoped tag names: ' + (ConvertTo-Json $scopedTags -Compress))
    }
    $values = Invoke-InstalledSkill "values $base -Tag k8s.namespace.name -Service checkout -Match checkout"
    if ($values.tag -cne 'k8s_namespace_name' -or $values.limit -ne 50 -or $values.returned -ne 2 -or $values.limit_reached -or
        @($values.values) -join ',' -ne 'checkout-prod,checkout-staging') {
        throw ('Unexpected tag values: ' + (ConvertTo-Json $values -Compress))
    }
    $one = Invoke-InstalledSkill "values $base -Tag k8s_namespace_name -Service checkout -Limit 1"
    if ($one.returned -ne 1 -or -not $one.limit_reached -or @($one.values).Count -ne 1) { throw 'Unexpected limited tag values.' }
    Invoke-InstalledSkill "tags $base -Query '{bad=}'" 'HTTP 400\. invalid query expression: parse error'
    if ($script:lastErrors -match 'retrying') { throw 'A query error was retried.' }
    Invoke-InstalledSkill "values $base -Tag message" 'Use logs or issues'

    # An empty rollup arrives as {"tags":null}.
    $env:CARDINAL_QUERY_URL = "http://127.0.0.1:$port/empty/api/v1"
    $emptyTags = Invoke-InstalledSkill "tags $base"
    if ($emptyTags.matched -ne 0 -or @($emptyTags.tags).Count -ne 0 -or
        @($emptyTags.core_fields_not_listed) -join ',' -ne 'service_name,level,message,chq_fingerprint') {
        throw ('Unexpected empty tag list: ' + (ConvertTo-Json $emptyTags -Compress))
    }
    $env:CARDINAL_QUERY_URL = "http://127.0.0.1:$port/gateway/api/v1"

    # De-duplicate full values, then shorten by code point without splitting a surrogate pair.
    $emoji = [char]::ConvertFromUtf32(0x1F600)
    $ellipsis = [string][char]0x2026
    $long = Invoke-InstalledSkill "values $base -Tag long"
    $expected = @('2026-11-16T10:00:00.000+00:00', '2026-11-16T10:00:00Z', ('a' + ($emoji * 255) + $ellipsis),
        (('y' * 256) + $ellipsis), (('y' * 256) + $ellipsis))
    if ($long.returned -ne 5 -or $long.limit_reached -or $long.shortened_values -ne 3 -or @($long.values).Count -ne 5 -or
        (@(for ($i = 0; $i -lt 5; $i++) { $long.values[$i] -ceq $expected[$i] }) -contains $false) -or
        (@($long.values) -join '').Contains([string][char]0xFFFD)) {
        throw ('Unexpected long tag values: ' + (ConvertTo-Json $long -Compress))
    }
    $three = Invoke-InstalledSkill "values $base -Tag long -Limit 3"
    if ($three.returned -ne 2 -or $three.limit_reached -or $three.shortened_values -ne 2) {
        throw ('Unexpected de-duplicated tag values: ' + (ConvertTo-Json $three -Compress))
    }
    # .NET $ matches before a final newline; names must not.
    Invoke-InstalledSkill "values $base -Tag ([string]::Concat('level', [char]10))" 'label name'
    Invoke-InstalledSkill "logs $base -Label ([string]::Concat('level', [char]10, '=x'))" 'LABEL=VALUE'
    Invoke-InstalledSkill "issues $base -Fingerprint ([string]::Concat('1', [char]10))" '64-bit'
    Invoke-InstalledSkill "logs -Since ([string]::Concat('1h', [char]10))" 'duration'
    # Server messages reach the agent with the key redacted.
    Invoke-InstalledSkill "values $base -Tag failing" 'query limit exceeded for \[redacted\] Query ID'
    Invoke-InstalledSkill "tags $base -Query '{echo=`"1`"}'" 'HTTP 400\. rejected key \[redacted\]'

    # Retries.
    $flaky = Invoke-InstalledSkill "logs $base -Service flaky"
    if ($flaky.logs[0].tags.message -cne 'attempt 2' -or $script:lastErrors -notmatch 'retrying after HTTP 503 \(attempt 2 of 3\)') {
        throw ('Unexpected retry after HTTP 503: ' + $script:lastErrors)
    }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $busy = Invoke-InstalledSkill "logs $base -Service busy"
    if ($busy.logs[0].tags.message -cne 'attempt 2' -or $clock.ElapsedMilliseconds -lt 1000 -or
        $script:lastErrors -notmatch 'retrying after HTTP 429') { throw 'Retry-After was not honored.' }
    Invoke-InstalledSkill "logs $base -Service down" 'HTTP 503\. upstream unavailable Failed after 3 attempts\. Query ID'
    Invoke-InstalledSkill "logs $base -Service down -Retries 0" 'HTTP 503\. upstream unavailable Query ID'
    if ($script:lastErrors -match 'retrying|attempts') { throw '-Retries 0 retried.' }
    Invoke-InstalledSkill "logs $base -Service broken" 'HTTP 500\. worker failed Query ID'
    if ($script:lastErrors -match 'retrying') { throw 'HTTP 500 was retried.' }
    Invoke-InstalledSkill "logs $base -Service drop" 'ended before completion\. Query ID'
    if ($script:lastErrors -match 'retrying') { throw 'A stream that returned a row was retried.' }

    $empty = Invoke-InstalledSkill "logs $base -Service missing"
    if ($empty.returned -ne 0 -or $empty.logs.Count -ne 0) { throw 'Unexpected empty result.' }
    Invoke-InstalledSkill "logs $base -Service partial" 'Results may be incomplete'
    Invoke-InstalledSkill "logs $base -Service cut" 'ended before completion\. Failed after 3 attempts'
    Invoke-InstalledSkill "issues -Since 10s -End '2026-06-01T12:00:30Z'" 'no completed bucket'
    $env:CARDINAL_QUERY_API_KEY = 'invalid-key'
    Invoke-InstalledSkill "logs $base" 'HTTP 401'
    if ($script:lastErrors -match 'retrying') { throw 'HTTP 401 was retried.' }
    $env:CARDINAL_QUERY_API_KEY = 'fixture-only'
    Invoke-InstalledSkill "logs $base -Service slow -Timeout 1" 'timed out'
    $portProbe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $portProbe.Start()
    $closedPort = $portProbe.LocalEndpoint.Port
    $portProbe.Stop()
    $env:CARDINAL_QUERY_URL = "http://127.0.0.1:$closedPort/api/v1"
    $clock = [Diagnostics.Stopwatch]::StartNew()
    Invoke-InstalledSkill "tags $base -Timeout 15" 'Query API connection failed: .* Failed after 3 attempts\. Query ID'
    if ($clock.ElapsedMilliseconds -gt 16000 -or $script:lastErrors -notmatch 'retrying after a connection error \(attempt 3 of 3\)') {
        throw 'Unexpected connection retry.'
    }
    $env:CARDINAL_QUERY_URL = "http://127.0.0.1:$port/gateway/api/v1"
    'PASS: installed skill ran log filters, Issues, fingerprint drill-down, tag discovery, retries, and API error handling with Windows PowerShell 5.1 and no Python or pwsh on PATH.'

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
