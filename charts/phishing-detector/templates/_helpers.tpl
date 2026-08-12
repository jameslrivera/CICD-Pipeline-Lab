{{/*
Helper templates. These exist so names and labels are computed in exactly one
place — a chart that spells its own name slightly differently in the Deployment
and the Service produces a Service that selects nothing, which is a genuinely
annoying failure to debug because everything reports healthy.
*/}}

{{- define "phishing-detector.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified name. Truncated to 63 characters because that is the limit for
a Kubernetes label value and for most resource names.
*/}}
{{- define "phishing-detector.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Full label set, applied to every object. Includes the chart and release so that
`kubectl get all -l app.kubernetes.io/instance=<release>` finds everything this
chart owns.
*/}}
{{- define "phishing-detector.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "phishing-detector.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels ONLY. These are deliberately a smaller set than the full labels:
a Deployment's selector is immutable after creation, so anything that changes
between releases — the chart version, the app version — must never appear here.
Put app.kubernetes.io/version in a selector and the next `helm upgrade` fails
with "field is immutable".
*/}}
{{- define "phishing-detector.selectorLabels" -}}
app.kubernetes.io/name: {{ include "phishing-detector.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "phishing-detector.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "phishing-detector.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
