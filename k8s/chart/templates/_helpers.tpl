{{/*
Expand the name of the chart.
*/}}
{{- define "sentinel-controller.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Truncated at 63 chars for k8s label limits.
If release name already contains the chart name, no prefix is added.
*/}}
{{- define "sentinel-controller.fullname" -}}
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
Chart name and version, joined with a dash, for the helm.sh/chart label.
*/}}
{{- define "sentinel-controller.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels — attached to every resource the chart renders.
*/}}
{{- define "sentinel-controller.labels" -}}
helm.sh/chart: {{ include "sentinel-controller.chart" . }}
{{ include "sentinel-controller.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: sentinel
{{- end -}}

{{/*
Selector labels — used by Deployment.spec.selector and Pod template.
Must stay stable across upgrades.
*/}}
{{- define "sentinel-controller.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sentinel-controller.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
ServiceAccount name to use. Honors serviceAccount.create + .name.
*/}}
{{- define "sentinel-controller.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "sentinel-controller.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Resolved image tag — defaults to the chart appVersion when not set.
*/}}
{{- define "sentinel-controller.imageTag" -}}
{{- default .Chart.AppVersion .Values.image.tag -}}
{{- end -}}
