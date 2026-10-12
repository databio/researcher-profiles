"""The Pydantic models exchanged over HTTP between the server and client, plus the models of the published standard.

Distinct from :mod:`researcher_profiles.schema`, the on-disk profile document.
Nothing here imports the server, so these work without the ``api`` extra.
:mod:`.results` holds in-process return values, not wire models.
"""
