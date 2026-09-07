{{/*
Common naming helpers.
*/}}
{{- define "finecorpus.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "finecorpus.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "finecorpus.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "finecorpus.labels" -}}
app.kubernetes.io/name: {{ include "finecorpus.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{/*
finecorpus.image — resolve a fully-pinned image reference.
Args: dict with keys "img" (a map with repository/tag/digest) and "global".
- Prepends global.imageRegistry when set.
- Uses @digest when img.digest is set, else :tag.
This is the ONLY place image refs are assembled, so "no implicit :latest" is
enforced structurally (M-013).
*/}}
{{- define "finecorpus.image" -}}
{{- $img := .img -}}
{{- $registry := .global.imageRegistry | default "" -}}
{{- $repo := $img.repository -}}
{{- if $registry -}}
{{- $repo = printf "%s/%s" $registry $img.repository -}}
{{- end -}}
{{- if $img.digest -}}
{{- printf "%s@%s" $repo $img.digest -}}
{{- else -}}
{{- $tag := $img.tag | toString -}}
{{- if or (eq $tag "") (eq $tag "latest") -}}
{{- fail (printf "M-013 air-gap: image %q must be pinned to an explicit tag or digest, not %q" $img.repository $tag) -}}
{{- end -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end -}}

{{/*
finecorpus.imagePullSecrets — render global.imagePullSecrets if any.
*/}}
{{- define "finecorpus.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{ toYaml . | indent 2 }}
{{- end }}
{{- end -}}

{{/*
finecorpus.secretName — the Secret to reference (existing or chart-managed).
*/}}
{{- define "finecorpus.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "finecorpus.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
finecorpus.airgapAssertion — M-013 guardrail.
When .Values.airgap is true, EVERY configured provider MUST declare isLocal: true.
Rendering fails otherwise, so a cloud provider can never be wired into an
air-gapped install. Also asserts no providerApiKeys are set in air-gap mode.
Call once (from the ConfigMap) so the whole render aborts early.
*/}}
{{- define "finecorpus.airgapAssertion" -}}
{{- if .Values.airgap -}}
{{- range $p := .Values.config.providers -}}
{{- if not $p.isLocal -}}
{{- fail (printf "M-013 air-gap: airgap=true but provider %q has isLocal!=true. Air-gapped installs require local providers only (§4.3). Remove the provider or run a local model." $p.name) -}}
{{- end -}}
{{- end -}}
{{- if .Values.secrets.providerApiKeys -}}
{{- fail "M-013 air-gap: airgap=true but secrets.providerApiKeys is non-empty. Cloud provider keys imply egress; remove them for an air-gapped install." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
finecorpus.appEnv — env shared by all app pods (postgres DSN, qdrant, minio,
config path, airgap flag). Secrets come via secretKeyRef.
*/}}
{{- define "finecorpus.appEnv" -}}
- name: FINECORPUS_CONFIG_PATH
  value: /app/config/corpus.yaml
{{- if .Values.airgap }}
- name: RTFC_AIRGAP
  value: "true"
{{- end }}
- name: POSTGRES_DB
  value: {{ .Values.postgresql.auth.database | quote }}
- name: POSTGRES_USER
  value: {{ .Values.postgresql.auth.username | quote }}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "finecorpus.secretName" . }}
      key: POSTGRES_PASSWORD
- name: POSTGRES_DSN
  value: "postgresql://{{ .Values.postgresql.auth.username }}:$(POSTGRES_PASSWORD)@{{ include "finecorpus.fullname" . }}-postgres:{{ .Values.postgresql.service.port }}/{{ .Values.postgresql.auth.database }}"
- name: FINECORPUS_STORAGE__POSTGRES__URL
  value: "postgresql://{{ .Values.postgresql.auth.username }}:$(POSTGRES_PASSWORD)@{{ include "finecorpus.fullname" . }}-postgres:{{ .Values.postgresql.service.port }}/{{ .Values.postgresql.auth.database }}"
- name: QDRANT_URL
  value: "http://{{ include "finecorpus.fullname" . }}-qdrant:{{ .Values.qdrant.service.httpPort }}"
- name: FINECORPUS_STORAGE__QDRANT__URL
  value: "http://{{ include "finecorpus.fullname" . }}-qdrant:{{ .Values.qdrant.service.httpPort }}"
{{- range $k, $v := .Values.secrets.providerApiKeys }}
- name: {{ $k }}
  valueFrom:
    secretKeyRef:
      name: {{ include "finecorpus.secretName" $ }}
      key: {{ $k }}
{{- end }}
{{- end -}}

{{/*
finecorpus.minioEnv — object-store env for services that touch MinIO
(ingest-worker). Endpoint is internal-only.
*/}}
{{- define "finecorpus.minioEnv" -}}
- name: MINIO_ENDPOINT
  value: "{{ include "finecorpus.fullname" . }}-minio:{{ .Values.minio.service.apiPort }}"
- name: MINIO_BUCKET
  value: {{ .Values.minio.bucket | quote }}
- name: MINIO_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "finecorpus.secretName" . }}
      key: MINIO_ROOT_USER
- name: MINIO_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "finecorpus.secretName" . }}
      key: MINIO_ROOT_PASSWORD
{{- end -}}

{{/*
finecorpus.httpProbes — readiness/liveness for HTTP app services, mirroring the
compose healthcheck (GET /healthz on 8000).
*/}}
{{- define "finecorpus.httpProbes" -}}
livenessProbe:
  httpGet:
    path: /healthz
    port: 8000
  initialDelaySeconds: 15
  periodSeconds: 15
  timeoutSeconds: 5
  failureThreshold: 5
readinessProbe:
  httpGet:
    path: /healthz
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 5
{{- end -}}
