# Phase 4 release

Included in this release:
- Phase 1 production hardening
- Phase 2 marketplace-quality workflows
- Phase 3 automated supplier inventory ingestion
- Phase 4 canonical parts catalogue, aliases and inventory linkage
- sourced/reviewed supersession and compatibility graph
- catalogue-grounded sourcing assistant
- optional OpenAI natural-language intent extraction
- optional OpenAI photo part identification with local visible-text fallback
- catalogue administration and audit/history integration
- living `SETUP_AND_TESTING.md` with deployment and external-validation checklist

Automated validation at packaging time:
- `PHASE4_SMOKE_TEST_OK`
- `PHASE3_SMOKE_TEST_OK`
- `PHASE2_SMOKE_TEST_OK`
- all Jinja templates compile
- both Compose YAML files parse
- Python modules/scripts compile

See `SETUP_AND_TESTING.md` for the items that still require real credentials/infrastructure validation.
