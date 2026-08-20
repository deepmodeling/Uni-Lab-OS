# OpenLab virtual environment presets

UniLabOS exposes a deliberately narrow demo-environment operation for OpenLab. It rebuilds the
active `materials.db` material trees while preserving resource templates and the append-only
inventory ledger.

## Start in virtual mode

```bash
unilab --graph <virtual-graph.json> --backend hostlink --test_mode
```

`--test_mode` makes device actions return simulated results and is also the mandatory safety gate
for material-library reset. A normal UniLabOS process may list the presets, but reset returns HTTP
403.

## API

```text
GET  /api/v1/materials/virtual-environments
POST /api/v1/materials/virtual-environments/{organic|biology|materials}/reset
```

The POST body is:

```json
{
  "request_uuid": "1ee8e6ce-26e7-4be5-9d7e-cfb948121354",
  "confirmation": "reset-virtual-materials"
}
```

The request UUID is reused as the materials command UUID. Retiring each existing root and creating
the selected preset use separate effect keys, so retrying the same request is idempotent. The
created root carries `meta_data.openlab_virtual_environment` with the preset, setup UUID, and
timestamp.

The operation intentionally resets only active Material trees. It does not delete template rows,
truncate the ledger, change Workflow Authority state, or reload the device graph. Run it only when
no Task depends on existing Material UUIDs.
