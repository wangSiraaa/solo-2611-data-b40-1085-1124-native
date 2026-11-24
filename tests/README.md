# Tests

Run the suite from the repository root:

```bash
python3 -m pytest tests -q
TELBER_FULL_SCALE=1 python3 -m pytest tests/test_scale.py -q
```

Modules:

| file | focus |
| --- | --- |
| `conftest.py` | fixtures: strict schema builder, small products |
| `test_ber_wire.py` | tag/length encoding, minimality, indefinite rules |
| `test_schema.py` | directory strictness, ambiguity, depth/field limits, revisions |
| `test_codec.py` | decoding against independently built bytes and provenance |
| `test_nesting.py` | all definite/indefinite nestings, depth 32 |
| `test_stream.py` | block-size invariance, truncation, bounded memory, no resync |
| `test_repository.py` | product binding, relation rows, partial imports |
| `test_service.py` | artifact recompilation, presence/type preservation |
| `test_demo_truth.py` | demo relation table against independent truth |
| `test_scale.py` | 100 MiB / 1 MiB records / 2 MiB body cap (opt-in) |
| `test_pigeon_book.py` | clock-book import, provenance ranges, domain checks, sealed vs truncated |
| `test_pigeon_engine.py` | FIFO queue coupling, tri-state ring reads, at-most-once arrivals |
| `test_pigeon_certificate.py` | stamp/pending/revoke, opposing witnesses, anonymised receipts, exclusive cross-check |
| `test_pigeon_review.py` | both review organisations, same-final-beat honesty, non-transferable film rights |
| `test_pigeon_demo.py` | worked meet outcomes against independent expectations |
