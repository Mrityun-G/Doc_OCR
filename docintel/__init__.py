"""docintel - relationship-aware document understanding for intake-style PDFs.

Pipeline:  PDF -> words / widgets -> sections -> record-bound observations
           -> entities + relationships (with per-attribute traces)
           -> validation -> DOC_xxx.json -> standardized reference-template PDF
"""

__version__ = "1.0.0"
SCHEMA_VERSION = "1.0"
