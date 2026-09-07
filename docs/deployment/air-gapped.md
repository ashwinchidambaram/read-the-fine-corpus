# Air-gapped deployment (M-013)

Read The Fine Corpus runs fully air-gapped — **no outbound egress** — as a
first-class configuration, not a degraded one (spec §4.3). This page is the
operator's guide to standing up the stack behind an air gap with the Helm chart
in [`deploy/helm/finecorpus/`](../../deploy/helm/finecorpus/).

Air-gapped operation has two independent parts, and you need both:

1. **Deploy-time**: every container image must come from a registry you control.
   Nothing in this chart reaches the public internet at `helm install` time.
2. **Run-time**: the application must never make an outbound HTTP call. This is
   enforced *in the code* by air-gap mode (`platform.airgap` / `RTFC_AIRGAP`),
   and asserted at chart-render time.

---

## 1. Run-time no-egress guarantee (the code path)

Air-gap mode is enforced in the application itself, independently of the chart.
The authoritative check is `_is_airgap_active()` in
[`src/finecorpus/llm/registry.py`](../../src/finecorpus/llm/registry.py):

```python
def _is_airgap_active(config) -> bool:
    return config.platform.airgap or os.environ.get("RTFC_AIRGAP", "").lower() in {
        "1",
        "true",
        "yes",
    }
```

When air-gap mode is active, requesting any cloud provider raises before a single
byte leaves the process (`registry.py`, the `_CLOUD_PROVIDER_TARGETS` guard).
Cloud provider implementations refuse independently as a second layer — e.g.
`OpenAILLMProvider.health_check` / `complete` short-circuit when `RTFC_AIRGAP` is
set (`src/finecorpus/llm/openai_provider.py`). Local providers such as Ollama
(`is_local=True`) are unaffected (`src/finecorpus/llm/ollama_provider.py`).

The chart wires this on **two** switches so it cannot be half-on:

- It writes `platform.airgap: true` into the mounted `corpus.yaml` ConfigMap, and
- It injects `RTFC_AIRGAP=true` as an env var on every application pod.

Set it with a single value:

```yaml
# values-airgap.yaml
airgap: true
```

### Render-time assertion (fail-closed)

When `airgap: true`, the chart refuses to render if any configured provider is
not local, or if any cloud provider API key is supplied. See
`finecorpus.airgapAssertion` in
[`templates/_helpers.tpl`](../../deploy/helm/finecorpus/templates/_helpers.tpl):

- every entry in `config.providers` must have `isLocal: true`, and
- `secrets.providerApiKeys` must be empty.

So an operator cannot accidentally wire a cloud provider into an air-gapped
install — the install aborts with an explanatory error before anything is created.

---

## 2. Mirror the images to a private registry

Every image reference in `values.yaml` is **fully pinned** — an explicit tag, and
optionally a digest. There is no implicit `:latest` anywhere; the
`finecorpus.image` helper *fails the render* if it ever sees an empty or `latest`
tag. Pin by digest for supply-chain integrity where you can.

Images to mirror (defaults; override tags/digests to match your build):

| Image | values path |
|---|---|
| `postgres:16` | `postgresql.image` |
| `qdrant/qdrant:v1.19.0` | `qdrant.image` |
| `minio/minio:RELEASE.2025-09-07T16-13-09Z` | `minio.image` |
| `minio/mc:RELEASE.2025-08-13T08-35-41Z` | `minio.mcImage` |
| `rtfc/finecorpus:0.1.0` (retrieval/control/embedding/ingest — one image) | `app.image` |

Mirror them (run this from a host that *can* reach both registries, before the
gap is closed):

```bash
for ref in \
  postgres:16 \
  qdrant/qdrant:v1.19.0 \
  minio/minio:RELEASE.2025-09-07T16-13-09Z \
  minio/mc:RELEASE.2025-08-13T08-35-41Z \
  rtfc/finecorpus:0.1.0 ; do
    docker pull "$ref"
    docker tag  "$ref" "registry.internal:5000/$ref"
    docker push "registry.internal:5000/$ref"
done
```

Point the chart at your mirror with a single global value — it is prepended to
every image `repository`:

```yaml
global:
  imageRegistry: "registry.internal:5000"
  imagePullSecrets:
    - name: regcred        # if your mirror needs auth
```

To pin by digest instead of tag (recommended), set e.g.:

```yaml
app:
  image:
    digest: "sha256:...."   # wins over tag
```

---

## 3. Supply local embedding / LLM providers

Air-gapped installs must run local models. Declare them in `config.providers`
with `isLocal: true` and point at an in-cluster (or on-node) endpoint — e.g. an
Ollama deployment you run alongside this chart:

```yaml
airgap: true

config:
  providers:
    - name: ollama-local
      kind: ollama
      isLocal: true
      baseUrl: "http://ollama:11434"

secrets:
  providerApiKeys: {}       # MUST be empty in air-gap mode
```

The embedding-service and ingest-worker resolve providers through the same
registry, so the same air-gap guard applies to embedding calls.

---

## 4. Verify no egress

1. **Render check** — the assertion fails fast on a misconfiguration:

   ```bash
   helm template rtfc deploy/helm/finecorpus -f values-airgap.yaml >/dev/null
   # aborts if any provider isn't local or any API key is set
   ```

2. **In-cluster check** — confirm the env + config landed:

   ```bash
   kubectl exec deploy/rtfc-finecorpus-retrieval-api -- printenv RTFC_AIRGAP   # -> true
   kubectl exec deploy/rtfc-finecorpus-retrieval-api -- \
     cat /app/config/corpus.yaml | grep airgap                                # -> airgap: true
   ```

3. **Network policy (defence in depth)** — the chart blocks egress at the app
   layer, but you should also deny egress at the cluster layer. Apply a default
   `NetworkPolicy` that permits only intra-namespace traffic (postgres, qdrant,
   minio, the app services) and DNS, and denies all other egress. This makes the
   no-egress property enforceable by the platform, not just the application.

---

## Related

- Air-gap config reference: [`docs/configuration/reference.md`](../configuration/reference.md) (`platform.airgap`)
- Deployment targets: spec §4.3; transformation tiers / air-gap: §7.2
- Trust boundary (M-068): [`docs/security/trust-boundary.md`](../security/trust-boundary.md)
