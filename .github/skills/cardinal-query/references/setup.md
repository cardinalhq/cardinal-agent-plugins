# Set up Cardinal Query in GitHub Copilot

Extract the skill ZIP and copy the entire `cardinal-query` folder into `.github/skills/cardinal-query` at the root of the repository where you use Copilot. For a personal CLI install, copy it to `~/.copilot/skills/cardinal-query` instead.

Every environment that runs the skill needs Python 3.9 or newer and network access to the Cardinal Data Lake Query API. Configure these two values in the environment where Copilot runs commands.

| Name | Value |
| --- | --- |
| `CARDINAL_QUERY_URL` | Your HTTPS Data Lake Query API base URL, such as `https://query.example.com`, or the full URL ending in `/api/v1/logs/query`. |
| `CARDINAL_QUERY_API_KEY` | The organization-scoped Data Lake key issued for that endpoint. |

## Get a Query API key

Ask your Cardinal Data Lake administrator for your organization's Query API URL and a dedicated organization API key for Copilot. If Cardinal manages your deployment, ask your Cardinal contact to arrange the key. The Data Lake administrator can create it with `POST /api/v1/organizations/{org_id}/api-keys` on the Data Lake Admin API, using a name such as `copilot-query`. The response contains the secret in `full_key`. Save it in your organization's secret manager and use that value for `CARDINAL_QUERY_API_KEY`.

For an active Data Lake integration in Cardinal UI, an organization owner can also open **Settings → API Keys** and click **Create Key** in the workspace's **Organization API Keys** panel. Older versions label the panel **Ingest API Keys**. The resulting organization key also authenticates Query API requests. This UI flow updates the key Cardinal uses for the integration, so use the Data Lake Admin API if you need a dedicated credential without changing that integration. The UI displays the secret once. The skill uses the Data Lake organization key. Personal access tokens and MCP keys are separate credentials.

[Cardinal Query API authentication](https://docs.cardinalhq.io/data-lake/api#authentication)

## Copilot CLI

Set both variables in the terminal session using your organization's secret manager and start `copilot` from the repository. If the CLI is already running, restart it to pick up environment changes. Run `/skills list` to confirm that `cardinal-query` appears. Allow the skill's Python command when prompted.

[GitHub's CLI skill installation guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills)

## IntelliJ IDEA

Update to the latest compatible GitHub Copilot extension. GitHub's matrix lists agent skills in preview starting at version 1.5.64. Open the repository in IntelliJ and start an **Agent** session in Copilot Chat. Launch IntelliJ with the two variables supplied in its process environment so the agent's command runner inherits them. Restart the IDE after changing that environment. If the extension does not expose agent skills in your setup, use Copilot CLI in IntelliJ's integrated terminal with the same repository install.

[GitHub's JetBrains feature matrix](https://docs.github.com/en/copilot/reference/copilot-feature-matrix?tool=jetbrains)

## GitHub.com

Commit the skill folder to a repository branch that the Copilot agent can use. In the repository's **Settings → Secrets and variables → Agents**, add `CARDINAL_QUERY_API_KEY` as an Agents secret. For an Actions-backed session, add `CARDINAL_QUERY_URL` as an Agents variable. For a cloud sandbox session, add `CARDINAL_QUERY_URL` as a second Agents secret, since Agents variables are available to Actions-backed sessions.

Allow the Query API host in the repository's Copilot **Internet access** settings, and ensure the selected agent environment can reach it. Start a **Copilot agent session** on that repository and select the branch containing the skill. If you start in the older GitHub.com chat experience, open the repository's **Agents** tab to start the session with a shell.

[GitHub's agent session guide](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-on-github) · [Agents secrets and variables](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/configure-secrets-and-variables) · [Copilot internet access settings](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-the-firewall)

## Try it

Ask Copilot “Use cardinal-query to find checkout logs containing timeout in the last 30 minutes.” Then ask “Show the top Cardinal Issues for checkout in the same window, grouped by severity and recurring pattern.”

With the variables set, you can also run the same commands directly from the repository root.

```sh
python3 .github/skills/cardinal-query/scripts/cardinal_query.py logs --service checkout --since 30m --contains timeout
python3 .github/skills/cardinal-query/scripts/cardinal_query.py issues --service checkout --since 30m
```
