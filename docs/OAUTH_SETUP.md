# Built-in sign-in: one-time setup for the publisher

"Sign in with Google" and "Sign in with Microsoft" need an app registration
owned by the publisher. Create each once, paste three values into the GitHub
repository secrets, and every release from then on bakes them in
(`app/plannerboard/_oauth_ids.py`, filled by `scripts/build_release.ps1`).
Without them the wizard still works through app passwords.

## Google (Gmail)

1. console.cloud.google.com, create a project "Email Boss".
2. APIs & Services > Library: enable the Gmail API.
3. OAuth consent screen: External, app name Email Boss, your support email,
   scopes `gmail.modify`. Publish the app (or keep it in Testing and add the
   tester addresses while you trial).
4. Credentials > Create credentials > OAuth client ID > Desktop app.
5. Secrets: `PB_GMAIL_CLIENT_ID` = the client id, `PB_GMAIL_CLIENT_SECRET` =
   the client secret (not confidential for a desktop app; PKCE protects the
   flow).

## Microsoft (Outlook, Microsoft 365)

1. entra.microsoft.com (or portal.azure.com) > App registrations > New.
2. Name Email Boss; supported account types: "Accounts in any organizational
   directory and personal Microsoft accounts".
3. Redirect URI: platform "Mobile and desktop applications", add
   `http://localhost` (the app listens on a loopback port with PKCE).
4. Authentication: enable "Allow public client flows".
5. API permissions: Microsoft Graph delegated `Mail.ReadWrite`, `Mail.Send`,
   `offline_access`, `User.Read`.
6. Secret: `PB_GRAPH_CLIENT_ID` = the Application (client) ID. No secret.

## GitHub

Repository > Settings > Secrets and variables > Actions > New repository
secret, for each of the three names above. Re-run the release workflow.
