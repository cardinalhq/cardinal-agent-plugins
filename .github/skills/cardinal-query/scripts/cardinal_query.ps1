#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('logs', 'issues')][string]$Command,
    [string]$Since, [string]$Start, [string]$End, [string]$Service,
    [string[]]$Label, [string[]]$Contains, [string[]]$NotContains,
    [string[]]$Grep, [string[]]$NotGrep,
    [ValidateRange(1, 2147483647)][int]$Limit = 20,
    [ValidateRange(1, 3600)][int]$Timeout = 90,
    [string]$Query, [string[]]$Field,
    [ValidateSet('warn', 'error', 'fatal')][string]$Level,
    [ValidateSet('warn', 'error', 'fatal')][string]$Severity,
    [string]$Fingerprint,
    [ValidateRange(0, 20)][int]$Samples = 5
)

$ErrorActionPreference = 'Stop'
$levels = @{ fatal = 'fatal'; error = 'error'; warn = 'warn|warning' }
$utc = [Globalization.CultureInfo]::InvariantCulture

function Convert-QueryTime([string]$Value) {
    if ($Value -match '^\d{10}$') { return [DateTimeOffset]::FromUnixTimeSeconds([long]$Value) }
    if ($Value -match '^\d{13}$') { return [DateTimeOffset]::FromUnixTimeMilliseconds([long]$Value) }
    if ($Value -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$') {
        throw 'Use an RFC 3339 timestamp with a timezone or Unix seconds/milliseconds.'
    }
    return [DateTimeOffset]::Parse($Value, $utc)
}

function Convert-LogQLString([string]$Value) {
    # ConvertTo-Json can escape characters that the LogQL parser also accepts.
    return (ConvertTo-Json -InputObject $Value -Compress)
}

function New-Selector([string]$Name, [string]$LevelPattern, [string]$PatternID) {
    $matchers = @(
        if ($Name) { 'service_name=' + (Convert-LogQLString $Name) }
        else { 'service_name=~".+"' }
        if ($LevelPattern) { 'level=~"(?i)^(?:' + $LevelPattern + ')$"' }
        foreach ($item in $Label) {
            if ($item -cnotmatch '^([A-Za-z_][A-Za-z0-9_]*)=([\s\S]*)$') {
                throw 'Expected LABEL=VALUE with a valid LogQL label name.'
            }
            $Matches[1] + '=' + (Convert-LogQLString $Matches[2])
        }
        if ($PatternID) { 'chq_fingerprint=' + (Convert-LogQLString $PatternID) }
    )
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

function Invoke-Query($Payload, $IDs) {
    $queryID = [Guid]::NewGuid().ToString()
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, $endpoint)
    $request.Headers.Add('X-CardinalHQ-API-Key', $key)
    $request.Headers.Add('X-CardinalHQ-Query-Id', $queryID)
    $request.Headers.Accept.ParseAdd('text/event-stream')
    $request.Content = [System.Net.Http.StringContent]::new(
        (ConvertTo-Json -InputObject $Payload -Depth 8 -Compress), [Text.Encoding]::UTF8, 'application/json')
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $response = $null
    $reader = $null
    try {
        $response = $client.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        $serverID = [Guid]::Empty
        if ($response.Headers.Contains('X-CardinalHQ-Query-Id') -and
            [Guid]::TryParse(($response.Headers.GetValues('X-CardinalHQ-Query-Id') | Select-Object -First 1), [ref]$serverID)) {
            $queryID = $serverID.ToString()
        }
        if (-not $response.IsSuccessStatusCode) {
            $status = [int]$response.StatusCode
            $hint = switch ($status) {
                { $_ -in 401, 403 } { ' Check that the key has query access to this Data Lake.' }
                { $_ -eq 404 -or ($_ -ge 300 -and $_ -lt 400) } { ' Check CARDINAL_QUERY_URL for the direct Query API endpoint.' }
                default { '' }
            }
            throw "Query API returned HTTP ${status}.$hint"
        }
        $IDs.Add($queryID)
        if ($response.Content.Headers.ContentType.MediaType -ne 'text/event-stream') {
            throw 'Expected a Query API event stream. Check CARDINAL_QUERY_URL.'
        }
        $reader = [IO.StreamReader]::new($response.Content.ReadAsStreamAsync().GetAwaiter().GetResult())
        while ($true) {
            $remaining = ($Timeout * 1000) - [int]$clock.ElapsedMilliseconds
            if ($remaining -le 0) { throw 'Query timed out. Try a shorter time range.' }
            $read = $reader.ReadLineAsync()
            if (-not $read.Wait($remaining)) { throw 'Query timed out. Try a shorter time range.' }
            $line = $read.Result
            if ($null -eq $line) { throw 'Query API stream ended before completion.' }
            if (-not $line.StartsWith('data:', [StringComparison]::Ordinal)) { continue }
            $event = ConvertFrom-Json -InputObject $line.Substring(5)
            if ($null -eq $event.data) { throw 'Invalid Query API event.' }
            switch -CaseSensitive ($event.type) {
                'result' { $event.data }
                'error' { throw 'Query execution failed.' }
                'done' {
                    if ($event.data.status -ceq 'ok') { return }
                    throw 'Query API reported a failure. Results may be incomplete.'
                }
            }
        }
    }
    catch {
        throw ($_.Exception.Message.Replace($key, '[redacted]') + " Query ID: $queryID")
    }
    finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
        $request.Dispose()
    }
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
    $endpoint = $url.TrimEnd('/')
    if (-not $endpoint.EndsWith('/api/v1/logs/query', [StringComparison]::Ordinal)) {
        $endpoint = ($endpoint -creplace '/api/v1$', '') + '/api/v1/logs/query'
    }
    if ($Since -and $Start) { throw 'Choose either -Since or -Start.' }
    if ($Fingerprint) {
        $id = 0L
        if ($Fingerprint -cnotmatch '^-?\d+$' -or -not [long]::TryParse($Fingerprint, [ref]$id)) {
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
    if ($Query -and ($Service -or $Level -or $Label -or -not $Query.TrimStart().StartsWith('{'))) {
        throw '-Query expects a raw LogQL selector. Include service, level and label matchers in the selector.'
    }

    $last = if ($End) { Convert-QueryTime $End } else { [DateTimeOffset]::UtcNow }
    $first = if ($Start) { Convert-QueryTime $Start } else {
        if (-not $Since) { $Since = '1h' }
        if ($Since -cnotmatch '^([1-9][0-9]*)([smhdw])$') { throw 'Use a duration such as 30m, 2h, or 1d for -Since.' }
        $units = @{ s = 1; m = 60; h = 3600; d = 86400; w = 604800 }
        $last.AddSeconds(-([double]$Matches[1] * $units[$Matches[2]]))
    }
    if ($first.ToUnixTimeMilliseconds() -ge $last.ToUnixTimeMilliseconds()) {
        throw 'The start time must be earlier than the end time.'
    }
    $format = "yyyy-MM-dd'T'HH:mm:ss.fff'Z'"
    $window = @{ start = $first.UtcDateTime.ToString($format, $utc); end = $last.UtcDateTime.ToString($format, $utc) }

    Add-Type -AssemblyName System.Net.Http
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($Timeout)
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
