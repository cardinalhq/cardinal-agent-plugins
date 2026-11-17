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
    [ValidateRange(0, 5)][int]$Retries = 2,
    [switch]$Raw, [switch]$AllFields, [switch]$FullMessage
)

$ErrorActionPreference = 'Stop'
$levels = @{ fatal = 'fatal'; error = 'error'; warn = 'warn|warning' }
$coreFields = @('service_name', 'level', 'message', 'chq_fingerprint')
# Fields returned for raw rows and exemplars unless -Field or -AllFields says otherwise.
$rowFields = @('message', 'service_name', 'level', 'chq_fingerprint')
$messageChars = 300
# -FullMessage raises the per-message cap for raw rows.
$fullMessageChars = 8000
$maxFields = 50
$maxNameChars = 256
$maxLogLimit = 200
# One newest-first fetch picks exemplars; patterns it misses get their own query.
$exemplarCount = 10
$exemplarRows = 50
$maxServices = 10
# Long count windows use day buckets, at most this many.
$maxBuckets = 120
# Output budget, in bytes of stdout: compact JSON with keys sorted by UTF-16 code unit
# and only ASCII, so every byte is one character in both clients, plus a newline (CRLF
# on Windows). The budget measures everything it prints. Only next[].run differs by
# platform, so each is measured as $runBytes long, and a longer command is replaced by
# a short hint. Both clients therefore make the same decisions.
$maxOutputBytes = 16384
$outputLimit = $maxOutputBytes - 2
$runBytes = 1024
$runPlaceholder = 'x' * ($runBytes - 2)
# An echoed query or scope longer than this many bytes of JSON text is shortened.
$echoBytes = 2048
$echoFields = @('query', 'scope')
# Budget counters are measured at this value until their real value is known.
$counterReserve = 9999999999
$budgetCaps = @(200, 100)
$budgetLists = @('logs', 'patterns', 'values', 'tags', 'service_counts', 'trend', 'warnings')
$budgetKeep = [Collections.Generic.HashSet[string]]::new([string[]]@('fingerprint', 'chq_fingerprint', 'service',
    'service_name', 'severity', 'level', 'first_seen', 'last_seen', 'timestamp', 'timestamp_ns'), [StringComparer]::Ordinal)
$budgetFailed = 'The result does not fit the output budget. Narrow the filters or the window.'
# Add-Json stops writing objects and arrays once the text is longer than this.
$script:jsonCap = [int]::MaxValue
# PowerShell 7.5+ can keep date-like JSON strings as text; earlier versions convert them.
$jsonOptions = @{}
if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) { $jsonOptions.DateKind = 'String' }
$jsonEscape = [regex]'[^ !#-\[\]-~]'
$jsonOther = [regex]'[^ -~]+'
$jsonUnit = [regex]'([0-9A-F]{2})-([0-9A-F]{2})-?'
$jsonUnits = [Text.RegularExpressions.MatchEvaluator]{
    param($Run)
    # Copy the UTF-16 code units as they are, lone surrogates included, then write each as \uxxxx.
    $bytes = New-Object byte[] (2 * $Run.Value.Length)
    [Buffer]::BlockCopy($Run.Value.ToCharArray(), 0, $bytes, 0, $bytes.Length)
    return $jsonUnit.Replace([BitConverter]::ToString($bytes), '\u$2$1').ToLowerInvariant()
}
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

$controlChar = [regex]'[\x00-\x1f]'
$controlEscape = [Text.RegularExpressions.MatchEvaluator]{ param($Char) '\u{0:x4}' -f [int][char]$Char.Value }

function Convert-LogQLString([string]$Value) {
    # The same text as the Python client's json.dumps(ensure_ascii=False), so both clients
    # send and echo the same selector.
    $text = $Value.Replace('\', '\\').Replace('"', '\"').Replace([string][char]8, '\b').Replace([string][char]12, '\f')
    $text = $text.Replace([string][char]10, '\n').Replace([string][char]13, '\r').Replace([string][char]9, '\t')
    return '"' + $controlChar.Replace($text, $controlEscape) + '"'
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

function Get-FilterPatterns([string]$Flags) {
    # Return the include and exclude RE2 patterns for the message filters.
    $includes = [Collections.Generic.List[string]]::new()
    $excludes = [Collections.Generic.List[string]]::new()
    foreach ($item in $Contains) {
        $literal = $item -creplace '([\\.^$|?*+()\[\]{}])', '\$1'
        $includes.Add("(?${Flags}:.*$literal.*)")
    }
    foreach ($item in $Grep) { $includes.Add($item) }
    foreach ($item in $NotContains) {
        $literal = $item -creplace '([\\.^$|?*+()\[\]{}])', '\$1'
        $excludes.Add("(?${Flags}:.*$literal.*)")
    }
    foreach ($item in $NotGrep) { $excludes.Add($item) }
    return @{ includes = $includes; excludes = $excludes }
}

function New-LineFilters([string]$Flags) {
    $patterns = Get-FilterPatterns $Flags
    $clauses = @(
        foreach ($item in $patterns.includes) { ' |~ ' + (Convert-LogQLString $item) }
        foreach ($item in $patterns.excludes) { ' !~ ' + (Convert-LogQLString $item) }
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
                $event = ConvertFrom-Json -InputObject $json @jsonOptions
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

function Limit-Text([string]$Value, [int]$Max = 256) {
    # Count Unicode code points, as the Python client does, and never split a
    # surrogate pair. Returns $null when the value is short enough.
    if ($Value.Length -le $Max) { return $null }
    # Atomic steps: a surrogate pair is one code point, and a failed match cannot backtrack
    # into reading the pair as two units, which would take exponential time.
    $head = [regex]::Match($Value, '^(?>[\uD800-\uDBFF][\uDC00-\uDFFF]|[\s\S]){' + $Max + '}')
    if (-not $head.Success -or $head.Length -eq $Value.Length) { return $null }
    return $head.Value + [char]0x2026
}

function Get-CodePointCount([string]$Value) {
    return $Value.Length - [regex]::Matches($Value, '[\uD800-\uDBFF][\uDC00-\uDFFF]').Count
}

function Get-Sample($Row) {
    # An exemplar row's message and time, with the message cut to $messageChars code points.
    $message = $Row.tags.message
    $sample = @{ message = $message; timestamp = $Row.timestamp }
    if ($message -is [string]) {
        $short = Limit-Text $message $messageChars
        if ($null -ne $short) {
            $sample.message = $short
            $sample.message_chars = Get-CodePointCount $message
        }
    }
    return $sample
}

function Test-Fingerprint($Value) {
    # As in the Python client, null, "", 0 and "0" mean the row has no fingerprint.
    if ($null -eq $Value) { return $false }
    if ($Value -is [string]) { return $Value -cne '' -and $Value -cne '0' }
    if ($Value -is [ValueType]) { return -not ($Value -eq 0) }
    return $true
}

function Get-SortedByKey([object[]]$Items, [string[]]$Keys) {
    # Sort by ordinal UTF-16 keys, as the Python client sorts.
    # Cast to Array and IComparer: the generic overload would sort a converted copy of the items.
    $sorted = [object[]]$Items.Clone()
    [Array]::Sort([Array]([object[]]$Keys), [Array]$sorted, [Collections.IComparer][StringComparer]::Ordinal)
    return ,$sorted
}

function Get-RankKey([long]$Count, [string[]]$Parts) {
    return ([long]::MaxValue - $Count).ToString('D19', $utc) + [char]0 + ($Parts -join [char]0)
}

function Format-Ms([long]$Value) {
    return [DateTimeOffset]::FromUnixTimeMilliseconds($Value).UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", $utc)
}

function Get-SampleRows($Window, $IDs, [string[]]$Selectors) {
    # Fetch the newest row for each selector. Return @{row} or @{error} for each, in order.
    $results = [Collections.Generic.List[object]]::new()
    foreach ($selector in $Selectors) {
        $rowIDs = [Collections.Generic.List[string]]::new()
        try {
            $rows = @(Invoke-Query @{
                q = $selector; s = $Window.start; e = $Window.end; limit = 1; reverse = $true; fields = $rowFields
            } $rowIDs)
            $IDs.AddRange($rowIDs)
            $results.Add(@{ row = $(if ($rows.Count) { $rows[0] } else { $null }) })
        }
        catch { $results.Add(@{ error = $_.Exception.Message }) }
    }
    return ,$results
}

function Format-Quoted([string]$Value) {
    # PowerShell also treats typographic single quotes as quote characters.
    return "'" + ($Value -replace '[''\u2018\u2019\u201A\u201B]', '$0$0') + "'"
}

function Format-Command([string]$Name, $Options) {
    # The exact follow-up command for this platform, with the same absolute window.
    # $Options holds pairs of parameter name and value, or $null for a switch.
    $words = [Collections.Generic.List[string]]::new()
    $words.Add('&')
    $words.Add((Format-Quoted $PSCommandPath))
    $words.Add($Name)
    $all = [Collections.Generic.List[object]]::new()
    $all.Add(@('Start', $window.start))
    $all.Add(@('End', $window.end))
    $all.AddRange($Options)
    $previous = $null
    foreach ($pair in $all) {
        if ($null -eq $pair[1]) {
            $words.Add('-' + $pair[0])
            $previous = $null
        }
        elseif ($pair[0] -ceq $previous) { $words[$words.Count - 1] += ',' + (Format-Quoted $pair[1]) }
        else {
            $words.Add('-' + $pair[0])
            $words.Add((Format-Quoted $pair[1]))
            $previous = $pair[0]
        }
    }
    return ($words -join ' ')
}

function Format-LabelOption([string]$Item) {
    if ($Item -cnotmatch '^([A-Za-z_][A-Za-z0-9_.]*)=([\s\S]*)\z') { throw 'Expected LABEL=VALUE with a valid LogQL label name.' }
    return $Matches[1].Replace('.', '_') + '=' + $Matches[2]
}

# The stream selector at the start of a raw query, and an exact service_name matcher in it.
$selectorHead = [regex]'^\s*\{(?:[^}"`]|"(?:[^"\\]|\\.)*"|`[^`]*`)*\}'
$pinnedService = [regex]'(?<![A-Za-z0-9_.])service_name\s*=(?![=~])'

function Test-PinsService([string]$Selector) {
    # Whether a raw query's stream selector has an exact service_name matcher.
    $head = $selectorHead.Match($Selector)
    return $head.Success -and $pinnedService.IsMatch($head.Value)
}

function Add-Matchers([string]$Selector, [string[]]$Matchers) {
    # Add matchers to the front of a raw stream selector.
    if (-not $Matchers.Count) { return $Selector }
    $rest = $Selector.TrimStart().Substring(1)
    $separator = if ($rest.TrimStart().StartsWith('}', [StringComparison]::Ordinal)) { '' } else { ', ' }
    return '{' + ($Matchers -join ', ') + $separator + $rest
}

function Get-PatternMatchers([string]$PatternID, [string]$ServiceName) {
    # Matchers that narrow -Query to one pattern: its fingerprint, and its service unless
    # the selector already pins one.
    $matchers = [Collections.Generic.List[string]]::new()
    if ($PatternID) { $matchers.Add('chq_fingerprint=' + (Convert-LogQLString $PatternID)) }
    if ($ServiceName -and -not (Test-PinsService $Query)) { $matchers.Add('service_name=' + (Convert-LogQLString $ServiceName)) }
    return ,$matchers.ToArray()
}

function Get-LogOptions([string]$ServiceName, [string]$PatternID, [bool]$RawRows) {
    # Options that repeat this logs search, optionally narrowed to one pattern.
    $options = [Collections.Generic.List[object]]::new()
    if ($Query) { $options.Add(@('Query', (Add-Matchers $Query (Get-PatternMatchers '' $ServiceName)))) }
    else {
        if ($ServiceName) { $options.Add(@('Service', $ServiceName)) }
        elseif ($Service) { $options.Add(@('Service', $Service)) }
        if ($Level) { $options.Add(@('Level', $Level)) }
        foreach ($item in $Label) { $options.Add(@('Label', (Format-LabelOption $item))) }
    }
    foreach ($item in $Contains) { $options.Add(@('Contains', $item)) }
    foreach ($item in $NotContains) { $options.Add(@('NotContains', $item)) }
    foreach ($item in $Grep) { $options.Add(@('Grep', $item)) }
    foreach ($item in $NotGrep) { $options.Add(@('NotGrep', $item)) }
    if ($PatternID) { $options.Add(@('Fingerprint', $PatternID)) }
    elseif ($Fingerprint) { $options.Add(@('Fingerprint', $Fingerprint)) }
    if ($RawRows) { $options.Add(@('Raw', $null)) }
    return ,$options
}

function New-Hint([string]$Why, [string[]]$Runs) {
    # A follow-up with the first run whose JSON text fits in $runBytes. The last run is a short text.
    foreach ($run in $Runs) { if ((ConvertTo-JsonText $run).Length -le $runBytes) { break } }
    return @{ why = $Why; run = $run }
}

function New-PatternHint([string]$ServiceName, [string]$PatternID) {
    # The drill-down into one pattern's rows.
    if ($Query) {
        $extra = ''
        if ($ServiceName -and -not (Test-PinsService $Query)) { $extra = ', adding service_name=' + (Convert-LogQLString $ServiceName) + ' to -Query' }
        $short = "Run the same logs command with -Fingerprint '$PatternID' -Raw$extra."
    }
    else {
        $extra = ''
        if ($ServiceName -and $ServiceName -cne $Service) { $extra = ' -Service ' + (Format-Quoted $ServiceName) }
        $short = "Run the same logs command with$extra -Fingerprint '$PatternID' -Raw."
    }
    return (New-Hint 'Rows for the top pattern, newest first.' @(
        (Format-Command 'logs' (Get-LogOptions $ServiceName $PatternID $true)), $short,
        "Run the same logs command with the top pattern's service and fingerprint, and -Raw."))
}

function Get-LogSelector([string]$PatternID, [string]$ServiceName) {
    if ($Query) { return (Add-Matchers $Query (Get-PatternMatchers $PatternID $ServiceName)) }
    $levelPattern = if ($Level) { $levels[$Level] }
    $name = if ($ServiceName) { $ServiceName } else { $Service }
    return (New-Selector $name $levelPattern $PatternID)
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
            'fields with no logs in this window. Scope to a service, label or level for names on matching rows.'
    }
    return $result
}

function Get-TagValues($Window, $IDs) {
    if (-not $Tag -or $Tag -cnotmatch $labelName -or $Tag.Length -gt $maxNameChars) {
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
            'include values with no logs in this window. Scope to a service, label or level for values on matching rows.'
    }
    return $result
}

function Get-CountStep([double]$Minutes) {
    if ($Minutes -le 30) { return 60 }
    if ($Minutes -le 120) { return 300 }
    if ($Minutes -le 480) { return 1200 }
    if ($Minutes -le 1440) { return 3600 }
    if ($Minutes -le 4320) { return 7200 }
    if ($Minutes -le 10080) { return 21600 }
    # Longer windows use whole days, at most $maxBuckets of them, so trend stays bounded.
    return [int](86400 * [Math]::Max(1.0, [Math]::Ceiling($Minutes / (1440 * $maxBuckets))))
}

function Get-CountWindow([DateTimeOffset]$First, [DateTimeOffset]$Last, [string]$Message) {
    # Return the bucket step, the window bounds and the end to query, in milliseconds.
    # The Query API stamps each bucket at its end and leaves out the bucket that straddles
    # the query end. When the window ends now, query up to the next bucket boundary so the
    # newest bucket is counted: nothing exists after now, so the counts stay exact.
    $step = Get-CountStep ($Last - $First).TotalMinutes
    $stepMs = [long]$step * 1000
    $startMs = $First.ToUnixTimeMilliseconds()
    $endMs = $Last.ToUnixTimeMilliseconds()
    $queryEndMs = $endMs
    if ($openEnd) { $queryEndMs = [long][Math]::Ceiling($endMs / $stepMs) * $stepMs }
    if ([Math]::Floor($startMs / $stepMs) -eq [Math]::Floor($queryEndMs / $stepMs)) { throw $Message }
    return @{ step = $step; startMs = $startMs; endMs = $endMs; queryEndMs = $queryEndMs }
}

function Get-BucketNote($Bounds) {
    $stepMs = [long]$Bounds.step * 1000
    $clauses = @(
        if ($Bounds.startMs % $stepMs) { 'the bucket that straddles the start is counted in full' }
        if ($openEnd) { 'the newest bucket runs to now' }
        elseif ($Bounds.endMs % $stepMs) { 'the bucket that straddles the end is left out' }
    )
    $note = "Counts are totals in fixed $($Bounds.step)-second buckets"
    if ($clauses.Count) { $note += ': ' + ($clauses -join ' and ') }
    return $note + '. first_seen and last_seen are bucket bounds. Use raw rows for exact boundaries.'
}

function Get-BucketCount($Point, $Bounds) {
    # Return a point's rounded count, or -1 outside the window or when invalid.
    # The Query API stamps a count_over_time point at the end of its bucket.
    if ($Point.timestamp -isnot [ValueType]) { return -1L }
    $bucket = [long]$Point.timestamp
    if ($bucket -le $Bounds.startMs -or $bucket -gt $Bounds.queryEndMs) { return -1L }
    if ($Point.value -isnot [ValueType] -or -not ($Point.value -gt 0)) { return -1L }
    return [long][Math]::Round([double]$Point.value)
}

function Get-CountPoints($Window, $IDs, [string]$Selector, [string]$Filters, [int]$Step, [string]$By) {
    # Query to the end of the newest bucket that the window counts.
    $Window = @{ start = $Window.start; end = $Window.countEnd }
    $query = 'sum by ({0})(count_over_time({1}{2}[{3}s]))' -f $By, $Selector, $Filters, $Step
    $found = $false
    Invoke-Query @{ q = $query; s = $Window.start; e = $Window.end; step = $Step } $IDs | ForEach-Object {
        $found = $true
        $_
    }
    if (-not $found -and -not $Filters) {
        # Bypass materialized counts if the deployment only has raw logs.
        $query = 'sum by ({0})(count_over_time({1} |~ "(?s:.*)"[{2}s]))' -f $By, $Selector, $Step
        Invoke-Query @{ q = $query; s = $Window.start; e = $Window.end; step = $Step } $IDs
    }
}

function Get-RawLogs($Window, $IDs, [string]$Selector, [int]$Count) {
    # Assign in each branch: an if expression would unwrap a one-element array.
    $fields = $null
    if ($Field) { $fields = [string[]]$Field } elseif (-not $AllFields) { $fields = $rowFields }
    $payload = @{ q = $Selector; s = $Window.start; e = $Window.end; limit = $Count; reverse = $true }
    if ($fields) { $payload.fields = $fields }
    $rows = @(Invoke-Query $payload $IDs)
    $cap = if ($FullMessage) { $fullMessageChars } else { $messageChars }
    $truncated = 0
    foreach ($row in $rows) {
        if ($row.tags -isnot [Management.Automation.PSCustomObject] -or $row.tags.message -isnot [string]) { continue }
        $short = Limit-Text $row.tags.message $cap
        if ($null -eq $short) { continue }
        $row | Add-Member -NotePropertyName message_chars -NotePropertyValue (Get-CodePointCount $row.tags.message)
        $row.tags.message = $short
        $truncated++
    }
    $result = @{
        mode = 'raw'; query = $Selector; query_ids = $IDs.ToArray(); fields = $fields; limit = $Count
        returned = $rows.Count; limit_reached = $rows.Count -ge $Count; logs = $rows
    }
    if ($truncated) { $result.truncated_messages = $truncated }
    $patternsHint = New-Hint 'Count every match in the window by pattern.' @((Format-Command 'logs' (Get-LogOptions '' '' $false)),
        'Run the same logs command without -Raw, -Field, -AllFields or -FullMessage.')
    if ($result.limit_reached) { $result.next = @($patternsHint) }
    $script:budgetHint = $patternsHint
    return $result
}

function Get-LogPatterns($Window, $IDs, [DateTimeOffset]$First, [DateTimeOffset]$Last, [string]$Selector, [string]$Filters, [int]$Count) {
    $bounds = Get-CountWindow $First $Last 'The window has no completed bucket for pattern counts. Use a wider window or -Raw.'
    $step = $bounds.step
    $counts = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    $total = 0L
    $missing = 0L
    $base = Get-LogSelector $Fingerprint ''
    Get-CountPoints ($Window + @{ countEnd = (Format-Ms $bounds.queryEndMs) }) $IDs $base $Filters $step 'service_name, chq_fingerprint' | ForEach-Object {
        $number = Get-BucketCount $_ $bounds
        if ($number -lt 0) { return }
        $total += $number
        if (-not (Test-Fingerprint $_.tags.chq_fingerprint)) {
            $missing += $number
            return
        }
        $name = [string]$_.tags.service_name
        $id = [string]$_.tags.chq_fingerprint
        $bucket = [long]$_.timestamp
        $identity = $name + [char]0 + $id
        if (-not $counts.ContainsKey($identity)) {
            $counts[$identity] = @{ service = $name; fingerprint = $id; count = 0L; first = $bucket; last = $bucket }
        }
        $entry = $counts[$identity]
        $entry.count += $number
        if ($bucket -lt $entry.first) { $entry.first = $bucket }
        if ($bucket -gt $entry.last) { $entry.last = $bucket }
    }
    $entries = @($counts.Values)
    $ranked = Get-SortedByKey $entries @(foreach ($entry in $entries) { Get-RankKey $entry.count @($entry.service, $entry.fingerprint) })
    $patterns = @(for ($index = 0; $index -lt [Math]::Min($Count, $ranked.Count); $index++) {
        $entry = $ranked[$index]
        @{
            fingerprint = $entry.fingerprint; service = $(if ($entry.service) { $entry.service } else { $null })
            count = $entry.count; first_seen = Format-Ms ($entry.first - $step * 1000)
            # The newest bucket can end after now; the window ends at now.
            last_seen = Format-Ms ([Math]::Min($entry.last, $bounds.endMs))
        }
    })
    $unlistedEvents = 0L
    for ($index = $patterns.Count; $index -lt $ranked.Count; $index++) { $unlistedEvents += $ranked[$index].count }

    $warnings = [Collections.Generic.List[string]]::new()
    $targets = @($patterns | Select-Object -First $exemplarCount)
    if ($targets.Count) {
        $rows = @()
        $rowIDs = [Collections.Generic.List[string]]::new()
        try {
            $rows = @(Invoke-Query @{
                q = $Selector; s = $Window.start; e = $Window.end; limit = $exemplarRows; reverse = $true; fields = $rowFields
            } $rowIDs)
            $IDs.AddRange($rowIDs)
        }
        catch {
            $rows = @()
            $warnings.Add('Could not load exemplars: ' + $_.Exception.Message)
        }
        $newest = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
        foreach ($row in $rows) {
            if ($row.tags -is [Management.Automation.PSCustomObject] -and (Test-Fingerprint $row.tags.chq_fingerprint)) {
                $identity = [string]$row.tags.service_name + [char]0 + [string]$row.tags.chq_fingerprint
                if (-not $newest.ContainsKey($identity)) { $newest[$identity] = $row }
            }
        }
        foreach ($pattern in $targets) {
            $identity = [string]$pattern.service + [char]0 + $pattern.fingerprint
            if ($newest.ContainsKey($identity)) { $pattern.exemplar = Get-Sample $newest[$identity] }
        }
        $missingRows = @($targets | Where-Object { -not $_.ContainsKey('exemplar') })
        $selectors = [string[]]@(foreach ($pattern in $missingRows) { (Get-LogSelector $pattern.fingerprint $pattern.service) + $Filters })
        $fetched = Get-SampleRows $Window $IDs $selectors
        for ($index = 0; $index -lt $missingRows.Count; $index++) {
            if ($fetched[$index].ContainsKey('error')) {
                $warnings.Add("Could not load exemplar for $($missingRows[$index].fingerprint): $($fetched[$index].error)")
            }
            elseif ($fetched[$index].row) { $missingRows[$index].exemplar = Get-Sample $fetched[$index].row }
        }
    }
    $result = @{
        mode = 'patterns'; query = $Selector; query_ids = $IDs.ToArray(); step_seconds = $step; total = $total
        unfingerprinted = $missing; matched_patterns = $counts.Count; limit = $Count; patterns = $patterns
        unlisted_patterns = $ranked.Count - $patterns.Count; unlisted_events = $unlistedEvents
        warnings = $warnings.ToArray(); note = Get-BucketNote $bounds
    }
    if ($patterns.Count) { $result.next = @(New-PatternHint $patterns[0].service $patterns[0].fingerprint) }
    elseif ($total) {
        $result.next = @(New-Hint 'Matching rows, newest first.' @((Format-Command 'logs' (Get-LogOptions '' '' $true)),
            'Run the same logs command with -Raw.'))
    }
    return $result
}

function Get-IssuePatterns($Window, $IDs, [DateTimeOffset]$First, [DateTimeOffset]$Last) {
    $levelPattern = if ($Severity) { $levels[$Severity] } else { 'fatal|error|warn|warning' }
    $bounds = Get-CountWindow $First $Last 'The Issues window has no completed bucket. Use a wider window or search raw logs.'
    $step = $bounds.step
    $filters = New-LineFilters 'is'
    $selector = New-Selector $Service $levelPattern $Fingerprint
    $services = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    $patterns = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    $trend = @{}
    $warnings = [Collections.Generic.List[string]]::new()
    $total = 0L
    Get-CountPoints ($Window + @{ countEnd = (Format-Ms $bounds.queryEndMs) }) $IDs $selector $filters $step 'service_name, level, chq_fingerprint' | ForEach-Object {
        $count = Get-BucketCount $_ $bounds
        $bucket = [long]$_.timestamp
        $name = [string]$_.tags.service_name
        $severityName = ([string]$_.tags.level).ToLowerInvariant()
        if ($severityName -eq 'warning') { $severityName = 'warn' }
        if ($count -ge 0 -and $name -and $levels.ContainsKey($severityName)) {
            $total += $count
            if (-not $services.ContainsKey($name)) { $services[$name] = @{service = $name; warn = 0L; error = 0L; fatal = 0L; total = 0L} }
            if (-not $trend.ContainsKey($bucket)) { $trend[$bucket] = @{timestamp = $bucket; warn = 0L; error = 0L; fatal = 0L} }
            $services[$name][$severityName] += $count
            $services[$name].total += $count
            $trend[$bucket][$severityName] += $count
            if (Test-Fingerprint $_.tags.chq_fingerprint) {
                $id = [string]$_.tags.chq_fingerprint
                $identity = $name + [char]0 + $severityName + [char]0 + $id
                if (-not $patterns.ContainsKey($identity)) {
                    $patterns[$identity] = @{service = $name; severity = $severityName; fingerprint = $id; count = 0L}
                }
                $patterns[$identity].count += $count
            }
        }
    }
    $entries = @($patterns.Values)
    $sorted = Get-SortedByKey $entries @(foreach ($entry in $entries) {
        Get-RankKey $entry.count @($entry.service, $entry.severity, $entry.fingerprint) })
    $ranked = @($sorted | Select-Object -First $Limit)
    $unlistedEvents = 0L
    for ($index = $ranked.Count; $index -lt $sorted.Count; $index++) { $unlistedEvents += $sorted[$index].count }
    $sampled = @($ranked | Select-Object -First $Samples)
    $selectors = [string[]]@(foreach ($pattern in $sampled) { (New-Selector $pattern.service $levels[$pattern.severity] $pattern.fingerprint) + $filters })
    $fetched = Get-SampleRows $Window $IDs $selectors
    for ($index = 0; $index -lt $sampled.Count; $index++) {
        if ($fetched[$index].ContainsKey('error')) {
            $warnings.Add("Could not load sample for $($sampled[$index].fingerprint): $($fetched[$index].error)")
        }
        elseif ($fetched[$index].row) { $sampled[$index].sample = Get-Sample $fetched[$index].row }
    }
    $serviceRows = @($services.Values)
    $serviceRows = Get-SortedByKey $serviceRows @(foreach ($row in $serviceRows) { Get-RankKey $row.total @($row.service) })
    $result = @{
        query_ids = $IDs.ToArray(); step_seconds = $step; total = $total
        service_counts = @($serviceRows | Select-Object -First $maxServices)
        trend = @($trend.Values | Sort-Object { $_.timestamp })
        matched_patterns = $patterns.Count; patterns = $ranked; warnings = $warnings.ToArray()
        unlisted_patterns = $sorted.Count - $ranked.Count; unlisted_events = $unlistedEvents
    }
    if ($serviceRows.Count -gt $maxServices) {
        $other = @{ services = $serviceRows.Count - $maxServices; warn = 0L; error = 0L; fatal = 0L; total = 0L }
        foreach ($row in ($serviceRows | Select-Object -Skip $maxServices)) {
            foreach ($name in 'warn', 'error', 'fatal', 'total') { $other[$name] += $row[$name] }
        }
        $result.service_counts_other = $other
    }
    if ($ranked.Count) {
        $top = $ranked[0]
        $filterPatterns = Get-FilterPatterns 'is'
        $options = [Collections.Generic.List[object]]::new()
        $options.Add(@('Service', $top.service))
        $options.Add(@('Level', $top.severity))
        foreach ($item in $Label) { $options.Add(@('Label', (Format-LabelOption $item))) }
        foreach ($item in $filterPatterns.includes) { $options.Add(@('Grep', $item)) }
        foreach ($item in $filterPatterns.excludes) { $options.Add(@('NotGrep', $item)) }
        $options.Add(@('Fingerprint', $top.fingerprint))
        $options.Add(@('Raw', $null))
        $result.next = @(New-Hint 'Rows for the top pattern, newest first.' @((Format-Command 'logs' $options),
            ("Run logs with the same window and filters and -Service $(Format-Quoted $top.service) " +
             "-Level '$($top.severity)' -Fingerprint '$($top.fingerprint)' -Raw."),
            "Run logs with the same window and filters and the top pattern's service, level and fingerprint, and -Raw."))
    }
    return $result
}

function Format-JsonNumber([double]$Value) {
    # Python's repr: the shortest round-trip digits, in fixed notation from 1e-4 up to 1e16.
    if ([double]::IsNaN($Value)) { return 'NaN' }
    if ([double]::IsInfinity($Value)) { if ($Value -gt 0) { return 'Infinity' } else { return '-Infinity' } }
    if ($Value -eq 0) { if ([BitConverter]::DoubleToInt64Bits($Value) -lt 0) { return '-0.0' } else { return '0.0' } }
    # 'R' is not the shortest round trip on .NET Framework. Take the fewest digits that
    # round-trip, as Python does: a decimal of up to 15 digits survives a round trip through
    # a double, and subnormal values hold fewer digits, so look for a shorter one. At each
    # length try the nearest decimal, then its neighbour on the far side of the value: the
    # nearest can miss when it is a tie or when the value is a power of two.
    $first = 15
    if ([Math]::Abs($Value) -lt 2.2250738585072014E-308) { $first = 1 }
    for ($precision = $first; $precision -le 17; $precision++) {
        $text = $Value.ToString('E' + ($precision - 1), $utc)
        $back = [double]::Parse($text, $utc)
        if ($back -eq $Value) { break }
        $mark = $text.IndexOf('E')
        $digits = [decimal]::Parse($text.Substring(0, $mark), $utc)
        $unit = [decimal]::One
        for ($place = 1; $place -lt $precision; $place++) { $unit = $unit / 10 }
        if (($back -lt $Value) -eq ($Value -gt 0)) { $digits = [Math]::Abs($digits) + $unit } else { $digits = [Math]::Abs($digits) - $unit }
        if ($digits -ge 1 -and $digits -lt 10) {
            $sign = if ($Value -lt 0) { '-' } else { '' }
            $other = $sign + $digits.ToString('F' + ($precision - 1), $utc) + $text.Substring($mark)
            if ([double]::Parse($other, $utc) -eq $Value) { $text = $other; break }
        }
    }
    $sign = ''
    if ($text.StartsWith('-', [StringComparison]::Ordinal)) { $sign = '-'; $text = $text.Substring(1) }
    $exponent = 0
    $mark = $text.IndexOf('E')
    if ($mark -ge 0) {
        $exponent = [int]$text.Substring($mark + 1)
        $text = $text.Substring(0, $mark)
    }
    $dot = $text.IndexOf('.')
    if ($dot -lt 0) { $digits = $text; $point = $text.Length } else { $digits = $text.Remove($dot, 1); $point = $dot }
    $trimmed = $digits.TrimStart('0')
    $point -= $digits.Length - $trimmed.Length
    $digits = $trimmed.TrimEnd('0')
    if (-not $digits) { return $sign + '0.0' }
    # The value is 0.<digits> times ten to the power $point.
    $point += $exponent
    if ($point -gt -4 -and $point -le 16) {
        if ($point -le 0) { return $sign + '0.' + ('0' * -$point) + $digits }
        if ($point -ge $digits.Length) { return $sign + $digits + ('0' * ($point - $digits.Length)) + '.0' }
        return $sign + $digits.Substring(0, $point) + '.' + $digits.Substring($point)
    }
    $mantissa = $digits.Substring(0, 1)
    if ($digits.Length -gt 1) { $mantissa += '.' + $digits.Substring(1) }
    $power = $point - 1
    $powerSign = if ($power -lt 0) { '-' } else { '+' }
    return $sign + $mantissa + 'e' + $powerSign + [Math]::Abs($power).ToString('00', $utc)
}

function Add-Json([Text.StringBuilder]$Out, $Value) {
    # Write the same text as Python's json.dumps(ensure_ascii=True, separators=(",", ":")),
    # with object keys sorted by UTF-16 code unit. Stop writing objects and arrays once the
    # text is longer than $script:jsonCap; the caller then only needs to know that.
    if ($null -eq $Value) { $null = $Out.Append('null'); return }
    if ($Value -is [string]) {
        $null = $Out.Append('"')
        if ($jsonEscape.IsMatch($Value)) {
            $text = $Value.Replace('\', '\\').Replace('"', '\"').Replace([string][char]8, '\b').Replace([string][char]12, '\f')
            $text = $text.Replace([string][char]10, '\n').Replace([string][char]13, '\r').Replace([string][char]9, '\t')
            $null = $Out.Append($jsonOther.Replace($text, $jsonUnits))
        }
        else { $null = $Out.Append($Value) }
        $null = $Out.Append('"')
        return
    }
    if ($Value -is [int] -or $Value -is [long]) { $null = $Out.Append($Value.ToString($utc)); return }
    if ($Value -is [bool]) { $null = $Out.Append($(if ($Value) { 'true' } else { 'false' })); return }
    $isMap = $Value -is [Collections.IDictionary]
    if ($isMap -or $Value -is [Management.Automation.PSCustomObject]) {
        $names = [Collections.Generic.List[string]]::new()
        $items = [Collections.Generic.List[object]]::new()
        if ($isMap) { foreach ($entry in $Value.GetEnumerator()) { $names.Add([string]$entry.Key); $items.Add($entry.Value) } }
        else { foreach ($property in $Value.PSObject.Properties) { $names.Add($property.Name); $items.Add($property.Value) } }
        $keys = $names.ToArray()
        $values = $items.ToArray()
        [Array]::Sort([Array]$keys, [Array]$values, [Collections.IComparer][StringComparer]::Ordinal)
        $null = $Out.Append('{')
        for ($index = 0; $index -lt $keys.Length; $index++) {
            if ($Out.Length -gt $script:jsonCap) { return }
            if ($index) { $null = $Out.Append(',') }
            Add-Json $Out $keys[$index]
            $null = $Out.Append(':')
            Add-Json $Out $values[$index]
        }
        $null = $Out.Append('}')
        return
    }
    if ($Value -is [Collections.IEnumerable]) {
        $null = $Out.Append('[')
        $first = $true
        foreach ($item in $Value) {
            if ($Out.Length -gt $script:jsonCap) { return }
            if (-not $first) { $null = $Out.Append(',') }
            $first = $false
            Add-Json $Out $item
        }
        $null = $Out.Append(']')
        return
    }
    if ($Value -is [double] -or $Value -is [float]) { $null = $Out.Append((Format-JsonNumber $Value)); return }
    if ($Value -is [decimal]) {
        # Windows PowerShell parses JSON numbers with a fraction or exponent as decimal.
        # Parse the decimal's digits as a double, as Python parses the JSON text.
        $text = $Value.ToString($utc)
        if ($text.Contains('.') -or $text.Contains('E')) {
            if ($Value -eq 0 -and [decimal]::GetBits($Value)[3] -lt 0) { $text = '-0.0' }
            else { $text = Format-JsonNumber ([double]::Parse($text, $utc)) }
        }
        $null = $Out.Append($text)
        return
    }
    if ($Value -is [DateTime]) { Add-Json $Out $Value.ToString('o', $utc); return }
    if ($Value -is [IFormattable]) { $null = $Out.Append($Value.ToString($null, $utc)); return }
    Add-Json $Out ([string]$Value)
}

function ConvertTo-JsonText($Value, [int]$Cap = [int]::MaxValue) {
    # With a $Cap, text longer than $Cap is cut short: its length only says it is too long.
    $out = [Text.StringBuilder]::new()
    $script:jsonCap = $Cap
    try { Add-Json $out $Value }
    finally { $script:jsonCap = [int]::MaxValue }
    return $out.ToString()
}

function Measure-Text($Value) {
    # The printed size of $Value, or any size over $outputLimit when it is over.
    return (ConvertTo-JsonText $Value $outputLimit).Length
}

function Get-JsonByteCount([string]$Text) {
    # Bytes Add-Json writes for $Text, without quotes.
    $count = 0
    for ($index = 0; $index -lt $Text.Length; $index++) {
        $code = [int]$Text[$index]
        if ($code -eq 0x22 -or $code -eq 0x5C -or $code -eq 8 -or $code -eq 9 -or $code -eq 10 -or $code -eq 12 -or $code -eq 13) { $count += 2 }
        elseif ($code -ge 0x20 -and $code -lt 0x7F) { $count++ }
        else { $count += 6 }
    }
    return $count
}

function Limit-JsonBytes([string]$Value, [int]$Room) {
    # Shorten $Value so its JSON text, without quotes, is at most $Room bytes, ending in an
    # ellipsis. A surrogate pair is one code point of twelve bytes and is never split.
    if ((Get-JsonByteCount $Value) -le $Room) { return $Value }
    if ($Room -lt 6) { return '' }
    $used = 6
    $index = 0
    while ($index -lt $Value.Length) {
        $code = [int]$Value[$index]
        $units = 1
        if ($code -ge 0xD800 -and $code -le 0xDBFF -and $index + 1 -lt $Value.Length -and
            [int]$Value[$index + 1] -ge 0xDC00 -and [int]$Value[$index + 1] -le 0xDFFF) { $units = 2; $size = 12 }
        elseif ($code -eq 0x22 -or $code -eq 0x5C -or $code -eq 8 -or $code -eq 9 -or $code -eq 10 -or $code -eq 12 -or $code -eq 13) { $size = 2 }
        elseif ($code -ge 0x20 -and $code -lt 0x7F) { $size = 1 }
        else { $size = 6 }
        if ($used + $size -gt $Room) { break }
        $used += $size
        $index += $units
    }
    return $Value.Substring(0, $index) + [char]0x2026
}

function Copy-Shortened($Value, [int]$Cap, $Keep, [string]$Key) {
    # Copy $Value, shortening strings longer than $Cap code points unless their key is in
    # $Keep. Add the number shortened to $script:shortenedCount.
    if ($Value -is [string]) {
        if ($Value.Length -gt $Cap -and -not $Keep.Contains($Key)) {
            $short = Limit-Text $Value $Cap
            if ($null -ne $short) { $script:shortenedCount++; return $short }
        }
        return $Value
    }
    if ($Value -is [Collections.IDictionary]) {
        $copy = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
        foreach ($entry in $Value.GetEnumerator()) { $copy[[string]$entry.Key] = Copy-Shortened $entry.Value $Cap $Keep ([string]$entry.Key) }
        return $copy
    }
    if ($Value -is [Management.Automation.PSCustomObject]) {
        $copy = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
        foreach ($property in $Value.PSObject.Properties) { $copy[$property.Name] = Copy-Shortened $property.Value $Cap $Keep $property.Name }
        return $copy
    }
    if ($Value -is [Collections.IList]) {
        $copy = [object[]]::new($Value.Count)
        for ($index = 0; $index -lt $Value.Count; $index++) { $copy[$index] = Copy-Shortened $Value[$index] $Cap $Keep $Key }
        return ,$copy
    }
    return $Value
}

function Get-DefaultFields($Row) {
    # The row with only the default fields in its tags, or $null if it has no others.
    if ($Row -isnot [Management.Automation.PSCustomObject] -or $Row.tags -isnot [Management.Automation.PSCustomObject]) { return $null }
    $kept = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    $other = $false
    foreach ($property in $Row.tags.PSObject.Properties) {
        if ($rowFields -ccontains $property.Name) { $kept[$property.Name] = $property.Value } else { $other = $true }
    }
    if (-not $other) { return $null }
    $copy = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    foreach ($property in $Row.PSObject.Properties) { $copy[$property.Name] = $property.Value }
    $copy['tags'] = $kept
    return $copy
}

function Measure-Output($Result) {
    # The printed size, counting each next[].run as $runBytes, or any size over $outputLimit.
    $measured = $Result
    if ($Result.ContainsKey('next') -and @($Result.next).Count) {
        $measured = $Result.Clone()
        $measured.next = @(foreach ($item in $Result.next) { @{ why = $item.why; run = $runPlaceholder } })
    }
    return (Measure-Text $measured)
}

function Limit-RowMessage($Result) {
    # With -FullMessage, shorten the one kept row's message to fit the budget.
    if (-not @($Result.logs).Count) { return }
    $row = $Result.logs[0]
    if ($row -isnot [Collections.IDictionary] -or $row['tags'] -isnot [Collections.IDictionary]) { return }
    $tags = $row['tags']
    $message = $tags['message']
    if ($message -isnot [string]) { return }
    if (-not $row.ContainsKey('message_chars')) {
        $row['message_chars'] = Get-CodePointCount $message
        $Result.truncated_messages = 1 + [long]$Result.truncated_messages
    }
    $Result.shortened_strings = 1 + [long]$Result.shortened_strings
    $tags['message'] = ''
    $tags['message'] = Limit-JsonBytes $message ($outputLimit - (Measure-Output $Result))
}

function Format-Output($Result, $BudgetHint, [bool]$FullMessages) {
    # Serialize the result within $outputLimit bytes, as the Python client's fit_output does.
    # Shorten an echoed query or scope over $echoBytes. Then, only until the result fits:
    # shorten strings in lists to 200 and then 100 code points, never identifiers, and never
    # messages with -FullMessage; keep the leading items of each list that fit, where raw
    # rows keep only the default fields from the first row that does not fit with all of its
    # fields; with -FullMessage, keep the first row and shorten its message to fit.
    foreach ($name in $echoFields) {
        $value = $Result[$name]
        if ($value -is [string] -and (Get-JsonByteCount $value) + 2 -gt $echoBytes) {
            $Result[$name] = Limit-JsonBytes $value ($echoBytes - 2)
            $Result[$name + '_chars'] = Get-CodePointCount $value
        }
    }
    if ((Measure-Output $Result) -le $outputLimit) { return (ConvertTo-JsonText $Result) }
    $Result.budget_applied = $true
    if ($BudgetHint) {
        $hints = @()
        if ($Result.ContainsKey('next')) { $hints = @($Result.next) }
        if (-not @($hints | Where-Object { $_.run -ceq $BudgetHint.run -and $_.why -ceq $BudgetHint.why }).Count) {
            $Result.next = @($hints) + @($BudgetHint)
        }
    }
    $keep = $budgetKeep
    if ($FullMessages) {
        $keep = [Collections.Generic.HashSet[string]]::new($budgetKeep, [StringComparer]::Ordinal)
        $null = $keep.Add('message')
    }
    $names = @(foreach ($name in $budgetLists) {
        if ($Result.ContainsKey($name) -and $Result[$name] -is [Collections.IList] -and $Result[$name].Count) { $name }
    })
    $source = @{}
    foreach ($name in $names) { $source[$name] = @($Result[$name]) }
    $rows = $null
    if ($Result.mode -ceq 'raw' -and $source.ContainsKey('logs')) { $rows = 'logs' }
    $cache = @{}

    function Get-BudgetItem([string]$Name, [int]$Index, [int]$Cap, [bool]$Strip) {
        # One list item shortened at $Cap: its value, size, strings shortened and whether fields were dropped.
        $cacheKey = "$Name $Index $Cap $Strip"
        if (-not $cache.ContainsKey($cacheKey)) {
            $value = $source[$Name][$Index]
            $stripped = $null
            if ($Strip) { $stripped = Get-DefaultFields $value }
            if ($null -ne $stripped) { $value = $stripped }
            $script:shortenedCount = 0
            $copy = Copy-Shortened $value $Cap $keep $Name
            $cache[$cacheKey] = @{ value = $copy; size = (Measure-Text $copy); count = $script:shortenedCount; stripped = ($null -ne $stripped) }
        }
        return $cache[$cacheKey]
    }
    function Get-WholeList([string]$Name, [int]$Cap) {
        return ,@(for ($index = 0; $index -lt $source[$Name].Count; $index++) { Get-BudgetItem $Name $index $Cap $false })
    }
    function Get-ListSize($Entries) {
        $total = 0
        foreach ($entry in $Entries) { $total += $entry.size }
        if ($Entries.Count) { $total += $Entries.Count - 1 }
        return $total
    }

    # Measure everything but the list items once, with every counter at its largest value.
    foreach ($name in $names) { $Result[$name] = @() }
    $largest = @{}
    foreach ($name in $names) { $largest[$name] = $source[$name].Count }
    if ($rows) { $largest['fields'] = $source[$rows].Count }
    $probe = $Result.Clone()
    $probe.shortened_strings = $counterReserve
    $probe.omitted = $largest
    $fixed = Measure-Output $probe

    $chosen = @{}
    $fitted = $false
    foreach ($cap in $budgetCaps) {
        $total = $fixed
        $fitted = $true
        foreach ($name in $names) {
            for ($index = 0; $index -lt $source[$name].Count -and $fitted; $index++) {
                $total += (Get-BudgetItem $name $index $cap $false).size
                if ($index) { $total++ }
                if ($total -gt $outputLimit) { $fitted = $false }
            }
            if (-not $fitted) { break }
        }
        if ($fitted) {
            foreach ($name in $names) { $chosen[$name] = Get-WholeList $name $cap }
            break
        }
    }
    if (-not $fitted) {
        for ($position = 0; $position -lt $names.Count; $position++) {
            $name = $names[$position]
            $total = $fixed
            for ($other = 0; $other -lt $names.Count; $other++) {
                if ($other -lt $position) { $total += Get-ListSize $chosen[$names[$other]] }
                elseif ($other -gt $position) { $total += Get-ListSize (Get-WholeList $names[$other] $cap) }
            }
            $kept = [Collections.Generic.List[object]]::new()
            $strip = $false
            for ($index = 0; $index -lt $source[$name].Count; $index++) {
                $comma = if ($kept.Count) { 1 } else { 0 }
                $entry = Get-BudgetItem $name $index $cap $strip
                if ($name -ceq $rows -and -not $strip -and $total + $comma + $entry.size -gt $outputLimit) {
                    $strip = $true
                    $entry = Get-BudgetItem $name $index $cap $strip
                }
                if ($total + $comma + $entry.size -gt $outputLimit) {
                    if ($FullMessages -and $name -ceq $rows -and -not $kept.Count) {
                        $kept.Add($entry)
                        $total += $entry.size
                    }
                    break
                }
                $kept.Add($entry)
                $total += $comma + $entry.size
            }
            $chosen[$name] = $kept.ToArray()
            if ($total -le $outputLimit) {
                for ($other = $position + 1; $other -lt $names.Count; $other++) { $chosen[$names[$other]] = Get-WholeList $names[$other] $cap }
                break
            }
        }
    }

    $omitted = @{}
    $shortened = 0
    foreach ($name in $names) {
        $entries = @($chosen[$name])
        $Result[$name] = @(foreach ($entry in $entries) { , $entry.value })
        foreach ($entry in $entries) { $shortened += $entry.count }
        if ($entries.Count -lt $source[$name].Count) { $omitted[$name] = $source[$name].Count - $entries.Count }
    }
    if ($rows) {
        $stripped = @($chosen[$rows] | Where-Object { $_.stripped }).Count
        if ($stripped) { $omitted['fields'] = $stripped }
    }
    if ($omitted.Count) { $Result.omitted = $omitted }
    if ($shortened) { $Result.shortened_strings = $shortened }
    if ($FullMessages -and $rows -and (Measure-Output $Result) -gt $outputLimit) { Limit-RowMessage $Result }
    if ((Measure-Output $Result) -gt $outputLimit) {
        # Reached when identifiers the shrink steps never shorten (service names,
        # fingerprints, timestamps) are too long on their own: drop every list item.
        $all = @{}
        foreach ($name in $names) { $Result[$name] = @(); $all[$name] = $source[$name].Count }
        $Result.omitted = $all
        $Result.Remove('shortened_strings')
        if ((Measure-Output $Result) -gt $outputLimit) {
            $Result = @{ start = $Result.start; end = $Result.end; budget_applied = $true; error = $budgetFailed }
        }
    }
    return (ConvertTo-JsonText $Result)
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
    if ($Command -eq 'logs' -and ($Severity -or $PSBoundParameters.ContainsKey('Samples'))) {
        throw 'Use -Severity and -Samples with issues.'
    }
    if ($Command -eq 'issues' -and ($Level -or $Query -or $Field -or $Raw -or $AllFields -or $FullMessage)) {
        throw 'Use -Level, -Query, -Field, -Raw, -AllFields and -FullMessage with logs.'
    }
    $discovery = $Command -in 'tags', 'values'
    if ($discovery -and ($Severity -or $Fingerprint -or $Field -or $Contains -or $NotContains -or $Grep -or $NotGrep -or
        $Raw -or $AllFields -or $FullMessage -or $PSBoundParameters.ContainsKey('Samples'))) {
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
    if ($Field -and $AllFields) { throw 'Use either -Field or -AllFields.' }
    if ($Field.Count -gt $maxFields) { throw "Use at most $maxFields -Field values." }
    foreach ($name in $Field) {
        if ($name -cnotmatch $labelName -or $name.Length -gt $maxNameChars) {
            throw 'Expected -Field with a label name such as message or k8s_namespace_name.'
        }
    }
    # Projection and full messages apply to rows.
    if ($Field -or $AllFields -or $FullMessage) { $Raw = [switch]$true }

    # Undocumented test hook: a fixed current time.
    $now = [DateTimeOffset]::UtcNow
    if (([string]$env:CARDINAL_QUERY_NOW).Trim()) { $now = Convert-QueryTime ([string]$env:CARDINAL_QUERY_NOW).Trim() }
    # Without -End the window ends now, and counts include the newest, partial bucket.
    $openEnd = -not $End
    $last = if ($End) { Convert-QueryTime $End } else { $now }
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
    $script:budgetHint = $null
    if ($Command -eq 'logs') {
        $requested = if ($PSBoundParameters.ContainsKey('Limit')) { $Limit } elseif ($Raw) { 20 } else { 10 }
        $count = [Math]::Min($requested, $maxLogLimit)
        $filters = New-LineFilters 's'
        $selector = (Get-LogSelector $Fingerprint '') + $filters
        if ($Raw) { $result = Get-RawLogs $window $IDs $selector $count }
        else { $result = Get-LogPatterns $window $IDs $first $last $selector $filters $count }
        if ($count -lt $requested) { $result.limit_clamped_from = $requested }
    }
    elseif ($Command -eq 'tags') { $result = Get-TagNames $window $IDs }
    elseif ($Command -eq 'values') { $result = Get-TagValues $window $IDs }
    else { $result = Get-IssuePatterns $window $IDs $first $last }
    # The output is ASCII JSON, so the console encoding cannot change it.
    $text = Format-Output ($window + $result) $script:budgetHint ([bool]$FullMessage)
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $text
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
