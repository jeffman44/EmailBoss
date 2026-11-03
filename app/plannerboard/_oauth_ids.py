"""Built-in OAuth client ids baked into a release build.

The release script fills these from the build environment (PB_GMAIL_CLIENT_ID,
PB_GMAIL_CLIENT_SECRET, PB_GRAPH_CLIENT_ID, held as CI secrets) and restores
the file afterwards. A source checkout keeps them empty and the wizard falls
back to the app-password path. The Google "secret" for an installed app is not
confidential (the flow is protected by PKCE and the loopback redirect).
"""
GMAIL_CLIENT_ID = ""
GMAIL_CLIENT_SECRET = ""
GRAPH_CLIENT_ID = ""
