# Integrating the Grader

The grader is a standalone HTTP service. The core does **not** require LDR or any
particular research tool. Any pipeline can submit a report, poll the job, and
read the grade.

## Contract

| Item | Value |
|---|---|
| Base URL | `http://<host>:9131` |
| Auth | `X-API-Token: <token>` or `Authorization: Bearer <token>` |
| Submit | `POST /v1/papers` |
| Poll | `GET /v1/jobs/{job_id}` |
| Read grade | `GET /v1/grades/{grade_id}` or `GET /v1/papers/{paper_id}/current-grade` |
| Discover | `GET /v1/stats`, `GET /v1/scores` |

`POST /v1/papers` accepts:

| `source_type` | Required field | Use |
|---|---|---|
| `text` | `content` | Send the report body directly |
| `url` | `url` | Fetch a public HTML/PDF URL |
| `file` | `path` **or** `content_base64` + `filename` | A path inside the container, or a self-contained upload |
| `ldr` | `job_id` | Optional LDR adapter only |

Common optional fields: `title`, `query_text`, `query_id`, `rubric_id`,
`model_id`, `metadata` (free-form JSON), `grade` (default `true`), and `wait`
(default `false`; when true the request blocks up to 300 seconds).

```bash
curl -sS http://localhost:9131/v1/papers \
  -H "X-API-Token: $GRADER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"source_type":"text","title":"My report","content":"# Report\n...","wait":true}'
```

Without `wait`, the response is immediate with a `job_id`; poll it:

```bash
curl -sS http://localhost:9131/v1/jobs/JOB_ID \
  -H "X-API-Token: $GRADER_API_TOKEN"
```

## Rubrics

The default rubric is `generic_research_v1`, a source-agnostic research-report
rubric. To add a rubric without rebuilding the image:

1. Put `<rubric_id>.md` in a host directory.
2. Mount it read-only at `/config/rubrics`.
3. Set `GRADER_RUBRIC_OVERLAY=/config/rubrics` in `.env`.
4. Submit with `"rubric_id":"<rubric_id>"`.

The overlay is searched before the shipped rubrics; the shipped `generic.md` is
still used when a custom rubric does not define one. A custom `registry.json` in
the overlay may provide `calibration_status` and `review_after`.

To provide checklist-style query mappings, mount a `queries.json` and set
`GRADER_QUERIES_FILE=/config/rubrics/queries.json`. Without it, unknown queries
fall back to `generic_research_v1`, which is safe for arbitrary pipelines.
Use `generic_research_v1` or your own rubric for another pipeline.

## LDR is optional

The LDR adapter is disabled by default. Enable it only when this deployment
should poll an LDR instance:

```dotenv
GRADER_LDR_ENABLED=true
GRADER_LDR_BASE=http://ldr-host:5000
LDR_USER=...
LDR_PASS=...
```

The watcher baselines existing LDR history on first start, then grades new
completed jobs. A pipeline that is not LDR should simply call
`POST /v1/papers`; no LDR code or credentials are needed.

`POST /v1/grades` with `{"paper_id":"...","force":true,"wait":true}` re-runs
the judges even when a cached grade exists. The new grade becomes the canonical
current grade; the previous row is retained as history with a
`<cache_key>:previous:<grade_id>` key.

## Batches

`POST /v1/batches` with `{"paper_ids":[...]}` grades many already-registered
papers. Read progress at `GET /v1/batches/{batch_id}` and export with
`GET /v1/batches/{batch_id}/export?format=csv`.
