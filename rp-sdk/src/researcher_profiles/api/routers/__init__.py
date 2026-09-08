"""The FastAPI route modules, one per surface area.

Each module attaches its endpoints to the shared routers defined in
:mod:`researcher_profiles.api._projection` (``public_router``, ``router``,
``edit_router``); importing the module is what registers its routes. The
grouping here is by surface: read, search, edit, push, identity, generative.
"""
