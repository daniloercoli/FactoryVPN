# FactoryVPN website

English-only static product website, product roadmap, developer guide and customer API reference.
The public document root is **`website/dist/`**. A Python build embeds the visible
product name before publication; the result is ordinary HTML, CSS, JavaScript and
JSON. Serving it requires no backend, Python, Node.js, account, external font,
analytics or CDN.

The site uses the same **`APP_NAME`** parameter as the portal, with `FactoryVPN`
as its default. It covers page titles, navigation, prose, diagram, monograms,
favicon and the downloadable API reference. Protocol headers, cookie names,
download filenames and the configured hostname remain stable.

## Build

Use the project's Python environment with `requirements.txt` installed. From the
repository root:

```bash
.venv/bin/python -m scripts.build_website
```

On native Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.
The name comes from `--app-name`, then the process's `APP_NAME`, then `APP_NAME`
in the repository root `.env`, and finally the default `FactoryVPN`. Only the
public name is selected from `.env`; deployment settings are not passed to the
isolated API exporter. The `.env` value is read literally, without variable
expansion. Set the final name directly rather than using `${OTHER_VARIABLE}`.
Names must contain 1–80 characters on one line, without control characters;
leading and trailing whitespace is removed.

To prepare and verify a separate customized directory:

```bash
.venv/bin/python -m scripts.build_website --app-name "Assistenza aziendale" --output .build/website
.venv/bin/python -m scripts.build_website --check --app-name "Assistenza aziendale" --output .build/website
```

Use `--app-name vpn` to build with the generic name. Every build starts from
`website/src/` HTML templates and `website/static/` assets; edit those sources,
then rebuild. `website/dist/` is generated output. The default output committed
to the repository uses `FactoryVPN`; regenerate and check it explicitly before
committing source or API changes:

```bash
.venv/bin/python -m scripts.build_website --app-name FactoryVPN
.venv/bin/python -m scripts.build_website --check --app-name FactoryVPN
```

## Preview

After building, from the repository root:

```bash
python3 -m http.server 8873 --bind 127.0.0.1 --directory website/dist
```

Open <http://127.0.0.1:8873/>. Serve over HTTP rather than opening files directly:
Swagger UI fetches the local JSON specification.
For a separate build, replace `website/dist` with its output directory.

- `/` — software and gateway integration, illustrative service workflows and assistance flow.
- `/roadmap.html` — current 0.9.0 prerelease toward 1.0, Single LAN 2.0 and MQTT 2.1.
- `/developers.html` — authentication, examples, lifecycle, errors and client guidance.
- `/developers.html#gateway-setup` — factory enrollment, manual ZIP setup, VPN/HTTPS trust and subsequent certificate renewal.
- `/developers.html#network-scope` — split tunnel, local Internet and access to services on the gateway's fixed VPN IP.
- `/developers.html#timing` — VPN/login durations, temporary API failures and lease limits.
- `/api/` — read-only Swagger UI.
- `/api/openapi.json` — downloadable customer OpenAPI 3.1 contract.

## Update the API reference

The website build also regenerates the customer OpenAPI contract with the same
name as the HTML pages. To export or check only the default API contract:

```bash
.venv/bin/python -m scripts.export_openapi
.venv/bin/python -m scripts.export_openapi --check
```

The exporter imports the real FastAPI application in an isolated subprocess with
a temporary working directory and a minimal environment. It does not read the
deployment `.env`, connect to the database or start the API lifespan.
Its default name is deterministically `FactoryVPN`, independent of the caller's
`APP_NAME`; an explicit `--app-name` overrides that default for a standalone export.
Request schemas and route definitions come from FastAPI. Additional response
models in `api/schemas/client.py` describe existing responses through route
metadata; they do not change runtime serialization. Update those contracts if
an untyped response body changes. The exporter adds customer guidance, common
errors, explicit admin roles and a placeholder API server, and excludes worker
and gateway machine operations and unused schemas.

Rebuild the full website and commit its default generated output alongside API
changes. The export regression test fails when the committed specification is stale.
The full API schema is still available directly from FastAPI at `/openapi.json`.

## Keep public guidance aligned

The gateway setup section follows the [initial setup guide](../documentation/gateway/INITIAL_SETUP.md)
and [factory enrollment contract](../documentation/gateway/FACTORY_ENROLLMENT.md),
with runtime activation and network enforcement in the [gateway runtime contract](../documentation/gateway/RUNTIME_PROTOCOL.md),
and subsequent renewals in the [gateway renewal contract](../documentation/gateway/CERTIFICATE_RENEWAL.md).
Those files ship with the source distribution and are not served from `dist/`.

The HTML templates are maintained in `website/src/`; exporting only OpenAPI does
not update their prose. When session timing, routing or logout behavior changes, review
the homepage, developer guide, [repository README](../README.md) and
[operator guides](../documentation/README.md) together. Check the deployment
defaults against [.env.example](../.env.example) and the code that consumes them.
The [installation guide](../documentation/DOCKER_GUIDE.md#durate-timeout-e-reti-instabili)
is the operator reference for configuration and applying changes.

Keep the homepage focused on software integration, compatible commercial
gateways and the software agent. Use the roadmap for network modes, release
status and future capabilities; keep technical contracts in the developer guide.
Do not imply universal gateway compatibility or ready-made third-party connectors.
Qualify hardware and connector scope for the individual integration project.
Product-page styles live in `website/static/marketing.css`, loaded by the
homepage and roadmap only. The shared stylesheet and developer pages are kept
separate so marketing changes do not alter the API documentation layout.

Check the four copyable shell examples in `website/src/developers.html`: login, session
creation, readiness and closure. Keep closure in its own block so copying a
readiness check cannot close the session. Preserve the same idempotency key and
request body when demonstrating a retry.

Validate local links and section anchors in the served `dist/` tree, including
Swagger UI and its JSON download, at desktop and mobile widths. Operator guides
live in the repository; do not add links from `dist/` to files outside that
public document root. Building these files does not deploy the website.

## API viewer

Swagger UI **5.33.1** is vendored in `static/vendor/swagger-ui/` and copied into
`dist/vendor/swagger-ui/`, including its
Apache-2.0 license and notice. The package was obtained from the official npm
`swagger-ui-dist` package and checked against the registry's SHA-512 integrity
value. Browser requests and the remote validator are disabled. No bearer token
is requested or saved on this public documentation site.

References: [FastAPI response metadata](https://fastapi.tiangolo.com/advanced/additional-responses/)
and [Swagger UI configuration](https://swagger.io/docs/open-source-tools/swagger-ui/usage/configuration/).

## Publish at factoryvpn.ercoliconsulting.eu

The canonical links, sitemap and robots file already use
`https://factoryvpn.ercoliconsulting.eu`. When deploying to a different hostname,
update canonical URLs in `website/src/`, `sitemap.xml` and `robots.txt` in
`website/static/`, and the documentation URL in `scripts/export_openapi.py`,
then rebuild the website.

1. Configure that hostname on the selected static hosting service or web server.
2. Build with the desired `APP_NAME` and upload only the **contents of the build
   output** (`website/dist/` by default), preserving subdirectories.
3. Serve `index.html` as the directory index, including for `/api/`.
4. Point the hostname's DNS record to the destination supplied by the hosting
   provider and enable HTTPS for the hostname.
5. Verify the homepage, roadmap, developer guide, Swagger UI and OpenAPI download over HTTPS.

Do not upload source templates, the repository root, deployment `.env`, private
certificates or runtime directories. Keep the documentation origin distinct from the operational
API: the site's `/api/` route serves documentation and must not be proxied to the
VPN backend. Use an appropriate separate hostname for the operational API.

The marketing site has no form or automatic requests to a live VPN service.
