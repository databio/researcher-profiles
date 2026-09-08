# Expertise

## Methods

Rivas-Keller's core methodological contribution is a graph-based approach to
discovering tissue domains directly from multiplexed spatial proteomics data,
treating each cell as a node in a spatial-expression graph rather than
clustering on expression alone [doe2020spatial]. The method builds a joint
neighborhood graph from marker intensity and physical position, then applies
unsupervised community detection to recover domains that respect both
signal and geography [doe2020spatial].

For imaging mass cytometry specifically, the group has published a
standardized panel-design and segmentation workflow that other labs have
adopted wholesale [doe2019methods]. That workflow treats segmentation error
as a first-class source of uncertainty rather than an afterthought, and
propagates per-cell confidence scores through to the downstream domain
calls [doe2019methods].

Whole-slide processing at the scale required for multi-region cohorts
depends on a tiling and stitching pipeline described in the same methods
paper [doe2019methods], which parallelizes marker quantification across
tiles while preserving neighborhood continuity at tile boundaries.

Spatial statistics work in the group emphasizes honest null models: cell-cell
neighborhood enrichment is always reported against a spatially-constrained
permutation null, not a naive random-labeling null, because the latter
systematically overstates significance in tissues with any large-scale
gradient [doe2020spatial]. A synthesis of this null-model critique across
the broader spatial statistics literature appears in a review the group
co-authored [smith2021review].

Where spatial transcriptomics and protein imaging are combined, registration
between modalities is handled with a landmark-free alignment step, evaluated
against the same permutation-null framework used elsewhere in the group's
work [doe2020spatial].

## Intellectual lineage

The graph-based domain-discovery work traces directly to unsupervised
community-detection methods from network science, adapted here to carry a
spatial weighting term [doe2020spatial]. The imaging and segmentation
pipeline descends from a lineage of quantitative microscopy tools, but
departs from that lineage by treating segmentation uncertainty as data to
be propagated rather than noise to be minimized away [doe2019methods].

The broader methodological stance, that a null model is not an
afterthought but the thing that determines whether a result means anything,
is argued at length in the review paper and is the throughline connecting
the group's imaging work to its statistics work [smith2021review].

## Recurring critiques

A recurring critique in this body of work is that naive random-labeling
null models for spatial enrichment are still the default in most published
tissue imaging studies, despite being known to inflate significance in any
tissue with structure at a scale larger than a few cell diameters
[doe2020spatial, smith2021review]. Rivas-Keller has argued this point in
review and in print, and treats it as close to a bright line for judging
whether a spatial statistics result should be trusted [smith2021review].

A second recurring critique concerns segmentation: many imaging pipelines
report a single "best" segmentation and propagate it as ground truth, which
silently discards uncertainty that matters most exactly where domain
boundaries are being drawn [doe2019methods]. The group's position is that
per-cell confidence should be carried through every downstream analysis
step, not discarded after QC [doe2019methods, doe2020spatial].

## Career trajectory

Early work centered on imaging mass cytometry method development, including
the panel-design and segmentation pipeline that became a lab standard
[doe2019methods]. This shifted toward spatial statistics and unsupervised
domain discovery once it became clear that segmentation alone could not
answer questions about tissue architecture [doe2020spatial]. The most recent
phase of the work has been synthetic and critical rather than purely
methodological: consolidating the null-model argument into a review aimed
at the broader field [smith2021review], while continuing to apply the
domain-discovery method to new tissue types [doe2020spatial].
