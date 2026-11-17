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
        # The same pattern and budget data as test_cardinal_query.py, so both clients must
        # report the same counts, exemplars and budget results.
        $eAcute = [string][char]0xE9
        $longMessage = $eAcute + ($emoji * 400)
        $shopPoint = { param($Offset, $ID, $Value)
            $tags = @{service_name='shop'}
            if ($null -ne $ID) { $tags.chq_fingerprint = $ID }
            @{timestamp=($bucket + $Offset); tags=$tags; value=$Value}
        }
        $shopRow = { param($ID, $Message, $Offset, $Projection)
            $tags = @{service_name='shop'; level='ERROR'; message=$Message; projection=$Projection}
            if ($null -ne $ID) { $tags.chq_fingerprint = $ID }
            @{timestamp=($bucket + 100000 - $Offset); tags=$tags}
        }
        # (service, fingerprint, minutes after 12:00, count), as EDGE_POINTS in test_cardinal_query.py.
        # The server emits points stamped from the start to the query end.
        $edgePoints = @(@('a', '1', 0, 50), @('a', '1', 1, 2.6), @('b', '5', 3, 3.5), @('a', '9', 2, 2),
            @('a', '9', 4, 2), @('b', '1', 2, 4), @('a', '1', 5, 7), @('a', '1', 6, 99))
        $culture = [Globalization.CultureInfo]::InvariantCulture
        # Rows written as JSON text: a multi-KB stack trace, numbers and strings the client must
        # write as Python does, and wide rows built once.
        $stackLines = for ($i = 0; $i -lt 80; $i++) {
            '  File \"/srv/app/module_{0:D3}.py\", line {1}, in handler_{1}\n\traise Error({1})\n' -f $i, $i }
        $stackJson = '"Traceback (most recent call last):\n' + ($stackLines -join '') + '"'
        $exactRow = '{"timestamp":' + $bucket + ',"tags":{"service_name":"exact","f1":0.7999999999999999,' +
            '"f2":0.30000000000000004,"f3":1.25,"f4":-0.5,"i1":9007199254740993,"s1":"del\u007f sep\u2028 nul\u0000"}}'
        $perfRows = $null
        $keepPoints = @(for ($i = 0; $i -lt 40; $i++) {
            @{timestamp=$bucket; value=(100 - $i); tags=@{service_name=('svc{0:D2}-' -f $i) + ('s' * 300); chq_fingerprint=('1' * 150) + ('{0:D2}' -f $i)}} })
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
                    # Without -End, CARDINAL_QUERY_NOW ends the window at 12:04:30 and counts query to 12:05.
                    if ($payload.s -ne '2026-06-01T12:00:00.000Z' -or $payload.e -notin '2026-06-01T12:05:00.000Z', '2026-06-01T12:04:30.000Z') {
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
                            # Assign inside each branch: an if statement that outputs @() yields AutomationNull,
                            # which Windows PowerShell 5.1 ConvertTo-Json writes as {} rather than [].
                            $names = @()
                            if ($payload.q -ceq '{service_name="checkout"}') { $names = @('service_name','level','message','chq_fingerprint','http_route') }
                            elseif (-not $payload.q) { $names = @('service_name','k8s_namespace_name','message','chq_fingerprint') }
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
                        if ($context.Request.QueryString['tagName'] -ceq 'emoji') { $records = @(@{value=($emoji * 200)}) }
                        elseif ($context.Request.QueryString['tagName'] -ceq 'long') {
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
                        if ($payload.q.Contains('service_name="edge"')) {
                            $from = [DateTimeOffset]::Parse($payload.s, $culture).ToUnixTimeMilliseconds()
                            $to = [DateTimeOffset]::Parse($payload.e, $culture).ToUnixTimeMilliseconds()
                            $records = @(foreach ($point in $edgePoints) {
                                $stamp = $bucket + ($point[2] - 1) * 60000
                                if ($stamp -ge $from -and $stamp -le $to) {
                                    @{timestamp=$stamp; value=$point[3]; tags=@{service_name=$point[0]; chq_fingerprint=$point[1]; level='ERROR'}}
                                }
                            })
                        }
                        elseif ($payload.q.Contains('service_name="rawonly"')) {
                            # No fingerprint rollups: only a raw scan, forced by a message predicate, has counts.
                            if ($payload.q.Contains('|~ "(?s:.*)"')) { $records = @(@{timestamp=$bucket; value=4; tags=@{service_name='rawonly'; chq_fingerprint='5'}}) }
                        }
                        elseif ($payload.q.Contains('scenario="keep"')) { $records = $keepPoints }
                        elseif ($payload.q.Contains('service_name="shop"')) {
                            if (-not $payload.q.StartsWith('sum by (service_name, chq_fingerprint)(count_over_time(')) { throw 'Unexpected pattern query' }
                            $records = @(
                                (& $shopPoint 0 $pattern 3), (& $shopPoint 120000 $pattern 4), (& $shopPoint 60000 42 5),
                                (& $shopPoint 60000 '7' 5), (& $shopPoint 60000 $null 2), (& $shopPoint 180000 '0' 1),
                                (& $shopPoint -60000 $pattern 99), (& $shopPoint 300000 $pattern 99), (& $shopPoint 0 '9' 0),
                                (& $shopPoint 0 '9' 2.4))
                        }
                        elseif ($payload.q.Contains('service_name="wide"')) {
                            $records = @(for ($i = 0; $i -lt 150; $i++) {
                                @{timestamp=$bucket; tags=@{service_name='wide'; chq_fingerprint=[string](5000 + $i)}; value=(1000 - $i)} })
                        }
                        elseif ($payload.q.Contains('scenario="services"')) {
                            $records = @(for ($i = 0; $i -lt 12; $i++) {
                                @{timestamp=$bucket; tags=@{service_name=('svc{0:D2}' -f $i); level='ERROR'; chq_fingerprint=[string](100 + $i)}; value=(12 - $i)} })
                            $records += @{timestamp=$bucket; tags=@{service_name='svc00'; level='WARN'; chq_fingerprint='200'}; value=1}
                        }
                        elseif ($payload.q.Contains('chq_fingerprint=')) {
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
                    elseif ($payload.reverse -and $payload.q.Contains('service_name="shop"')) {
                        # The projection tag reports the fields the client asked for.
                        $projection = if ($payload.fields) { @($payload.fields) -join ',' } else { 'all' }
                        if ($payload.q.Contains('chq_fingerprint="42"')) { $records = @(& $shopRow '42' 'forty-two' 0 $projection) }
                        elseif ($payload.q.Contains("chq_fingerprint=`"$pattern`"")) { $records = @(& $shopRow $pattern $longMessage 0 $projection) }
                        elseif (-not $payload.q.Contains('chq_fingerprint=')) {
                            $records = @((& $shopRow $pattern $longMessage 0 $projection), (& $shopRow $pattern 'older' 1000 $projection),
                                (& $shopRow '7' 'seven' 2000 $projection), (& $shopRow $null 'no fingerprint' 3000 $projection)) |
                                Select-Object -First $payload.limit
                        }
                    }
                    elseif ($payload.reverse -and $payload.q.Contains('service_name="flood"')) {
                        # Rows of non-ASCII and emoji text, larger than the output budget.
                        $records = @(for ($i = 0; $i -lt $payload.limit; $i++) {
                            @{timestamp=($bucket + 200000 - $i * 1000); tags=@{service_name='flood'; level='INFO'
                                chq_fingerprint=[string](1000 + $i); message=(('{0:D3} ' -f $i) + ($eAcute * 100) + ($emoji * 200))}} })
                    }
                    elseif ($payload.reverse -and $payload.q.Contains('service_name="wide"') -and -not $payload.q.Contains('chq_fingerprint=')) {
                        $records = @(for ($i = 0; $i -lt 10; $i++) {
                            @{timestamp=$bucket; tags=@{service_name='wide'; chq_fingerprint=[string](5000 + $i); message=($emoji * 300)}} })
                    }
                    elseif ($payload.reverse -and $payload.q.Contains('scenario="keep"')) {
                        $records = @(foreach ($point in $keepPoints) {
                            @{timestamp=$bucket; tags=@{service_name=$point.tags.service_name; chq_fingerprint=$point.tags.chq_fingerprint; message=('k' * 300)}} })
                    }
                    elseif ($payload.reverse -and $payload.q.Contains('service_name="stack"')) {
                        $message = if ($payload.q.Contains('chq_fingerprint="78"')) { '"' + ('\ud83d\ude00' * 9000) + '"' } else { $stackJson }
                        $records = @(for ($i = 0; $i -lt $payload.limit; $i++) {
                            '{"timestamp":' + ($bucket - $i) + ',"tags":{"service_name":"stack","chq_fingerprint":"77","message":' + $message + '}}' })
                    }
                    elseif ($payload.reverse -and $payload.q.Contains('service_name="attrs"')) {
                        $records = @(for ($i = 0; $i -lt $payload.limit; $i++) {
                            $tags = @{service_name='attrs'; level='INFO'; chq_fingerprint=[string]$i; message="row $i"}
                            for ($k = 0; $k -lt 150; $k++) { $tags['attr_{0:D3}' -f $k] = 'v' * 50 }
                            @{timestamp=($bucket - $i); tags=$tags} })
                    }
                    elseif ($payload.reverse -and $payload.q.Contains('service_name="perf"')) {
                        if ($null -eq $perfRows) {
                            # The same rows as perf_row in test_cardinal_query.py.
                            $perfRows = @(for ($i = 0; $i -lt 200; $i++) {
                                $tags = @{service_name='perf'; level='ERROR'; chq_fingerprint=[string]$i; message=('m' * 2000)}
                                for ($k = 0; $k -lt 150; $k++) {
                                    $tags['attr_{0:D3}' -f $k] = if ($k % 2) { 'v' * (5 + ($k * 37 + $i * 11) % 396) } else { @{a=@(1, 2, @{b=('c' * 50)}); d=1.25} }
                                }
                                ConvertTo-Json -InputObject @{timestamp=($bucket - $i); timestamp_ns=1; tags=$tags} -Depth 8 -Compress })
                        }
                        $records = @($perfRows | Select-Object -First $payload.limit)
                    }
                    elseif ($payload.reverse -and $payload.q.Contains('service_name="exact"')) { $records = @($exactRow) }
                    elseif ($payload.reverse -and ($payload.q.Contains('service_name="edge"') -or $payload.q.Contains('service_name="rawonly"'))) { $records = @() }
                    elseif ($payload.reverse -and $payload.q.Contains('scenario="services"') -and $payload.q.Contains('chq_fingerprint="100"')) {
                        $records = @(@{timestamp=$bucket; tags=@{service_name='svc00'; message=('x' * 500)}})
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
                        if ($record -is [string]) { $writer.WriteLine('data: {"type":"result","data":' + $record + '}') }
                        else { $writer.WriteLine('data: ' + (ConvertTo-Json -InputObject @{type='result'; data=$record} -Depth 8 -Compress)) }
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
        Invoke-SkillCommand "& $quotedScript $Arguments" $ExpectedError
    }
    function Invoke-SkillCommand([string]$CommandText, [string]$ExpectedError) {
        # Invoke an independent Windows PowerShell process with the same command
        # syntax an agent uses. Encode the command to preserve Windows quoting.
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($CommandText))
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
            $script:lastOutput = $output
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
    $logs = Invoke-InstalledSkill "logs $base -Service checkout -Level error -Label 'deployment_environment=test' -Contains @('Timeout','::') -NotContains 'health' -Grep 'db|cache' -NotGrep 'debug$' -Limit 1 -Raw"
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
    $drill = Invoke-InstalledSkill "logs $base -Service checkout -Level error -Label 'chq_fingerprint=$fingerprint' -Field @('message','level') -Raw"
    if ($drill.returned -ne 1 -or $drill.logs[0].tags.chq_fingerprint -cne $fingerprint) { throw 'Unexpected fingerprint drill-down.' }

    # logs defaults to patterns with true window counts and one exemplar per top pattern.
    $emoji = [char]::ConvertFromUtf32(0x1F600)
    $ellipsis = [string][char]0x2026
    $eAcute = [string][char]0xE9
    $window = "-Start '2026-06-01T12:00:00.000Z' -End '2026-06-01T12:05:00.000Z'"
    $bucketMs = [DateTimeOffset]::Parse('2026-06-01T12:01:00Z', [Globalization.CultureInfo]::InvariantCulture).ToUnixTimeMilliseconds()
    $patterns = Invoke-InstalledSkill "logs $base -Service shop"
    $top = $patterns.patterns[0]
    if ($patterns.mode -cne 'patterns' -or $patterns.total -ne 22 -or $patterns.unfingerprinted -ne 3 -or
        $patterns.matched_patterns -ne 4 -or $patterns.limit -ne 10 -or
        (@($patterns.patterns | ForEach-Object { "$($_.fingerprint)=$($_.count)" }) -join ',') -cne "$fingerprint=7,42=5,7=5,9=2" -or
        $top.service -cne 'shop' -or $top.first_seen -cne '2026-06-01T12:00:00.000Z' -or $top.last_seen -cne '2026-06-01T12:03:00.000Z' -or
        $top.exemplar.message -cne ($eAcute + ($emoji * 299) + $ellipsis) -or $top.exemplar.message_chars -ne 401 -or
        $patterns.patterns[1].exemplar.message -cne 'forty-two' -or $patterns.patterns[2].exemplar.message -cne 'seven' -or
        $null -ne $patterns.patterns[3].exemplar -or @($patterns.query_ids).Count -ne 4 -or $patterns.warnings.Count -ne 0) {
        throw ('Unexpected log patterns: ' + $script:lastOutput)
    }
    # The hint is the exact drill-down command, with the full signed fingerprint.
    $run = $patterns.next[0].run
    if (-not $run.StartsWith("& '") -or -not $run.EndsWith("' logs $window -Service 'shop' -Fingerprint '$fingerprint' -Raw")) {
        throw "Unexpected drill-down hint: $run"
    }
    $hinted = Invoke-SkillCommand $run
    if ($hinted.mode -cne 'raw' -or $hinted.returned -ne 1 -or $hinted.logs[0].tags.chq_fingerprint -cne $fingerprint -or
        $hinted.logs[0].message_chars -ne 401) { throw ('Unexpected hinted drill-down: ' + $script:lastOutput) }

    # -Raw keeps rows, newest first, with a default projection and truncated messages.
    $raw = Invoke-InstalledSkill "logs $base -Service shop -Raw"
    if ($raw.mode -cne 'raw' -or (@($raw.fields) -join ',') -cne 'message,service_name,level,chq_fingerprint' -or
        $raw.logs[0].tags.projection -cne 'message,service_name,level,chq_fingerprint' -or $raw.returned -ne 4 -or $raw.limit -ne 20 -or
        $raw.logs[0].tags.message -cne ($eAcute + ($emoji * 299) + $ellipsis) -or $raw.logs[0].message_chars -ne 401 -or
        $raw.truncated_messages -ne 1 -or $null -ne $raw.next) { throw ('Unexpected raw rows: ' + $script:lastOutput) }
    $all = Invoke-InstalledSkill "logs $base -Service shop -Raw -AllFields -Limit 2"
    if ($null -ne $all.fields -or $all.logs[0].tags.projection -cne 'all' -or -not $all.limit_reached -or
        -not $all.next[0].run.EndsWith("' logs $window -Service 'shop'")) { throw ('Unexpected -AllFields rows: ' + $script:lastOutput) }
    $one = Invoke-InstalledSkill "logs $base -Service shop -Raw -Field message"
    if ((@($one.fields) -join ',') -cne 'message' -or $one.logs[0].tags.projection -cne 'message') { throw 'Unexpected -Field projection.' }
    $clamped = Invoke-InstalledSkill "logs $base -Service shop -Raw -Limit 500"
    if ($clamped.limit -ne 200 -or $clamped.limit_clamped_from -ne 500) { throw 'The raw row limit was not clamped.' }
    # -Field, -AllFields and -FullMessage imply -Raw.
    foreach ($extra in '-Field message', '-AllFields', '-FullMessage') {
        $implied = Invoke-InstalledSkill "logs $base -Service shop $extra"
        if ($implied.mode -cne 'raw' -or $implied.returned -ne 4 -or $implied.limit -ne 20) { throw "$extra did not imply -Raw: $script:lastOutput" }
    }
    Invoke-InstalledSkill "logs $base -Field 1bad" 'label name'
    Invoke-InstalledSkill "logs $base -Raw -Field message -AllFields" 'either -Field or -AllFields'

    # The output budget: ASCII JSON of at most 16384 bytes, the same cut as the Python client.
    $flood = Invoke-InstalledSkill "logs $base -Service flood -Raw -Limit 200"
    $text = $script:lastOutput.TrimEnd()
    if ($text.Length -gt 16384 -or $text -cnotmatch '^[\x20-\x7E]+\z' -or -not $flood.budget_applied -or
        $flood.shortened_strings -ne 20 -or $flood.omitted.logs -ne 180 -or @($flood.logs).Count -ne 20 -or $flood.returned -ne 200 -or
        $flood.logs[0].tags.message -cne ('000 ' + ($eAcute * 96) + $ellipsis) -or $flood.logs[19].tags.chq_fingerprint -cne '1019' -or
        -not $flood.next[0].run.EndsWith("' logs $window -Service 'flood'")) {
        throw ('Unexpected budget for raw rows: ' + $text.Length + ' ' + $text.Substring(0, [Math]::Min(400, $text.Length)))
    }
    $wide = Invoke-InstalledSkill "logs $base -Service wide -Limit 150"
    $text = $script:lastOutput.TrimEnd()
    if ($text.Length -gt 16384 -or -not $wide.budget_applied -or $wide.omitted.patterns -ne 134 -or @($wide.patterns).Count -ne 16 -or
        $wide.matched_patterns -ne 150 -or $wide.patterns[0].exemplar.message -cne (($emoji * 100) + $ellipsis)) {
        throw ('Unexpected budget for patterns: ' + $text.Length)
    }

    # Whatever the length of echoed arguments, stdout stays within 16384 bytes
    # (test_budget_holds_for_long_arguments). Expressions build the long arguments.
    foreach ($size in 1500, 3000, 9000, 14000) {
        $grep = "('(a|' + ('b' * $($size - 4)) + ')')"
        $literal = "('c' * $size)"
        $cases = @(
            @("logs $base -Service shop -Grep $grep", $false),
            @("logs $base -Service wide -Limit 150 -Grep $grep", $true),
            @("logs $base -Service flood -Raw -Limit 200 -Grep $grep", $true),
            @(("logs $base -Query ('{{service_name=`"shop`"}} |~ `"' + {0} + '`"')" -f $literal), $false),
            @(("logs $base -Query ('{{service_name=`"flood`"}} !~ `"' + {0} + '`"') -Raw -Limit 200" -f $literal), $true),
            @("issues $base -Label 'scenario=services' -Contains $literal", $false),
            @(("tags $base -Query ('{{service_name=`"checkout`", x=`"' + {0} + '`"}}')" -f $literal), $false))
        foreach ($case in $cases) {
            $sized = Invoke-InstalledSkill $case[0]
            $echo = if ($null -ne $sized.query) { 'query' } else { 'scope' }
            if ($script:lastOutput.Length -gt 16384 -or $script:lastOutput -cnotmatch '^[\x00-\x7E]+\z' -or
                [bool]$sized.budget_applied -ne $case[1] -or
                ($null -ne $sized.$echo -and ([string]$sized.$echo).Length -gt 1900 -and ($sized.($echo + '_chars') -le 2048 -or -not $sized.$echo.EndsWith($ellipsis))) -or
                @(@($sized.next) | Where-Object { $null -ne $_ -and $_.run -cnotmatch '^Run (the same )?logs ' }).Count) {
                throw "Unexpected output for $size-character arguments ($($case[0].Substring(0, 30))): $($script:lastOutput.Length) bytes"
            }
        }
    }

    # -FullMessage returns a whole stack trace, drops rows before shortening messages, and
    # shortens a message only when one row alone does not fit.
    $stack = Invoke-InstalledSkill "logs $base -Service stack -Fingerprint 77 -Raw -Limit 1 -FullMessage"
    $trace = $stack.logs[0].tags.message
    if (-not $trace.StartsWith('Traceback') -or $trace.Length -lt 5000 -or $null -ne $stack.logs[0].message_chars -or $stack.budget_applied) {
        throw ('Unexpected full message: ' + $script:lastOutput.Substring(0, 300))
    }
    $stacks = Invoke-InstalledSkill "logs $base -Service stack -Limit 5 -FullMessage"
    if ($script:lastOutput.Length -gt 16384 -or $stacks.mode -cne 'raw' -or @($stacks.logs).Count -ne 2 -or $stacks.omitted.logs -ne 3 -or
        @($stacks.logs | Where-Object { $_.tags.message -cne $trace }).Count -or $null -ne $stacks.shortened_strings) {
        throw ('Unexpected full messages under the budget: ' + $script:lastOutput.Length)
    }
    $emojiStack = Invoke-InstalledSkill "logs $base -Service stack -Fingerprint 78 -FullMessage"
    if ($script:lastOutput.Length -gt 16384 -or $emojiStack.omitted.logs -ne 19 -or $emojiStack.shortened_strings -ne 1 -or
        $emojiStack.logs[0].message_chars -ne 9000 -or $emojiStack.logs[0].tags.message -cne (($emoji * 1230) + $ellipsis)) {
        throw ('Unexpected shortened full message: ' + $script:lastOutput.Length)
    }

    # Wide rows lose their extra fields before rows are dropped.
    $attrs = Invoke-InstalledSkill "logs $base -Service attrs -AllFields -Limit 200"
    if ($script:lastOutput.Length -gt 16384 -or @($attrs.logs).Count -ne 45 -or $attrs.omitted.fields -ne 44 -or $attrs.omitted.logs -ne 155 -or
        @($attrs.logs[0].tags.PSObject.Properties).Count -ne 154 -or @($attrs.logs[1].tags.PSObject.Properties).Count -ne 4 -or
        $attrs.logs[44].tags.chq_fingerprint -cne '44') { throw ('Unexpected wide rows: ' + $script:lastOutput.Length) }
    # 200 rows of 150 attributes, about 5 MB, within a few seconds. The first call warms the fixture.
    $null = Invoke-InstalledSkill "logs $base -Service perf -AllFields -Limit 200"
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $perf = Invoke-InstalledSkill "logs $base -Service perf -AllFields -Limit 200"
    $perfSeconds = $clock.Elapsed.TotalSeconds
    if ($perfSeconds -gt 8 -or $script:lastOutput.Length -gt 16384 -or @($perf.logs).Count -ne 58 -or $perf.omitted.fields -ne 58 -or
        $perf.omitted.logs -ne 142 -or $perf.shortened_strings -ne 58) { throw "Unexpected wide-row budget in $perfSeconds s: $($script:lastOutput.Length)" }
    "Wide-row budget took $perfSeconds s."

    # Service names and fingerprints are never shortened.
    $keep = Invoke-InstalledSkill "logs $base -Label 'scenario=keep' -Limit 40"
    if ($script:lastOutput.Length -gt 16384 -or @($keep.patterns).Count -ne 22 -or $keep.omitted.patterns -ne 18 -or
        @(for ($i = 0; $i -lt 22; $i++) { $keep.patterns[$i].service -ceq (('svc{0:D2}-' -f $i) + ('s' * 300)) -and
            $keep.patterns[$i].fingerprint -ceq (('1' * 150) + ('{0:D2}' -f $i)) }) -contains $false -or
        $keep.patterns[0].exemplar.message -cne (('k' * 100) + $ellipsis)) { throw ('Unexpected identifiers under the budget: ' + $script:lastOutput.Length) }

    # Without -End the window ends now, and counts include the newest bucket; an explicit end
    # keeps exact boundaries. 2.6 and 3.5 round to 3 and 4, and ties rank by service.
    $env:CARDINAL_QUERY_NOW = '2026-06-01T12:04:30.000Z'
    try {
        $open = Invoke-InstalledSkill "logs -Start '2026-06-01T12:00:00Z' -Service edge"
        $openIssues = Invoke-InstalledSkill "issues -Start '2026-06-01T12:00:00Z' -Service edge"
    }
    finally { Remove-Item Env:CARDINAL_QUERY_NOW }
    $ranking = { param($Items) (@($Items | ForEach-Object { "$($_.service)/$($_.fingerprint)=$($_.count)" }) -join ',') }
    if ($open.end -cne '2026-06-01T12:04:30.000Z' -or $open.total -ne 22 -or (& $ranking $open.patterns) -cne 'a/1=10,a/9=4,b/1=4,b/5=4' -or
        $open.patterns[0].first_seen -cne '2026-06-01T12:00:00.000Z' -or $open.patterns[0].last_seen -cne '2026-06-01T12:04:30.000Z' -or
        -not $open.note.Contains('the newest bucket runs to now') -or
        -not $open.next[0].run.EndsWith("' logs -Start '2026-06-01T12:00:00.000Z' -End '2026-06-01T12:04:30.000Z' -Service 'a' -Fingerprint '1' -Raw") -or
        $openIssues.total -ne 22 -or $openIssues.trend[-1].timestamp -ne ($bucketMs + 240000) -or
        (& $ranking $openIssues.patterns) -cne 'a/1=10,a/9=4,b/1=4,b/5=4') { throw ('Unexpected open-ended counts: ' + $script:lastOutput) }
    $exact = Invoke-InstalledSkill "logs -Start '2026-06-01T12:00:00Z' -End '2026-06-01T12:04:30Z' -Service edge"
    $exactIssues = Invoke-InstalledSkill "issues -Start '2026-06-01T12:00:00Z' -End '2026-06-01T12:04:30Z' -Service edge"
    $aligned = Invoke-InstalledSkill "logs $base -Service edge"
    if ($exact.total -ne 15 -or (& $ranking $exact.patterns) -cne 'a/9=4,b/1=4,b/5=4,a/1=3' -or
        -not $exact.note.Contains('the bucket that straddles the end is left out') -or $exactIssues.total -ne 15 -or
        $aligned.total -ne 22 -or $aligned.patterns[0].last_seen -cne '2026-06-01T12:05:00.000Z' -or
        $aligned.note -cne 'Counts are totals in fixed 60-second buckets. first_seen and last_seen are bucket bounds. Use raw rows for exact boundaries.') {
        throw ('Unexpected exact-boundary counts: ' + $script:lastOutput)
    }

    # Without fingerprint rollups, an unfiltered search scans raw logs.
    $rawOnly = Invoke-InstalledSkill "logs $base -Service rawonly"
    if ($rawOnly.total -ne 4 -or $rawOnly.patterns[0].fingerprint -cne '5' -or @($rawOnly.query_ids).Count -ne 4) { throw ('Unexpected raw-scan fallback: ' + $script:lastOutput) }

    # A raw selector that pins no service gets the pattern's service for exemplars and the drill-down.
    $scoped = Invoke-InstalledSkill "logs $base -Query '{scenario=`"services`"}' -Limit 1"
    if ($scoped.patterns[0].exemplar.message_chars -ne 500 -or $scoped.unlisted_patterns -ne 12 -or
        -not $scoped.next[0].run.EndsWith("-Query '{service_name=`"svc00`", scenario=`"services`"}' -Fingerprint '100' -Raw")) {
        throw ('Unexpected raw-selector drill-down: ' + $script:lastOutput)
    }
    $scopedRows = Invoke-SkillCommand $scoped.next[0].run
    if ($scopedRows.returned -ne 1 -or $scopedRows.query -cne '{chq_fingerprint="100", service_name="svc00", scenario="services"}') { throw 'Unexpected raw-selector rows.' }
    $unlisted = Invoke-InstalledSkill "logs $base -Service shop -Limit 1"
    $unlistedIssues = Invoke-InstalledSkill "issues $base -Label 'scenario=services' -Limit 10 -Samples 0"
    if ($unlisted.unlisted_patterns -ne 3 -or $unlisted.unlisted_events -ne 12 -or
        $unlistedIssues.unlisted_patterns -ne 3 -or $unlistedIssues.unlisted_events -ne 4) { throw 'Unexpected unlisted counts.' }

    # The serializer writes numbers and strings as the Python client does
    # (test_serializer_matches_the_powershell_client), on .NET Framework as on .NET.
    $null = Invoke-InstalledSkill "logs $base -Service exact -AllFields"
    foreach ($expected in '"f1":0.7999999999999999', '"f2":0.30000000000000004', '"f3":1.25', '"f4":-0.5', '"i1":9007199254740993',
        '"s1":"del\u007f sep\u2028 nul\u0000"') {
        if (-not $script:lastOutput.Contains($expected)) { throw "Missing $expected in $script:lastOutput" }
    }
    $ast = [Management.Automation.Language.Parser]::ParseFile($installedScript, [ref]$null, [ref]$null)
    $definitions = [Collections.Generic.List[string]]::new()
    foreach ($statement in $ast.EndBlock.Statements) {
        if ($statement.Extent.Text.StartsWith('$client = $null')) { break }
        $definitions.Add($statement.Extent.Text)
    }
    $serialized = & {
        . ([scriptblock]::Create(($definitions -join [Environment]::NewLine)))
        $values = @([BitConverter]::Int64BitsToDouble(0x3FE9999999999999), [BitConverter]::Int64BitsToDouble(0x3FD3333333333334),
            [BitConverter]::Int64BitsToDouble(1), [BitConverter]::Int64BitsToDouble([long]::MinValue),
            [BitConverter]::Int64BitsToDouble(0x3E7AD7F29ABCAF48), [BitConverter]::Int64BitsToDouble(0x4415AF1D78B58C40),
            [BitConverter]::Int64BitsToDouble(0x3E70000000000000), [BitConverter]::Int64BitsToDouble(0x43B0000000000000),
            [long]9007199254740993, [decimal]::Parse('18446744073709551616', [Globalization.CultureInfo]::InvariantCulture),
            [bigint]::Pow(10, 29), [double]::NaN, [double]::PositiveInfinity, [double]::NegativeInfinity,
            ([string][char]0x7F + [char]0x2028 + [char]0))
        ConvertTo-JsonText $values
        Convert-LogQLString ("o'k<>&" + [char]1 + [char]0x7F)
        # Long windows bound their buckets (test_long_windows_bound_their_buckets).
        (@(8, 120, 121, 364, 3650) | ForEach-Object { ConvertTo-JsonText (Get-CountStep ($_ * 1440)) }) -join ','
    }
    $expectedJson = '[0.7999999999999999,0.30000000000000004,5e-324,-0.0,1e-07,1e+20,5.960464477539063e-08,1.152921504606847e+18,' +
        '9007199254740993,18446744073709551616,' +
        '100000000000000000000000000000,NaN,Infinity,-Infinity,"\u007f\u2028\u0000"]'
    if ($serialized[0] -cne $expectedJson -or $serialized[1] -cne ('"o''k<>&\u0001' + [char]0x7F + '"') -or
        $serialized[2] -cne '86400,86400,172800,345600,2678400') {
        throw "Unexpected serializer output: $serialized"
    }

    # Issues caps service_counts to ten services plus an other total.
    $capped = Invoke-InstalledSkill "issues $base -Label 'scenario=services'"
    if ($capped.total -ne 79 -or @($capped.service_counts).Count -ne 10 -or $capped.service_counts[0].service -cne 'svc00' -or
        $capped.service_counts[0].total -ne 13 -or $capped.service_counts_other.services -ne 2 -or
        $capped.service_counts_other.total -ne 3 -or $capped.service_counts_other.error -ne 3 -or
        $capped.patterns[0].sample.message -cne (('x' * 300) + $ellipsis) -or $capped.patterns[0].sample.message_chars -ne 500 -or
        $capped.patterns[11].service -cne 'svc00' -or $capped.patterns[12].service -cne 'svc11' -or
        -not $capped.next[0].run.EndsWith("' logs $window -Service 'svc00' -Level 'error' -Label 'scenario=services' -Fingerprint '100' -Raw")) {
        throw ('Unexpected capped Issues: ' + $script:lastOutput)
    }

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
    $long = Invoke-InstalledSkill "values $base -Tag long"
    $expected = @('2026-11-16T10:00:00.000+00:00', '2026-11-16T10:00:00Z', ('a' + ($emoji * 255) + $ellipsis),
        (('y' * 256) + $ellipsis), (('y' * 256) + $ellipsis))
    if ($long.returned -ne 5 -or $long.limit_reached -or $long.shortened_values -ne 3 -or @($long.values).Count -ne 5 -or
        (@(for ($i = 0; $i -lt 5; $i++) { $long.values[$i] -ceq $expected[$i] }) -contains $false) -or
        (@($long.values) -join '').Contains([string][char]0xFFFD)) {
        throw ('Unexpected long tag values: ' + (ConvertTo-Json $long -Compress))
    }
    # 400 UTF-16 code units but 200 code points: under the limit, and returned quickly.
    $short = Invoke-InstalledSkill "values $base -Tag emoji"
    if (@($short.values).Count -ne 1 -or $short.values[0] -cne ($emoji * 200) -or $null -ne $short.shortened_values) {
        throw ('Unexpected emoji tag value: ' + $script:lastOutput)
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
    $flaky = Invoke-InstalledSkill "logs $base -Service flaky -Raw"
    if ($flaky.logs[0].tags.message -cne 'attempt 2' -or $script:lastErrors -notmatch 'retrying after HTTP 503 \(attempt 2 of 3\)') {
        throw ('Unexpected retry after HTTP 503: ' + $script:lastErrors)
    }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $busy = Invoke-InstalledSkill "logs $base -Service busy -Raw"
    if ($busy.logs[0].tags.message -cne 'attempt 2' -or $clock.ElapsedMilliseconds -lt 1000 -or
        $script:lastErrors -notmatch 'retrying after HTTP 429') { throw 'Retry-After was not honored.' }
    Invoke-InstalledSkill "logs $base -Service down" 'HTTP 503\. upstream unavailable Failed after 3 attempts\. Query ID'
    Invoke-InstalledSkill "logs $base -Service down -Retries 0" 'HTTP 503\. upstream unavailable Query ID'
    if ($script:lastErrors -match 'retrying|attempts') { throw '-Retries 0 retried.' }
    Invoke-InstalledSkill "logs $base -Service broken" 'HTTP 500\. worker failed Query ID'
    if ($script:lastErrors -match 'retrying') { throw 'HTTP 500 was retried.' }
    Invoke-InstalledSkill "logs $base -Service drop -Raw" 'ended before completion\. Query ID'
    if ($script:lastErrors -match 'retrying') { throw 'A stream that returned a row was retried.' }

    $empty = Invoke-InstalledSkill "logs $base -Service missing -Raw"
    if ($empty.returned -ne 0 -or $empty.logs.Count -ne 0) { throw 'Unexpected empty result.' }
    # Partial results fail closed in both modes.
    Invoke-InstalledSkill "logs $base -Service partial" 'Results may be incomplete'
    Invoke-InstalledSkill "logs $base -Service partial -Raw" 'Results may be incomplete'
    Invoke-InstalledSkill "logs -Since 10s -End '2026-06-01T12:00:30Z'" 'no completed bucket'
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
    'PASS: installed skill ran log patterns, raw rows, the output budget, full messages, bucket edges, Issues, fingerprint drill-down, tag discovery, retries, and API error handling with Windows PowerShell 5.1 and no Python or pwsh on PATH.'

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
