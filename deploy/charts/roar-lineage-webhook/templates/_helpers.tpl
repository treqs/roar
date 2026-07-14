{{- define "roar-webhook.fullname" -}}
{{- printf "%s-roar-lineage-webhook" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "roar-webhook.labels" -}}
app.kubernetes.io/name: roar-lineage-webhook
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "roar-webhook.selectorLabels" -}}
app.kubernetes.io/name: roar-lineage-webhook
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "roar-webhook.runtimeImage" -}}
{{- if .Values.injection.runtimeImage -}}
{{ .Values.injection.runtimeImage }}
{{- else -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}
{{- end -}}

{{- define "roar-webhook.tlsSecretName" -}}
{{- if .Values.tls.secretName -}}
{{ .Values.tls.secretName }}
{{- else -}}
{{ include "roar-webhook.fullname" . }}-tls
{{- end -}}
{{- end -}}
