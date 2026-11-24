"""Independent catalog + citation repository.

The package deliberately does not read PDFs or images.  A volume is an ordered
list of physical pages; a page identity is ``(volume_id, page_id)``.  Citation
products are exported as JSONL and can be consumed without opening SQLite.

Two optional layers build on the catalog/repository without changing its
schema or export format: ``wind`` reads citations as calibration flights and
infers the per-band winds, and ``docking`` plans layer transitions for a
partially observed docking script.
"""

__version__ = "1.1.0"
