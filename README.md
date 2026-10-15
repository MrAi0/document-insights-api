# Document Insights API

A backend service that accepts document text, summarizes it in the background and lets clients poll for the result. The summarization is mocked: each job takes 10–30 seconds and about 10% of attempts fail.

**Stack:** Python 3.12, FastAPI, MongoDB, Redis, Docker Compose.

## Running it

```bash
docker compose up --build
```

This starts the API, a worker, MongoDB and Redis. Only the API and worker logs are shown; `docker compose logs mongo redis` has the rest.

- API: http://localhost:8000
- Swagger docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health

```bash
curl -s -X POST localhost:8000/documents -H 'content-type: application/json' \
  -d '{"user_id": "alice", "title": "Q3 report", "content": "Revenue grew 12% in Q3. Costs stayed flat."}'

curl -s localhost:8000/documents/<document_id>        # poll until "completed"
curl -s 'localhost:8000/users/alice/documents?page=1&page_size=20&status=completed'
```

Other useful commands:

```bash
docker compose up -d --scale worker=3                 # more workers
docker compose logs -f api worker                     # logs
docker compose down                                   # stop
API_PORT=8080 REDIS_PORT=6380 docker compose up       # if 8000 / 6379 are already in use
```

**Tests:**

```bash
docker compose run --rm tests
```

Or run them locally with `pip install -r requirements-dev.txt && docker compose up -d mongo redis && pytest`. There are 70 tests: unit tests, plus integration tests using pytest + `httpx.AsyncClient` against real MongoDB and Redis.

**Without Docker** (Python 3.11+, with Mongo and Redis from compose):

```bash
pip install -r requirements-dev.txt
docker compose up -d mongo redis
uvicorn app.main:app --reload     # API
python -m app.worker              # worker, in a second terminal
```

**Postman:** `postman/` has a collection that tests every endpoint (happy path, resubmissions, cache hits, validation errors, rate limit) and a local environment. Import both into Postman and run the collection in the Collection Runner, or from the command line with the [Postman CLI](https://learning.postman.com/docs/postman-cli/postman-cli-installation/):

```bash
postman collection run postman/document-insights-api.postman_collection.json \
  -e postman/local.postman_environment.json
```

A full run takes 20–100 seconds, most of it waiting for the worker to finish the submitted document. If the API isn't on port 8000, change `baseUrl` in the environment.

## Configuration

All settings are environment variables, optionally set in a `.env` file. `.env.example` lists every one with a short description. The main ones:

| Variable | Default | |
|---|---|---|
| `MAX_ACTIVE_JOBS_PER_USER` | 3 | queued + processing documents allowed per user |
| `SUMMARY_CACHE_TTL_SECONDS` | 86400 | how long a summary stays in Redis (after that it's still reused from MongoDB) |
| `WORKER_CONCURRENCY` | 4 | jobs processed in parallel per worker process |
| `MAX_ATTEMPTS` | 3 | tries per document before it's marked `failed` |
| `PROCESSING_MIN_SECONDS` / `PROCESSING_MAX_SECONDS` | 10 / 30 | simulated processing time |
| `FAILURE_RATE` | 0.1 | chance an attempt fails |
| `LOG_JSON` | true | `false` for readable plain-text logs (compose uses `false` unless set) |

## API

| Endpoint | Description | Status codes |
|---|---|---|
| `POST /documents` | Submit `user_id`, `title`, `content` | 201, 200 already submitted (returns that document), 422 invalid input, 429 limit reached |
| `GET /documents/{id}` | Poll a document | 200, 404 |
| `GET /users/{user_id}/documents` | Paginated list, newest first. `page`, `page_size` (max 100), optional `status` | 200, 422 |
| `GET /health` | Checks MongoDB and Redis | 200 `ok` or `degraded` (Redis down), 503 (Mongo down) |

Any endpoint returns 503 while MongoDB is unreachable. Errors are JSON, `{"detail": "..."}`, except 422, which has FastAPI's list of validation errors. An unexpected error is a 500 with only `"Internal server error"` in the body; its traceback is logged under the response's `X-Request-ID`.

A document goes `queued → processing → completed | failed`. `GET /documents/{id}` and `POST /documents` return it in one of two shapes, depending on `status`:

While it's `queued` or `processing`, only what a client needs to decide whether to poll again:

```json
{ "document_id": "6ac7a26fb52c99a11550b084", "status": "processing" }
```

Once it's `completed` or `failed`, the whole document. `summary` is set when it completed, `error` when it failed:

```json
{
  "document_id": "6ac7a26fb52c99a11550b084",
  "user_id": "alice",
  "title": "Q3 report",
  "content": "Revenue grew 12% in Q3. Costs stayed flat.",
  "status": "completed",
  "summary": {
    "text": "Revenue grew 12% in Q3. Costs stayed flat.",
    "keywords": ["revenue", "grew", "costs", "stayed", "flat"],
    "word_count": 8,
    "reading_time_seconds": 2,
    "model": "mock-summarizer-v1"
  },
  "error": null,
  "attempts": 1,
  "cache_hit": false,
  "created_at": "2026-10-08T14:02:23.312000Z",
  "updated_at": "2026-10-08T14:02:41.208000Z",
  "completed_at": "2026-10-08T14:02:41.208000Z"
}
```

The OpenAPI spec (`/docs`) documents this as a `oneOf` with `status` as the discriminator, so generated clients get both types.

## Project structure

The code is split into routes, controllers, services and repositories:

```
app/
  routes/           which URL and method go to which controller, plus status codes and docs
  controllers/      request handlers: call a service, shape the HTTP response
  services/         business logic, plus the MongoDB and Redis connection singletons
  repositories/     all MongoDB queries
  middleware/       request id + access logging, exception -> status code mapping
  utils/            logging, hashing, id parsing, clock
  main.py           FastAPI app: wires routes, middleware, startup/shutdown
  worker.py         background worker (python -m app.worker)
  dependencies.py   dependency injection
  config.py         settings from env vars
  models.py         Pydantic request/response models
tests/
postman/            Postman collection + local environment
```

A request goes routes → controllers → services → repositories. Each routes module creates an `APIRouter` and registers controller functions on it with `router.add_api_route(...)`, which is what FastAPI's `@router.get(...)` decorator does under the hood. Keeping that in `routes/` means the URL map is in one place and controllers are plain async functions. Their parameters still declare what they need (the request body, path and query parameters, services through `Depends`), and FastAPI resolves those wherever the function is defined. Services don't know about HTTP: they raise exceptions (e.g. `TooManyActiveJobs`) that the middleware turns into status codes, so the worker reuses the same services as the API. MongoDB and Redis clients are created once per process (`services/mongo_service.py`, `services/redis_service.py`), connected on startup and closed on shutdown.

## How it works

**Submitting** (`services/document_service.py`):

1. Validate the input and hash the content (SHA-256).
2. If this text has been summarized before (checked in Redis, then MongoDB), save the document as `completed` and return the summary straight away.
3. Otherwise reserve one of the user's 3 slots in Redis, or return 429.
4. Insert the document into MongoDB as `queued`. This insert is what queues the job.
5. Wake an idle worker through Redis, and return 201.

**The worker** (`worker.py`, `services/processing_service.py`). Each worker runs 4 loops:

```
forever:
    process jobs until none are ready
    sleep until: a new document is announced, OR the next retry is due, OR 30s pass
```

For each job: claim it atomically → check the cache → summarize → save the result, cache it and free the user's slot. A failed attempt is retried after 5s, then 10s; after 3 attempts the document is `failed`.

## Design decisions

- **MongoDB is the queue.** A queued document *is* the job, so saving and queuing can't get out of sync. Processing also keeps going if Redis is down.
- **Workers sleep when idle and get woken up.** Instead of polling, an idle worker blocks on a Redis list (`BLPOP`), and the API pushes to it when a document is queued. Idle workers make no queries, and new jobs start instantly. The message is only a doorbell: the job is always claimed from Mongo, so a lost message costs at most 30s. Workers also wake up on their own when a retry is due, and fall back to polling every second while Redis is down. I used a list rather than pub/sub because a list keeps the message if no worker is listening at that moment.
- **No job is processed twice.** Claiming is a single atomic `find_one_and_update`, so two workers can't get the same job. A claimed job has a 60s lease: if the worker crashes, the job becomes available again. Every write checks the worker's `lease_token`, so a stale worker can't overwrite a newer result.
- **Rate limiting** uses a Redis sorted set of each user's active document ids, updated by a Lua script so check-and-add is atomic. A set rather than a counter means a slot can't be freed twice, and slots left behind by a crash expire after an hour. Cache hits don't use a slot.
- **The cache has two layers.** Redis keeps summaries for 24h, keyed by content hash. On a Redis miss, MongoDB is checked for any earlier completed document with the same hash. If one exists, its summary is reused and put back in Redis, so text that has been summarized once is never processed again. The cache is shared across users, since the summary depends only on the content.
- **Duplicate submissions:** a user sending text they already sent gets their existing document back (`200`, same `document_id`) instead of a new one. A unique index on `user_id + content_hash` (ignoring `failed` documents, so those can be retried) makes this hold even for two identical requests at the same moment. Different users sending the same text each get their own document, served from the cache. If they arrive at the same time, a short lock on the content hash makes one job wait for the other's result instead of summarizing it again.
- **Redis down:** the API keeps working. The cache and rate limit read MongoDB instead, workers poll, and `/health` reports `degraded`. With **Mongo down** the API returns 503 and workers retry until it's back.
- **Polling returns as little as possible until there's something to return.** A client polls every few seconds for 10–30 seconds, and until the job is finished every response would be the same. So while a document is queued or processing, the response is just `document_id` and `status`. Once it's finished, the whole document, including the text. `POST` follows the same rule, so a cache hit comes back as the whole document straight away.
- **Fast reads.** Every read is a direct index lookup: `content_hash` for finding an earlier summary, `user_id + content_hash` for a user's earlier submission, `user_id + created_at` and `user_id + status + created_at` for listing, `status + visible_at` for claiming jobs. With 100,000 documents, each read took under 1.5 ms (p95). `tests/test_indexes.py` checks MongoDB's query plans, so a missing index or a full collection scan fails the tests.
- **Production basics:** JSON logs with a request id on every line, unexpected errors returned as a JSON 500 (and logged with their traceback and request id), config validated at startup, graceful worker shutdown (a job in progress is put back in the queue), and the API and worker containers run as a non-root user.

## Assumptions

- No authentication. `user_id` is trusted as sent.
- The 10% failure rate is per attempt. With 3 attempts, about 0.1% of documents end up `failed`. Set `MAX_ATTEMPTS=1` to see failures more often.
- "Identical content" means the same text after trimming whitespace.
- A malformed document id returns 404, the same as an unknown one.
- "Processed" means finished either way: a `failed` document is returned in full too, since the client needs its `error`. The full document includes `content`. The list endpoint leaves it out, since a page of documents could be several MB.

## What I'd do with more time

- Authentication, and checking that a document belongs to the user reading it.
- Periodically rebuild the rate-limit sets from MongoDB. After a Redis restart a user could briefly have more than 3 active documents.
- Store document text in object storage (e.g. S3) instead of in the MongoDB record. The API would upload the text and keep only its storage key and hash in Mongo, and the worker would download it when it starts the job. That keeps the queue records small, and allows much bigger documents than Mongo's 16MB limit (e.g. PDFs uploaded directly by the client with a presigned URL).
- A dedicated message broker (e.g. SQS or RabbitMQ) once volume outgrows MongoDB as a queue.
- Put the model version in the cache key, so a model upgrade doesn't keep reusing old summaries.
- Cursor-based pagination instead of skip/limit.
- Metrics (queue length, processing time, failure rate, cache hit ratio).
- Scale workers on queue length (e.g. KEDA), including down to zero.
