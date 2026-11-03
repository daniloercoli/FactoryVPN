# FactoryVPN website

English-only static product website, developer guide and customer API reference.
The public document root is **`website/dist/`**. No frontend build, backend,
account, external font, analytics or CDN is required to serve it.

## Preview

From the repository root:

```bash
python3 -m http.server 8873 --bind 127.0.0.1 --directory website/dist
```

Open <http://127.0.0.1:8873/>. Serve over HTTP rather than opening files directly:
Swagger UI fetches the local JSON specification.

- `/` — product, assistance flow and custom connector integration.
- `/developers.html` — authentication, examples, lifecycle, errors and client guidance.
- `/api/` — read-only Swagger UI.
- `/api/openapi.json` — downloadable customer OpenAPI 3.1 contract.

## Update the API reference

Use the project's Python environment with `requirements.txt` installed:

```bash
.venv/bin/python -m scripts.export_openapi
.venv/bin/python -m scripts.export_openapi --check
```

The exporter imports the real FastAPI application in an isolated subprocess with
a temporary working directory and a minimal environment. It does not read the
deployment `.env`, connect to the database or start the API lifespan.
Request schemas and route definitions come from FastAPI. Additional response
models in `api/schemas/client.py` describe existing responses through route
metadata; they do not change runtime serialization. Update those contracts if
an untyped response body changes. The exporter adds customer guidance, common
errors, explicit admin roles and a placeholder API server, and excludes worker
and gateway machine operations and unused schemas.

Commit regenerated `website/dist/api/openapi.json` alongside API changes. The
export regression test fails when the committed specification is stale.
The full API schema is still available directly from FastAPI at `/openapi.json`.

Swagger UI **5.33.1** is vendored in `dist/vendor/swagger-ui/`, including its
Apache-2.0 license and notice. The package was obtained from the official npm
`swagger-ui-dist` package and checked against the registry's SHA-512 integrity
value. Browser requests and the remote validator are disabled. No bearer token
is requested or saved on this public documentation site.

References: [FastAPI response metadata](https://fastapi.tiangolo.com/advanced/additional-responses/)
and [Swagger UI configuration](https://swagger.io/docs/open-source-tools/swagger-ui/usage/configuration/).

## Publish at factoryvpn.ercoliconsulting.eu

The canonical links, sitemap and robots file already use
`https://factoryvpn.ercoliconsulting.eu`. When deploying to a different hostname,
update canonical URLs, `sitemap.xml`, `robots.txt` and the documentation URL in
`scripts/export_openapi.py`, then regenerate the API reference.

1. Configure that hostname on the selected static hosting service or web server.
2. Upload only the **contents of `website/dist/`**, preserving subdirectories.
3. Serve `index.html` as the directory index, including for `/api/`.
4. Point the hostname's DNS record to the destination supplied by the hosting
   provider and enable HTTPS for the hostname.
5. Verify the homepage, developer guide, Swagger UI and OpenAPI download over HTTPS.

Do not upload the repository root, deployment `.env`, private certificates or
runtime directories. Keep the documentation origin distinct from the operational
API: the site's `/api/` route serves documentation and must not be proxied to the
VPN backend. Use an appropriate separate hostname for the operational API.

The marketing site has no form or automatic requests to a live VPN service.
