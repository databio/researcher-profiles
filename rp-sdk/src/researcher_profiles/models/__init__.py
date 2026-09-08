"""The Pydantic models exchanged over HTTP between the server and client, plus the models of the published standard.

These are the request/response bodies the HTTP server and client exchange
(:mod:`.api`) and the models of the published researcher-profiles standard
(:mod:`.published`) -- distinct from :mod:`researcher_profiles.schema`, which
is the on-disk profile document itself. Nothing here imports the server, so a
bare install can construct these without the ``api`` extra.

:mod:`.results` also lives in this subpackage, for adjacency to what it is not:
its dataclasses are in-process return values, never serialized wire models, so
nothing here re-exports them.
"""
