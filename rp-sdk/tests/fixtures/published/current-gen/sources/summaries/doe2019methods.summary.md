Presents a standardized imaging mass cytometry (IMC) workflow covering antibody
panel design, whole-slide tiling and stitching, cell segmentation, and
single-cell marker quantification. The central contribution is treating
segmentation as a source of quantifiable per-cell uncertainty rather than a
one-shot preprocessing step: each cell carries a confidence score that
propagates into downstream quantification and, in later work, into domain
calls. The tiling and stitching pipeline preserves marker continuity across
tile boundaries, which the authors show matters for any downstream spatial
analysis. Benchmarked against three prior segmentation tools on multi-region
cohorts, with the largest gains in tissue regions with irregular cell density.
