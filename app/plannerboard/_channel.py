"""Build channel. Source checkouts are "dev"; the release scripts flip this
to "release" for the wheel and the bundle they produce (and restore it).

Developer mode (PB_LICENSE_DEV=1 / --dev-license) only works on the "dev"
channel, so a shipped build cannot be talked out of its license check by an
environment variable or a CLI flag.
"""
CHANNEL = "dev"
