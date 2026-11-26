# Slack app

Create the app from the manifest instead of clicking through settings.

1. Open <https://api.slack.com/apps> → **Create New App** → **From a manifest** → pick your workspace → paste [`slack-app-manifest.yml`](../slack-app-manifest.yml) → **Create**.
2. **Basic Information → App-Level Tokens → Generate Token** with scope `connections:write`. This is `SLACK_APP_TOKEN` (`xapp-…`).
3. **Install App → Install to Workspace.** The *Bot User OAuth Token* is `SLACK_BOT_TOKEN` (`xoxb-…`).
4. Put both tokens in `.env` and invite the bot to the channels it should work in. Its display name is set in the Slack app (`BOT_NAME` only changes how the assistant names itself).

The manifest cannot create the tokens, which is why steps 2 and 3 stay manual.

## What the manifest sets
```yaml
display_information:
  name: Dex
  description: Reviews pull requests and answers questions about your code from Slack.
features:
  bot_user:
    display_name: Dex
    always_online: true
oauth_config:
  scopes:
    bot:
      - "app_mentions:read"   # @Dex mentions
      - "channels:history"    # read the thread a message is in (public channels)
      - "groups:history"      # same for private channels
      - "chat:write"          # reply in the thread
      - "reactions:write"     # status reactions on the message
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.channels
      - message.groups
  interactivity:
    is_enabled: false
  socket_mode_enabled: true      # no public URL needed
  org_deploy_enabled: false
  token_rotation_enabled: false
```
The scopes are the minimum the code uses (post a message, read a thread, add and remove a reaction). The file in the repo has the same content with more comments.
