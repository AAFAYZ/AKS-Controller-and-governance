{{- define "workload-resource-controller.name" -}}
workload-resource-controller
{{- end }}

{{- define "workload-resource-controller.fullname" -}}
{{ include "workload-resource-controller.name" . }}-operator
{{- end }}

{{- define "workload-resource-controller.serviceAccountName" -}}
{{ include "workload-resource-controller.name" . }}-sa
{{- end }}
