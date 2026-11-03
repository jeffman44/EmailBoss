"""Mail layer: providers that produce Power Automate shaped records.

Every provider (drop folder, IMAP, Gmail API, Microsoft Graph) ends in the same
place: a JSON record per message under ``<mail_cache>/{inbox,sent}/`` plus the
attachment bytes under ``<mail_cache>/attachments/<id>/``. The triage layer only
ever reads those files, so it never learns which provider wrote them.
"""
