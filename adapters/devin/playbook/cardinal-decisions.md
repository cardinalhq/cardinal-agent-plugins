# Cardinal: record material decisions

Attach this playbook, together with the structured output schema from
`cardinal-devin decision-schema`, when you create a session. Cardinal
reads the session's structured output when the session finishes and
records each decision against the session's pull request.

## Instructions for Devin

While you work, keep the session's structured output up to date with the
material decisions you make. The structured output is a JSON object with
a single `decisions` array.

Record a decision when the work raised a real question and you picked
one answer over others. Good candidates: an approach, a library, a data
shape, an interface, an error-handling strategy, what to leave out of
scope, or a trade-off the reviewer should know about. Skip routine steps
such as running tests, formatting, or following an existing pattern.

For each decision:

- `choice` (required): the option you chose, in a few words.
- `question`: the question the work raised.
- `rationale`: why you chose it, in one or two sentences.
- `alternatives`: the options you considered and rejected.
- `decided_by`: `user` if the user told you what to do, otherwise `agent`.
- `anchors`: the code the decision concerns. Each anchor has a `kind`
  (`file`, `directory`, `symbol`, `interface`, `schema`, `config`,
  `workflow`, `benchmark`) and an `identifier`. For `file` and
  `directory`, the identifier is the repo-relative path. For other kinds,
  it is the name, with `path` set to the file it lives in.
- `id`: optional. Lowercase letters, digits, `.`, `_`, `-`. Give one when
  a later decision needs to point at it.
- `follows_from`, `refines`, `supersedes`: ids of earlier decisions this
  one builds on, narrows, or replaces. If you change your mind, add a new
  decision that supersedes the old one rather than deleting it.

Rules:

- Record what actually happened. Do not invent alternatives you did not
  consider.
- Keep entries short. A decision is a record, not a transcript.
- Update the structured output as decisions are made, not only at the
  end, so nothing is lost if the session stops early.
- Aim for a handful of decisions per session, not dozens.

## Example

```json
{
  "decisions": [
    {
      "id": "retry-backoff",
      "question": "How should the sync client retry on HTTP 429?",
      "choice": "Exponential backoff capped at 60 seconds",
      "rationale": "The upstream API rate-limits under load; the cap bounds worst-case latency.",
      "decided_by": "agent",
      "alternatives": ["Fixed 5 second delay", "No retry"],
      "anchors": [
        {"kind": "file", "identifier": "src/sync/retry.py"},
        {"kind": "symbol", "identifier": "SyncClient.fetch", "path": "src/sync/client.py"}
      ]
    }
  ]
}
```
