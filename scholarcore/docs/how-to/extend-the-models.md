# How to extend the models

Two ways to carry extra data on a core model, and two rules for reading
records another system wrote.

!!! info "Prerequisites"
    - `scholarcore`
    - A model you want to store more data on than the core defines

## Add your own fields

Subclass the core model and declare the fields you need:

```python
from scholarcore import Award


class TrackedAward(Award):
    """An award plus the fields our review workflow needs."""

    reviewer: str | None = None
    internal_priority: int = 3


award = TrackedAward(
    application_id="1R01GM123456",
    title="Chromatin accessibility across cell types",
    reviewer="jdoe",
)
```

Your subclass validates and serializes the core fields exactly as `Award` does,
so a consumer that knows only `Award` can still read what you write.

Store your fields alongside the core ones and keep the canonical identifier on
every record. That identifier is what lets another system join to your data
later.

## Extend without subclassing

Every model sets `extra="allow"`, so a field you did not declare is accepted
and preserved through a round trip:

```python
from scholarcore import Award

award = Award(
    application_id="1R01GM123456",
    title="Chromatin accessibility across cell types",
    reviewer="jdoe",  # not a declared field
)

award.model_dump()["reviewer"]  # 'jdoe'
```

Use this for data passing through your system. Use a subclass for data your
system owns, so the fields are typed, validated, and visible to anyone reading
your code.

## Read records you did not write

Two rules make a record from another system safe to load.

**Unknown fields are kept, not rejected.** A producer that adds a field does
not break your reader. Access it through `model_extra` when you need it:

```python
award = Award.model_validate(payload)
award.model_extra.get("reviewer")
```

**Unknown enum values load as `None`.** `Award.status`, `Award.role`, and
`Award.submission_type` accept any value; anything outside the declared
vocabulary becomes `None` rather than raising:

```python
Award(title="…", status="active").status  # AwardStatus.active
Award(title="…", status="under_appeal").status  # None
```

So check for `None` when you branch on one of those fields:

```python
if award.status is None:
    ...  # unknown or unset; do not assume a default
```

## Keep your own vocabulary

When your system has a finer-grained vocabulary than a core enum, write the
core value to the enum field and keep yours in a separate field:

```python
class TrackedAward(Award):
    local_status: str | None = None  # our full lifecycle vocabulary


TrackedAward(
    application_id="1R01GM123456",
    title="Chromatin accessibility across cell types",
    status="pending",  # the core value every reader understands
    local_status="awaiting_council",  # ours
)
```

Every consumer gets a value it can act on, and none of them lose your detail.

## Validate from another language

The generated JSON Schemas in `scholarcore/schemas/` describe the core models,
so a non-Python consumer can validate a record without installing the package.
Regenerate them after any model change:

```python
from scholarcore.schema_export import export_schemas

export_schemas("schemas")
```

A test compares the committed schemas against the models and fails when they
differ, so a model change that skips this step is caught before it ships.
