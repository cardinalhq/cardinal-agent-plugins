#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('logs', 'issues', 'tags', 'values')][string]$Command,
    [string]$Since, [string]$Start, [string]$End, [string]$Service,
    [string[]]$Label, [string[]]$Contains, [string[]]$NotContains,
    [string[]]$Grep, [string[]]$NotGrep,
    [ValidateRange(1, 2147483647)][int]$Limit = 20,
    [ValidateRange(1, 3600)][int]$Timeout = 90,
    [string]$Query, [string[]]$Field,
    [ValidateSet('warn', 'error', 'fatal')][string]$Level,
    [ValidateSet('warn', 'error', 'fatal')][string]$Severity,
    [string]$Fingerprint,
    [ValidateRange(0, 20)][int]$Samples = 5,
    [string]$Tag, [string]$Match,
    [ValidateRange(0, 5)][int]$Retries = 2
)

$ErrorActionPreference = 'Stop'
$levels = @{ fatal = 'fatal'; error = 'error'; warn = 'warn|warning' }
$coreFields = @('service_name', 'level', 'message', 'chq_fingerprint')
# The Query API stores dotted OpenTelemetry names with underscores. Use \z:
# in .NET, $ also matches before a final newline.
$labelName = '^[A-Za-z_][A-Za-z0-9_.]*\z'
$utc = [Globalization.CultureInfo]::InvariantCulture
$timedOut = 'Query timed out. Try a shorter time range.'
# Transient failures: rate limiting and gateway errors. HTTP 500 is a query error.
$retryStatuses = @(429, 502, 503, 504)
$maxRetryWaitMs = 8000.0
$random = [Random]::new()
# Undocumented test hook. The default first backoff is 0.5 seconds.
$retryBaseMs = 500.0
if ([string]$env:CARDINAL_QUERY_RETRY_BASE_MS -cmatch '^\s*[0-9]{1,9}\s*\z' -and [int]$env:CARDINAL_QUERY_RETRY_BASE_MS -gt 0) {
    $retryBaseMs = [double][int]$env:CARDINAL_QUERY_RETRY_BASE_MS
}

function Convert-QueryTime([string]$Value) {
    if ($Value -cmatch '^[0-9]{10}\z') { return [DateTimeOffset]::FromUnixTimeSeconds([long]$Value) }
    if ($Value -cmatch '^[0-9]{13}\z') { return [DateTimeOffset]::FromUnixTimeMilliseconds([long]$Value) }
    if ($Value -cnotmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})\z') {
        throw 'Use an RFC 3339 timestamp with a timezone or Unix seconds/milliseconds.'
    }
    return [DateTimeOffset]::Parse($Value, $utc)
}

function Convert-LogQLString([string]$Value) {
    # ConvertTo-Json can escape characters that the LogQL parser also accepts.
    return (ConvertTo-Json -InputObject $Value -Compress)
}

function New-Selector([string]$Name, [string]$LevelPattern, [string]$PatternID, [bool]$AnyService = $true) {
    $matchers = @(
        if ($Name) { 'service_name=' + (Convert-LogQLString $Name) }
        elseif ($AnyService) { 'service_name=~".+"' }
        if ($LevelPattern) { 'level=~"(?i)^(?:' + $LevelPattern + ')$"' }
        foreach ($item in $Label) {
            if ($item -cnotmatch '^([A-Za-z_][A-Za-z0-9_.]*)=([\s\S]*)\z') {
                throw 'Expected LABEL=VALUE with a valid LogQL label name.'
            }
            $Matches[1].Replace('.', '_') + '=' + (Convert-LogQLString $Matches[2])
        }
        if ($PatternID) { 'chq_fingerprint=' + (Convert-LogQLString $PatternID) }
    )
    if (-not $matchers.Count) { return '' }
    return '{' + ($matchers -join ', ') + '}'
}

function New-LineFilters([string]$Flags) {
    $clauses = @(
        foreach ($item in $Contains) {
            $literal = $item -creplace '([\\.^$|?*+()\[\]{}])', '\$1'
            ' |~ ' + (Convert-LogQLString "(?${Flags}:.*$literal.*)")
        }
        foreach ($item in $Grep) { ' |~ ' + (Convert-LogQLString $item) }
        foreach ($item in $NotContains) {
            $literal = $item -creplace '([\\.^$|?*+()\[\]{}])', '\$1'
            ' !~ ' + (Convert-LogQLString "(?${Flags}:.*$literal.*)")
        }
        foreach ($item in $NotGrep) { ' !~ ' + (Convert-LogQLString $item) }
    )
    return ($clauses -join '')
}

function New-QueryRequest($Payload, [string]$Path, [string]$Accept, [string]$QueryID) {
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, $apiBase + $Path)
    $request.Headers.Add('X-CardinalHQ-API-Key', $key)
    $request.Headers.Add('X-CardinalHQ-Query-Id', $QueryID)
    $request.Headers.Accept.ParseAdd($Accept)
    $request.Content = [System.Net.Http.StringContent]::new(
        (ConvertTo-Json -InputObject $Payload -Depth 8 -Compress), [Text.Encoding]::UTF8, 'application/json')
    return $request
}

function New-QueryException([string]$Message, [string]$Retry, $RetryAfter) {
    # Data carries the transient cause, if any, to Wait-QueryRetry.
    $exception = [Exception]::new($Message)
    if ($Retry) {
        $exception.Data['retry'] = $Retry
        $exception.Data['retryAfter'] = $RetryAfter
    }
    return $exception
}

function Send-QueryRequest($Request, $Completion, $Clock) {
    # Bound each attempt by the time left in the request's -Timeout budget.
    $remaining = ($Timeout * 1000) - [int]$Clock.ElapsedMilliseconds
    if ($remaining -le 0) { throw $timedOut }
    $cancel = [Threading.CancellationTokenSource]::new([TimeSpan]::FromMilliseconds($remaining))
    try { return $client.SendAsync($Request, $Completion, $cancel.Token).GetAwaiter().GetResult() }
    finally { $cancel.Dispose() }
}

function Get-QueryFailure($ErrorRecord) {
    $exception = $ErrorRecord.Exception
    while (($exception -is [Management.Automation.MethodInvocationException] -or $exception -is [AggregateException]) -and
        $exception.InnerException) {
        $exception = $exception.InnerException
    }
    if ($exception.Data.Contains('retry')) {
        return @{ message = $exception.Message; retry = $exception.Data['retry']; retryAfter = $exception.Data['retryAfter'] }
    }
    # HttpClient reports a timeout as a canceled task.
    if ($exception -is [OperationCanceledException]) { return @{ message = $timedOut; retry = 'a timeout' } }
    if ($exception -is [System.Net.Http.HttpRequestException] -or $exception -is [IO.IOException] -or
        $exception -is [Net.WebException] -or $exception -is [Net.Sockets.SocketException]) {
        $inner = $exception
        while ($inner.InnerException) { $inner = $inner.InnerException }
        return @{ message = 'Query API connection failed: ' + $inner.Message; retry = 'a connection error' }
    }
    return @{ message = $exception.Message }
}

function Wait-QueryRetry($Failure, [int]$Attempt, $Clock, [string]$QueryID, [bool]$Retryable) {
    # Wait before the next attempt, or throw the final error.
    $attempts = $Retries + 1
    if ($Retryable -and $Failure.retry -and $Attempt -lt $attempts) {
        if ($null -ne $Failure.retryAfter) { $wait = [Math]::Min([double]$Failure.retryAfter * 1000, $maxRetryWaitMs) }
        else { $wait = $random.NextDouble() * [Math]::Min($maxRetryWaitMs, $retryBaseMs * [Math]::Pow(2, $Attempt - 1)) }
        if ($wait -lt (($Timeout * 1000) - $Clock.ElapsedMilliseconds)) {
            [Console]::Error.WriteLine("cardinal-query: retrying after $($Failure.retry) (attempt $($Attempt + 1) of $attempts)")
            Start-Sleep -Milliseconds ([int][Math]::Floor($wait))
            return
        }
    }
    $message = ([string]$Failure.message).Replace($key, '[redacted]')
    if ($Attempt -gt 1) { $message += " Failed after $Attempt attempts." }
    throw "$message Query ID: $QueryID"
}

function Get-ResponseQueryID($Response, [string]$QueryID) {
    $serverID = [Guid]::Empty
    if ($Response.Headers.Contains('X-CardinalHQ-Query-Id') -and
        [Guid]::TryParse(($Response.Headers.GetValues('X-CardinalHQ-Query-Id') | Select-Object -First 1), [ref]$serverID)) {
        return $serverID.ToString()
    }
    return $QueryID
}

function Assert-QuerySuccess($Response) {
    if ($Response.IsSuccessStatusCode) { return }
    $status = [int]$Response.StatusCode
    $hint = switch ($status) {
        { $_ -in 401, 403 } { ' Check that the key has query access to this Data Lake.' }
        { $_ -eq 404 -or ($_ -ge 300 -and $_ -lt 400) } { ' Check CARDINAL_QUERY_URL for the direct Query API endpoint.' }
        default {
            # Query endpoints return JSON errors; tag endpoints return plain text.
            $details = ''
            try {
                # Read at most 4 KB for up to 5 seconds so a trickling error body cannot hang the client.
                $stream = $Response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
                $buffer = New-Object byte[] 4096
                $total = 0
                $bodyClock = [Diagnostics.Stopwatch]::StartNew()
                while ($total -lt $buffer.Length) {
                    $left = 5000 - [int]$bodyClock.ElapsedMilliseconds
                    if ($left -le 0) { break }
                    $chunk = $stream.ReadAsync($buffer, $total, $buffer.Length - $total)
                    if (-not $chunk.Wait($left) -or $chunk.Result -le 0) { break }
                    $total += $chunk.Result
                }
                $details = [Text.Encoding]::UTF8.GetString($buffer, 0, $total)
                try {
                    $parsed = ConvertFrom-Json -InputObject $details
                    if ($parsed.message -is [string]) { $details = $parsed.message }
                }
                catch { }
            }
            catch { $details = '' }
            $details = ($details -replace '\s+', ' ').Trim()
            if ($details.Length -gt 500) { $details = $details.Substring(0, 500) }
            if ($details) { ' ' + $details } else { '' }
        }
    }
    $retry = $null
    $retryAfter = $null
    if ($status -in $retryStatuses) {
        $retry = "HTTP $status"
        # Honor the delay-seconds form only; an HTTP-date falls back to backoff.
        if ($status -in 429, 503 -and $Response.Headers.RetryAfter -and $null -ne $Response.Headers.RetryAfter.Delta) {
            $retryAfter = [Math]::Floor($Response.Headers.RetryAfter.Delta.TotalSeconds)
        }
    }
    throw (New-QueryException "Query API returned HTTP ${status}.$hint" $retry $retryAfter)
}

function Get-JsonText([string]$Json, [object[]]$Path) {
    # PowerShell 7 converts date-like JSON strings to DateTime. Read the original
    # text with System.Text.Json, which Windows PowerShell 5.1 does not need.
    if ($PSVersionTable.PSEdition -ne 'Core') { return $null }
    $document = ('System.Text.Json.JsonDocument' -as [type])::Parse($Json, (New-Object 'System.Text.Json.JsonDocumentOptions'))
    try {
        $element = $document.RootElement
        foreach ($step in $Path) {
            if ($step -is [int]) { $element = $element[$step] } else { $element = $element.GetProperty($step) }
        }
        if ([string]$element.ValueKind -ceq 'String') { return $element.GetString() }
        return $null
    }
    catch { return $null }
    finally { $document.Dispose() }
}

function Get-TagValue($Data, [string]$Json) {
    $value = $Data.value
    if ($value -isnot [string] -and $null -ne $value) { $value = Get-JsonText $Json @('data', 'value') }
    if ($value -isnot [string]) { throw 'Invalid Query API tag value.' }
    return $value
}

function Invoke-Query($Payload, $IDs, [string]$Path = '/logs/query', [switch]$TagValues) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    for ($attempt = 1; ; $attempt++) {
        $queryID = [Guid]::NewGuid().ToString()
        $request = New-QueryRequest $Payload $Path 'text/event-stream' $queryID
        $response = $null
        $reader = $null
        $yielded = $false
        try {
            $response = Send-QueryRequest $request ([System.Net.Http.HttpCompletionOption]::ResponseHeadersRead) $clock
            $queryID = Get-ResponseQueryID $response $queryID
            Assert-QuerySuccess $response
            if ($response.Content.Headers.ContentType.MediaType -ne 'text/event-stream') {
                throw 'Expected a Query API event stream. Check CARDINAL_QUERY_URL.'
            }
            $reader = [IO.StreamReader]::new($response.Content.ReadAsStreamAsync().GetAwaiter().GetResult())
            while ($true) {
                $remaining = ($Timeout * 1000) - [int]$clock.ElapsedMilliseconds
                if ($remaining -le 0) { throw $timedOut }
                $read = $reader.ReadLineAsync()
                if (-not $read.Wait($remaining)) { throw $timedOut }
                $line = $read.Result
                if ($null -eq $line) { throw (New-QueryException 'Query API stream ended before completion.' 'an interrupted stream') }
                if (-not $line.StartsWith('data:', [StringComparison]::Ordinal)) { continue }
                $json = $line.Substring(5)
                $event = ConvertFrom-Json -InputObject $json
                if ($null -eq $event.data) { throw 'Invalid Query API event.' }
                switch -CaseSensitive ($event.type) {
                    'result' {
                        $row = if ($TagValues) { Get-TagValue $event.data $json } else { $event.data }
                        if (-not $yielded) {
                            # Never retry once the caller has a row.
                            $yielded = $true
                            $IDs.Add($queryID)
                        }
                        $row
                    }
                    'error' {
                        $message = $event.data.message
                        if ($message -isnot [string] -or -not $message.Trim()) { $message = 'Query execution failed.' }
                        throw $message
                    }
                    'done' {
                        if ($event.data.status -ceq 'ok') {
                            if (-not $yielded) { $IDs.Add($queryID) }
                            return
                        }
                        throw 'Query API reported a failure. Results may be incomplete.'
                    }
                }
            }
        }
        catch {
            Wait-QueryRetry (Get-QueryFailure $_) $attempt $clock $queryID (-not $yielded)
        }
        finally {
            if ($reader) { $reader.Dispose() }
            if ($response) { $response.Dispose() }
            $request.Dispose()
        }
    }
}

function Invoke-TagNames($Payload, $IDs) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    for ($attempt = 1; ; $attempt++) {
        $queryID = [Guid]::NewGuid().ToString()
        $request = New-QueryRequest $Payload '/logs/tags' 'application/json' $queryID
        $response = $null
        try {
            $response = Send-QueryRequest $request ([System.Net.Http.HttpCompletionOption]::ResponseContentRead) $clock
            $queryID = Get-ResponseQueryID $response $queryID
            Assert-QuerySuccess $response
            if ($response.Content.Headers.ContentType.MediaType -ne 'application/json') {
                throw 'Expected a JSON tag list. Check CARDINAL_QUERY_URL.'
            }
            $json = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            $body = ConvertFrom-Json -InputObject $json
            if ($null -eq $body -or $body -is [array] -or $body.PSObject.Properties.Name -notcontains 'tags') {
                throw 'Invalid Query API tag list.'
            }
            # The server encodes an empty rollup as null; @($null) would have one element.
            $tags = $body.tags
            if ($null -eq $tags) { $tags = @() }
            if ($tags -isnot [array]) { throw 'Invalid Query API tag list.' }
            $names = for ($index = 0; $index -lt $tags.Count; $index++) {
                $name = $tags[$index]
                if ($name -isnot [string] -and $null -ne $name) { $name = Get-JsonText $json @('tags', $index) }
                if ($name -isnot [string]) { throw 'Invalid Query API tag list.' }
                $name
            }
            $IDs.Add($queryID)
            return ,@($names)
        }
        catch {
            Wait-QueryRetry (Get-QueryFailure $_) $attempt $clock $queryID $true
        }
        finally {
            if ($response) { $response.Dispose() }
            $request.Dispose()
        }
    }
}

function Limit-Text([string]$Value) {
    # Count Unicode code points, as the Python client does, and never split a
    # surrogate pair. Returns $null when the value is short enough.
    $points = 0
    for ($index = 0; $index -lt $Value.Length; $index++) {
        if ($points -eq 256) { return $Value.Substring(0, $index) + [char]0x2026 }
        if ([char]::IsHighSurrogate($Value[$index]) -and $index + 1 -lt $Value.Length -and
            [char]::IsLowSurrogate($Value[$index + 1])) { $index++ }
        $points++
    }
    return $null
}

function Get-DiscoveryScope {
    if ($Query) {
        $selector = $Query.Trim()
        if (-not $selector.StartsWith('{') -or -not $selector.EndsWith('}')) {
            throw '-Query for tags and values expects a LogQL stream selector such as {service_name="checkout"}.'
        }
        return $selector
    }
    $levelPattern = if ($Level) { $levels[$Level] }
    return (New-Selector $Service $levelPattern '' $false)
}

function Get-TagNames($Window, $IDs) {
    $scope = Get-DiscoveryScope
    $payload = @{ s = $Window.start; e = $Window.end }
    if ($scope) { $payload.q = $scope }
    $listed = [Collections.Generic.SortedSet[string]]::new([StringComparer]::Ordinal)
    foreach ($name in (Invoke-TagNames $payload $IDs)) { $null = $listed.Add($name) }
    $names = @($listed | Where-Object { -not $Match -or $_.IndexOf($Match, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
    $source = 'org_daily_rollup'
    if ($scope) { $source = 'matching_rows' } else { $scope = $null }
    $result = @{
        scope = $scope; source = $source
        query_ids = $IDs.ToArray(); matched = $names.Count
        returned = [Math]::Min($names.Count, $Limit); truncated = $names.Count -gt $Limit
        tags = @($names | Select-Object -First $Limit)
        core_fields_not_listed = @($coreFields | Where-Object { -not $listed.Contains($_) })
    }
    if ($null -eq $scope) {
        $result.note = 'Unscoped names come from a daily rollup for the whole organization and may include ' +
            'fields with no logs in this window. Scope with -Service or -Label for names on matching rows.'
    }
    return $result
}

function Get-TagValues($Window, $IDs) {
    if (-not $Tag -or $Tag -cnotmatch $labelName) {
        throw 'Expected -Tag with a label name such as service_name or k8s_namespace_name.'
    }
    $tagName = $Tag.Replace('.', '_')
    if ($tagName -ceq 'message') { throw 'Use logs or issues to read messages; values lists label values.' }
    if ($Limit -gt 1000) { throw 'Use -Limit 1000 or less with values.' }
    if ($Match -and [Text.Encoding]::UTF8.GetByteCount($Match) -gt 256) { throw '-Match must be at most 256 bytes.' }
    $scope = Get-DiscoveryScope
    $payload = @{ s = $Window.start; e = $Window.end; limit = $Limit }
    if ($scope) { $payload.q = $scope }
    if ($Match) { $payload.contains = $Match }
    # De-duplicate full values; shorten only for display, so counts stay exact.
    $distinct = [Collections.Generic.SortedSet[string]]::new([StringComparer]::Ordinal)
    Invoke-Query $payload $IDs ('/logs/tagvalues?tagName=' + [Uri]::EscapeDataString($tagName)) -TagValues | ForEach-Object {
        if ($distinct.Count -lt $Limit) { $null = $distinct.Add($_) }
    }
    $shortened = 0
    $values = @(foreach ($value in $distinct) {
        $short = Limit-Text $value
        if ($null -eq $short) { $value } else { $shortened++; $short }
    })
    # The server answers an unscoped request without -Match from a daily
    # org-wide rollup when it tracks the label, and scans rows otherwise.
    $rollup = -not $scope -and -not $Match
    if (-not $scope) { $scope = $null }
    $result = @{
        tag = $tagName; scope = $scope; query_ids = $IDs.ToArray()
        source = if ($rollup) { 'org_daily_rollup' } else { 'matching_rows' }
        limit = $Limit; returned = $distinct.Count; limit_reached = $distinct.Count -ge $Limit; values = $values
    }
    if ($shortened) { $result.shortened_values = $shortened }
    if ($rollup) {
        $result.note = 'Unscoped values usually come from a daily rollup for the whole organization and may ' +
            'include values with no logs in this window. Scope with -Service or -Label for values on matching rows.'
    }
    return $result
}

function Get-IssueStep([double]$Minutes) {
    if ($Minutes -le 30) { return 60 }
    if ($Minutes -le 120) { return 300 }
    if ($Minutes -le 480) { return 1200 }
    if ($Minutes -le 1440) { return 3600 }
    if ($Minutes -le 4320) { return 7200 }
    if ($Minutes -le 10080) { return 21600 }
    return 86400
}

function Get-IssuePoints($Window, $IDs, [string]$Selector, [string]$Filters, [int]$Step) {
    $query = "sum by (service_name, level, chq_fingerprint)(count_over_time($Selector${Filters}[${Step}s]))"
    $found = $false
    Invoke-Query @{ q = $query; s = $Window.start; e = $Window.end; step = $Step } $IDs | ForEach-Object {
        $found = $true
        $_
    }
    if (-not $found -and -not $Filters) {
        # Bypass materialized counts if the deployment only has raw logs.
        $query = 'sum by (service_name, level, chq_fingerprint)(count_over_time({0} |~ "(?s:.*)"[{1}s]))' -f $Selector, $Step
        Invoke-Query @{ q = $query; s = $Window.start; e = $Window.end; step = $Step } $IDs
    }
}

function Get-IssuePatterns($Window, $IDs, [DateTimeOffset]$First, [DateTimeOffset]$Last) {
    $levelPattern = if ($Severity) { $levels[$Severity] } else { 'fatal|error|warn|warning' }
    $step = Get-IssueStep ($Last - $First).TotalMinutes
    $startMs = $First.ToUnixTimeMilliseconds()
    $endMs = $Last.ToUnixTimeMilliseconds()
    if ([Math]::Floor($startMs / ($step * 1000)) -eq [Math]::Floor($endMs / ($step * 1000))) {
        throw 'The Issues window has no completed bucket. Use a wider window or search raw logs.'
    }
    $filters = New-LineFilters 'is'
    $selector = New-Selector $Service $levelPattern $Fingerprint
    $services = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    $patterns = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    $trend = @{}
    $warnings = [Collections.Generic.List[string]]::new()
    $total = 0L
    Get-IssuePoints $Window $IDs $selector $filters $step | ForEach-Object {
        $bucket = [long]$_.timestamp
        $name = [string]$_.tags.service_name
        $severityName = ([string]$_.tags.level).ToLowerInvariant()
        if ($severityName -eq 'warning') { $severityName = 'warn' }
        if ($bucket -gt $startMs -and $bucket -le $endMs -and $name -and
            $levels.ContainsKey($severityName) -and $_.value -is [ValueType] -and $_.value -gt 0) {
            $count = [long][Math]::Round([double]$_.value)
            $total += $count
            if (-not $services.ContainsKey($name)) { $services[$name] = @{service = $name; warn = 0L; error = 0L; fatal = 0L; total = 0L} }
            if (-not $trend.ContainsKey($bucket)) { $trend[$bucket] = @{timestamp = $bucket; warn = 0L; error = 0L; fatal = 0L} }
            $services[$name][$severityName] += $count
            $services[$name].total += $count
            $trend[$bucket][$severityName] += $count
            $id = [string]$_.tags.chq_fingerprint
            if ($id -and $id -ne '0') {
                $identity = ConvertTo-Json -InputObject @($name, $severityName, $id) -Compress
                if (-not $patterns.ContainsKey($identity)) {
                    $patterns[$identity] = @{service = $name; severity = $severityName; fingerprint = $id; count = 0L}
                }
                $patterns[$identity].count += $count
            }
        }
    }
    $ranked = @($patterns.Values | Sort-Object { -$_.count }, { $_.service }, { $_.severity }, { $_.fingerprint } | Select-Object -First $Limit)
    if ($Samples -gt 0) {
        foreach ($pattern in ($ranked | Select-Object -First $Samples)) {
            try {
                $sampleQuery = (New-Selector $pattern.service $levels[$pattern.severity] $pattern.fingerprint) + $filters
                $rows = @(Invoke-Query @{
                    q = $sampleQuery; s = $Window.start; e = $Window.end; limit = 1; reverse = $true
                    fields = @('message', 'service_name', 'level', 'chq_fingerprint')
                } $IDs)
                if ($rows.Count) { $pattern.sample = @{message = $rows[0].tags.message; timestamp = $rows[0].timestamp} }
            }
            catch { $warnings.Add("Could not load sample for $($pattern.fingerprint): $($_.Exception.Message)") }
        }
    }
    return @{
        query_ids = $IDs.ToArray(); step_seconds = $step; total = $total
        service_counts = @($services.Values | Sort-Object { -$_.total }, { $_.service })
        trend = @($trend.Values | Sort-Object { $_.timestamp })
        matched_patterns = $patterns.Count; patterns = $ranked; warnings = $warnings.ToArray()
    }
}

$client = $null
$key = ([string]$env:CARDINAL_QUERY_API_KEY).Trim()
try {
    $url = ([string]$env:CARDINAL_QUERY_URL).Trim()
    if (-not $url -or -not $key) {
        throw 'Set CARDINAL_QUERY_URL and CARDINAL_QUERY_API_KEY in the agent environment.'
    }
    if ($key -match '[\r\n]') { throw 'CARDINAL_QUERY_API_KEY must be a single line.' }
    $uri = $null
    if (-not [Uri]::TryCreate($url, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -notin 'http', 'https' -or -not $uri.Host -or $uri.UserInfo -or $url -match '[?#]') {
        throw 'Set CARDINAL_QUERY_URL to the Data Lake Query API URL.'
    }
    if ($uri.Scheme -ne 'https' -and $uri.Host -notin 'localhost', '127.0.0.1', '[::1]') {
        throw 'Use HTTPS for remote Query API URLs.'
    }
    $apiBase = ($url.TrimEnd('/') -creplace '/api/v1/logs/query\z', '' -creplace '/api/v1\z', '') + '/api/v1'
    if ($Since -and $Start) { throw 'Choose either -Since or -Start.' }
    if ($Fingerprint) {
        $id = 0L
        if ($Fingerprint -cnotmatch '^-?[0-9]+\z' -or -not [long]::TryParse($Fingerprint, [ref]$id)) {
            throw 'Expected a signed 64-bit fingerprint.'
        }
        $Fingerprint = $id.ToString($utc)
    }
    if ($Command -eq 'logs' -and ($Severity -or $Fingerprint -or $PSBoundParameters.ContainsKey('Samples'))) {
        throw 'Use -Severity, -Fingerprint and -Samples with issues.'
    }
    if ($Command -eq 'issues' -and ($Level -or $Query -or $Field)) {
        throw 'Use -Level, -Query and -Field with logs.'
    }
    $discovery = $Command -in 'tags', 'values'
    if ($discovery -and ($Severity -or $Fingerprint -or $Field -or $Contains -or $NotContains -or $Grep -or $NotGrep -or
        $PSBoundParameters.ContainsKey('Samples'))) {
        throw 'Use -Service, -Label, -Level or -Query to scope tags and values; use -Match to filter the list.'
    }
    if (-not $discovery -and ($Tag -or $Match)) { throw 'Use -Tag and -Match with tags or values.' }
    if ($Command -eq 'tags' -and $Tag) { throw 'Use -Tag with values.' }
    if ($discovery -and -not $PSBoundParameters.ContainsKey('Limit')) { $Limit = if ($Command -eq 'tags') { 200 } else { 50 } }
    if ($Query -and ($Service -or $Level -or $Label)) {
        throw 'Put service, level and label matchers in -Query when using a raw selector.'
    }
    if ($Command -eq 'logs' -and $Query -and -not $Query.TrimStart().StartsWith('{')) {
        throw "-Query expects a raw LogQL stream selector beginning with '{'."
    }

    $last = if ($End) { Convert-QueryTime $End } else { [DateTimeOffset]::UtcNow }
    $first = if ($Start) { Convert-QueryTime $Start } else {
        if (-not $Since) { $Since = '1h' }
        if ($Since -cnotmatch '^([1-9][0-9]*)([smhdw])\z') { throw 'Use a duration such as 30m, 2h, or 1d for -Since.' }
        $units = @{ s = 1; m = 60; h = 3600; d = 86400; w = 604800 }
        $last.AddSeconds(-([double]$Matches[1] * $units[$Matches[2]]))
    }
    if ($first.ToUnixTimeMilliseconds() -ge $last.ToUnixTimeMilliseconds()) {
        throw 'The start time must be earlier than the end time.'
    }
    $format = "yyyy-MM-dd'T'HH:mm:ss.fff'Z'"
    $window = @{ start = $first.UtcDateTime.ToString($format, $utc); end = $last.UtcDateTime.ToString($format, $utc) }

    Add-Type -AssemblyName System.Net.Http
    # Hardened Windows images can leave .NET Framework without TLS 1.2. Add it unless the
    # process already defers to the operating system (SystemDefault, value 0).
    $protocols = [Net.ServicePointManager]::SecurityProtocol
    if ([int]$protocols -ne 0 -and -not ($protocols -band [Net.SecurityProtocolType]::Tls12)) {
        [Net.ServicePointManager]::SecurityProtocol = $protocols -bor [Net.SecurityProtocolType]::Tls12
    }
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    # Each attempt has its own cancellation, bounded by the -Timeout budget.
    $client.Timeout = [Threading.Timeout]::InfiniteTimeSpan
    $IDs = [Collections.Generic.List[string]]::new()
    if ($Command -eq 'logs') {
        $selector = if ($Query) { $Query } else {
            $levelPattern = if ($Level) { $levels[$Level] }
            New-Selector $Service $levelPattern ''
        }
        $selector += New-LineFilters 's'
        $payload = @{q = $selector; s = $window.start; e = $window.end; limit = $Limit; reverse = $true}
        if ($Field) { $payload.fields = $Field }
        $rows = @(Invoke-Query $payload $IDs)
        $result = @{
            query = $selector; query_ids = $IDs.ToArray(); limit = $Limit
            returned = $rows.Count; limit_reached = $rows.Count -ge $Limit; logs = $rows
        }
    }
    elseif ($Command -eq 'tags') { $result = Get-TagNames $window $IDs }
    elseif ($Command -eq 'values') { $result = Get-TagValues $window $IDs }
    else { $result = Get-IssuePatterns $window $IDs $first $last }
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    ConvertTo-Json -InputObject ($window + $result) -Depth 30 -Compress
}
catch {
    $message = $_.Exception.Message
    if ($key) { $message = $message.Replace($key, '[redacted]') }
    [Console]::Error.WriteLine("cardinal-query: $message")
    exit 1
}
finally {
    if ($client) { $client.Dispose() }
}
